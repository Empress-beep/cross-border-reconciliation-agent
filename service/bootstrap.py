"""启动引导：日志 → 库表 → 历史迁移 → 中断恢复 → 示例数据 → 健康体检 →
看门狗 → RQ worker 子进程守护（顺序即正确性：恢复必须先于 worker 收活）。"""

import atexit
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from logging.handlers import RotatingFileHandler

from service import db, paths, settings, task_store, task_runner, watchdog

lg = logging.getLogger("app")

_worker_proc = None
_worker_lock = threading.Lock()
_worker_streak = 0          # 连续快死次数（防疯狂重启）
_worker_started_at = 0.0
_shutdown = False


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


def setup_logging():
    os.makedirs(paths.LOGS_DIR, exist_ok=True)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = RotatingFileHandler(
        os.path.join(paths.LOGS_DIR, "app.log"), maxBytes=1024 * 1024,
        backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    fh.setLevel(logging.DEBUG)
    ch = logging.StreamHandler(_ConsoleStream())
    ch.setFormatter(fmt)
    for h in list(lg.handlers):
        lg.removeHandler(h)
    lg.setLevel(logging.DEBUG)  # 聊天代理的请求/响应摘要按计划以 DEBUG 落盘
    lg.addHandler(fh)
    lg.addHandler(ch)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    # rq 内部日志（job 执行、worker 崩溃等）也落同一批 handler，避免 worker 静默退出
    rq_lg = logging.getLogger("rq")
    rq_lg.setLevel(logging.INFO)
    rq_lg.propagate = False
    rq_lg.addHandler(fh)
    rq_lg.addHandler(ch)


def ensure_dirs():
    os.makedirs(paths.TASKS_DIR, exist_ok=True)
    os.makedirs(paths.LOGS_DIR, exist_ok=True)


def ensure_sample():
    """示例数据预拷贝到 data/sample/（防止 后台数据/ 被占用或挪走）。"""
    os.makedirs(paths.SAMPLE_DST, exist_ok=True)
    for name in paths.CANONICALS:
        src = os.path.join(paths.SAMPLE_SRC, name)
        dst = os.path.join(paths.SAMPLE_DST, name)
        if not os.path.isfile(dst) and os.path.isfile(src):
            try:
                shutil.copy(src, dst)
                lg.info("示例数据已预拷贝：%s", name)
            except OSError as e:
                lg.warning("示例数据拷贝失败 %s：%s", name, e)
    for name in paths.CANONICALS:
        if not os.path.isfile(os.path.join(paths.SAMPLE_DST, name)):
            lg.warning("示例数据缺失：data/sample/%s（一键示例可能不可用）", name)


def health_warnings():
    """配置体检：缺 key / 队列不通 → 启动 WARNING（功能降级，不阻断）。"""
    from engine.config import load_settings

    for warning in load_settings()["warnings"]:
        lg.warning(warning)
    if not task_runner.ping():
        lg.warning("Redis 不可用（%s:%s）——任务将无法入队，"
                   "请确认 Redis 服务已启动",
                   settings.REDIS_HOST, settings.REDIS_PORT)


# ===================== RQ worker 子进程守护 =====================

def _spawn_worker():
    global _worker_proc, _worker_started_at
    os.makedirs(paths.LOGS_DIR, exist_ok=True)
    log_path = os.path.join(paths.LOGS_DIR, "worker_console.log")
    log_fh = open(log_path, "ab")
    try:
        _worker_proc = subprocess.Popen(
            [sys.executable, "-m", "service.worker_main"],
            cwd=paths.BASE_DIR,
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
        )
    finally:
        log_fh.close()
    _worker_started_at = time.time()
    lg.info("RQ worker 子进程已拉起：pid=%s（控制台输出 → data/logs/worker_console.log）",
            _worker_proc.pid)


def _supervisor_loop():
    global _worker_proc, _worker_streak
    while not _shutdown:
        time.sleep(10)
        try:
            with _worker_lock:
                proc = _worker_proc
                if proc is None:
                    if _worker_streak < 5:
                        _spawn_worker()
                    continue
                if proc.poll() is None:
                    if time.time() - _worker_started_at > 60:
                        _worker_streak = 0
                    continue
                _worker_streak += 1
                if _worker_streak >= 5:
                    lg.error("RQ worker 连续崩溃 %d 次，停止自动拉起——"
                             "请查 data/logs/worker_console.log 后手动重启服务",
                             _worker_streak)
                    _worker_proc = None
                    continue
                lg.error("RQ worker 异常退出（code=%s），3 秒后自动拉起（第 %d 次）",
                         proc.returncode, _worker_streak)
                time.sleep(3)
                _spawn_worker()
        except Exception:
            lg.error("worker 守护线程异常", exc_info=True)


def _shutdown_worker():
    global _shutdown, _worker_proc
    _shutdown = True
    with _worker_lock:
        proc = _worker_proc
        _worker_proc = None
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        lg.info("RQ worker 子进程已随服务退出终止：pid=%s", proc.pid)


def start_worker():
    """拉起 worker 子进程并启动守护线程；pytest / RECONCILE_WORKER=off 时跳过。"""
    if not settings.WORKER_AUTOSPAWN:
        lg.info("RECONCILE_WORKER=off：跳过 worker 自动拉起（手动模式）")
        return
    if "pytest" in sys.modules:
        lg.info("pytest 环境：跳过 RQ worker 自动拉起")
        return
    with _worker_lock:
        if _worker_proc is None or _worker_proc.poll() is not None:
            _spawn_worker()
    atexit.register(_shutdown_worker)
    threading.Thread(target=_supervisor_loop, daemon=True,
                     name="worker-supervisor").start()
    lg.info("worker 守护线程已启动：每 10 秒探活，崩溃自动拉起（上限 5 次）")


# ===================== init =====================

def init_web():
    """Web 进程初始化（app 导入时执行）。"""
    ensure_dirs()
    setup_logging()
    db.init_schema()
    migrated = task_store.migrate_legacy()
    if migrated:
        lg.info("历史 meta.json 已迁移入库：%d 个任务", migrated)
    recovered = task_store.recover_interrupted()
    if recovered:
        lg.warning("启动恢复：%d 个中断任务已置 failed（服务重启导致任务中断）",
                   recovered)
    ensure_sample()
    health_warnings()
    watchdog.start()
    start_worker()
    lg.info("Web 服务初始化完成（tasks=%s，MySQL=%s:%s，Redis=%s:%s 队列=%s）",
            paths.TASKS_DIR, settings.MYSQL_HOST, settings.MYSQL_PORT,
            settings.REDIS_HOST, settings.REDIS_PORT, settings.QUEUE_NAME)


def init_worker():
    """Worker 进程初始化（不做恢复——恢复的唯一责任方是 Web）。"""
    ensure_dirs()
    setup_logging()
    db.init_schema()
    lg.info("RQ worker 初始化完成（队列=%s）", settings.QUEUE_NAME)
