"""熔断器状态机单测（注入假时钟，不真等冷却）。"""

import pytest

from bellwether.core.circuit import CircuitBreaker, breaker_for, reset_all_breakers
from bellwether.core.exceptions import CircuitOpenError


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _make(threshold=3, cooldown=60.0):
    clock = FakeClock()
    breaker = CircuitBreaker(("test", "method"), threshold, cooldown, clock)
    return breaker, clock


def _boom():
    raise ConnectionError("源挂了")


def test_closed_passes_through():
    breaker, _ = _make()
    assert breaker.call(lambda: "ok") == "ok"


def test_opens_after_consecutive_failures():
    breaker, _ = _make(threshold=3)
    for _ in range(3):
        with pytest.raises(ConnectionError):
            breaker.call(_boom)
    # 已打开：不再调用底层函数，直接快速失败
    calls = {"n": 0}

    def counted():
        calls["n"] += 1

    with pytest.raises(CircuitOpenError):
        breaker.call(counted)
    assert calls["n"] == 0


def test_success_resets_failure_count():
    breaker, _ = _make(threshold=3)
    for _ in range(2):
        with pytest.raises(ConnectionError):
            breaker.call(_boom)
    breaker.call(lambda: "ok")  # 成功清零
    for _ in range(2):
        with pytest.raises(ConnectionError):
            breaker.call(_boom)
    # 2+2 但中间清零过，未达阈值 3，仍应放行
    assert breaker.call(lambda: "ok") == "ok"


def test_half_open_recovers_on_success():
    breaker, clock = _make(threshold=2, cooldown=60.0)
    for _ in range(2):
        with pytest.raises(ConnectionError):
            breaker.call(_boom)
    with pytest.raises(CircuitOpenError):
        breaker.call(lambda: "ok")
    clock.advance(61.0)  # 冷却期满 → half-open 放行试探
    assert breaker.call(lambda: "ok") == "ok"
    assert breaker.call(lambda: "ok") == "ok"  # 试探成功 → closed


def test_half_open_reopens_on_failure():
    breaker, clock = _make(threshold=2, cooldown=60.0)
    for _ in range(2):
        with pytest.raises(ConnectionError):
            breaker.call(_boom)
    clock.advance(61.0)
    with pytest.raises(ConnectionError):  # 试探失败 → 重新 open 并重新计时
        breaker.call(_boom)
    with pytest.raises(CircuitOpenError):
        breaker.call(lambda: "ok")
    clock.advance(61.0)  # 再冷却一轮后才再放行
    assert breaker.call(lambda: "ok") == "ok"


def test_nested_circuit_open_not_counted_as_failure():
    breaker, _ = _make(threshold=1)

    def inner_open():
        raise CircuitOpenError("内层源熔断")

    with pytest.raises(CircuitOpenError):
        breaker.call(inner_open)
    # 内层熔断不算本器失败：本器仍 closed，正常放行
    assert breaker.call(lambda: "ok") == "ok"


def test_registry_returns_same_instance():
    reset_all_breakers()
    a = breaker_for("eastmoney", "kline_cn")
    b = breaker_for("eastmoney", "kline_cn")
    c = breaker_for("sina", "kline_cn")
    assert a is b
    assert a is not c
    reset_all_breakers()
    assert breaker_for("eastmoney", "kline_cn") is not a
