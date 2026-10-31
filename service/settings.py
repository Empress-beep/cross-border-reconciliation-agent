"""服务端基础设施配置：MySQL / Redis / RQ 队列（.env 可覆盖）。"""

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")

# ===== MySQL（任务元数据唯一真相） =====
MYSQL_HOST = os.getenv("MYSQL_HOST", "127.0.0.1").strip() or "127.0.0.1"
MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306") or "3306")
MYSQL_USER = os.getenv("MYSQL_USER", "root").strip() or "root"
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "").strip()
MYSQL_DB = os.getenv("MYSQL_DB", "reconcile_agent").strip() or "reconcile_agent"

# ===== Redis + RQ（任务队列） =====
# protocol=2：兼容 Windows 老版 Redis（3.2 不支持 HELLO 握手）
REDIS_HOST = os.getenv("REDIS_HOST", "127.0.0.1").strip() or "127.0.0.1"
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379") or "6379")
REDIS_DB = int(os.getenv("REDIS_DB", "0") or "0")
QUEUE_NAME = os.getenv("QUEUE_NAME", "reconcile").strip() or "reconcile"

# worker 由 Web 进程自动拉起与守护；分布式部署时可关掉改用手动 worker
WORKER_AUTOSPAWN = (os.getenv("RECONCILE_WORKER", "auto").strip().lower()
                    not in ("off", "0", "no"))

# ===== 超时 =====
WATCHDOG_SECONDS = 15 * 60          # 单任务超时（看门狗/启动恢复共用）
JOB_TIMEOUT_SECONDS = 1800           # RQ job 硬超时（看门狗兜底之上再加一层）
