"""M2-B0 批 B：technical/compare_peers 证据化 + DCF 派生证据（op="model"）。"""

import json
from datetime import UTC, datetime

import pandas as pd
import pytest

from bellwether.agent import tools as tools_mod
from bellwether.core.capture import CaptureStore
from bellwether.core.context import AnalysisContext, FrozenClock
from bellwether.ir.recorder import ToolRecorder
from bellwether.ir.store import EvidenceStore
from bellwether.ir.verify import verify_provenance
from bellwether.models import FundamentalData, TradingRules

AS_OF = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)


def _synthetic_closes(n: int) -> list[float]:
    """确定性锯齿上升收盘价：足够指标预热窗口，且非单调（RSI/MACD 非退化）。"""
    out = []
    price = 100.0
    for i in range(n):
        price += 1.2 if i % 3 else -0.6
        out.append(round(price, 2))
    return out


def _ohlcv_df(closes: list[float]) -> pd.DataFrame:
    n = len(closes)
    df = pd.DataFrame(
        {
            "open": [c - 0.1 for c in closes],
            "high": [c + 0.2 for c in closes],
            "low": [c - 0.2 for c in closes],
            "close": closes,
            "volume": [1000.0 + i for i in range(n)],
        },
        index=pd.date_range("2026-04-01", periods=n, freq="B"),
    )
    df.attrs["upstream_source"] = "sina"
    df.attrs["captured_at"] = AS_OF.isoformat()
    return df


class FakeProviderB:
    """技术指标（≥30 根 K 线）与 DCF（含 fcf/shares/debt/cash）可算的替身。"""

    market = "CN"
    source = "akshare"

    def resolve_symbol(self, query, *, context):
        return query.strip().upper()

    def get_ohlcv(self, symbol, start, end, interval="1d", adjust="default", *, context):
        return _ohlcv_df(_synthetic_closes(60))

    def get_fundamentals(self, symbol, *, context):
        if symbol == "BADPEER":
            raise ValueError("no data for BADPEER")
        return FundamentalData(
            symbol=symbol,
            currency="CNY",
            pe=25.5,
            pb=3.2,
            roe=0.18,
            market_cap=1.2e12,
            free_cashflow=5.0e10,
            shares_outstanding=1.256e10,
            total_debt=2.0e10,
            total_cash=3.0e10,
            fetched_at=AS_OF,
            source=self.source,
        )

    def get_news(self, symbol, limit=20, *, context):
        return []

    def trading_rules(self):
        return TradingRules(
            market="CN", timezone="Asia/Shanghai", has_price_limit=True, settlement="T+1"
        )


@pytest.fixture()
def recorder(tmp_path):
    context = AnalysisContext(as_of=AS_OF, capture_policy="live", clock=FrozenClock(AS_OF))
    rec = ToolRecorder(
        context=context, evidence=EvidenceStore("600519"), captures=CaptureStore(tmp_path)
    )
    rec.current_tool_call_id = "tc_1"
    return rec


def _evidence_dict(rec):
    return {b.eid: rec.evidence.get(b.eid) for b in rec.bindings}


# ─────────────────────────── get_technical_analysis ───────────────────────────
_INDICATOR_KEYS = (
    "last_close",
    "MA20",
    "MA50",
    "RSI14",
    "MACD",
    "MACD_signal",
    "MACD_hist",
    "BOLL_mid",
    "BOLL_upper",
    "BOLL_lower",
)


def test_technical_indicators_registered_and_verified(recorder):
    out = json.loads(
        tools_mod.execute_tool(
            "get_technical_analysis",
            {"symbol": "600519"},
            FakeProviderB(),
            context=recorder.context,
            trace=recorder,
        )
    )
    indicators = out["indicators"]
    for key in _INDICATOR_KEYS:
        assert isinstance(indicators[key], dict) and "eid" in indicators[key], key
    # last_close 顶层字段与 indicators["last_close"] 引用同一条证据
    assert out["last_close"] == indicators["last_close"]
    ev = recorder.evidence.get(indicators["MA20"]["eid"])
    assert ev.kind == "series_stat" and ev.price_basis == "qfq" and ev.anchor_date == AS_OF.date()
    # R7/R8 全链核验通过（抽取器重算与 TechnicalModule 计算完全一致）
    assert (
        verify_provenance(
            _evidence_dict(recorder), recorder.bindings, recorder.captures, recorder.tool_call_ids
        )
        == []
    )


def test_technical_analysis_without_trace_keeps_m1_shape(recorder):
    out = json.loads(
        tools_mod.execute_tool(
            "get_technical_analysis",
            {"symbol": "600519"},
            FakeProviderB(),
            context=recorder.context,
        )
    )
    assert isinstance(out["last_close"], float)  # trace=None：M1 裸值形态不变
    assert isinstance(out["indicators"]["MA20"], float)
    assert recorder.bindings == []


