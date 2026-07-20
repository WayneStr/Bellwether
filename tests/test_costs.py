"""CostLedger 单测（M2-D3）：token/成本记账、价目覆盖、未知模型诚实计零、硬预算前置检查。"""

import pytest

from bellwether.core.costs import PRICE_BOOK_VERSION, CostLedger
from bellwether.core.exceptions import BudgetExceededError

# ─────────────────────────── CostLedger 记账 ───────────────────────────


def test_record_llm_accumulates_known_model_cost():
    ledger = CostLedger()
    cost = ledger.record_llm("claude-sonnet-5", 1_000_000, 1_000_000, latency_s=1.2)
    assert cost == pytest.approx(18.0)  # 内置价目：input $3 + output $15 每百万 token
    assert ledger.total_usd == pytest.approx(18.0)
    assert ledger.total_input_tokens == 1_000_000
    assert ledger.total_output_tokens == 1_000_000
    assert ledger.calls == 1


def test_record_llm_accumulates_across_multiple_calls():
    ledger = CostLedger()
    ledger.record_llm("claude-haiku-4-5-20251001", 500_000, 500_000)
    ledger.record_llm("claude-haiku-4-5-20251001", 500_000, 500_000)
    assert ledger.calls == 2
    assert ledger.total_input_tokens == 1_000_000
    assert ledger.total_output_tokens == 1_000_000
    assert ledger.total_usd > 0


# ─────────────────────────── 价目覆盖 ───────────────────────────


def test_overrides_replace_builtin_price():
    ledger = CostLedger({"claude-sonnet-5": {"input_per_mtok": 1.0, "output_per_mtok": 2.0}})
    cost = ledger.record_llm("claude-sonnet-5", 1_000_000, 1_000_000)
    assert cost == pytest.approx(3.0)
    assert ledger.total_usd == pytest.approx(3.0)


# ─────────────────────────── 未知模型：诚实计零 ───────────────────────────


def test_unknown_model_costs_zero_and_is_recorded_honestly():
    ledger = CostLedger()
    cost = ledger.record_llm("mystery-model", 1000, 1000)
    assert cost == 0.0
    assert ledger.total_usd == 0.0
    assert ledger.calls == 1
    assert ledger.total_input_tokens == 1000  # token 用量仍如实累计，只是不计价
    assert "mystery-model" in ledger.unknown_models


def test_summary_contains_price_book_version_and_unknown_models():
    ledger = CostLedger()
    ledger.record_llm("claude-sonnet-5", 100, 100)
    ledger.record_llm("mystery-model", 100, 100)
    summary = ledger.summary()
    assert summary["price_book_version"] == PRICE_BOOK_VERSION
    assert summary["unknown_models"] == ["mystery-model"]
    assert summary["calls"] == 2
    assert summary["total_input_tokens"] == 200
    assert summary["total_output_tokens"] == 200
    assert summary["total_usd"] == ledger.total_usd


# ─────────────────────────── 硬预算前置检查（orchestrator 织入） ───────────────────────────


class _Usage:
    def __init__(self, input_tokens: int, output_tokens: int):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _tool_use_msg():
    msg = _Block(stop_reason="tool_use", usage=_Usage(1, 1))
    msg.content = [
        _Block(type="tool_use", name="get_price_history", input={"symbol": "AAPL"}, id="tu_1")
    ]
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


def test_budget_exceeded_raises_before_next_llm_call(tmp_path, monkeypatch, ctx):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr("bellwether.core.trace.DEFAULT_TRACE_ROOT", tmp_path)
    monkeypatch.setattr("bellwether.agent.orchestrator.DEFAULT_CAPTURE_ROOT", tmp_path / "cap")
    # tool 执行 mock 掉：这里测预算前置检查，不测 tool 本身（不打网）
    monkeypatch.setattr(
        "bellwether.agent.tools.execute_tool",
        lambda name, tool_input, provider, *, context, trace=None: '{"ok": true}',
    )
    from bellwether.agent.llm import ResilientLLM
    from bellwether.agent.orchestrator import Orchestrator
    from bellwether.config import AppConfig

    config = AppConfig()
    # 把首轮折算金额推到远超 quick 档默认上限 $0.35（ADR-0004），
    # 使第二轮 llm.create 之前的预算前置检查必然命中。
    config.pricing = {
        "claude-sonnet-5": {"input_per_mtok": 1_000_000.0, "output_per_mtok": 1_000_000.0}
    }
    orch = Orchestrator(config)
    fake = FakeClient([_tool_use_msg(), _tool_use_msg()])
    orch.llm = ResilientLLM(fake)

    with pytest.raises(BudgetExceededError):
        orch.analyze("AAPL", context=ctx)

    assert fake.calls == ["claude-sonnet-5"]  # 预算前置检查挡住了第二轮 create

    data = orch.last_trace_path.read_text(encoding="utf-8")
    assert "error:BudgetExceededError" in data
