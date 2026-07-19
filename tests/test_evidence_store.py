"""EvidenceStore 单测：串行分配、拒绝自带 eid、闭包、fingerprint 语义键。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest

from bellwether.ir.models import Derivation, SourceRef
from bellwether.ir.store import EvidenceStore, semantic_fingerprint

NOW = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)


def _source(upstream="eastmoney"):
    return SourceRef(
        provider_id="akshare",
        upstream_source=upstream,
        tool_name="get_price_history",
        tool_call_id="tc1",
        data_type="ohlcv",
        capture_id="c1",
        response_sha256="a" * 64,
        captured_at=NOW,
        license_tag="private-ok-backup-ok",
    )


def _register_close(store, value=1710.0, upstream="eastmoney"):
    return store.register(
        metric_name="close",
        kind="metric",
        value=value,
        pit_class="observed",
        first_seen_at=NOW,
        available_at=NOW,
        price_basis="unadjusted",
        source=_source(upstream),
        confidence="reported",
    )


def test_monotonic_allocation_and_fingerprint():
    store = EvidenceStore("600519")
    e1 = _register_close(store)
    e2 = _register_close(store, value=1720.0)
    assert (e1.eid, e2.eid) == ("E1", "E2")
    assert e1.fingerprint and e2.fingerprint
    # fingerprint 排除 value：同语义键不同值 → 同 fingerprint（跨运行对齐同一事实）
    assert e1.fingerprint == e2.fingerprint


def test_fingerprint_distinguishes_upstream_source():
    store = EvidenceStore("600519")
    em = _register_close(store, upstream="eastmoney")
    sina = _register_close(store, upstream="sina")
    assert em.fingerprint != sina.fingerprint  # 降级子源必须可区分（ADR-0006 S2/B9）


def test_caller_supplied_eid_and_fingerprint_rejected():
    store = EvidenceStore("600519")
    with pytest.raises(ValueError):
        store.register(metric_name="close", eid="E99")
    with pytest.raises(ValueError):
        store.register(metric_name="close", fingerprint="f" * 64)


def test_concurrent_registration_unique_eids():
    store = EvidenceStore("600519")
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda _i: _register_close(store).eid, range(50)))
    assert len(set(results)) == 50  # 串行分配器：并发注册无重复
    assert len(store) == 50


def test_closure_traverses_derivation_inputs():
    store = EvidenceStore("AAPL")
    e1 = _register_close(store)
    e2 = _register_close(store, value=1650.0)
    derived = store.register(
        metric_name="pct_change",
        kind="metric",
        value=3.6,
        pit_class="observed",
        first_seen_at=NOW,
        available_at=NOW,
        derivation=Derivation(op="pct_change", inputs=[e1.eid, e2.eid], formula="(a/b-1)*100"),
        confidence="derived",
    )
    closure = store.closure([derived.eid])
    assert set(closure) == {e1.eid, e2.eid, derived.eid}  # 只引用派生值也带回其输入


def test_fingerprint_preimage_components():
    a = semantic_fingerprint("AAPL", "metric", "pe", None, None, None)
    b = semantic_fingerprint("AAPL", "metric", "pb", None, None, None)
    c = semantic_fingerprint("MSFT", "metric", "pe", None, None, None)
    assert len({a, b, c}) == 3  # 指标名与 symbol 均参与区分
