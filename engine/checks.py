"""双层对账规则（自 cross_check.py 等价迁移，判定逻辑未改动）。

第一层：订单 ↔ 平台结算（check_order_settle）
第二层：结算 ↔ 银行回款（check_batch_pay）
"""

import pandas as pd


def check_order_settle(row) -> str:
    """第一层：逐行判定订单‑结算对账状态。"""
    # 优先级1：无结算记录
    if pd.isna(row["settlement_id"]):
        # 区分退款中订单和普通未结算
        if str(row["order_status"]) in ["退款中", "Refunded", "退款处理中"]:
            return "退款处理中"
        return "已下单未结算"
    # 优先级2：退款结算
    if round(float(row["refund_amount"]), 2) > 0:
        return "已退款结算"
    # 优先级3：金额校验（仅正向结算）
    expected = round(
        float(row["order_amount"]) - float(row["platform_fee"])
        - float(row["logistics_fee"]) - float(row["other_fee"]),
        2
    )
    if abs(expected - round(float(row["settle_amount"]), 2)) > 0.01:
        return "结算金额差异"
    return "结算正常"


def check_batch_pay(row) -> str:
    """第二层：批次级判定结算‑回款对账状态。"""
    # 无任何回款
    if pd.isna(row["到账总额"]):
        return "已结算未回款"

    settle_total = round(float(row["结算总额"]), 2)
    receive_total = round(float(row["到账总额"]), 2)
    diff = abs(settle_total - receive_total)
    # 全额到账
    if diff <= 0.01:
        return "回款正常"
    # 部分到账（回款小于结算，属于正常在途）
    if receive_total < settle_total:
        return "部分到账"
    # 回款大于结算，属于真实异常
    return "回款金额差异"
