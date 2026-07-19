"""数据源侧故障注入（ROADMAP D2 验收面之一）：类型化、重试、熔断、降级链协同。

monkeypatch 数据源函数模拟故障，不打网；tenacity sleep 与熔断状态由 conftest 隔离。
"""

from datetime import date

import pandas as pd
import pytest

from bellwether.core.exceptions import (
    BellwetherError,
    DataUnavailableError,
    RateLimitError,
)
from bellwether.data.base import classify_provider_error

_START, _END = date(2026, 1, 1), date(2026, 1, 7)


def _ohlcv_df():
    return pd.DataFrame(
        {
            "date": ["2026-01-02", "2026-01-03"],
            "open": [1.0, 2.0],
            "high": [1.0, 2.0],
            "low": [1.0, 2.0],
            "close": [1.0, 2.0],
            "volume": [10.0, 20.0],
        }
    )


# ─────────────────────────── 异常分类 ───────────────────────────
def test_classify_connection_and_timeout_are_retryable():
    assert isinstance(classify_provider_error(ConnectionError("挂")), RateLimitError)
    assert isinstance(classify_provider_error(TimeoutError("超时")), RateLimitError)
    assert isinstance(classify_provider_error(OSError("socket 断")), RateLimitError)


def test_classify_parse_errors_not_retryable():
    assert isinstance(classify_provider_error(KeyError("列名变了")), DataUnavailableError)
    assert isinstance(classify_provider_error(ValueError("解析失败")), DataUnavailableError)


def test_classify_passes_through_typed_errors():
    exc = DataUnavailableError("已类型化")
    assert classify_provider_error(exc) is exc


# ─────────────────────────── CN 降级链 × 熔断 ───────────────────────────
def _patch_cn(monkeypatch, akshare, hist, daily):
    monkeypatch.setattr(akshare, "stock_zh_a_hist", hist)
    monkeypatch.setattr(akshare, "stock_zh_a_daily", daily)
    monkeypatch.setattr(
        "bellwether.data.akshare_provider.cached_dataframe",
        lambda key, ttl, loader: loader(),
    )


def test_cn_empty_both_sides_not_retried(monkeypatch, ctx):
    akshare = pytest.importorskip("akshare")
    from bellwether.data.akshare_provider import AkshareCNProvider

    calls = {"em": 0, "sina": 0}

    def empty_hist(**kwargs):
        calls["em"] += 1
        return pd.DataFrame()

    def empty_daily(**kwargs):
        calls["sina"] += 1
        return pd.DataFrame()

    _patch_cn(monkeypatch, akshare, empty_hist, empty_daily)
    with pytest.raises(DataUnavailableError):
        AkshareCNProvider().get_ohlcv("600519", _START, _END, context=ctx)
    # 空数据不可重试：两源各只被调一次，不做无意义退避
    assert calls == {"em": 1, "sina": 1}


def test_cn_connection_failures_retried_then_typed(monkeypatch, ctx):
    akshare = pytest.importorskip("akshare")
    from bellwether.data.akshare_provider import AkshareCNProvider

    calls = {"em": 0, "sina": 0}

    def down_hist(**kwargs):
        calls["em"] += 1
        raise ConnectionError("em 断连")

    def down_daily(**kwargs):
        calls["sina"] += 1
        raise ConnectionError("sina 断连")

    _patch_cn(monkeypatch, akshare, down_hist, down_daily)
    with pytest.raises(RateLimitError):
        AkshareCNProvider().get_ohlcv("600519", _START, _END, context=ctx)
    # 连接类可重试：整链退避重试 4 次（datasource_retry），每轮两源各试一次
    assert calls == {"em": 4, "sina": 4}


def test_cn_eastmoney_circuit_opens_and_skips(monkeypatch, ctx):
    akshare = pytest.importorskip("akshare")
    from bellwether.data.akshare_provider import AkshareCNProvider

    calls = {"em": 0, "sina": 0}

    def down_hist(**kwargs):
        calls["em"] += 1
        raise ConnectionError("em 持续不可达")

    def ok_daily(**kwargs):
        calls["sina"] += 1
        return _ohlcv_df()

    _patch_cn(monkeypatch, akshare, down_hist, ok_daily)
    provider = AkshareCNProvider()

    # 前 5 次：东财失败→新浪成功（每次东财失败计入熔断），结果始终可用
    for _ in range(5):
        df = provider.get_ohlcv("600519", _START, _END, context=ctx)
        assert len(df) == 2
    assert calls == {"em": 5, "sina": 5}

    # 阈值已到（默认 5）：东财熔断打开，后续直接走新浪，不再白等东财
    for _ in range(3):
        df = provider.get_ohlcv("600519", _START, _END, context=ctx)
        assert len(df) == 2
    assert calls["em"] == 5  # 东财不再被调用
    assert calls["sina"] == 8


# ─────────────────────────── US（yfinance）───────────────────────────
class _FakeTicker:
    """yf.Ticker 替身：history 行为由类属性注入。"""

    behavior = staticmethod(lambda **kw: pd.DataFrame())
    calls = 0

    def __init__(self, symbol):
        self.symbol = symbol

    def history(self, **kwargs):
        type(self).calls += 1
        assert "timeout" in kwargs  # D2：显式超时必须传给底层
        return type(self).behavior(**kwargs)


@pytest.fixture()
def fake_yf(monkeypatch):
    import bellwether.data.yfinance_provider as mod

    _FakeTicker.calls = 0
    monkeypatch.setattr(mod.yf, "Ticker", _FakeTicker)
    monkeypatch.setattr(mod, "cached_dataframe", lambda key, ttl, loader: loader())
    return _FakeTicker


def test_yf_empty_is_unavailable_no_retry(fake_yf, ctx):
    from bellwether.data.yfinance_provider import YFinanceProvider

    fake_yf.behavior = staticmethod(lambda **kw: pd.DataFrame())
    with pytest.raises(DataUnavailableError):
        YFinanceProvider().get_ohlcv("AAPL", _START, _END, context=ctx)
    assert fake_yf.calls == 1  # 空数据不重试


def test_yf_connection_error_retried_then_typed(fake_yf, ctx):
    from bellwether.data.yfinance_provider import YFinanceProvider

    def boom(**kw):
        raise ConnectionError("yahoo 断连")

    fake_yf.behavior = staticmethod(boom)
    with pytest.raises(RateLimitError):
        YFinanceProvider().get_ohlcv("AAPL", _START, _END, context=ctx)
    assert fake_yf.calls == 4  # 退避重试 4 次后类型化上抛


def test_yf_typed_errors_are_bellwether(fake_yf, ctx):
    """所有对外抛出的失败都应是 BellwetherError 家族（调用方按类型决策）。"""
    from bellwether.data.yfinance_provider import YFinanceProvider

    def boom(**kw):
        raise KeyError("上游改字段")

    fake_yf.behavior = staticmethod(boom)
    with pytest.raises(BellwetherError):
        YFinanceProvider().get_ohlcv("AAPL", _START, _END, context=ctx)
