"""看门狗（仅 Web 进程启动）：周期扫 MySQL，超时任务置 failed。

任务状态在数据库里，看门狗不再依赖进程内存——Web 重启也不丢。
每 60 秒一轮：running 超 15 分钟 → failed；pending 超 15 分钟（job 丢失）→ failed。
"""

import logging
import threading
import time

from service import task_store

lg = logging.getLogger("app")

SCAN_SECONDS = 60
_started = False


def _loop():
    while True:
        try:
            n_run = task_store.fail_timed_out()
            n_pend = task_store.fail_stale_pending()
            if n_run:
                lg.error("[看门狗] 执行超时任务已置 failed：%d 个", n_run)
            if n_pend:
                lg.error("[看门狗] 排队丢失任务已置 failed：%d 个", n_pend)
        except Exception:
            lg.error("[看门狗] 扫描异常（下轮重试）", exc_info=True)
        time.sleep(SCAN_SECONDS)


def start():
    global _started
    if _started:
        return
    threading.Thread(target=_loop, daemon=True, name="watchdog").start()
    _started = True
    lg.info("看门狗已启动：每 %d 秒扫描，超时阈值见 settings.WATCHDOG_SECONDS",
            SCAN_SECONDS)
