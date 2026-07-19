"""M2-B0 批 A 集成：tool 证据化全链（捕获→注册→{v,eid} 输出→R7/R8 核验）+ 篡改注入。"""

import json
from datetime import UTC, datetime

import pandas as pd
import pytest

from bellwether.agent import tools as tools_mod
from bellwether.core.capture import CaptureStore
from bellwether.core.context import AnalysisContext, FrozenClock
from bellwether.ir.recorder import ToolRecorder, ref
from bellwether.ir.store import EvidenceStore
from bellwether.ir.verify import verify_provenance
from bellwether.models import FundamentalData, NewsItem, TradingRules

AS_OF = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)


class FakeProvider:
    """三方法可控替身：OHLCV 带 attrs（模拟 provider 的 B8/B9 标注）。"""

    market = "CN"
    source = "akshare"

    def resolve_symbol(self, query, *, context):
        return query.strip().upper()

    def get_ohlcv(self, symbol, start, end, interval="1d", adjust="default", *, context):
        df = pd.DataFrame(
            {
                "open": [10.0, 11.0],
                "high": [11.5, 12.5],
                "low": [9.5, 10.5],
                "close": [11.0, 12.0],
                "volume": [100.0, 200.0],
            },
            index=pd.to_datetime(["2026-07-16", "2026-07-17"]),
        )
        df.attrs["upstream_source"] = "sina"
        df.attrs["captured_at"] = AS_OF.isoformat()
        return df

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
def recorder(tmp_path):
    context = AnalysisContext(as_of=AS_OF, capture_policy="live", clock=FrozenClock(AS_OF))
    rec = ToolRecorder(
        context=context, evidence=EvidenceStore("600519"), captures=CaptureStore(tmp_path)
    )
    rec.current_tool_call_id = "tc_1"
    return rec


def _evidence_dict(rec):
    return {b.eid: rec.evidence.get(b.eid) for b in rec.bindings}


# ─────────────────────────── price history ───────────────────────────
def test_price_history_registers_and_refs(recorder):
    out = json.loads(
        tools_mod.execute_tool(
            "get_price_history",
            {"symbol": "600519"},
            FakeProvider(),
            context=recorder.context,
            trace=recorder,
        )
    )
    assert out["last_close"] == {"v": 12.0, "eid": out["last_close"]["eid"]}
    assert out["period_return_pct"]["v"] == pytest.approx((12.0 / 11.0 - 1) * 100)
    assert out["bars_count"]["v"] == 2.0
    assert out["period_high"]["v"] == 12.5 and out["period_low"]["v"] == 9.5
    # 证据侧：CN 复权视图口径 + 锚点 + 降级子源全部落档
    ev = recorder.evidence.get(out["last_close"]["eid"])
    assert ev.price_basis == "qfq" and ev.anchor_date == AS_OF.date()
    assert ev.source.upstream_source == "sina"
    assert ev.confidence == "reported"  # captured_at == as_of，未超龄
    # R7/R8 全链核验通过
    assert (
        verify_provenance(
            _evidence_dict(recorder), recorder.bindings, recorder.captures, recorder.tool_call_ids
        )
        == []
    )


def test_price_history_without_trace_keeps_m1_shape(recorder):
    out = json.loads(
        tools_mod.execute_tool(
            "get_price_history", {"symbol": "600519"}, FakeProvider(), context=recorder.context
        )
    )
    assert isinstance(out["last_close"], float)  # trace=None：M1 裸值形态不变
    assert recorder.bindings == []


# ─────────────────────────── fundamentals ───────────────────────────
def test_fundamentals_registers_report_scale_values(recorder):
    out = json.loads(
        tools_mod.execute_tool(
            "get_fundamentals",
            {"symbol": "600519"},
            FakeProvider(),
            context=recorder.context,
            trace=recorder,
        )
    )
    assert out["metrics"]["PE"]["v"] == 25.5
    assert out["metrics"]["ROE(%)"]["v"] == 47.86  # 报告口径（百分比）注册，单位一致
    assert "PB" in out["metrics"] and out["metrics"]["PB"] is None  # 缺失字段不注册不造值
    roe_ev = recorder.evidence.get(out["metrics"]["ROE(%)"]["eid"])
    assert roe_ev.unit == "%"
    assert (
        verify_provenance(
            _evidence_dict(recorder), recorder.bindings, recorder.captures, recorder.tool_call_ids
        )
        == []
    )


