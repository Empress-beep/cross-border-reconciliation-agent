"""Agent 聊天面板：FastGPT 服务端代理（key 与原始数据均不出本机）。

契约要点（实测踩坑沉淀）：
- FastGPT 丢弃入站 system 消息 → 约束与脱敏上下文必须并入当前 user 消息
- Agent V2 content 可能是结构化块（reasoning/agentAsk/interactive）→ 三层提取
- 一切上游异常 → 抛给路由层统一回「AI服务暂不可用」，绝不上抛 500
"""

import json
import logging
import threading
import traceback

import requests

from engine.config import (
    FASTGPT_API_KEY,
    FASTGPT_APP_ID,
    FASTGPT_BASE_URL,
    fastgpt_configured,
)
from engine.desensitize import build_diff_context
from service import task_store

lg = logging.getLogger("app")

CHAT_TIMEOUT = 180                    # FastGPT 上游超时（秒）：Agent V2 实测 34~90s
CHAT_HISTORY_MAX_MSGS = 12            # 本地历史：近 6 轮（user+assistant 各 12 条）
CHAT_MSG_MAX_CHARS = 2000             # 单条问题长度上限

# 聊天会话历史（内存，按 task_id；进程重启即清空）
_chat_history = {}
_chat_lock = threading.Lock()


def fastgpt_chat_url():
    """拼 FastGPT OpenAI 兼容接口地址；兼容 BASE 填主机根或 .../api 两种写法。"""
    base = (FASTGPT_BASE_URL or "").rstrip("/")
    if base.endswith("/api"):
        return base + "/v1/chat/completions"
    return base + "/api/v1/chat/completions"


def extract_reply(message: dict) -> str:
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


def _history_get(tid):
    with _chat_lock:
        return list(_chat_history.get(tid, []))


def _history_append(tid, user_msg, reply):
    with _chat_lock:
        hist = _chat_history.setdefault(tid, [])
        hist.append({"role": "user", "content": user_msg})
        hist.append({"role": "assistant", "content": reply})
        if len(hist) > CHAT_HISTORY_MAX_MSGS:
            del hist[: len(hist) - CHAT_HISTORY_MAX_MSGS]


def send(tid, message):
    """发一次对话，返回回复文本；失败抛异常（路由层转「AI服务暂不可用」）。"""
    meta = task_store.load(tid)
    if meta is None:
        raise LookupError("任务不存在")
    context = build_diff_context(meta)
    history = _history_get(tid)
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
    lg.debug("[CHAT] 请求 task=%s 历史=%d条 问题=%s",
             tid, len(history), message[:50])
    resp = requests.post(
        fastgpt_chat_url(),
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
    reply = extract_reply(choices[0].get("message") if choices else None)
    if not reply:
        raise RuntimeError("FastGPT 响应缺少回复内容: {}".format(str(body)[:200]))
    _history_append(tid, message, reply)
    lg.debug("[CHAT] 回复 task=%s 长度=%d 摘要=%s",
             tid, len(reply), reply[:80])
    return reply


def fail_detail(tid, exc):
    lg.error("聊天代理失败 task=%s：%s\n%s", tid, exc, traceback.format_exc())
