"""对账主流程（自 cross_check.py 的 main() 等价迁移）。

7 步流程、merge 键、批次级对账、差异筛选、双 sheet 导出均保持原逻辑；
改造点：去影刀依赖、结果返回结构化 dict（不再写 task JSON）、
按《任务计划书》第二章接入双写日志与三层异常捕获。
"""

import json
import logging
import os
import sys
import time
import traceback
from contextlib import contextmanager
from datetime import datetime

import pandas as pd

from .attribution import Attributor
from .checks import check_batch_pay, check_order_settle
from .config import QWEN_API_KEY
from .normalize import normalize_orders, normalize_settlements

# 5 份原始报表的规范文件名
CSV_AMAZON_ORDERS = "亚马逊订单明细.csv"
CSV_TEMU_ORDERS = "Temu订单明细.csv"
CSV_AMAZON_SETTLE = "亚马逊结算账单.csv"
CSV_TEMU_SETTLE = "Temu结算账单.csv"
CSV_PAYMENT = "收款流水报表.csv"

# 结果文件名模板：带日期时间戳，同日多次执行不覆盖、可区分新旧
RESULT_FILENAME_FMT = "对账差异_%Y%m%d_%H%M%S.xlsx"


# ===================== 日志 =====================

class _ConsoleStream:
    """Windows GBK 控制台安全写入：不可编码字符降级替换，绝不让日志崩溃。"""

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


def _make_logger(log_dir, task_id):
    """每个任务独立 logger：控制台 + run.log 双写，互不串扰。"""
    name = "engine.task.{}.{}".format(task_id or "default", int(time.time() * 1000))
    lg = logging.getLogger(name)
    lg.setLevel(logging.DEBUG)
    lg.propagate = False
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler(_ConsoleStream())
    console.setFormatter(formatter)
    lg.addHandler(console)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
        file_handler = logging.FileHandler(
            os.path.join(log_dir, "run.log"), encoding="utf-8")
        file_handler.setFormatter(formatter)
        lg.addHandler(file_handler)
    return lg


def _close_logger(lg):
    for handler in list(lg.handlers):
        handler.close()
        lg.removeHandler(handler)


# ===================== 步骤与工具 =====================

@contextmanager
def _step(lg, name):
    """步骤边界：INFO 记起止与耗时，异常时记 ERROR + 完整堆栈后重新抛出。"""
    t0 = time.time()
    lg.info("%s 开始", name)
    try:
        yield
    except Exception as e:
        lg.error("%s 失败: %s: %s\n%s",
                 name, type(e).__name__, e, traceback.format_exc())
        raise
    else:
        lg.info("%s 完成，耗时 %dms", name, int((time.time() - t0) * 1000))


def _read_csv(lg, raw_dir, filename) -> pd.DataFrame:
    path = os.path.join(raw_dir, filename)
    lg.info("读取文件：%s", filename)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"原始文件不存在：{path}")
    df = pd.read_csv(path, encoding="utf-8")
    if df.empty:
        raise ValueError(f"原始文件 {filename} 为空，无法继续对账")
    lg.info("  %s：%d 行 × %d 列", filename, df.shape[0], df.shape[1])
    return df


def _records(df: pd.DataFrame) -> list:
    """DataFrame -> JSON 安全记录列表（NaN->null、numpy->原生、时间->ISO）。"""
    return json.loads(df.to_json(orient="records", force_ascii=False, date_format="iso"))


def _ai_fill(lg, attributor: Attributor, diff_df: pd.DataFrame) -> pd.DataFrame:
    """为差异行填充「AI业务备注」列；AI 整批异常时降级，绝不让任务失败。"""
    if not attributor.enabled or diff_df.empty:
        diff_df["AI业务备注"] = ""
        return diff_df
    lg.info("[AI] 开始批量归因，共 %d 条", len(diff_df))
    try:
        remarks = []
        for _, row in diff_df.iterrows():
            remarks.append(
                attributor.qwen_analyze_diff(row["对账状态"], row.dropna().to_dict()))
        diff_df["AI业务备注"] = remarks
    except Exception as e:
        lg.warning("AI批量归因整体异常，已降级为占位备注：%s\n%s",
                   e, traceback.format_exc())
        diff_df["AI业务备注"] = ["AI分析暂不可用"] * len(diff_df)
    return diff_df


# ===================== 主入口 =====================

