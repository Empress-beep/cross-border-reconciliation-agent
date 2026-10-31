"""Agent 聊天面板路由：参数校验 + 统一异常兜底（绝不上抛 500）。"""

import logging

from flask import Blueprint, abort, jsonify, request
from werkzeug.exceptions import HTTPException

from service import chat_service, task_store

lg = logging.getLogger("app")

bp = Blueprint("chat", __name__)


@bp.route("/api/chat", methods=["POST"])
def chat():
    tid = None
    try:
        data = request.get_json(silent=True) or {}
        tid = (data.get("task_id") or "").strip()
        message = (data.get("message") or "").strip()
        if not tid or not message:
            return jsonify({"error": "缺少 task_id 或 message"}), 400
        if len(message) > chat_service.CHAT_MSG_MAX_CHARS:
            return jsonify({"error": "消息过长（超过{}字符）".format(
                chat_service.CHAT_MSG_MAX_CHARS)}), 400
        if not task_store.exists(tid):
            abort(404, description="任务不存在")
        if not chat_service.fastgpt_configured():
            lg.warning("聊天请求被拒绝：FastGPT 未配置 task=%s", tid)
            return jsonify({"error": "AI服务暂不可用"}), 200
        reply = chat_service.send(tid, message)
        return jsonify({"reply": reply})
    except HTTPException:
        raise
    except Exception as e:
        # 聊天面板一切异常 → AI服务暂不可用 + 堆栈落盘，绝不上抛 500
        chat_service.fail_detail(tid, e)
        return jsonify({"error": "AI服务暂不可用"}), 200
