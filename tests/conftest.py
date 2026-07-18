"""共享测试基座：tenacity 不真睡 + 熔断器状态隔离。

两个 autouse fixture 都是全局可变状态的隔离需求：退避重试真 sleep 会把故障注入
测试拖慢数十秒；熔断器是进程级单例，测试间不清空会互相污染。
"""

import pytest

from bellwether.core.circuit import reset_all_breakers


@pytest.fixture(autouse=True)
def _no_tenacity_sleep(monkeypatch):
    monkeypatch.setattr("tenacity.nap.time.sleep", lambda _s: None)


@pytest.fixture(autouse=True)
def _isolated_breakers():
    reset_all_breakers()
    yield
    reset_all_breakers()
