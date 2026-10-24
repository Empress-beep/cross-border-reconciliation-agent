"""P4 聊天面板 /api/chat 代理测试（mock FastGPT 上游，不发真实请求）。"""

import json
import os
import shutil
import time

import pytest
import requests

import app as app_mod


class FakeResp:
    def __init__(self, status_code=200, data=None, text=""):
        self.status_code = status_code
        self._data = data if data is not None else {}
        self.text = text or json.dumps(self._data, ensure_ascii=False)

    def json(self):
        return self._data


def _reply_data(text="答复内容"):
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


@pytest.fixture
def client():
    app_mod.app.config["TESTING"] = True
    with app_mod.app.test_client() as c:
        yield c


@pytest.fixture
def success_task():
    tid = app_mod._new_task_id()
    meta = {
        "task_id": tid,
        "status": "success",
        "ai_enabled": False,
        "ai_note": None,
        "created_at": app_mod._now(),
        "finished_at": app_mod._now(),
        "stats": {"total_orders": 8, "settled_count": 7,
                  "received_count": 3, "diff_count": 9},
        "diff_summary": ["金额差异：2笔", "已结算未到账：2笔"],
        "order_settle_diff": [{
            "platform": "AMZ", "order_id": "AMZ-20260904-007",
            "order_status": "已结算", "对账状态": "金额差异",
            "order_amount": 22.54, "settle_amount": 21.0,
            "settlement_id": "AMZ-ST-011", "AI业务备注": "",
        }],
        "settle_pay_diff": [],
        "excel_file": None, "excel_path": None, "elapsed_ms": 100,
        "files": [],
    }
    tdir = os.path.join(app_mod.TASKS_DIR, tid)
    os.makedirs(tdir, exist_ok=True)
    app_mod._save_meta(tid, meta)
    yield tid
    shutil.rmtree(tdir, ignore_errors=True)


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(app_mod, "FASTGPT_API_KEY", "fastgpt-test-key")
    monkeypatch.setattr(app_mod, "FASTGPT_APP_ID", "app-test-id")
    monkeypatch.setattr(app_mod, "FASTGPT_BASE_URL", "https://fake.fastgpt.cn")
    monkeypatch.setattr(app_mod, "fastgpt_configured", lambda: True)


def _post_chat(client, payload):
    return client.post("/api/chat", data=json.dumps(payload, ensure_ascii=False),
                       content_type="application/json")


def test_chat_success_reply(client, success_task, configured, monkeypatch):
    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return FakeResp(200, _reply_data("金额差异通常是汇率差或手续费导致的。"))

    monkeypatch.setattr(app_mod.requests, "post", fake_post)
    r = _post_chat(client, {"task_id": success_task, "message": "金额差异怎么处理？"})
    assert r.status_code == 200
    data = r.get_json()
    assert data["reply"].startswith("金额差异")
    assert captured["url"] == "https://fake.fastgpt.cn/api/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer fastgpt-test-key"
    payload = captured["json"]
    assert payload["appId"] == "app-test-id"
    assert payload["chatId"] == success_task
    assert payload["stream"] is False
    msgs = payload["messages"]
    assert all(m["role"] in ("user", "assistant") for m in msgs)
    assert msgs[-1]["role"] == "user"
    assert "金额差异怎么处理？" in msgs[-1]["content"]
    assert "【系统注入·本批对账差异上下文" in msgs[-1]["content"]


def test_chat_context_sanitized(client, success_task, configured, monkeypatch):
    captured = {}

    def fake_post(url, **kwargs):
        captured.update(kwargs)
        return FakeResp(200, _reply_data())

    monkeypatch.setattr(app_mod.requests, "post", fake_post)
    r = _post_chat(client, {"task_id": success_task, "message": "第一条差异是什么？"})
    assert r.status_code == 200
    last_msg = captured["json"]["messages"][-1]["content"]
    assert "AMZ-****-007" in last_msg
    assert "AMZ-20260904-007" not in last_msg
    assert "22.54" not in last_msg
    assert "[20,30)" in last_msg
    assert "不要自行计算" in last_msg


def test_chat_upstream_500(client, success_task, configured, monkeypatch):
    monkeypatch.setattr(
        app_mod.requests, "post",
        lambda url, **kw: FakeResp(500, text="internal error"))
    r = _post_chat(client, {"task_id": success_task, "message": "在吗"})
    assert r.status_code == 200
    assert r.get_json()["error"] == "AI服务暂不可用"


def test_chat_timeout(client, success_task, configured, monkeypatch):
    def fake_post(url, **kwargs):
        raise requests.Timeout("connect timeout")

    monkeypatch.setattr(app_mod.requests, "post", fake_post)
    r = _post_chat(client, {"task_id": success_task, "message": "在吗"})
    assert r.status_code == 200
    assert r.get_json()["error"] == "AI服务暂不可用"


