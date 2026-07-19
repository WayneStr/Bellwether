"""共享测试基座：tenacity 不真睡 + 熔断器状态隔离。

两个 autouse fixture 都是全局可变状态的隔离需求：退避重试真 sleep 会把故障注入
测试拖慢数十秒；熔断器是进程级单例，测试间不清空会互相污染。
"""

from datetime import UTC, datetime

import pytest

from bellwether.core.circuit import reset_all_breakers
from bellwether.core.context import AnalysisContext, FrozenClock


@pytest.fixture(autouse=True)
def _no_tenacity_sleep(monkeypatch):
    monkeypatch.setattr("tenacity.nap.time.sleep", lambda _s: None)


@pytest.fixture(autouse=True)
def _isolated_breakers():
    reset_all_breakers()
    yield
    reset_all_breakers()


@pytest.fixture
def ctx() -> AnalysisContext:
    """固定的 AnalysisContext（spec-002）：as_of 与 clock 恒为 2026-01-07，供各测试复用。"""
    as_of = datetime(2026, 1, 7, tzinfo=UTC)
    return AnalysisContext(as_of=as_of, capture_policy="live", clock=FrozenClock(as_of))
