"""跨境多平台订单&结算对账系统 · 组合根（thin app）。

分层（见 docs/项目复盘.md §二）：
- api/      路由与协议，只做参数校验与出入转换
- service/  任务编排：MySQL 任务表（状态唯一真相）+ Redis/RQ 队列 + 看门狗 + 聊天代理
- engine/   纯对账核（不依赖 Flask，可独立运行）
文件：data/tasks/{task_id}/ 只存 raw/output/run.log；任务元数据在 MySQL 表 tasks。
"""

import argparse

import requests  # noqa: F401  测试经由 app.requests 打桩上游（requests 为全局单例）

from api import create_app
from service.chat_service import (
    CHAT_HISTORY_MAX_MSGS,
    CHAT_MSG_MAX_CHARS,
    CHAT_TIMEOUT,
    extract_reply as _extract_reply,
    fastgpt_chat_url as _fastgpt_chat_url,
    fastgpt_configured,
)
from service.paths import TASKS_DIR
from service.task_store import (
    delete as _delete_meta,
    load as _load_meta,
    new_task_id as _new_task_id,
    now_str as _now,
    save_meta as _save_meta,
)
from service.uploader import classify as _classify
from engine.config import FASTGPT_API_KEY, FASTGPT_APP_ID, FASTGPT_BASE_URL

# 兼容再导出：测试与外部脚本按旧入口访问，内部实现全部在 service/api 层

app = create_app()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="跨境多平台订单&结算对账系统 Web 服务（MySQL + RQ 队列）")
    parser.add_argument("--host", default="127.0.0.1", help="默认 127.0.0.1（本机）")
    parser.add_argument("--port", type=int, default=8765, help="默认 8765")
    args = parser.parse_args()
    from service.bootstrap import setup_logging
    setup_logging()
    import logging
    logging.getLogger("app").info(
        "启动 Web 服务 http://%s:%d", args.host, args.port)
    app.run(host=args.host, port=args.port, debug=False, threaded=True)
