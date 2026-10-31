"""上传接入层：文件识别/校验/落盘 → 建任务行（MySQL）→ 入队（RQ）。"""

import logging
import os

from service import paths, task_store, task_runner

lg = logging.getLogger("app")


class QueueUnavailable(Exception):
    """Redis/RQ 不可用（上传层转 503，并把任务行置 failed）。"""


def classify(orig_name):
    """按文件名关键字识别类型（不区分大小写）；无法识别返回 None。"""
    lower = orig_name.lower()
    if "亚马逊" in orig_name and "订单" in orig_name:
        return "亚马逊订单明细.csv"
    if "亚马逊" in orig_name and "结算" in orig_name:
        return "亚马逊结算账单.csv"
    if "temu" in lower and "订单" in orig_name:
        return "Temu订单明细.csv"
    if "temu" in lower and "结算" in orig_name:
        return "Temu结算账单.csv"
    if "收款" in orig_name or "流水" in orig_name:
        return "收款流水报表.csv"
    return None


def _new_meta(tid, files, ai):
    return {
        "task_id": tid,
        "status": "pending",
        "ai_enabled": bool(ai),
        "created_at": task_store.now_str(),
        "started_at": None,
        "finished_at": None,
        "step": None,
        "error": None,
        "traceback": None,
        "stats": {},
        "diff_summary": [],
        "order_settle_diff": [],
        "settle_pay_diff": [],
        "excel_file": None,
        "excel_path": None,
        "elapsed_ms": None,
        "files": list(files),
    }


def _create_and_enqueue(classified, ai):
    """classified: {规范名: (原始名, bytes)}。落盘 → 建行 → 入队。"""
    tid = task_store.new_task_id()
    tdir = paths.task_dir(tid)
    os.makedirs(os.path.join(tdir, "raw"), exist_ok=True)
    for canonical, (orig, data) in classified.items():
        with open(os.path.join(tdir, "raw", canonical), "wb") as fh:
            fh.write(data)
        lg.info("上传文件：%s (%dB) → 识别成功：%s", orig, len(data), canonical)

    task_store.save_meta(tid, _new_meta(tid, list(classified.keys()), ai))
    try:
        task_runner.enqueue(tid)
    except Exception as e:
        task_store.cas_finish(tid, {
            "task_id": tid,
            "status": "failed",
            "step": "[入队]",
            "error": "任务队列不可用（Redis 未启动？）：{}".format(e),
            "finished_at": task_store.now_str(),
        }, from_statuses=("pending",))
        raise QueueUnavailable(str(e)) from e
    lg.info("任务已创建并入队：%s（AI=%d，队列长度=%s）",
            tid, ai, task_runner.queue_length())
    return tid


def ingest_uploads(flask_files, ai):
    classified = {}
    for f in flask_files:
        orig = os.path.basename((f.filename or "").strip())
        if not orig:
            raise ValueError("存在没有文件名的上传项，请重新选择文件")
        if not orig.lower().endswith(".csv"):
            raise ValueError("仅支持 .csv 文件，收到：{}".format(orig))
        data = f.read(paths.MAX_FILE_BYTES + 1)
        if len(data) > paths.MAX_FILE_BYTES:
            raise ValueError("文件超过 5MB 限制：{}".format(orig))
        canonical = classify(orig)
        if canonical is None:
            raise ValueError(
                "无法识别的文件：{}（文件名需包含平台+类型关键字，"
                "如「亚马逊订单」「Temu结算」「收款流水」）".format(orig))
        if canonical in classified:
            raise ValueError(
                "重复上传：「{}」与「{}」均识别为「{}」".format(
                    classified[canonical][0], orig, canonical))
        classified[canonical] = (orig, data)
    missing = [c for c in paths.CANONICALS if c not in classified]
    if missing:
        raise ValueError("缺少必需文件：" + "、".join(missing))
    return _create_and_enqueue(classified, ai)


def ingest_sample(ai):
    classified = {}
    for canonical in paths.CANONICALS:
        path = os.path.join(paths.SAMPLE_DST, canonical)
        if not os.path.isfile(path):
            raise ValueError("示例数据缺失：{}（请检查 后台数据/ 目录）".format(canonical))
        with open(path, "rb") as fh:
            classified[canonical] = ("示例/" + canonical, fh.read())
    return _create_and_enqueue(classified, ai)
