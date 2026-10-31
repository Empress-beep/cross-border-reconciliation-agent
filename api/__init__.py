"""API 层：Flask 应用工厂与路由注册（只做参数校验与出入协议，业务在 service）。"""

import os

from flask import Flask

from api import errors, routes_chat, routes_tasks, routes_web
from service import bootstrap, paths


def create_app():
    app = Flask(
        "reconcile",
        template_folder=os.path.join(paths.BASE_DIR, "web", "templates"),
        static_folder=os.path.join(paths.BASE_DIR, "web", "static"),
    )
    app.json.ensure_ascii = False
    # 模板改动即刻生效（本地工具场景，免重启）
    app.config["TEMPLATES_AUTO_RELOAD"] = True

    app.register_blueprint(routes_web.bp)
    app.register_blueprint(routes_tasks.bp)
    app.register_blueprint(routes_chat.bp)
    errors.register(app)

    bootstrap.init_web()
    return app
