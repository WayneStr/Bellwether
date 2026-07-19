"""E4 最小 trace 单测：provenance 包完整性、脱敏落盘、orchestrator 织入。"""

import json

import anthropic
import httpx
import pytest

from bellwether.core.trace import (
    ToolCallRecord,
    new_trace,
    prompt_version,
    write_trace,
)

# ─────────────────────────── 纯函数 ───────────────────────────


def test_prompt_version_is_stable_content_hash():
    assert prompt_version("你是分析师") == prompt_version("你是分析师")
    assert prompt_version("你是分析师") != prompt_version("你是分析师 v2")
    assert len(prompt_version("x")) == 12


def test_input_hash_deterministic_and_sensitive(ctx):
    a1 = new_trace("AAPL", True, ["m1", "m2"], "pv", ctx)
    a2 = new_trace("AAPL", True, ["m1", "m2"], "pv", ctx)
    b = new_trace("AAPL", False, ["m1", "m2"], "pv", ctx)
    assert a1.input_hash == a2.input_hash  # 同输入同哈希（trace_id 各不同）
    assert a1.input_hash != b.input_hash
    assert a1.trace_id != a2.trace_id


# ─────────────────────────── 落盘 ───────────────────────────


def test_write_trace_persists_and_redacts(tmp_path, monkeypatch, ctx):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    trace = new_trace("AAPL", False, ["m1"], "pv", ctx)
    trace.tool_calls.append(
        ToolCallRecord(
            name="get_news",
            input={"symbol": "AAPL"},
            output='{"error": "invalid x-api-key sk-ant-leaked-1234567890"}',
        )
    )
    path = write_trace(trace, tmp_path)
    assert path is not None and path.exists()
    text = path.read_text(encoding="utf-8")
    assert "sk-ant-leaked-1234567890" not in text  # D6 联动：落盘零泄漏
    data = json.loads(text)
    assert data["symbol"] == "AAPL"
    assert data["tool_calls"][0]["name"] == "get_news"
    assert data["snapshot_ref"] is None  # M1 恒 None，M2 证据层填充


def test_write_trace_failure_is_silent(tmp_path, ctx):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("occupied")  # 目录位置被文件占住 → mkdir 失败
    trace = new_trace("AAPL", False, ["m1"], "pv", ctx)
    assert write_trace(trace, blocker) is None  # 旁路失败不抛


# ─────────────────────────── orchestrator 织入 ───────────────────────────


def _api_error(cls, status):
    request = httpx.Request("POST", "https://api.test/v1/messages")
    return cls(f"HTTP {status}", response=httpx.Response(status, request=request), body=None)


class _Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _tool_use_msg():
    msg = _Block(stop_reason="tool_use")
    msg.content = [
        _Block(type="tool_use", name="get_price_history", input={"symbol": "AAPL"}, id="tu_1")
    ]
    return msg


def _end_turn_msg(text="研判"):
    msg = _Block(stop_reason="end_turn")
    msg.content = [_Block(type="text", text=text)]
    return msg


class FakeClient:
    def __init__(self, script):
        self._script = list(script)
        self.calls: list[str] = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs["model"])
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture()
def orch(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr("bellwether.core.trace.DEFAULT_TRACE_ROOT", tmp_path)
    # tool 执行 mock 掉：这里测 trace 织入，不测 tool 本身（不打网）
    monkeypatch.setattr(
        "bellwether.agent.tools.execute_tool",
        lambda name, tool_input, provider, *, context: '{"ok": true}',
    )
    from bellwether.agent.orchestrator import Orchestrator
    from bellwether.config import AppConfig

    return Orchestrator(AppConfig())


def _read_trace(orch):
    assert orch.last_trace_path is not None and orch.last_trace_path.exists()
    return json.loads(orch.last_trace_path.read_text(encoding="utf-8"))


def test_trace_records_full_analysis(orch, ctx):
    from bellwether.agent.llm import ResilientLLM

    orch.llm = ResilientLLM(FakeClient([_tool_use_msg(), _end_turn_msg()]))
    orch.analyze("AAPL", context=ctx)

    data = _read_trace(orch)
    assert data["outcome"] == "ok"
    assert data["symbol"] == "AAPL" and data["deep"] is False
    assert len(data["llm_calls"]) == 2
    assert data["llm_calls"][0]["stop_reason"] == "tool_use"
    assert data["tool_calls"] == [
        {"name": "get_price_history", "input": {"symbol": "AAPL"}, "output": '{"ok": true}'}
    ]
    assert data["degraded"] is False
    assert data["prompt_version"] and data["input_hash"]
    assert len(data["model_chain"]) == 2  # 非 deep 走 synthesis 角色：synthesis→parse 两档


def test_trace_marks_degradation(orch, ctx):
    from bellwether.agent.llm import ResilientLLM
    from bellwether.config import AppConfig

    orch.llm = ResilientLLM(FakeClient([_api_error(anthropic.NotFoundError, 404), _end_turn_msg()]))
    orch.analyze("AAPL", deep=True, context=ctx)

    data = _read_trace(orch)
    assert data["degraded"] is True
    assert data["final_model"] == AppConfig().models.synthesis.model
    assert data["outcome"] == "ok"


def test_trace_written_on_total_failure(orch, ctx):
    from bellwether.agent.llm import ResilientLLM
    from bellwether.core.exceptions import ModelNotFoundError

    orch.llm = ResilientLLM(
        FakeClient([_api_error(anthropic.NotFoundError, 404) for _ in range(3)])
    )
    with pytest.raises(ModelNotFoundError):
        orch.analyze("AAPL", context=ctx)

    data = _read_trace(orch)  # 失败也要落 trace（诊断依据）
    assert data["outcome"] == "error:ModelNotFoundError"
    assert data["llm_calls"] == []
