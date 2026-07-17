"""异常层级与重试策略单测（monkeypatch 掉 tenacity 的 sleep，不真等）。"""

import pytest

from bellwether.core.exceptions import (
    BellwetherError,
    DataSourceError,
    DataUnavailableError,
    LLMAuthError,
    LLMError,
    LLMRateLimitError,
    ModelNotFoundError,
    RateLimitError,
)
from bellwether.core.retry import datasource_retry, llm_retry


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    # tenacity 退避真 sleep 会拖慢测试；替换为无操作，只验证重试逻辑
    monkeypatch.setattr("tenacity.nap.time.sleep", lambda _s: None)


def test_exception_hierarchy():
    assert issubclass(DataUnavailableError, DataSourceError)
    assert issubclass(RateLimitError, DataSourceError)
    assert issubclass(DataSourceError, BellwetherError)
    assert issubclass(LLMAuthError, LLMError)
    assert issubclass(LLMRateLimitError, LLMError)
    assert issubclass(ModelNotFoundError, LLMError)
    assert issubclass(LLMError, BellwetherError)


def test_datasource_retry_recovers_on_ratelimit():
    calls = {"n": 0}

    @datasource_retry
    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RateLimitError("429")
        return "ok"

    assert flaky() == "ok"
    assert calls["n"] == 3  # 重试到第 3 次成功


def test_datasource_retry_skips_unavailable():
    calls = {"n": 0}

    @datasource_retry
    def empty():
        calls["n"] += 1
        raise DataUnavailableError("空结果")

    with pytest.raises(DataUnavailableError):
        empty()
    assert calls["n"] == 1  # 空数据不重试


def test_datasource_retry_exhausts_and_reraises():
    calls = {"n": 0}

    @datasource_retry
    def always_limited():
        calls["n"] += 1
        raise RateLimitError("持续限流")

    with pytest.raises(RateLimitError):
        always_limited()
    assert calls["n"] == 4  # stop_after_attempt(4)


@pytest.mark.parametrize("exc", [LLMAuthError("401"), ModelNotFoundError("503")])
def test_llm_retry_skips_auth_and_model_errors(exc):
    calls = {"n": 0}

    @llm_retry
    def fail():
        calls["n"] += 1
        raise exc

    with pytest.raises(type(exc)):
        fail()
    assert calls["n"] == 1  # 认证/模型错误不重试


def test_llm_retry_recovers_on_ratelimit():
    calls = {"n": 0}

    @llm_retry
    def flaky():
        calls["n"] += 1
        if calls["n"] < 2:
            raise LLMRateLimitError("429")
        return "ok"

    assert flaky() == "ok"
    assert calls["n"] == 2
