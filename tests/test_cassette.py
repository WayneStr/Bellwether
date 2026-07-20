"""C2a cassette 层测试（M2-C2a）：键确定性、录制→重放端到端、未命中/未完成失败、
回放确定性（spec-002 §5.1）、ir/recorder.py 的 capture_policy 分支。"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pandas as pd
import pytest

from bellwether.agent import tools as tools_mod
from bellwether.agent.tools import _df_records
from bellwether.core.capture import CaptureStore, canonical_json_bytes
from bellwether.core.context import AnalysisContext, FrozenClock
from bellwether.core.exceptions import ConfigError, DataUnavailableError
from bellwether.data.base import period_to_start
from bellwether.data.cassette import (
    CassetteProvider,
    CassetteRecorder,
    cassette_key,
    fundamentals_args,
    news_args,
    ohlcv_args,
)
from bellwether.ir.recorder import ToolRecorder
from bellwether.ir.store import EvidenceStore
from bellwether.models import FundamentalData, NewsItem, TradingRules

AS_OF = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
SYMBOL = "600519"
START = date(2026, 1, 19)
END = date(2026, 7, 18)


class FakeProvider:
    """三方法可控替身（仿 tests/test_tool_evidence.py），用于喂 CassetteRecorder。"""

    market = "CN"
    source = "akshare"

    def resolve_symbol(self, query, *, context):
        return query.strip().upper()

    def get_ohlcv(self, symbol, start, end, interval="1d", adjust="default", *, context):
        return pd.DataFrame(
            {
                "open": [10.0, 11.0],
                "high": [11.5, 12.5],
                "low": [9.5, 10.5],
                "close": [11.0, 12.0],
                "volume": [100.0, 200.0],
            },
            index=pd.to_datetime(["2026-07-16", "2026-07-17"]),
        )

    def get_fundamentals(self, symbol, *, context):
        return FundamentalData(
            symbol=symbol,
            currency="CNY",
            pe=25.5,
            roe=0.4786,
            fetched_at=AS_OF,
            source=self.source,
        )

    def get_news(self, symbol, limit=20, *, context):
        return [
            NewsItem(title="公司发布年报", url="http://x", published_at=None, summary="……"),
            NewsItem(title="行业政策更新", url="http://y", published_at=None, summary=None),
        ]

    def trading_rules(self):
        return TradingRules(
            market="CN", timezone="Asia/Shanghai", has_price_limit=True, settlement="T+1"
        )


@pytest.fixture()
def context() -> AnalysisContext:
    return AnalysisContext(as_of=AS_OF, capture_policy="live", clock=FrozenClock(AS_OF))


def _record_all(
    recorder: CassetteRecorder, provider: FakeProvider, context: AnalysisContext
) -> None:
    """仿 cli.py cassette-record 命令的录制逻辑：三类数据落 cassette。"""
    sym = provider.resolve_symbol(SYMBOL, context=context)
    df = provider.get_ohlcv(sym, START, END, context=context)
    recorder.record(
        provider.source, "get_ohlcv", ohlcv_args(sym, START, END), {"records": _df_records(df)}
    )
    fund = provider.get_fundamentals(sym, context=context)
    recorder.record(
        provider.source, "get_fundamentals", fundamentals_args(sym), fund.model_dump(mode="json")
    )
    news = provider.get_news(sym, 10, context=context)
    recorder.record(
        provider.source,
        "get_news",
        news_args(sym, 10),
        {"items": [n.model_dump(mode="json") for n in news]},
    )


# ─────────────────────────── cassette_key 确定性 ───────────────────────────
def test_cassette_key_order_independent():
    args_a = {"symbol": "AAPL", "start": "2026-01-01", "end": "2026-07-01"}
    args_b = {"end": "2026-07-01", "start": "2026-01-01", "symbol": "AAPL"}
    assert cassette_key("yfinance", "get_ohlcv", args_a) == cassette_key(
        "yfinance", "get_ohlcv", args_b
    )


def test_cassette_key_distinct_for_different_args():
    common = {"symbol": "AAPL", "start": "2026-01-01"}
    base = cassette_key("yfinance", "get_ohlcv", common)
    other_symbol = cassette_key("yfinance", "get_ohlcv", {**common, "symbol": "MSFT"})
    other_method = cassette_key("yfinance", "get_fundamentals", common)
    other_provider = cassette_key("akshare", "get_ohlcv", common)
    assert len({base, other_symbol, other_method, other_provider}) == 4


# ─────────────────────────── recorder → finalize → provider 端到端 ───────────────────────────
def test_recorder_finalize_provider_round_trip(tmp_path, context):
    root = tmp_path / "cassette"
    provider = FakeProvider()
    recorder = CassetteRecorder(root)
    _record_all(recorder, provider, context)
    recorder.finalize(AS_OF)

    replay = CassetteProvider(root, market="CN", inner_source_name=provider.source)
    assert replay.source == "cassette:akshare"

    df = replay.get_ohlcv(SYMBOL, START, END, context=context)
    original = provider.get_ohlcv(SYMBOL, START, END, context=context)
    assert list(df.index.strftime("%Y-%m-%d")) == list(original.index.strftime("%Y-%m-%d"))
    for col in ("open", "high", "low", "close", "volume"):
        assert df[col].tolist() == original[col].tolist()

    fund = replay.get_fundamentals(SYMBOL, context=context)
    original_fund = provider.get_fundamentals(SYMBOL, context=context)
    assert fund.pe == original_fund.pe
    assert fund.roe == original_fund.roe
    assert fund.currency == original_fund.currency
    assert fund.fetched_at == original_fund.fetched_at

    news = replay.get_news(SYMBOL, 10, context=context)
    original_news = provider.get_news(SYMBOL, 10, context=context)
    assert [n.title for n in news] == [n.title for n in original_news]
    assert [n.url for n in news] == [n.url for n in original_news]
    assert [n.summary for n in news] == [n.summary for n in original_news]


# ─────────────────────────── 失败路径 ───────────────────────────
def test_provider_miss_raises_data_unavailable(tmp_path, context):
    root = tmp_path / "cassette"
    recorder = CassetteRecorder(root)
    _record_all(recorder, FakeProvider(), context)
    recorder.finalize(AS_OF)

    replay = CassetteProvider(root, market="CN", inner_source_name="akshare")
    with pytest.raises(DataUnavailableError):
        replay.get_ohlcv("000001", START, END, context=context)  # 未录制的标的


def test_provider_without_complete_marker_raises_config_error(tmp_path, context):
    root = tmp_path / "cassette"
    recorder = CassetteRecorder(root)
    _record_all(recorder, FakeProvider(), context)
    # 故意不调用 finalize()：没有 _COMPLETE 标记
    with pytest.raises(ConfigError):
        CassetteProvider(root, market="CN", inner_source_name="akshare")


# ─────────────────────────── 回放确定性（核心验收，spec-002 §5.1）───────────────────────────
def test_replay_determinism_across_two_runs(tmp_path):
    cassette_root = tmp_path / "cassette"
    record_context = AnalysisContext(as_of=AS_OF, capture_policy="live", clock=FrozenClock(AS_OF))
    provider = FakeProvider()
    recorder = CassetteRecorder(cassette_root)

    sym = provider.resolve_symbol(SYMBOL, context=record_context)
    end = record_context.as_of.date()
    start = period_to_start("6mo", end)
    df = provider.get_ohlcv(sym, start, end, context=record_context)
    recorder.record(
        provider.source, "get_ohlcv", ohlcv_args(sym, start, end), {"records": _df_records(df)}
    )
    recorder.finalize(AS_OF)

    def _run(idx: int) -> ToolRecorder:
        context = AnalysisContext(as_of=AS_OF, capture_policy="cassette", clock=FrozenClock(AS_OF))
        replay_provider = CassetteProvider(
            cassette_root, market="CN", inner_source_name=provider.source
        )
        rec = ToolRecorder(
            context=context,
            evidence=EvidenceStore(SYMBOL),
            captures=CaptureStore(tmp_path / f"captures-{idx}"),
        )
        rec.current_tool_call_id = "tc_1"
        tools_mod.execute_tool(
            "get_price_history", {"symbol": SYMBOL}, replay_provider, context=context, trace=rec
        )
        return rec

    rec1 = _run(1)
    rec2 = _run(2)

    eids1 = [b.eid for b in rec1.bindings]
    eids2 = [b.eid for b in rec2.bindings]
    assert eids1 and eids1 == eids2  # eid 序列一致（非空）

    for eid in eids1:
        ev1 = rec1.evidence.get(eid)
        ev2 = rec2.evidence.get(eid)
        assert ev1.fingerprint == ev2.fingerprint
        assert ev1.pit_class == "replay" and ev2.pit_class == "replay"
        assert ev1.available_at is None and ev2.available_at is None
        assert ev1.source is not None and ev2.source is not None
        # canonical_request 逐位一致（规范化字节比较，而非仅 dict 值相等）
        assert canonical_json_bytes(ev1.source.canonical_request) == canonical_json_bytes(
            ev2.source.canonical_request
        )


# ─────────────────────────── recorder pit 分支单测 ───────────────────────────
def _seed_capture(rec: ToolRecorder):
    fake_df = FakeProvider().get_ohlcv(SYMBOL, START, END, context=None)
    return rec.capture(
        provider_id="cassette:akshare",
        tool_name="get_price_history",
        method="get_ohlcv",
        canonical_args={"symbol": SYMBOL, "start": str(START), "end": str(END)},
        payload={"records": _df_records(fake_df)},
        data_type="ohlcv",
    )


def _register_last_close(rec: ToolRecorder, captured):
    return rec.register_value(
        captured,
        metric_name="last_close",
        extractor_id="ohlcv.last_close",
        price_basis="unadjusted",
    )


def test_register_value_cassette_policy_sets_replay_and_none_times(tmp_path):
    context = AnalysisContext(as_of=AS_OF, capture_policy="cassette", clock=FrozenClock(AS_OF))
    rec = ToolRecorder(
        context=context, evidence=EvidenceStore(SYMBOL), captures=CaptureStore(tmp_path)
    )
    rec.current_tool_call_id = "tc_1"
    captured = _seed_capture(rec)
    ev = _register_last_close(rec, captured)
    assert ev is not None
    assert ev.pit_class == "replay"
    assert ev.first_seen_at is None
    assert ev.available_at is None
    assert ev.confidence == "reported"


def test_register_value_live_policy_stays_observed(tmp_path):
    context = AnalysisContext(as_of=AS_OF, capture_policy="live", clock=FrozenClock(AS_OF))
    rec = ToolRecorder(
        context=context, evidence=EvidenceStore(SYMBOL), captures=CaptureStore(tmp_path)
    )
    rec.current_tool_call_id = "tc_1"
    captured = _seed_capture(rec)
    ev = _register_last_close(rec, captured)
    assert ev is not None
    assert ev.pit_class == "observed"
    assert ev.first_seen_at == captured.captured_at
    assert ev.available_at == captured.captured_at
    assert ev.confidence == "reported"
