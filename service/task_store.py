"""任务元数据仓储：MySQL 表 tasks 是任务状态的唯一真相。

- meta dict ↔ 行记录互转：生命周期列独立成列（可索引/可查询），其余整体入 payload JSON
- 状态机 pending → running → success/failed；流转一律用条件 UPDATE（CAS），
  防止「启动恢复」与「worker 收尾」互相覆盖
- 启动恢复 / 看门狗超时 / 历史 meta.json 自动迁移
"""

import json
import os
import uuid
from datetime import datetime, timedelta

from service import db, paths, settings

COLUMNS = (
    "task_id", "status", "ai_enabled", "step", "error",
    "created_at", "started_at", "finished_at",
    "elapsed_ms", "excel_file", "diff_count",
)

_SQL_COLS = ", ".join("`%s`" % c for c in COLUMNS) + ", `payload`"
_SQL_PH = ", ".join(["%s"] * (len(COLUMNS) + 1))


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def new_task_id():
    return "task_{}_{}".format(
        datetime.now().strftime("%Y%m%d_%H%M%S"), uuid.uuid4().hex[:4])


# ===================== meta ↔ 行 转换 =====================

def _meta_to_parts(meta):
    cols = {
        "task_id": meta.get("task_id"),
        "status": meta.get("status") or "pending",
        "ai_enabled": 1 if meta.get("ai_enabled") else 0,
        "step": meta.get("step"),
        "error": meta.get("error"),
        "created_at": meta.get("created_at") or now_str(),
        "started_at": meta.get("started_at"),
        "finished_at": meta.get("finished_at"),
        "elapsed_ms": meta.get("elapsed_ms"),
        "excel_file": meta.get("excel_file"),
        "diff_count": int(((meta.get("stats") or {}).get("diff_count")) or 0),
    }
    payload = {k: v for k, v in meta.items() if k not in COLUMNS}
    return cols, json.dumps(payload, ensure_ascii=False, default=str)


def _row_to_meta(row):
    meta = dict(row)
    meta.pop("payload", None)
    meta["ai_enabled"] = bool(meta.get("ai_enabled"))
    meta["diff_count"] = int(meta.get("diff_count") or 0)
    try:
        payload = json.loads(row.get("payload") or "{}")
    except (TypeError, ValueError):
        payload = {}
    meta.update(payload)
    return meta


def _split_params(meta):
    cols, payload_json = _meta_to_parts(meta)
    return tuple(cols[c] for c in COLUMNS) + (payload_json,)


# ===================== CRUD =====================

def save_meta(tid, meta):
    """无条件 upsert（创建、兼容入口、整记录回写）。"""
    meta = dict(meta)
    meta["task_id"] = tid
    params = _split_params(meta)
    conn = db.get_conn()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT IGNORE INTO tasks ({}) VALUES ({})".format(_SQL_COLS, _SQL_PH),
            params)
        if cur.rowcount == 0:
            sets = ", ".join(
                "`%s`=%%s" % c for c in COLUMNS[1:]) + ", `payload`=%s"
            cur.execute(
                "UPDATE tasks SET {} WHERE task_id=%s".format(sets),
                params[1:] + (tid,))


def load(tid):
    conn = db.get_conn()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT {} FROM tasks WHERE task_id=%s".format(_SQL_COLS), (tid,))
        row = cur.fetchone()
    return _row_to_meta(row) if row else None


def exists(tid):
    conn = db.get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM tasks WHERE task_id=%s LIMIT 1", (tid,))
        return cur.fetchone() is not None


def delete(tid):
    conn = db.get_conn()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM tasks WHERE task_id=%s", (tid,))


def list_brief():
    """首页历史列表（倒序）。"""
    conn = db.get_conn()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT task_id, status, created_at, diff_count, error "
            "FROM tasks ORDER BY created_at DESC, task_id DESC LIMIT 500")
        rows = cur.fetchall()
    return [
        {"task_id": r["task_id"], "status": r["status"],
         "created_at": r["created_at"], "diff_count": r["diff_count"],
         "error": r["error"]}
        for r in rows
    ]


