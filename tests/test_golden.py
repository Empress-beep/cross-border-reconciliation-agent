"""golden 回归测试：样例数据跑通 + 9 处差异锁定 + 异常定位用例。"""

import re
import shutil
from pathlib import Path

import pandas as pd

from engine.reconcile import run
from engine.checks import check_batch_pay, check_order_settle

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "后台数据"
FILES = [
    "亚马逊订单明细.csv",
    "亚马逊结算账单.csv",
    "Temu订单明细.csv",
    "Temu结算账单.csv",
    "收款流水报表.csv",
]


def _copy_sample(dest: Path, skip=None, empty=None):
    dest.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        if name == skip:
            continue
        if name == empty:
            (dest / name).write_text("transaction_id\n", encoding="utf-8")
            continue
        shutil.copy(SAMPLE / name, dest / name)
    return dest


# ===================== golden 主用例 =====================

def test_golden_sample(tmp_path):
    result = run(
        raw_dir=str(SAMPLE),
        output_dir=str(tmp_path / "out"),
        ai_enabled=False,
        log_dir=str(tmp_path),
        task_id="test_golden",
    )
    assert result["status"] == "success", result.get("error")
    assert result["error"] is None
    assert result["stats"]["total_orders"] == 20
    assert result["stats"]["diff_count"] == 9
    assert len(result["order_settle_diff"]) == 4
    assert len(result["settle_pay_diff"]) == 5
    assert len(result["diff_summary"]) >= 2

    # Excel 产物：双 sheet + 日期时间戳命名（防同日覆盖、可区分新旧批次）
    excel = Path(result["excel_path"])
    assert excel.exists()
    assert re.fullmatch(r"对账差异_\d{8}_\d{6}\.xlsx", excel.name), excel.name
    sheets = pd.read_excel(excel, sheet_name=None)
    assert set(sheets) == {"订单结算差异", "结算回款差异"}

    # 任务日志：7 步完整、任务完成、无影刀依赖痕迹
    log_file = tmp_path / "run.log"
    assert log_file.exists()
    log_text = log_file.read_text(encoding="utf-8")
    for i in range(1, 8):
        assert f"[步骤{i}/7" in log_text
    assert "任务完成" in log_text
    assert "xbot" not in log_text
    assert "Logging error" not in log_text


# ===================== 异常定位用例 =====================

def test_missing_file_failed(tmp_path):
    raw = _copy_sample(tmp_path / "raw", skip="收款流水报表.csv")
    result = run(
        raw_dir=str(raw),
        output_dir=str(tmp_path / "out"),
        ai_enabled=False,
        log_dir=str(tmp_path),
        task_id="test_missing",
    )
    assert result["status"] == "failed"
    assert result["step"].startswith("[步骤1/7")
    assert "不存在" in result["error"]
    assert "FileNotFoundError" in result["traceback"]
    log_text = (tmp_path / "run.log").read_text(encoding="utf-8")
    assert "[步骤1/7" in log_text and "Traceback" in log_text


def test_empty_csv_failed(tmp_path):
    raw = _copy_sample(tmp_path / "raw", empty="收款流水报表.csv")
    result = run(
        raw_dir=str(raw),
        output_dir=str(tmp_path / "out"),
        ai_enabled=False,
        log_dir=str(tmp_path),
        task_id="test_empty",
    )
    assert result["status"] == "failed"
    assert result["step"].startswith("[步骤1/7")
    assert "为空" in result["error"]
    assert "ValueError" in result["traceback"]


def test_ai_disabled_no_network(tmp_path, monkeypatch):
    """ai_enabled=False 时不允许发生任何千问调用。"""
    import dashscope

    def _boom(*args, **kwargs):
        raise AssertionError("AI关闭时不应调用千问")

    monkeypatch.setattr(
        dashscope.MultiModalConversation, "call", _boom)
    result = run(
        raw_dir=str(SAMPLE),
        output_dir=str(tmp_path / "out"),
        ai_enabled=False,
        log_dir=str(tmp_path),
        task_id="test_ai_off",
    )
    assert result["status"] == "success"
    assert result["ai_enabled"] is False


def test_attributor_disabled_returns_placeholder():
    from engine.attribution import Attributor
    att = Attributor(enabled=False)
    assert att.qwen_analyze_diff("已下单未结算", {"order_id": "AMZ-1"}) == "AI分析未开启"


# ===================== 规则与脱敏 =====================

def test_check_order_settle_priority():
    import pandas as pd

    row_no_settle = {"settlement_id": float("nan"), "order_status": "Pending"}
    assert check_order_settle(row_no_settle) == "已下单未结算"
    row_refunding = {"settlement_id": float("nan"), "order_status": "退款中"}
    assert check_order_settle(row_refunding) == "退款处理中"
    row_refunded = {
        "settlement_id": "S1", "refund_amount": 10.8,
        "order_amount": 29.97, "platform_fee": 4.5,
        "logistics_fee": 6.3, "other_fee": 0, "settle_amount": -10.8,
    }
    assert check_order_settle(row_refunded) == "已退款结算"
    row_amount_diff = {
        "settlement_id": "S1", "refund_amount": 0,
        "order_amount": 45.99, "platform_fee": 6.9,
        "logistics_fee": 4.25, "other_fee": 0.3, "settle_amount": 30.0,
    }
    assert check_order_settle(row_amount_diff) == "结算金额差异"
    row_ok = dict(row_amount_diff, settle_amount=34.54)
    assert check_order_settle(row_ok) == "结算正常"


def test_check_batch_pay_states():
    assert check_batch_pay({"结算总额": 100.0, "到账总额": float("nan")}) == "已结算未回款"
    assert check_batch_pay({"结算总额": 100.0, "到账总额": 100.0}) == "回款正常"
    assert check_batch_pay({"结算总额": 100.0, "到账总额": 60.0}) == "部分到账"
    assert check_batch_pay({"结算总额": 100.0, "到账总额": 120.0}) == "回款金额差异"


def test_desensitize():
    from engine.desensitize import bucket_amount, mask_id, sanitize_row

    assert mask_id("AMZ-20260904-007") == "AMZ-****-007"
    assert mask_id("TEMU-902003") == "TE*******03"
    assert bucket_amount(34.54) == "[30,40)"
    assert bucket_amount(-10.8) == "[-20,-10)"

    row = {
        "order_id": "AMZ-20260904-007",
        "settle_amount": 34.54,
        "order_status": "Pending",
        "对账状态": "已下单未结算",
        "结算总额": 34.54,
        "settlement_date": "2026-09-05",
    }
    safe = sanitize_row(row)
    assert safe["order_id"] == "AMZ-****-007"
    assert safe["settle_amount"] == "[30,40)"
    assert safe["结算总额"] == "[30,40)"
    assert safe["order_status"] == "Pending"
    assert safe["对账状态"] == "已下单未结算"
    assert safe["settlement_date"] == "2026-09-05"
