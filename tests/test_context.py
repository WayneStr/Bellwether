"""spec-002 单测：AnalysisContext 构造、Clock 实现、静态时间守卫、同一性传播。"""

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

import bellwether
from bellwether.core.context import AnalysisContext, FrozenClock, SystemClock

# ─────────────────────────── AnalysisContext 构造 ───────────────────────────


def test_naive_as_of_rejected():
    with pytest.raises(ValueError, match="timezone-aware"):
        AnalysisContext(as_of=datetime(2026, 1, 7), capture_policy="live", clock=SystemClock())


def test_aware_as_of_accepted():
    as_of = datetime(2026, 1, 7, tzinfo=UTC)
    context = AnalysisContext(as_of=as_of, capture_policy="live", clock=SystemClock())
    assert context.as_of == as_of
    assert context.capture_policy == "live"


def test_context_is_frozen():
    context = AnalysisContext(
        as_of=datetime(2026, 1, 7, tzinfo=UTC), capture_policy="live", clock=SystemClock()
    )
    with pytest.raises(AttributeError):
        context.capture_policy = "cassette"  # type: ignore[misc]


# ─────────────────────────── Clock 实现 ───────────────────────────


def test_frozen_clock_returns_constant():
    frozen = datetime(2026, 1, 7, 12, 0, tzinfo=UTC)
    clock = FrozenClock(frozen)
    assert clock.now() == frozen
    assert clock.now() == frozen  # 多次调用恒定


def test_system_clock_returns_aware_utc_now():
    before = datetime.now(UTC)
    now = SystemClock().now()
    after = datetime.now(UTC)
    assert now.tzinfo is not None and now.utcoffset() == UTC.utcoffset(now)
    assert before <= now <= after


# ─────────────────────────── §5.3 静态时间守卫 ───────────────────────────

_TIME_CALL_RE = re.compile(r"datetime\.now\(|date\.today\(")


def _scan_direct_time_calls(pkg_root: Path) -> list[tuple[str, int, str]]:
    hits = []
    for path in sorted(pkg_root.rglob("*.py")):
        rel = path.relative_to(pkg_root).as_posix()
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if _TIME_CALL_RE.search(line):
                hits.append((rel, lineno, line.strip()))
    return hits


def _is_whitelisted(rel: str, line: str) -> bool:
    if rel == "core/context.py":
        return "datetime.now(UTC)" in line  # SystemClock.now() 的唯一直连调用
    if rel == "snapshot.py":
        return "finished_at" in line  # last_status 运营完成戳（spec-002 §4 白名单）
    return False


def test_direct_time_calls_are_confined_to_whitelist():
    """spec-002 §5.3：全库 datetime.now(/date.today( 出现位置必须 ⊆ 白名单
    （core/context.py 的 SystemClock.now、snapshot.py 的 last_status.finished_at）。
    出现未登记的新增即 fail。"""
    pkg_root = Path(bellwether.__file__).parent
    hits = _scan_direct_time_calls(pkg_root)

    unlisted = [h for h in hits if not _is_whitelisted(h[0], h[2])]
    assert unlisted == [], f"未登记的直连时间调用（不在 spec-002 §4 白名单内）：{unlisted}"

    context_hits = [h for h in hits if h[0] == "core/context.py"]
    snapshot_hits = [h for h in hits if h[0] == "snapshot.py"]
    assert len(context_hits) == 1, "白名单应恰好在 core/context.py 出现 1 处（SystemClock）"
    assert len(snapshot_hits) == 1, "白名单应恰好在 snapshot.py 出现 1 处（finished_at）"


# ─────────────────────────── §5.4 同一性传播 ───────────────────────────


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


class _FakeClient:
    def __init__(self, script):
        self._script = list(script)
        self.messages = self

    def create(self, **kwargs):
        return self._script.pop(0)


def test_context_identity_preserved_across_tool_calls(monkeypatch, ctx):
    """spec-002 §5.4：入口构造的 context 与传给每次 execute_tool 的 context 是同一对象（is）。"""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    seen_contexts = []

    def _fake_execute_tool(name, tool_input, provider, *, context, trace=None):
        seen_contexts.append(context)
        return '{"ok": true}'

    monkeypatch.setattr("bellwether.agent.tools.execute_tool", _fake_execute_tool)

    from bellwether.agent.llm import ResilientLLM
    from bellwether.agent.orchestrator import Orchestrator
    from bellwether.config import AppConfig

    orch = Orchestrator(AppConfig())
    orch.llm = ResilientLLM(_FakeClient([_tool_use_msg(), _tool_use_msg(), _end_turn_msg()]))
    orch.analyze("AAPL", context=ctx)

    assert len(seen_contexts) == 2  # 两轮 tool_use
    assert all(seen is ctx for seen in seen_contexts)