def run(raw_dir: str, output_dir: str, ai_enabled: bool = False,
        log_dir: str = None, task_id: str = "",
        logger: logging.Logger = None) -> dict:
    """执行一次完整对账，返回结构化结果（即 meta.json 数据源）。

    失败时绝不抛出：status=failed + step定位 + error摘要 + traceback全文。
    """
    t_start = time.time()
    lg = logger or _make_logger(log_dir, task_id)
    own_logger = logger is None
    current_step = None
    result = {
        "status": "failed",
        "ai_enabled": bool(ai_enabled),
        "ai_note": None,
        "step": None,
        "error": None,
        "traceback": None,
        "stats": {},
        "diff_summary": [],
        "order_settle_diff": [],
        "settle_pay_diff": [],
        "excel_path": None,
        "elapsed_ms": 0,
    }

    try:
        lg.info("=== 跨境对账任务启动 task_id=%s AI开关=%s ===",
                task_id or "-", "1" if ai_enabled else "0")
        if ai_enabled and not QWEN_API_KEY:
            lg.warning("未配置 DASHSCOPE_API_KEY，本次任务 AI归因降级关闭")
            ai_enabled = False
            result["ai_enabled"] = False
            result["ai_note"] = "未配置 AI Key，本次未归因"
        os.makedirs(output_dir, exist_ok=True)
        attributor = Attributor(ai_enabled, logger=lg)

        # ============== 1. 读取5份原始报表 ==============
        current_step = "[步骤1/7·读取原始报表]"
        with _step(lg, current_step):
            amazon_orders = _read_csv(lg, raw_dir, CSV_AMAZON_ORDERS)
            temu_orders = _read_csv(lg, raw_dir, CSV_TEMU_ORDERS)
            amazon_settle = _read_csv(lg, raw_dir, CSV_AMAZON_SETTLE)
            temu_settle = _read_csv(lg, raw_dir, CSV_TEMU_SETTLE)
            payment = _read_csv(lg, raw_dir, CSV_PAYMENT)

        # ============== 2. 订单数据统一格式 ==============
        current_step = "[步骤2/7·订单标准化]"
        with _step(lg, current_step):
            all_orders = normalize_orders(amazon_orders, temu_orders)
            lg.info("  标准化订单行数：%d", len(all_orders))

        # ============== 3. 结算数据统一格式 ==============
        current_step = "[步骤3/7·结算标准化]"
        with _step(lg, current_step):
            all_settlements = normalize_settlements(amazon_settle, temu_settle)
            lg.info("  标准化结算行数：%d", len(all_settlements))

        # ============== 4. 第一层对账：订单与结算 ==============
        current_step = "[步骤4/7·第一层对账：订单与结算]"
        with _step(lg, current_step):
            order_settle_merge = pd.merge(
                all_orders,                                 # 左表 -> 订单
                all_settlements,                            # 右表 -> 结算
                on=["line_id", "platform", "order_id"],     # 合并条件
                how="left",                                 # 左连接
                suffixes=("_order", "_settle")
            )
            order_settle_merge["对账状态"] = order_settle_merge.apply(
                check_order_settle, axis=1)
            # 筛选异常订单（保留所有非结算正常的记录）
            order_settle_diff = order_settle_merge[
                ~order_settle_merge["对账状态"].isin(["结算正常", "已退款结算"])
            ].copy()
            lg.info("  订单结算差异行数：%d", len(order_settle_diff))
            # AI分析：订单结算差异
            order_settle_diff = _ai_fill(lg, attributor, order_settle_diff)

        # ============== 5. 第二层对账：结算与收款 ==============
        current_step = "[步骤5/7·第二层对账：结算与回款]"
        with _step(lg, current_step):
            # 先按批次汇总结算总额
            settle_summary = all_settlements.groupby(["settlement_id", "platform"])[
                "settle_amount"].sum().reset_index()
            settle_summary.columns = ["settlement_id", "platform", "结算总额"]
            # 先按批次汇总所有渠道回款总额，用于主对账
            pay_batch_summary = payment.groupby("related_settlement")[
                ["received_amount", "fee", "net_amount"]
            ].sum().reset_index()
            pay_batch_summary.columns = [
                "settlement_id", "到账总额", "手续费总额", "净到账额"]

            # 链接结算表和回款表，进行批次级主对账（按总金额判断，避免分渠道假差异）
            batch_merge = pd.merge(
                settle_summary,
                pay_batch_summary,
                on="settlement_id",
                how="left"
            )
            batch_merge["对账状态"] = batch_merge.apply(
                check_batch_pay, axis=1)

            # 关联回渠道明细，保留渠道维度信息，状态沿用批次级结果
            payment_channel = payment.groupby(["related_settlement", "payment_channel"])[
                ["received_amount", "fee", "net_amount"]
            ].sum().reset_index()
            payment_channel.columns = [
                "settlement_id", "收款渠道", "到账总额", "手续费总额", "净到账额"]
            settle_pay_merge = pd.merge(
                payment_channel,
                batch_merge[["settlement_id", "platform", "结算总额", "对账状态"]],
                on="settlement_id",
                how="right"
            )
            # 筛选异常回款（保留所有非回款正常的批次）
            settle_pay_diff = settle_pay_merge[
                settle_pay_merge["对账状态"] != "回款正常"
            ].copy()
            lg.info("  结算回款差异行数：%d", len(settle_pay_diff))
            # AI分析：结算回款差异
            settle_pay_diff = _ai_fill(lg, attributor, settle_pay_diff)

        # ============== 6. 输出对账结果Excel ==============
        current_step = "[步骤6/7·导出差异Excel]"
        with _step(lg, current_step):
            excel_name = datetime.now().strftime(RESULT_FILENAME_FMT)
            excel_path = os.path.join(output_dir, excel_name)
            with pd.ExcelWriter(excel_path, engine="openpyxl") as writer:
                order_settle_diff.to_excel(
                    writer, sheet_name="订单结算差异", index=False)
                settle_pay_diff.to_excel(
                    writer, sheet_name="结算回款差异", index=False)
            lg.info("  已导出：%s (%d 字节)", excel_path, os.path.getsize(excel_path))

        # ============== 7. 生成结果摘要 ==============
        current_step = "[步骤7/7·生成结果摘要]"
        with _step(lg, current_step):
            total_orders = len(all_orders)
            settled_count = int(
                order_settle_merge["settlement_id"].notna().sum())
            received_count = int(
                (batch_merge["对账状态"] == "回款正常").sum())
            order_diff_cnt = len(order_settle_diff)
            pay_diff_cnt = len(
                settle_pay_diff[["settlement_id", "对账状态"]].drop_duplicates())
            diff_count = order_diff_cnt + pay_diff_cnt

            diff_summary = []
            if not order_settle_diff.empty:
                for status, cnt in order_settle_diff["对账状态"].value_counts().items():
                    diff_summary.append(f"{status}：{cnt}笔")
            pay_diff_stats = settle_pay_diff[[
                "settlement_id", "对账状态"]].drop_duplicates()
            if not pay_diff_stats.empty:
                for status, cnt in pay_diff_stats["对账状态"].value_counts().items():
                    diff_summary.append(f"{status}：{cnt}笔")

            result.update({
                "status": "success",
                "step": None,
                "error": None,
                "traceback": None,
                "stats": {
                    "total_orders": int(total_orders),
                    "settled_count": settled_count,
                    "received_count": received_count,
                    "diff_count": int(diff_count),
                },
                "diff_summary": diff_summary,
                "order_settle_diff": _records(order_settle_diff),
                "settle_pay_diff": _records(settle_pay_diff),
                "excel_path": excel_path,
            })

            lg.info("任务完成，差异共%d处，总耗时%.1fs（AI=%s 总订单=%d 已结算=%d 全额到账批次=%d）",
                    diff_count, (time.time() - t_start),
                    "开" if ai_enabled else "关",
                    total_orders, settled_count, received_count)
            for item in diff_summary:
                lg.info("  差异构成：%s", item)

    except Exception as e:
        # 第一层兜底：step 内异常已记过完整堆栈，这里补任务级定位信息
        tb = traceback.format_exc()
        if current_step is None:
            lg.error("任务失败（步骤外）: %s: %s\n%s",
                     type(e).__name__, e, tb)
        else:
            lg.error("任务失败于 %s：%s: %s", current_step, type(e).__name__, e)
        result["status"] = "failed"
        result["step"] = current_step
        result["error"] = f"{type(e).__name__}: {e}"
        result["traceback"] = tb
    finally:
        result["elapsed_ms"] = int((time.time() - t_start) * 1000)
        if own_logger:
            _close_logger(lg)
    return result
