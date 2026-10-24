"""P3 内置 AI 接入测试：脱敏出网、缺key降级、断网熔断、零网络调用。"""

import json
from pathlib import Path

from engine.attribution import Attributor
from engine.reconcile import run

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "后台数据"


class _FakeResp:
    status_code = 200
    message = ""

    def __init__(self, text):
        self._text = text

    def get(self, key, default=None):
        if key == "output":
            return {"choices": [{"message": {"content": [{"text": self._text}]}}]}
        return default


def _read_log(tmp_path):
    return (tmp_path / "run.log").read_text(encoding="utf-8")


# ===================== 验收5：出网 payload 脱敏 =====================

def test_ai_payload_sanitized(monkeypatch):
    """调用千问的 payload 不含完整订单号/结算号与精确金额（脱敏后出网）。"""
    import dashscope

    captured = []

    def _fake_call(**kwargs):
        captured.append(kwargs)
        return _FakeResp("订单已生成结算单但平台尚未回款")

    monkeypatch.setattr(dashscope.MultiModalConversation, "call", _fake_call)
    monkeypatch.setattr("engine.attribution.QWEN_API_KEY", "test-key")

    att = Attributor(enabled=True)
    assert att.enabled

    row = {
        "order_id": "AMZ-20260904-007",
        "settlement_id": "AMZ-SET-0906",
        "order_amount": 22.5,
        "settle_amount": 34.54,
        "order_status": "Pending",
        "对账状态": "已下单未结算",
    }
    remark = att.qwen_analyze_diff("已下单未结算", row)

    assert captured, "未捕获到出网调用"
    payload = json.dumps(captured[0], ensure_ascii=False, default=str)
    assert "AMZ-20260904-007" not in payload, "完整订单号泄露"
    assert "AMZ-SET-0906" not in payload, "完整结算号泄露"
    assert "22.5" not in payload and "34.54" not in payload, "精确金额泄露"
    assert "AMZ-****-007" in payload, "应包含掩码订单号"
    assert "[20,30)" in payload and "[30,40)" in payload, "应包含金额区间"
    assert remark and len(remark) <= 40


# ===================== 验收1：缺 key 降级 =====================

def test_no_key_downgrade(monkeypatch, tmp_path):
    """无 key 时勾选 ai=1：任务成功、强制降级、带 ai_note、零出网。"""
    import dashscope

    def _boom(*args, **kwargs):
        raise AssertionError("缺 key 不应出网")

    monkeypatch.setattr(dashscope.MultiModalConversation, "call", _boom)
    monkeypatch.setattr("engine.reconcile.QWEN_API_KEY", "")
    monkeypatch.setattr("engine.attribution.QWEN_API_KEY", "")

    result = run(
        raw_dir=str(SAMPLE),
        output_dir=str(tmp_path / "out"),
        ai_enabled=True,
        log_dir=str(tmp_path),
        task_id="test_no_key",
    )
    assert result["status"] == "success"
    assert result["ai_enabled"] is False
    assert result["ai_note"] and "未配置" in result["ai_note"]
    log_text = _read_log(tmp_path)
    assert "DASHSCOPE_API_KEY" in log_text


# ===================== 验收4：断网 → 熔断降级 =====================

def test_offline_circuit_breaker(monkeypatch, tmp_path):
    """断网 ai=1：任务仍成功，3 次 WARNING + 熔断 ERROR，备注列为熔断文案。"""
    import dashscope

    def _fail(*args, **kwargs):
        raise ConnectionError("network down")

    monkeypatch.setattr(dashscope.MultiModalConversation, "call", _fail)
    monkeypatch.setattr("engine.attribution.QWEN_API_KEY", "test-key")
    monkeypatch.setattr("engine.reconcile.QWEN_API_KEY", "test-key")

    result = run(
        raw_dir=str(SAMPLE),
        output_dir=str(tmp_path / "out"),
        ai_enabled=True,
        log_dir=str(tmp_path),
        task_id="test_offline",
    )
    assert result["status"] == "success", "AI 异常绝不能让任务失败"
    assert result["ai_enabled"] is True

    log_text = _read_log(tmp_path)
    assert log_text.count("AI归因失败(") >= 3, "连续失败应记 3 次 WARNING"
    assert "AI归因熔断" in log_text and "[ERROR]" in log_text, "应有熔断 ERROR 日志"

    remarks = [r.get("AI业务备注") or "" for r in result["order_settle_diff"]]
    remarks += [r.get("AI业务备注") or "" for r in result["settle_pay_diff"]]
    assert remarks, "差异行应有备注列"
    assert any(("熔断" in m) or ("失败" in m) for m in remarks), \
        "备注列应显示熔断/失败文案"


# ===================== 验收3：不勾选 → 零调用（日志维度） =====================

def test_disabled_no_ai_lines(monkeypatch, tmp_path):
    """有 key 但不勾选：任务日志里不出现任何 [AI] 归因调用记录。"""
    import dashscope

    def _boom(*args, **kwargs):
        raise AssertionError("未勾选不应出网")

    monkeypatch.setattr(dashscope.MultiModalConversation, "call", _boom)
    monkeypatch.setattr("engine.attribution.QWEN_API_KEY", "test-key")

    result = run(
        raw_dir=str(SAMPLE),
        output_dir=str(tmp_path / "out"),
        ai_enabled=False,
        log_dir=str(tmp_path),
        task_id="test_ai_off2",
    )
    assert result["status"] == "success"
    assert result["ai_enabled"] is False
    assert result["ai_note"] is None
    assert "[AI] 归因调用" not in _read_log(tmp_path)
