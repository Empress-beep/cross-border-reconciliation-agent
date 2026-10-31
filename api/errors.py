"""请求日志 + 兜底错误页（禁止裸 500）。"""

import logging
import traceback

from flask import jsonify, request

lg = logging.getLogger("app")


def register(app):
    @app.after_request
    def _log_api_request(resp):
        if request.path.startswith("/api/"):
            lg.info("%s %s -> %s", request.method, request.path,
                    resp.status_code)
        return resp

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
        lg.error("未捕获异常（500）：%s\n%s",
                 request.path, traceback.format_exc())
        return jsonify({"error": "服务器内部错误，请查看应用日志"}), 500
