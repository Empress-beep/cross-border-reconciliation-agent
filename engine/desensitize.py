"""出网数据脱敏：订单号掩码 + 金额区间化。

适用范围：调用千问归因的行数据、注入 FastGPT 聊天面板的差异上下文。
本地页面展示和 Excel 导出均使用原始数据，不经过本模块。
"""

import math

# ---------- 单值脱敏 ----------


def mask_id(s) -> str:
    """订单号/结算单号/流水号部分掩码。

    AMZ-20260904-007 -> AMZ-****-007（保留平台前缀与尾号）
    TEMU-902003       -> TEMU********03（无三段结构时首尾保留）
    """
    text = str(s)
    if text in ("", "nan", "None", "NaT"):
        return text
    parts = text.split("-")
    if len(parts) >= 3:
        return "{}-****-{}".format(parts[0], parts[-1])
    if len(text) <= 4:
        return "*" * len(text)
    return text[:2] + "*" * (len(text) - 4) + text[-2:]


def bucket_amount(x, step: float = 10):
    """金额区间化：34.54 -> '[30,40)'，-10.80 -> '[-20,-10)'。"""
    try:
        value = float(x)
    except (TypeError, ValueError):
        return x
    if math.isnan(value):
        return x
    low = math.floor(value / step) * step
    return "[{:g},{:g})".format(low, low + step)


# ---------- 行级批量脱敏 ----------

_AMOUNT_EN_TOKENS = ("amount", "fee", "amt", "commission")
_AMOUNT_CN_TOKENS = ("总额", "金额", "手续费", "到账")
_CN_ID_TOKENS = ("单号", "编号")


def _is_id_key(key) -> bool:
    k = str(key).lower()
    if k.endswith("_id") or k.endswith("_no"):
        return True
    if "related_settlement" in k:
        return True
    return any(t in str(key) for t in _CN_ID_TOKENS)


def _is_amount_key(key) -> bool:
    k = str(key).lower()
    if any(t in k for t in _AMOUNT_EN_TOKENS):
        return True
    return any(t in str(key) for t in _AMOUNT_CN_TOKENS)


def sanitize_row(row: dict) -> dict:
    """按字段名规则对整行脱敏：id类字段掩码，金额类字段区间化，其余原样。"""
    out = {}
    for k, v in row.items():
        if v is None:
            out[k] = v
        elif _is_id_key(k):
            out[k] = mask_id(v)
        elif _is_amount_key(k):
            out[k] = bucket_amount(v)
        else:
            out[k] = v
    return out


# ---------- 聊天面板：差异上下文（出网文本） ----------

_CONTEXT_MAX_CHARS = 4000


def _fmt_row(row: dict) -> str:
    """脱敏行渲染为紧凑单行：跳过空值字段。"""
    parts = []
    for k, v in row.items():
        if v is None or str(v).strip() in ("", "nan", "NaT"):
            continue
        parts.append("{}={}".format(k, v))
    return "  - " + " ".join(parts)


def build_diff_context(meta: dict, max_rows: int = 8) -> str:
    """从任务 meta 生成脱敏差异摘要文本（FastGPT 聊天面板的唯一出网上下文）。

    只输出统计、差异构成、脱敏后的差异明细；不含完整订单号与精确金额。
    """
    stats = (meta or {}).get("stats") or {}
    lines = [
        "【本批对账统计】总订单{}笔，已结算{}笔，全额到账批次{}个，差异共{}笔。".format(
            stats.get("total_orders", "-"),
            stats.get("settled_count", "-"),
            stats.get("received_count", "-"),
            stats.get("diff_count", "-"),
        )
    ]
    summary = (meta or {}).get("diff_summary") or []
    if summary:
        lines.append("【差异构成】" + "；".join(str(s) for s in summary) + "。")
    for title, key in (
        ("订单-结算差异明细（已脱敏，最多{}条）".format(max_rows), "order_settle_diff"),
        ("结算-回款差异明细（已脱敏，最多{}条）".format(max_rows), "settle_pay_diff"),
    ):
        rows = (meta or {}).get(key) or []
        if rows:
            lines.append("【{}】".format(title))
            for row in rows[:max_rows]:
                lines.append(_fmt_row(sanitize_row(row)))
    text = "\n".join(lines)
    if len(text) > _CONTEXT_MAX_CHARS:
        text = text[:_CONTEXT_MAX_CHARS] + "\n（上下文过长已截断）"
    return text
