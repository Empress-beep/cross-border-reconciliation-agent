"""文件系统路径与上传常量（Web/Worker 共用）。"""

import os
import re

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
TASKS_DIR = os.path.join(DATA_DIR, "tasks")
LOGS_DIR = os.path.join(DATA_DIR, "logs")
SAMPLE_DST = os.path.join(DATA_DIR, "sample")
SAMPLE_SRC = os.path.join(BASE_DIR, "后台数据")

MAX_FILE_BYTES = 5 * 1024 * 1024      # 单文件 5MB
LOG_TAIL_LINES = 200                  # 日志接口默认尾部行数
TASK_ID_RE = re.compile(r"^task_\d{8}_\d{6}_[a-z0-9]{4}$")

# 5 类规范文件名（顺序 = 缺失提示顺序）
CANONICALS = [
    "亚马逊订单明细.csv",
    "亚马逊结算账单.csv",
    "Temu订单明细.csv",
    "Temu结算账单.csv",
    "收款流水报表.csv",
]


def task_dir(tid):
    return os.path.join(TASKS_DIR, tid)
