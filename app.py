"""跨境多平台订单&结算对账系统 v2.0 · Web 主流程（P2）。

网页上传 / 一键示例 → 任务队列 → engine.run() 对账 → 结果页 → Excel 下载。
双日志：data/logs/app.log（应用，滚动 1MB×5）+ data/tasks/{id}/run.log（任务）。
三层异常捕获第三层：路由 try + errorhandler + worker 顶层 catch + 15 分钟看门狗。
"""

import argparse
import json
import logging
import os
import queue
import re
import shutil
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime
from logging.handlers import RotatingFileHandler

from flask import Flask, abort, jsonify, render_template, request, send_file
from werkzeug.exceptions import HTTPException

import requests

from engine.config import (
    FASTGPT_API_KEY,
    FASTGPT_APP_ID,
    FASTGPT_BASE_URL,
    fastgpt_configured,
    load_settings,
)
from engine.desensitize import build_diff_context
from engine.reconcile import run as engine_run

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
TASKS_DIR = os.path.join(DATA_DIR, "tasks")
LOGS_DIR = os.path.join(DATA_DIR, "logs")
SAMPLE_DST = os.path.join(DATA_DIR, "sample")
SAMPLE_SRC = os.path.join(BASE_DIR, "后台数据")

MAX_FILE_BYTES = 5 * 1024 * 1024          # 单文件 5MB
WATCHDOG_SECONDS = 15 * 60                # 看门狗：单任务 15 分钟
LOG_TAIL_LINES = 200                      # 日志接口默认尾部行数
CHAT_TIMEOUT = 180                        # FastGPT 上游超时（秒）：Agent V2 多轮工具+RAG 实测 34~75s，偶发更久
CHAT_HISTORY_MAX_MSGS = 12                # 聊天本地历史：近 6 轮（user+assistant 各 12 条）
CHAT_MSG_MAX_CHARS = 2000                 # 单条问题长度上限
TASK_ID_RE = re.compile(r"^task_\d{8}_\d{6}_[a-z0-9]{4}$")

# 5 类规范文件名（顺序 = 缺失提示顺序）
CANONICALS = [
    "亚马逊订单明细.csv",
    "亚马逊结算账单.csv",
    "Temu订单明细.csv",
    "Temu结算账单.csv",
    "收款流水报表.csv",
]

app = Flask(__name__, template_folder="web/templates", static_folder="web/static")
app.json.ensure_ascii = False
# 模板改动即刻生效（本地工具场景，免重启）
app.config["TEMPLATES_AUTO_RELOAD"] = True

app_log = logging.getLogger("app")

_task_queue = queue.Queue()
_worker_started = False

# 聊天会话历史（内存，按 task_id；进程重启即清空）
_chat_history = {}
_chat_lock = threading.Lock()


# ===================== 工具 =====================

class _ConsoleStream:
    """Windows GBK 控制台安全写入：不可编码字符降级替换。"""

    def write(self, s):
        stream = sys.stdout
        encoding = getattr(stream, "encoding", None) or "utf-8"
        try:
            s = s.encode(encoding, errors="replace").decode(encoding)
        except Exception:
            pass
        stream.write(s)

    def flush(self):
        try:
            sys.stdout.flush()
        except Exception:
            pass


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _new_task_id():
    return "task_{}_{}".format(
        datetime.now().strftime("%Y%m%d_%H%M%S"), uuid.uuid4().hex[:4])


def _task_dir(tid):
    return os.path.join(TASKS_DIR, tid)


def _meta_path(tid):
    return os.path.join(_task_dir(tid), "meta.json")


def _load_meta(tid):
    with open(_meta_path(tid), encoding="utf-8") as f:
        return json.load(f)


def _save_meta(tid, meta):
    path = _meta_path(tid)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _classify(orig_name):
    """按文件名关键字识别类型（不区分大小写）；无法识别返回 None。"""
    lower = orig_name.lower()
    if "亚马逊" in orig_name and "订单" in orig_name:
        return "亚马逊订单明细.csv"
    if "亚马逊" in orig_name and "结算" in orig_name:
        return "亚马逊结算账单.csv"
    if "temu" in lower and "订单" in orig_name:
        return "Temu订单明细.csv"
    if "temu" in lower and "结算" in orig_name:
        return "Temu结算账单.csv"
    if "收款" in orig_name or "流水" in orig_name:
        return "收款流水报表.csv"
    return None


