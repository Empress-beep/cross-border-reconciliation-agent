"""页面路由：首页（上传+历史列表）、结果页。"""

from flask import Blueprint, render_template

from service import chat_service, task_store

bp = Blueprint("web", __name__)


@bp.route("/")
def index():
    tasks = task_store.list_brief()
    return render_template("upload.html", tasks=tasks)


@bp.route("/result/<tid>")
def result_page(tid):
    from api.routes_tasks import require_task
    require_task(tid)
    return render_template("result.html", task_id=tid,
                           fastgpt_configured=chat_service.fastgpt_configured())
