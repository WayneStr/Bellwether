"""CaptureStore 与 RFC-000 §8 规范化序列化单测。"""

import hashlib
from datetime import UTC, datetime, timezone
from datetime import timedelta as td

import pytest

from bellwether.core.capture import CaptureStore, canonical_json_bytes

NOW = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)


# ─────────────────────────── 规范化序列化 ───────────────────────────
def test_canonical_key_order_independent():
    a = canonical_json_bytes({"b": 1, "a": {"y": 2, "x": 3}})
    b = canonical_json_bytes({"a": {"x": 3, "y": 2}, "b": 1})
    assert a == b == b'{"a":{"x":3,"y":2},"b":1}'


def test_canonical_float_shortest_roundtrip_no_scientific():
    assert canonical_json_bytes(0.1) == b"0.1"
    assert canonical_json_bytes([1e-07]) == b"[0.0000001]"  # 禁科学计数：定点展开
    assert canonical_json_bytes(-0.0) == b"0.0"  # -0.0 归一
    with pytest.raises(ValueError):
        canonical_json_bytes(float("nan"))


def test_canonical_datetime_utc_iso():
    cst = datetime(2026, 7, 18, 20, 0, tzinfo=timezone(td(hours=8)))
    assert canonical_json_bytes(cst) == b'"2026-07-18T12:00:00+00:00"'  # 归一到 UTC
    with pytest.raises(ValueError):
        canonical_json_bytes(datetime(2026, 7, 18))  # naive 拒绝


def test_canonical_unicode_not_escaped():
    assert canonical_json_bytes("茅台") == '"茅台"'.encode()


# ─────────────────────────── CaptureStore ───────────────────────────
def _persist(store, payload, tool_call_id="tc1"):
    return store.persist(
        payload,
        tool_call_id=tool_call_id,
        provider_id="akshare",
        tool_name="get_price_history",
        canonical_request={"symbol": "600519", "end": "2026-07-18"},
        captured_at=NOW,
        license_tag="private-ok-backup-ok",
        upstream_source="sina",
    )


def test_persist_resolve_verify_roundtrip(tmp_path):
    store = CaptureStore(tmp_path)
    capture_id, sha = _persist(store, {"close": 1710.0, "symbol": "600519"})
    body = store.resolve(capture_id)
    assert body is not None
    assert hashlib.sha256(body).hexdigest() == sha
    assert store.verify(capture_id, sha)  # R7：指针解析 + 哈希吻合
    assert not store.verify(capture_id, "f" * 64)  # 声称哈希不符 → 核验失败
    assert store.resolve("deadbeefdeadbeef") is None  # 不存在的指针


def test_persist_content_addressed_idempotent(tmp_path):
    store = CaptureStore(tmp_path)
    id1, sha1 = _persist(store, {"close": 1710.0}, tool_call_id="tc1")
    id2, sha2 = _persist(store, {"close": 1710.0}, tool_call_id="tc2")
    assert (id1, sha1) == (id2, sha2)  # 同字节同指针（内容寻址去重）
    events = store.events()
    assert len(events) == 2  # 但两次捕获事件都在 manifest
    assert {e["tool_call_id"] for e in events} == {"tc1", "tc2"}


def test_distinct_payloads_distinct_ids(tmp_path):
    store = CaptureStore(tmp_path)
    id1, _ = _persist(store, {"close": 1710.0})
    id2, _ = _persist(store, {"close": 1720.0})
    assert id1 != id2