def _require_valid_task(tid):
    if not TASK_ID_RE.match(tid) or not os.path.isfile(_meta_path(tid)):
        abort(404, description="任务不存在")


# ===================== 初始化 =====================

def _setup_logging():
    os.makedirs(LOGS_DIR, exist_ok=True)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = RotatingFileHandler(
        os.path.join(LOGS_DIR, "app.log"), maxBytes=1024 * 1024,
        backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    fh.setLevel(logging.DEBUG)
    ch = logging.StreamHandler(_ConsoleStream())
    ch.setFormatter(fmt)
    for h in list(app_log.handlers):
        app_log.removeHandler(h)
    app_log.setLevel(logging.DEBUG)  # 聊天代理的请求/响应摘要按计划以 DEBUG 落盘
    app_log.addHandler(fh)
    app_log.addHandler(ch)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)


def _ensure_sample():
    """示例数据预拷贝到 data/sample/（防止 后台数据/ 被占用或挪走，计划书风险#5）。"""
    os.makedirs(SAMPLE_DST, exist_ok=True)
    for name in CANONICALS:
        src = os.path.join(SAMPLE_SRC, name)
        dst = os.path.join(SAMPLE_DST, name)
        if not os.path.isfile(dst) and os.path.isfile(src):
            try:
                shutil.copy(src, dst)
                app_log.info("示例数据已预拷贝：%s", name)
            except OSError as e:
                app_log.warning("示例数据拷贝失败 %s：%s", name, e)
    for name in CANONICALS:
        if not os.path.isfile(os.path.join(SAMPLE_DST, name)):
            app_log.warning("示例数据缺失：data/sample/%s（一键示例可能不可用）", name)


