"""任务 API：创建（上传/示例）、查询、日志、下载。"""

import logging
import os
import traceback

from flask import Blueprint, abort, jsonify, request, send_file
from werkzeug.exceptions import HTTPException

from service import chat_service, paths, task_store, uploader

lg = logging.getLogger("app")

bp = Blueprint("tasks", __name__)

MAX_FILE_BYTES_LABEL = paths.MAX_FILE_BYTES


def require_task(tid):
    """任务 ID 格式 + 存在性校验，不满足一律 404。"""
    if not paths.TASK_ID_RE.match(tid) or not task_store.exists(tid):
        abort(404, description="任务不存在")


@bp.route("/api/tasks", methods=["POST"])
def create_task():
    try:
        files = request.files.getlist("files")
        if not files:
            return jsonify({"error": "未选择任何文件，请选择 5 份 CSV 后再提交"}), 400
        ai = 1 if request.form.get("ai") == "1" else 0
        tid = uploader.ingest_uploads(files, ai)
        return jsonify({"task_id": tid, "status": "pending"}), 201
    except uploader.QueueUnavailable as e:
        lg.error("入队失败（Redis 不可用？）：%s", e)
        return jsonify({"error": "任务队列不可用（Redis 未启动？），请查看应用日志"}), 503
    except ValueError as e:
        lg.warning("上传校验失败：%s", e)
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        lg.error("上传处理失败：%s\n%s", e, traceback.format_exc())
        return jsonify({"error": "上传处理失败，请查看应用日志"}), 500


@bp.route("/api/tasks/sample", methods=["POST"])
def create_sample_task():
    try:
        ai = 1 if request.form.get("ai") == "1" else 0
        tid = uploader.ingest_sample(ai)
        return jsonify({"task_id": tid, "status": "pending"}), 201
    except uploader.QueueUnavailable as e:
        lg.error("示例任务入队失败（Redis 不可用？）：%s", e)
        return jsonify({"error": "任务队列不可用（Redis 未启动？），请查看应用日志"}), 503
    except ValueError as e:
        lg.warning("示例任务创建失败：%s", e)
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        lg.error("示例任务处理失败：%s\n%s", e, traceback.format_exc())
        return jsonify({"error": "示例任务处理失败，请查看应用日志"}), 500


@bp.route("/api/tasks/<tid>")
def get_task(tid):
    try:
        require_task(tid)
        # 崩溃兜底：running 超时 → 查询时直接判失败（状态在 MySQL，重启不丢）
        task_store.check_timeout(tid)
        meta = task_store.load(tid)
        if meta is None:
            abort(404, description="任务不存在")
        return jsonify(meta)
    except HTTPException:
        raise
    except Exception as e:
        lg.error("查询任务失败 %s：%s\n%s", tid, e, traceback.format_exc())
        return jsonify({"error": "查询任务失败，请查看应用日志"}), 500


@bp.route("/api/tasks/<tid>/log")
def get_task_log(tid):
    try:
        require_task(tid)
        path = os.path.join(paths.task_dir(tid), "run.log")
        if not os.path.isfile(path):
            return jsonify({"log": "（暂无任务日志）"})
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return jsonify({"log": "".join(lines[-paths.LOG_TAIL_LINES:])})
    except HTTPException:
        raise
    except Exception as e:
        lg.error("读取任务日志失败 %s：%s\n%s", tid, e, traceback.format_exc())
        return jsonify({"error": "读取日志失败，请查看应用日志"}), 500


@bp.route("/api/tasks/<tid>/download")
def download_excel(tid):
    try:
        require_task(tid)
        meta = task_store.load(tid)
        excel_file = (meta or {}).get("excel_file")
        if not excel_file:
            return jsonify({"error": "该任务尚无对账结果文件"}), 404
        path = os.path.join(paths.task_dir(tid), "output", excel_file)
        if not os.path.isfile(path):
            return jsonify({"error": "结果文件不存在（可能已被清理）"}), 404
        return send_file(path, as_attachment=True, download_name=excel_file)
    except HTTPException:
        raise
    except Exception as e:
        lg.error("下载失败 %s：%s\n%s", tid, e, traceback.format_exc())
        return jsonify({"error": "下载失败，请查看应用日志"}), 500
