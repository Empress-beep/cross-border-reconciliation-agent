"""RQ 队列：Web 侧入队 + Worker 侧执行（唯一允许把任务置 running 的地方）。

执行语义（对齐原三层异常设计）：
1. cas_start 抢任务：抢不到（已被启动恢复置失败）→ 直接跳过，绝不复活死任务
2. 引擎在子线程跑、主线程 join 看门狗；超时 → 置 failed，daemon 线程弃置
3. 任何顶层异常 → 置 failed；收尾一律条件写（status IN running/pending 才生效），
   与「启动恢复」互不覆盖
"""

import logging
import os
import threading
import traceback

import redis as redis_lib
from rq import Queue

from service import paths, settings, task_store

lg = logging.getLogger("app")

_conn = None
_queue = None
# 可重入锁：get_queue 持锁期间会调 get_connection（其内部也要拿同一把锁）
_lock = threading.RLock()


def get_connection():
    """Redis 连接（protocol=2 兼容老版 Windows Redis 3.2）。"""
    global _conn
    if _conn is None:
        with _lock:
            if _conn is None:
                _conn = redis_lib.Redis(
                    host=settings.REDIS_HOST,
                    port=settings.REDIS_PORT,
                    db=settings.REDIS_DB,
                    socket_connect_timeout=5,
                    protocol=2,
                )
    return _conn


def get_queue():
    global _queue
    if _queue is None:
        with _lock:
            if _queue is None:
                _queue = Queue(settings.QUEUE_NAME, connection=get_connection())
    return _queue


def ping():
    try:
        return bool(get_connection().ping())
    except Exception:
        return False


def queue_length():
    try:
        return get_queue().count
    except Exception:
        return None


def enqueue(tid):
    """任务入队；Redis 不可用抛异常（由上传层转 503 并把行置 failed）。"""
    get_queue().enqueue(
        run_task_job, tid,
        job_timeout=settings.JOB_TIMEOUT_SECONDS,
        result_ttl=3600,
        failure_ttl=86400,
    )


def run_task_job(tid):
    """RQ worker 进程内执行体（顶层 catch：异常必须落 failed）。"""
    try:
        _run(tid)
    except Exception as e:
        try:
            fail_top_level(tid, e)
        except Exception:
            lg.error("[WORKER] 置 failed 也失败 task_id=%s\n%s",
                     tid, traceback.format_exc())


def _run(tid):
    from engine.reconcile import run as engine_run

    meta = task_store.load(tid)
    if meta is None:
        lg.warning("[WORKER] 任务不存在，跳过：%s", tid)
        return
    if not task_store.cas_start(tid, task_store.now_str()):
        lg.warning("[WORKER] 任务非 pending（可能已被启动恢复置失败），跳过：%s", tid)
        return
    lg.info("[WORKER] 开始执行对账任务：%s", tid)

    holder = {}

    def _target():
        holder["result"] = engine_run(
            raw_dir=os.path.join(paths.task_dir(tid), "raw"),
            output_dir=os.path.join(paths.task_dir(tid), "output"),
            ai_enabled=bool(meta.get("ai_enabled")),
            log_dir=paths.task_dir(tid),
            task_id=tid,
        )

    th = threading.Thread(target=_target, daemon=True, name="engine-" + tid)
    th.start()
    th.join(settings.WATCHDOG_SECONDS)
    if th.is_alive():
        lg.error("[WORKER] 任务 %s 执行超时（超过 %d 分钟），看门狗置 failed",
                 tid, settings.WATCHDOG_SECONDS // 60)
        _finish(tid, {
            "status": "failed",
            "step": "[看门狗]",
            "error": "执行超时（超过{}分钟）".format(settings.WATCHDOG_SECONDS // 60),
            "finished_at": task_store.now_str(),
        })
        return

    result = holder.get("result") or {
        "status": "failed", "step": "[WORKER]",
        "error": "引擎无返回结果", "traceback": None,
        "finished_at": task_store.now_str(),
    }
    _finish(tid, result)


def _finish(tid, result):
    meta = task_store.load(tid) or {"task_id": tid}
    meta.update(result)
    if meta.get("excel_path"):
        excel_file = os.path.basename(meta["excel_path"])
        meta["excel_file"] = excel_file
        meta["excel_path"] = "output/" + excel_file
    meta.setdefault("finished_at", task_store.now_str())
    ok = task_store.cas_finish(tid, meta, from_statuses=("pending", "running"))
    if not ok:
        lg.warning("[WORKER] 任务 %s 收尾条件写未命中（状态可能已被恢复/看门狗改写），"
                   "status=%s", tid, meta.get("status"))
    lg.info("[WORKER] 任务结束：%s status=%s 差异=%s 耗时=%sms",
            tid, meta.get("status"),
            (meta.get("stats") or {}).get("diff_count"),
            meta.get("elapsed_ms"))


def fail_top_level(tid, exc):
    """worker 顶层 catch：任何异常必须把任务置 failed，绝不允许卡 running。"""
    lg.error("[WORKER] 顶层异常 task_id=%s\n%s", tid, traceback.format_exc())
    _finish(tid, {
        "status": "failed",
        "step": "[WORKER]",
        "error": "任务执行器异常：{}".format(exc),
        "traceback": traceback.format_exc(),
        "finished_at": task_store.now_str(),
    })