# ===================== 状态机（CAS） =====================

def cas_start(tid, started_at):
    """pending → running。抢不到（已被恢复置失败等）返回 False。"""
    conn = db.get_conn()
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE tasks SET status='running', started_at=%s, step=NULL "
            "WHERE task_id=%s AND status='pending'",
            (started_at, tid))
        return cur.rowcount == 1


def cas_finish(tid, meta, from_statuses=("running",)):
    """running → success/failed，整记录回写。条件不满足返回 False（不覆盖恢复结果）。"""
    meta = dict(meta)
    meta["task_id"] = tid
    params = _split_params(meta)
    placeholders = ", ".join(
        "`%s`=%%s" % c for c in COLUMNS[1:]) + ", `payload`=%s"
    status_in = ", ".join(["%s"] * len(from_statuses))
    conn = db.get_conn()
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE tasks SET {} WHERE task_id=%s AND status IN ({})".format(
                placeholders, status_in),
            params[1:] + (tid,) + tuple(from_statuses))
        return cur.rowcount == 1


# ===================== 启动恢复 / 看门狗 =====================

def recover_interrupted():
    """服务重启：pending/running 一律置 failed 并给出原因，绝不卡在执行中。"""
    finished = now_str()
    conn = db.get_conn()
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE tasks SET status='failed', step=%s, error=%s, "
            "finished_at=%s WHERE status IN ('pending','running')",
            ("[启动恢复]", "服务重启导致任务中断，请重新发起对账", finished))
        count = cur.rowcount
    return count


def _timeout_cutoff():
    return (datetime.now() - timedelta(
        seconds=settings.WATCHDOG_SECONDS)).strftime("%Y-%m-%d %H:%M:%S")


def fail_timed_out():
    """看门狗：running 超过 15 分钟 → failed。"""
    conn = db.get_conn()
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE tasks SET status='failed', step=%s, error=%s, "
            "finished_at=%s WHERE status='running' AND started_at IS NOT NULL "
            "AND started_at < %s",
            ("[看门狗]",
             "执行超时（超过{}分钟）".format(settings.WATCHDOG_SECONDS // 60),
             now_str(), _timeout_cutoff()))
        return cur.rowcount


def check_timeout(tid):
    """查询时兜底：单个 running 超时任务立即判失败。"""
    conn = db.get_conn()
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE tasks SET status='failed', step=%s, error=%s, "
            "finished_at=%s WHERE task_id=%s AND status='running' "
            "AND started_at IS NOT NULL AND started_at < %s",
            ("[看门狗]",
             "执行超时（超过{}分钟）".format(settings.WATCHDOG_SECONDS // 60),
             now_str(), tid, _timeout_cutoff()))
        return cur.rowcount == 1


def fail_stale_pending():
    """兜底：pending 超过看门狗时长（入队的 job 已丢失）→ failed。"""
    conn = db.get_conn()
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE tasks SET status='failed', step=%s, error=%s, "
            "finished_at=%s WHERE status='pending' AND created_at < %s",
            ("[看门狗]", "排队超时未被执行（任务队列可能已丢失），请重新发起对账",
             now_str(), _timeout_cutoff()))
        return cur.rowcount


# ===================== 历史 meta.json 迁移 =====================

def migrate_legacy():
    """把旧版 data/tasks/*/meta.json 导入 MySQL（只补缺失行，幂等）。"""
    if not os.path.isdir(paths.TASKS_DIR):
        return 0
    migrated = 0
    for tid in os.listdir(paths.TASKS_DIR):
        mpath = os.path.join(paths.TASKS_DIR, tid, "meta.json")
        if not os.path.isfile(mpath):
            continue
        try:
            if exists(tid):
                continue
            with open(mpath, encoding="utf-8") as f:
                meta = json.load(f)
            meta.setdefault("task_id", tid)
            save_meta(tid, meta)
            migrated += 1
        except Exception:
            continue
    return migrated