def test_technical_short_window_skips_unregistrable_indicators(recorder):
    class ShortProvider(FakeProviderB):
        def get_ohlcv(self, symbol, start, end, interval="1d", adjust="default", *, context):
            return _ohlcv_df(_synthetic_closes(10))  # 不足 MA20/MA50/RSI14/BOLL 预热窗口

    out = json.loads(
        tools_mod.execute_tool(
            "get_technical_analysis",
            {"symbol": "600519"},
            ShortProvider(),
            context=recorder.context,
            trace=recorder,
        )
    )
    assert out["indicators"]["MA20"] is None  # 窗口不足：register_value 返回 None，跳过不造值
    assert out["indicators"]["MA50"] is None
    assert out["indicators"]["RSI14"] is None
    assert isinstance(out["indicators"]["last_close"], dict)  # 不受窗口影响，正常注册
    assert (
        verify_provenance(
            _evidence_dict(recorder), recorder.bindings, recorder.captures, recorder.tool_call_ids
        )
        == []
    )


# ─────────────────────────── compare_peers ───────────────────────────
def test_compare_peers_registers_independent_captures(recorder):
    out = json.loads(
        tools_mod.execute_tool(
            "compare_peers",
            {"symbol": "600519", "peers": ["600000", "BADPEER"]},
            FakeProviderB(),
            context=recorder.context,
            trace=recorder,
        )
    )
    comparison = out["comparison"]
    for sym in ("600519", "600000"):
        row = comparison[sym]
        for key in ("PE", "PB", "ROE", "market_cap"):
            assert isinstance(row[key], dict) and "eid" in row[key], (sym, key)
    assert comparison["600519"]["ROE"]["v"] == 0.18  # 现状口径：注册原始小数，非百分比
    assert "error" in comparison["BADPEER"]  # 失败分支结构不变
    # 每个 peer 独立捕获（独立 capture_id），失败的 peer 未捕获
    capture_ids = {b.capture_id for b in recorder.bindings}
    assert len(capture_ids) == 2
    # 同一次 tool 调用内所有捕获共享同一 tool_call_id
    assert recorder.tool_call_ids == {"tc_1"}
    assert (
        verify_provenance(
            _evidence_dict(recorder), recorder.bindings, recorder.captures, recorder.tool_call_ids
        )
        == []
    )


def test_compare_peers_without_trace_keeps_m1_shape(recorder):
    out = json.loads(
        tools_mod.execute_tool(
            "compare_peers",
            {"symbol": "600519", "peers": ["600000"]},
            FakeProviderB(),
            context=recorder.context,
        )
    )
    assert isinstance(out["comparison"]["600519"]["PE"], float)
    assert recorder.bindings == []


# ─────────────────────────── DCF 派生证据 ───────────────────────────
def test_dcf_derived_evidence(recorder):
    out = json.loads(
        tools_mod.execute_tool(
            "get_fundamentals",
            {"symbol": "600519"},
            FakeProviderB(),
            context=recorder.context,
            trace=recorder,
        )
    )
    assert "eid" in out["dcf_fair_value"]
    dcf_ev = recorder.evidence.get(out["dcf_fair_value"]["eid"])
    assert dcf_ev.source is None
    assert dcf_ev.confidence == "derived"
    assert dcf_ev.derivation is not None
    assert dcf_ev.derivation.op == "model"
    assert dcf_ev.derivation.formula == "simple_dcf"
    assert len(dcf_ev.derivation.inputs) >= 1
    for eid in dcf_ev.derivation.inputs:
        assert recorder.evidence.get(eid) is not None  # 输入证据均已在册
    params = dcf_ev.derivation.params
    assert params["growth_rate"] == pytest.approx(0.08)
    assert params["discount_rate"] == pytest.approx(0.09)
    assert params["net_debt"] == pytest.approx(2.0e10 - 3.0e10)
    # derived 不进 bindings 表（bindings 只装 source 类，R7/R8 核验对象）
    assert dcf_ev.eid not in {b.eid for b in recorder.bindings}
    # 但仍是全会话闭包的一部分：verify_provenance 对 derived 条目安全跳过（source is None）
    evidence_all = _evidence_dict(recorder)
    evidence_all[dcf_ev.eid] = dcf_ev
    assert (
        verify_provenance(
            evidence_all, recorder.bindings, recorder.captures, recorder.tool_call_ids
        )
        == []
    )


def test_dcf_derived_evidence_skips_missing_debt_cash_fields(recorder):
    class NoDebtCashProvider(FakeProviderB):
        def get_fundamentals(self, symbol, *, context):
            return FundamentalData(
                symbol=symbol,
                currency="CNY",
                pe=25.5,
                free_cashflow=5.0e10,
                shares_outstanding=1.256e10,
                total_debt=None,
                total_cash=None,
                fetched_at=AS_OF,
                source=self.source,
            )

    out = json.loads(
        tools_mod.execute_tool(
            "get_fundamentals",
            {"symbol": "600519"},
            NoDebtCashProvider(),
            context=recorder.context,
            trace=recorder,
        )
    )
    dcf_ev = recorder.evidence.get(out["dcf_fair_value"]["eid"])
    assert len(dcf_ev.derivation.inputs) == 2  # 仅 free_cashflow + shares_outstanding 可注册
    assert dcf_ev.derivation.params["net_debt"] == 0.0  # total_debt/total_cash 均缺失，净负债取 0


def test_fundamentals_without_trace_keeps_dcf_raw_value(recorder):
    out = json.loads(
        tools_mod.execute_tool(
            "get_fundamentals", {"symbol": "600519"}, FakeProviderB(), context=recorder.context
        )
    )
    assert isinstance(out["dcf_fair_value"], float)  # trace=None：M1 裸值形态不变
    assert recorder.bindings == []