def _recover_interrupted():
    """启动时扫描：服务重启导致中断的任务一律置 failed，绝不卡在 running。"""
    if not os.path.isdir(TASKS_DIR):
        return
    for tid in os.listdir(TASKS_DIR):
        mpath = os.path.join(TASKS_DIR, tid, "meta.json")
        if not os.path.isfile(mpath):
            continue
        try:
            with open(mpath, encoding="utf-8") as f:
                meta = json.load(f)
        except Exception as e:
            app_log.warning("启动恢复：读取 meta 失败 %s：%s", mpath, e)
            continue
        if meta.get("status") in ("pending", "running"):
            meta.update({
                "status": "failed",
                "step": "[启动恢复]",
                "error": "服务重启导致任务中断，请重新发起对账",
                "finished_at": _now(),
            })
            with open(mpath + ".tmp", "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
            os.replace(mpath + ".tmp", mpath)
            app_log.warning("启动恢复：任务 %s 因服务重启已置 failed", tid)


# ===================== 任务创建与入队 =====================

def _create_and_enqueue(classified, ai):
    """classified: {规范名: (原始名, bytes)}。落盘 → 写 meta → 入队。"""
    tid = _new_task_id()
    tdir = _task_dir(tid)
    os.makedirs(os.path.join(tdir, "raw"), exist_ok=True)
    for canonical, (orig, data) in classified.items():
        with open(os.path.join(tdir, "raw", canonical), "wb") as fh:
            fh.write(data)
        app_log.info("上传文件：%s (%dB) → 识别成功：%s", orig, len(data), canonical)
    meta = {
        "task_id": tid,
        "status": "pending",
        "ai_enabled": bool(ai),
        "created_at": _now(),
        "started_at": None,
        "finished_at": None,
        "step": None,
        "error": None,
        "traceback": None,
        "stats": {},
        "diff_summary": [],
        "order_settle_diff": [],
        "settle_pay_diff": [],
        "excel_file": None,
        "excel_path": None,
        "elapsed_ms": None,
        "files": list(classified.keys()),
    }
    _save_meta(tid, meta)
    _task_queue.put(tid)
    app_log.info("任务已创建并入队：%s（AI=%d，队列长度=%d）",
                 tid, ai, _task_queue.qsize())
    return tid


def _ingest_uploads(flask_files, ai):
    classified = {}
    for f in flask_files:
        orig = os.path.basename((f.filename or "").strip())
        if not orig:
            raise ValueError("存在没有文件名的上传项，请重新选择文件")
        if not orig.lower().endswith(".csv"):
            raise ValueError("仅支持 .csv 文件，收到：{}".format(orig))
        data = f.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise ValueError("文件超过 5MB 限制：{}".format(orig))
        canonical = _classify(orig)
        if canonical is None:
            raise ValueError(
                "无法识别的文件：{}（文件名需包含平台+类型关键字，"
                "如「亚马逊订单」「Temu结算」「收款流水」）".format(orig))
        if canonical in classified:
            raise ValueError(
                "重复上传：「{}」与「{}」均识别为「{}」".format(
                    classified[canonical][0], orig, canonical))
        classified[canonical] = (orig, data)
    missing = [c for c in CANONICALS if c not in classified]
    if missing:
        raise ValueError("缺少必需文件：" + "、".join(missing))
    return _create_and_enqueue(classified, ai)


def _ingest_sample(ai):
    classified = {}
    for canonical in CANONICALS:
        path = os.path.join(SAMPLE_DST, canonical)
        if not os.path.isfile(path):
            raise ValueError("示例数据缺失：{}（请检查 后台数据/ 目录）".format(canonical))
        with open(path, "rb") as fh:
            classified[canonical] = ("示例/" + canonical, fh.read())
    return _create_and_enqueue(classified, ai)


# ===================== worker（第三层：顶层 catch + 看门狗） =====================

def _worker_loop():
    app_log.info("对账 worker 线程已启动（单线程串行执行）")
    while True:
        tid = _task_queue.get()
        try:
            _execute_task(tid)
        except Exception:
            # worker 顶层 catch：任何异常必须把 meta 置 failed，绝不允许卡 running
            app_log.error("worker 顶层异常 task_id=%s\n%s", tid, traceback.format_exc())
            try:
                meta = _load_meta(tid)
                meta.update({
                    "status": "failed",
                    "step": "[worker]",
                    "error": "任务执行器异常：{}".format(sys.exc_info()[1]),
                    "traceback": traceback.format_exc(),
                    "finished_at": _now(),
                })
                _save_meta(tid, meta)
            except Exception:
                app_log.error("worker 顶层异常后写 meta 也失败 task_id=%s\n%s",
                              tid, traceback.format_exc())
        finally:
            _task_queue.task_done()


def _execute_task(tid):
    meta = _load_meta(tid)
    meta["status"] = "running"
    meta["started_at"] = _now()
    _save_meta(tid, meta)
    app_log.info("开始执行对账任务：%s", tid)

    holder = {}

    def _target():
        holder["result"] = engine_run(
            raw_dir=os.path.join(_task_dir(tid), "raw"),
            output_dir=os.path.join(_task_dir(tid), "output"),
            ai_enabled=bool(meta.get("ai_enabled")),
            log_dir=_task_dir(tid),
            task_id=tid,
        )

    th = threading.Thread(target=_target, daemon=True, name="engine-" + tid)
    th.start()
    th.join(WATCHDOG_SECONDS)
    if th.is_alive():
        # 看门狗：超时任务置 failed，线程弃置（daemon，进程退出即回收）
        app_log.error("任务 %s 执行超时（超过 %d 分钟），看门狗置 failed",
                      tid, WATCHDOG_SECONDS // 60)
        meta = _load_meta(tid)
        meta.update({
            "status": "failed",
            "step": "[看门狗]",
            "error": "执行超时（超过{}分钟）".format(WATCHDOG_SECONDS // 60),
            "finished_at": _now(),
        })
        _save_meta(tid, meta)
        return

    result = holder.get("result") or {
        "status": "failed", "step": "[worker]",
        "error": "引擎无返回结果", "traceback": None,
    }
    meta = _load_meta(tid)
    meta.update(result)
    if result.get("excel_path"):
        excel_file = os.path.basename(result["excel_path"])
        meta["excel_file"] = excel_file
        meta["excel_path"] = "output/" + excel_file
    meta["finished_at"] = _now()
    _save_meta(tid, meta)
    app_log.info("任务结束：%s status=%s 差异=%s 耗时=%sms",
                 tid, meta.get("status"),
                 (meta.get("stats") or {}).get("diff_count"),
                 meta.get("elapsed_ms"))


def _ensure_worker():
    global _worker_started
    if not _worker_started:
        threading.Thread(target=_worker_loop, daemon=True,
                         name="engine-worker").start()
        _worker_started = True


# ===================== 路由 =====================

@app.after_request
def _log_api_request(resp):
    if request.path.startswith("/api/"):
        app_log.info("%s %s -> %s", request.method, request.path, resp.status_code)
    return resp


@app.route("/")
def index():
    tasks = []
    if os.path.isdir(TASKS_DIR):
        for tid in os.listdir(TASKS_DIR):
            mpath = os.path.join(TASKS_DIR, tid, "meta.json")
            if not os.path.isfile(mpath):
                continue
            try:
                with open(mpath, encoding="utf-8") as f:
                    m = json.load(f)
            except Exception as e:
                app_log.warning("首页读取 meta 失败 %s：%s", mpath, e)
                continue
            tasks.append({
                "task_id": tid,
                "status": m.get("status"),
                "created_at": m.get("created_at"),
                "diff_count": (m.get("stats") or {}).get("diff_count"),
                "error": m.get("error"),
            })
    tasks.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return render_template("upload.html", tasks=tasks)


@app.route("/result/<tid>")
def result_page(tid):
    _require_valid_task(tid)
    return render_template("result.html", task_id=tid,
                           fastgpt_configured=fastgpt_configured())


@app.route("/api/tasks", methods=["POST"])
def create_task():
    try:
        files = request.files.getlist("files")
        if not files:
            return jsonify({"error": "未选择任何文件，请选择 5 份 CSV 后再提交"}), 400
        ai = 1 if request.form.get("ai") == "1" else 0
        tid = _ingest_uploads(files, ai)
        return jsonify({"task_id": tid, "status": "pending"}), 201
    except ValueError as e:
        app_log.warning("上传校验失败：%s", e)
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        app_log.error("上传处理失败：%s\n%s", e, traceback.format_exc())
        return jsonify({"error": "上传处理失败，请查看应用日志"}), 500


@app.route("/api/tasks/sample", methods=["POST"])
def create_sample_task():
    try:
        ai = 1 if request.form.get("ai") == "1" else 0
        tid = _ingest_sample(ai)
        return jsonify({"task_id": tid, "status": "pending"}), 201
    except ValueError as e:
        app_log.warning("示例任务创建失败：%s", e)
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        app_log.error("示例任务处理失败：%s\n%s", e, traceback.format_exc())
        return jsonify({"error": "示例任务处理失败，请查看应用日志"}), 500


@app.route("/api/tasks/<tid>")
def get_task(tid):
    try:
        _require_valid_task(tid)
        meta = _load_meta(tid)
        # 僵尸任务兜底：进程崩溃等极端场景下 running 超时 → 查询时直接判失败
        if meta.get("status") == "running" and meta.get("started_at"):
            try:
                started = datetime.strptime(
                    meta["started_at"], "%Y-%m-%d %H:%M:%S")
                if (datetime.now() - started).total_seconds() > WATCHDOG_SECONDS:
                    meta.update({
                        "status": "failed",
                        "step": "[看门狗]",
                        "error": "执行超时（超过{}分钟）".format(
                            WATCHDOG_SECONDS // 60),
                        "finished_at": _now(),
                    })
                    _save_meta(tid, meta)
                    app_log.error("任务 %s 状态兜底：running 超时已置 failed", tid)
            except ValueError:
                pass
        return jsonify(meta)
    except HTTPException:
        raise
    except Exception as e:
        app_log.error("查询任务失败 %s：%s\n%s", tid, e, traceback.format_exc())
        return jsonify({"error": "查询任务失败，请查看应用日志"}), 500


@app.route("/api/tasks/<tid>/log")
def get_task_log(tid):
    try:
        _require_valid_task(tid)
        path = os.path.join(_task_dir(tid), "run.log")
        if not os.path.isfile(path):
            return jsonify({"log": "（暂无任务日志）"})
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return jsonify({"log": "".join(lines[-LOG_TAIL_LINES:])})
    except HTTPException:
        raise
    except Exception as e:
        app_log.error("读取任务日志失败 %s：%s\n%s", tid, e, traceback.format_exc())
        return jsonify({"error": "读取日志失败，请查看应用日志"}), 500


@app.route("/api/tasks/<tid>/download")
def download_excel(tid):
    try:
        _require_valid_task(tid)
        meta = _load_meta(tid)
        excel_file = meta.get("excel_file")
        if not excel_file:
            return jsonify({"error": "该任务尚无对账结果文件"}), 404
        path = os.path.join(_task_dir(tid), "output", excel_file)
        if not os.path.isfile(path):
            return jsonify({"error": "结果文件不存在（可能已被清理）"}), 404
        return send_file(path, as_attachment=True, download_name=excel_file)
    except HTTPException:
        raise
    except Exception as e:
        app_log.error("下载失败 %s：%s\n%s", tid, e, traceback.format_exc())
        return jsonify({"error": "下载失败，请查看应用日志"}), 500


# ===================== Agent 聊天面板（FastGPT 服务端代理，P4） =====================

def _fastgpt_chat_url():
    """拼 FastGPT OpenAI 兼容接口地址；兼容 BASE 填主机根或 .../api 两种写法。"""
    base = (FASTGPT_BASE_URL or "").rstrip("/")
    if base.endswith("/api"):
        return base + "/v1/chat/completions"
    return base + "/api/v1/chat/completions"


def _extract_reply(message: dict) -> str:
    """从 FastGPT 响应 message 提取可读回复。

    兼容三种形态：①纯字符串 content；②Agent V2 结构化块列表
    （reasoning/agentAsk/interactive，抽出提问文本）；③content 为空但有
    reasoning_content 时降级用思考文本兜底。提取不到返回空串（由调用方判失败）。
    """
    content = (message or {}).get("content")
    if isinstance(content, str) and content.strip():
        return content.strip()
    if isinstance(content, list):
        texts = []

        def _ask_texts(params):
            out = []
            if isinstance(params, str):
                try:
                    params = json.loads(params)
                except Exception:
                    return out
            if isinstance(params, dict):
                if params.get("description"):
                    out.append(str(params["description"]))
                for q in params.get("questions") or []:
                    if isinstance(q, dict) and q.get("question"):
                        out.append(str(q["question"]))
            return out

        for block in content:
            if isinstance(block, str):
                texts.append(block)
                continue
            if not isinstance(block, dict):
                continue
            if block.get("type") == "reasoning":
                continue
            agent_ask = block.get("agentAsk")
            if isinstance(agent_ask, dict):
                texts.extend(_ask_texts(agent_ask.get("params")))
                continue
            interactive = block.get("interactive")
            if isinstance(interactive, dict) and interactive.get("type") == "agentAsk":
                texts.extend(_ask_texts(interactive.get("params")))
                continue
            if isinstance(block.get("content"), str) and block["content"].strip():
                texts.append(block["content"].strip())
        if texts:
            return "\n".join(t for t in texts if t).strip()
    reasoning = (message or {}).get("reasoning_content")
    if isinstance(reasoning, str) and reasoning.strip():
        return reasoning.strip()
    return ""


def _chat_history_get(tid):
    with _chat_lock:
        return list(_chat_history.get(tid, []))


def _chat_history_append(tid, user_msg, reply):
    with _chat_lock:
        hist = _chat_history.setdefault(tid, [])
        hist.append({"role": "user", "content": user_msg})
        hist.append({"role": "assistant", "content": reply})
        if len(hist) > CHAT_HISTORY_MAX_MSGS:
            del hist[: len(hist) - CHAT_HISTORY_MAX_MSGS]


@app.route("/api/chat", methods=["POST"])
def chat():
    tid = None
    try:
        data = request.get_json(silent=True) or {}
        tid = (data.get("task_id") or "").strip()
        message = (data.get("message") or "").strip()
        if not tid or not message:
            return jsonify({"error": "缺少 task_id 或 message"}), 400
        if len(message) > CHAT_MSG_MAX_CHARS:
            return jsonify({"error": "消息过长（超过{}字符）".format(CHAT_MSG_MAX_CHARS)}), 400
        _require_valid_task(tid)
        if not fastgpt_configured():
            app_log.warning("聊天请求被拒绝：FastGPT 未配置 task=%s", tid)
            return jsonify({"error": "AI服务暂不可用"}), 200
        meta = _load_meta(tid)
        context = build_diff_context(meta)
        history = _chat_history_get(tid)
        # 实测契约：FastGPT 丢弃入站 system 消息（以应用配置的系统提示词为准），
        # 约束与脱敏上下文必须合并进当前 user 消息才能到达模型
        user_content = (
            "【系统注入·本批对账差异上下文（已脱敏）】\n"
            "以下为本批真实对账数据，回答中的数字一律以此为准，不要自行计算；"
            "不要触发任何任务、不要索要文件路径。\n" + context
            + "\n\n【用户问题】\n" + message
        )
        payload = {
            "appId": FASTGPT_APP_ID,
            "chatId": tid,               # 按 task_id 固定，保持会话连续
            "stream": False,
            "detail": False,
            "messages": history + [{"role": "user", "content": user_content}],
        }
        app_log.debug("[CHAT] 请求 task=%s 历史=%d条 问题=%s",
                      tid, len(history), message[:50])
        resp = requests.post(
            _fastgpt_chat_url(),
            headers={
                "Authorization": "Bearer " + FASTGPT_API_KEY,
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=CHAT_TIMEOUT,
        )
        if resp.status_code != 200:
            raise RuntimeError("FastGPT HTTP {}: {}".format(
                resp.status_code, resp.text[:200]))
        body = resp.json()
        choices = body.get("choices") or []
        reply = _extract_reply(choices[0].get("message") if choices else None)
        if not reply:
            raise RuntimeError("FastGPT 响应缺少回复内容: {}".format(str(body)[:200]))
        _chat_history_append(tid, message, reply)
        app_log.debug("[CHAT] 回复 task=%s 长度=%d 摘要=%s",
                      tid, len(reply), reply[:80])
        return jsonify({"reply": reply})
    except HTTPException:
        raise
    except Exception as e:
        # 计划书：聊天面板一切异常 → AI服务暂不可用 + 堆栈落盘，绝不上抛 500
        app_log.error("聊天代理失败 task=%s：%s\n%s", tid, e, traceback.format_exc())
        return jsonify({"error": "AI服务暂不可用"}), 200


# ===================== 兜底错误页（第二层：禁止裸 500） =====================

@app.errorhandler(404)
def not_found(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": "接口或任务不存在"}), 404
    return jsonify({"error": "页面不存在"}), 404


@app.errorhandler(405)
def method_not_allowed(e):
    return jsonify({"error": "请求方法不允许"}), 405


@app.errorhandler(500)
def internal_error(e):
    app_log.error("未捕获异常（500）：%s\n%s",
                  request.path, traceback.format_exc())
    return jsonify({"error": "服务器内部错误，请查看应用日志"}), 500


# ===================== 启动 =====================

def _init_app():
    os.makedirs(TASKS_DIR, exist_ok=True)
    _setup_logging()
    # 配置体检：缺 key / 缺 FastGPT 配置 → 启动 WARNING（功能降级，不阻断）
    for warning in load_settings()["warnings"]:
        app_log.warning(warning)
    _ensure_sample()
    _recover_interrupted()
    _ensure_worker()
    app_log.info("Web 服务初始化完成（tasks=%s）", TASKS_DIR)


_init_app()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="跨境多平台订单&结算对账系统 v2.0 Web 服务")
    parser.add_argument("--host", default="127.0.0.1", help="默认 127.0.0.1（本机）")
    parser.add_argument("--port", type=int, default=8765, help="默认 8765")
    args = parser.parse_args()
    app_log.info("启动 Web 服务 http://%s:%d", args.host, args.port)
    app.run(host=args.host, port=args.port, debug=False, threaded=True)