def test_chat_upstream_empty_reply(client, success_task, configured, monkeypatch):
    monkeypatch.setattr(
        app_mod.requests, "post",
        lambda url, **kw: FakeResp(200, {"choices": []}))
    r = _post_chat(client, {"task_id": success_task, "message": "在吗"})
    assert r.status_code == 200
    assert r.get_json()["error"] == "AI服务暂不可用"


def test_chat_unconfigured(client, success_task, monkeypatch):
    def fake_post(url, **kwargs):
        raise AssertionError("未配置 FastGPT 时不应出网")

    monkeypatch.setattr(app_mod.requests, "post", fake_post)
    r = _post_chat(client, {"task_id": success_task, "message": "在吗"})
    assert r.status_code == 200
    assert r.get_json()["error"] == "AI服务暂不可用"


def test_chat_missing_fields(client):
    assert _post_chat(client, {}).status_code == 400
    assert _post_chat(client, {"task_id": "x"}).status_code == 400
    assert _post_chat(client, {"message": "hi"}).status_code == 400


def test_chat_unknown_task(client, configured):
    r = _post_chat(client, {"task_id": "task_19990101_000000_zzzz",
                            "message": "hi"})
    assert r.status_code == 404


def test_chat_history_capped(client, success_task, configured, monkeypatch):
    captured = {}

    def fake_post(url, **kwargs):
        captured.update(kwargs)
        return FakeResp(200, _reply_data())

    monkeypatch.setattr(app_mod.requests, "post", fake_post)
    for i in range(10):
        r = _post_chat(client, {"task_id": success_task, "message": "问题{}".format(i)})
        assert r.status_code == 200
        msgs = captured["json"]["messages"]
        history = msgs[:-1]
        assert len(history) <= app_mod.CHAT_HISTORY_MAX_MSGS
    # 近 6 轮 = 12 条；最后一条应含最新问题
    assert "问题9" in captured["json"]["messages"][-1]["content"]


def test_fastgpt_url_forms(monkeypatch):
    monkeypatch.setattr(app_mod, "FASTGPT_BASE_URL", "https://x.cn")
    assert app_mod._fastgpt_chat_url() == "https://x.cn/api/v1/chat/completions"
    monkeypatch.setattr(app_mod, "FASTGPT_BASE_URL", "https://x.cn/api/")
    assert app_mod._fastgpt_chat_url() == "https://x.cn/api/v1/chat/completions"


def test_build_diff_context_empty_meta():
    from engine.desensitize import build_diff_context
    text = build_diff_context({"stats": {}, "diff_summary": []})
    assert "【本批对账统计】" in text


def test_build_diff_context_buckets_net_amount():
    from engine.desensitize import build_diff_context
    meta = {
        "stats": {"total_orders": 8, "settled_count": 7,
                  "received_count": 3, "diff_count": 1},
        "diff_summary": ["部分到账：1笔"],
        "order_settle_diff": [],
        "settle_pay_diff": [{
            "settlement_id": "AMZ-ST-0906", "platform": "AMZ",
            "收款渠道": "连连国际", "结算总额": 125.0,
            "到账总额": 103.4, "净到账额": 102.2,
            "对账状态": "部分到账", "AI业务备注": "",
        }],
    }
    text = build_diff_context(meta)
    assert "102.2" not in text
    assert "净到账额=[100,110)" in text


def test_extract_reply_plain_string():
    assert app_mod._extract_reply({"content": "你好"}) == "你好"


def test_extract_reply_agent_v2_blocks():
    blocks = [
        {"type": "reasoning", "reasoning": {"content": "内部思考"}},
        {"type": "text", "agentAsk": {
            "params": '{"description": "需要数据路径", "questions": '
                      '[{"question": "路径是什么？"}]}'}},
        {"type": "interactive", "interactive": {
            "type": "agentAsk",
            "params": {"questions": [{"question": "备用问题？"}]}}},
    ]
    text = app_mod._extract_reply({"content": blocks})
    assert "需要数据路径" in text
    assert "路径是什么？" in text
    assert "备用问题？" in text
    assert "内部思考" not in text


def test_extract_reply_reasoning_fallback():
    text = app_mod._extract_reply(
        {"content": "", "reasoning_content": "先分析差异构成…"})
    assert text == "先分析差异构成…"


def test_extract_reply_empty():
    assert app_mod._extract_reply({"content": ""}) == ""
    assert app_mod._extract_reply(None) == ""


def test_chat_structured_content(client, success_task, configured, monkeypatch):
    blocks = [
        {"type": "reasoning", "reasoning": {"content": "思考"}},
        {"type": "text", "agentAsk": {"params": {
            "description": "这是提问描述",
            "questions": [{"question": "需要补充什么？"}]}}},
    ]

    def fake_post(url, **kwargs):
        return FakeResp(200, {"choices": [{"message": {"content": blocks}}]})

    monkeypatch.setattr(app_mod.requests, "post", fake_post)
    r = _post_chat(client, {"task_id": success_task, "message": "在吗"})
    assert r.status_code == 200
    reply = r.get_json()["reply"]
    assert "需要补充什么？" in reply