# ─────────────────────────── news ───────────────────────────
def test_news_titles_registered_as_text_evidence(recorder):
    out = json.loads(
        tools_mod.execute_tool(
            "get_news",
            {"symbol": "600519"},
            FakeProvider(),
            context=recorder.context,
            trace=recorder,
        )
    )
    first = out["news"][0]["title"]
    assert first["v"] == "公司发布年报"
    ev = recorder.evidence.get(first["eid"])
    assert ev.kind == "news" and isinstance(ev.value, str)
    assert (
        verify_provenance(
            _evidence_dict(recorder), recorder.bindings, recorder.captures, recorder.tool_call_ids
        )
        == []
    )


# ─────────────────────────── 篡改注入（R7/R8 必须抓住） ───────────────────────────
def _run_price(recorder):
    tools_mod.execute_tool(
        "get_price_history",
        {"symbol": "600519"},
        FakeProvider(),
        context=recorder.context,
        trace=recorder,
    )


def test_r8_catches_value_tampering(recorder):
    _run_price(recorder)
    evidence = _evidence_dict(recorder)
    eid = recorder.bindings[1].eid  # last_close
    evidence[eid] = evidence[eid].model_copy(update={"value": 999.0})  # 值被改写
    violations = verify_provenance(
        evidence, recorder.bindings, recorder.captures, recorder.tool_call_ids
    )
    assert any("R8" in v and eid in v for v in violations)


def test_r7_catches_missing_capture(recorder, tmp_path):
    _run_price(recorder)
    for path in (tmp_path / "objects").glob("*.json"):
        path.unlink()  # 捕获被删：指针悬空
    violations = verify_provenance(
        _evidence_dict(recorder), recorder.bindings, recorder.captures, recorder.tool_call_ids
    )
    assert violations and all("R7" in v for v in violations)


def test_r7_catches_foreign_tool_call_id(recorder):
    _run_price(recorder)
    violations = verify_provenance(
        _evidence_dict(recorder), recorder.bindings, recorder.captures, {"tc_other"}
    )
    assert any("tool_call_id" in v for v in violations)


def test_r7_catches_unbound_evidence(recorder):
    _run_price(recorder)
    evidence = _evidence_dict(recorder)
    bindings = recorder.bindings[1:]  # 抹掉一条绑定
    violations = verify_provenance(evidence, bindings, recorder.captures, recorder.tool_call_ids)
    assert any("无绑定记录" in v for v in violations)


# ─────────────────────────── stale（B8） ───────────────────────────
def test_stale_confidence_for_old_capture(tmp_path):
    later = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)  # as_of 距捕获 4 天 > 72h
    context = AnalysisContext(as_of=later, capture_policy="live", clock=FrozenClock(later))
    rec = ToolRecorder(
        context=context, evidence=EvidenceStore("600519"), captures=CaptureStore(tmp_path)
    )
    rec.current_tool_call_id = "tc_1"
    out = json.loads(
        tools_mod.execute_tool(
            "get_price_history",
            {"symbol": "600519"},
            FakeProvider(),  # attrs.captured_at 固定为 07-18
            context=context,
            trace=rec,
        )
    )
    ev = rec.evidence.get(out["last_close"]["eid"])
    assert ev.confidence == "stale"  # 缓存穿透的真实捕获时刻驱动 stale 判定


def test_ref_shape():
    from bellwether.ir.models import SourceRef
    from bellwether.ir.store import EvidenceStore as ES

    store = ES("AAPL")
    ev = store.register(
        metric_name="close",
        kind="metric",
        value=1.0,
        pit_class="observed",
        first_seen_at=AS_OF,
        available_at=AS_OF,
        price_basis="unadjusted",
        source=SourceRef(
            provider_id="yfinance",
            tool_name="get_price_history",
            tool_call_id="tc",
            data_type="ohlcv",
            capture_id="c",
            response_sha256="a" * 64,
            captured_at=AS_OF,
            license_tag="private-ok-backup-ok",
        ),
        confidence="reported",
    )
    assert ref(ev) == {"v": 1.0, "eid": "E1"}
