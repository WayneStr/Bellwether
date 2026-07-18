"""熔断器（ROADMAP D2）：单源粒度快速失败。

键 (provider_id, method) 与 spec-003 §3 冻结的 limiter 键一致——M3 建 ProviderExecutor
时熔断器沿同一键平移挂载，不需重做。单源粒度让降级链受益：东财熔断打开后，
CN 行情直接走新浪，不再每次白等东财超时。

状态机：closed --连续失败达阈值--> open --冷却期满--> half-open 放行一次试探
        试探成功 → closed（计数清零）；试探失败 → 重新 open（重新计时）
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import TypeVar

from .exceptions import CircuitOpenError

_T = TypeVar("_T")

DEFAULT_THRESHOLD = 5
DEFAULT_COOLDOWN_SECONDS = 300.0


class CircuitBreaker:
    """线程安全的最小熔断器。M1 为 CLI 单进程设计；half-open 未做单飞限制
    （冷却期满后并发调用可能同时试探），单线程使用下无影响。"""

    def __init__(
        self,
        key: tuple[str, str],
        threshold: int = DEFAULT_THRESHOLD,
        cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.key = key
        self._threshold = threshold
        self._cooldown = cooldown_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._failures = 0
        self._opened_at: float | None = None

    def before_call(self) -> None:
        """open 且未冷却 → 抛 CircuitOpenError；冷却期满 → half-open 放行试探。"""
        with self._lock:
            if self._opened_at is None:
                return
            if self._clock() - self._opened_at < self._cooldown:
                raise CircuitOpenError(
                    f"{self.key[0]}.{self.key[1]}: 熔断打开（连续失败 {self._failures} 次），"
                    f"冷却 {self._cooldown:.0f}s 内快速失败"
                )

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self._threshold:
                self._opened_at = self._clock()

    def call(self, fn: Callable[..., _T], *args, **kwargs) -> _T:
        """经熔断器执行 fn：打开时快速失败；否则透传结果/异常并记录成败。"""
        self.before_call()
        try:
            result = fn(*args, **kwargs)
        except CircuitOpenError:
            raise  # 嵌套熔断（如降级链内层）不计入本器失败
        except Exception:
            self.record_failure()
            raise
        self.record_success()
        return result


_breakers: dict[tuple[str, str], CircuitBreaker] = {}
_registry_lock = threading.Lock()


def breaker_for(provider_id: str, method: str) -> CircuitBreaker:
    """按 (provider_id, method) 取同一熔断器实例（进程内共享状态）。"""
    key = (provider_id, method)
    with _registry_lock:
        if key not in _breakers:
            _breakers[key] = CircuitBreaker(key)
        return _breakers[key]


def reset_all_breakers() -> None:
    """清空注册表（测试隔离用）。"""
    with _registry_lock:
        _breakers.clear()
