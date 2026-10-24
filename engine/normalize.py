"""订单与结算数据标准化（自 cross_check.py 等价迁移，映射逻辑未改动）。"""

import pandas as pd


def normalize_orders(amazon_orders: pd.DataFrame, temu_orders: pd.DataFrame) -> pd.DataFrame:
    """亚马逊 + Temu 订单 -> 统一 schema：
    line_id, platform, order_id, [sub_order_id], order_amount, order_status, order_date
    """
    amazon_order_lines = amazon_orders.copy()
    amazon_order_lines["line_id"] = amazon_order_lines["order_id"]
    amazon_order_lines["platform"] = "亚马逊"
    amazon_order_lines = amazon_order_lines[[
        "line_id", "platform", "order_id", "order_amount",
        "order_status", "order_date"
    ]]

    temu_order_lines = temu_orders.copy()
    temu_order_lines["line_id"] = temu_orders["sub_order_id"]
    temu_order_lines["platform"] = "Temu"
    temu_order_lines["order_amount"] = temu_order_lines["goods_amount"]
    temu_order_lines["order_date"] = temu_order_lines["create_time"]
    temu_order_lines = temu_order_lines[[
        "line_id", "platform", "order_id", "sub_order_id",
        "order_amount", "order_status", "order_date"
    ]]

    all_orders = pd.concat(
        [amazon_order_lines, temu_order_lines], ignore_index=True)
    return all_orders


def normalize_settlements(amazon_settle: pd.DataFrame, temu_settle: pd.DataFrame) -> pd.DataFrame:
    """亚马逊 + Temu 结算 -> 统一 schema：
    line_id, platform, settlement_id, order_id, [sub_order_id],
    settle_amount, refund_amount, platform_fee, logistics_fee, other_fee, settlement_date
    """
    amazon_settle_lines = amazon_settle.copy()
    amazon_settle_lines["line_id"] = amazon_settle_lines["order_id"]
    amazon_settle_lines["platform"] = "亚马逊"
    amazon_settle_lines["settle_amount"] = amazon_settle_lines["total_settlement"]
    amazon_settle_lines["refund_amount"] = amazon_settle_lines["refund_amount"]
    amazon_settle_lines["platform_fee"] = amazon_settle_lines["platform_commission"]
    amazon_settle_lines["logistics_fee"] = amazon_settle_lines["fba_fee"]
    amazon_settle_lines["other_fee"] = amazon_settle_lines["other_fee"]
    amazon_settle_lines = amazon_settle_lines[[
        "line_id", "platform", "settlement_id", "order_id",
        "settle_amount", "refund_amount", "platform_fee",
        "logistics_fee", "other_fee", "settlement_date"
    ]]

    temu_settle_lines = temu_settle.copy()
    temu_settle_lines["line_id"] = temu_settle_lines["sub_order_id"]
    temu_settle_lines["platform"] = "Temu"
    temu_settle_lines["settlement_id"] = temu_settle_lines["statement_no"]
    temu_settle_lines["settle_amount"] = temu_settle_lines["actual_settlement"]
    temu_settle_lines["refund_amount"] = temu_settle_lines["refund_amt"]
    temu_settle_lines["platform_fee"] = temu_settle_lines["platform_service_fee"]
    temu_settle_lines["logistics_fee"] = temu_settle_lines["logistics_fee"]
    temu_settle_lines["other_fee"] = 0
    temu_settle_lines["settlement_date"] = temu_settle_lines["settle_time"]
    temu_settle_lines = temu_settle_lines[[
        "line_id", "platform", "settlement_id", "order_id", "sub_order_id",
        "settle_amount", "refund_amount", "platform_fee",
        "logistics_fee", "other_fee", "settlement_date"
    ]]

    all_settlements = pd.concat(
        [amazon_settle_lines, temu_settle_lines], ignore_index=True)
    return all_settlements
