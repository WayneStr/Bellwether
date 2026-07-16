"""A股 / 港股 provider 字段解析单测（monkeypatch akshare，不打网）。

真实东财数据源需在能访问的网络验证；此处验证「给定 akshare 标准返回，解析正确」。
"""

import os
from datetime import date

import pandas as pd
import pytest

from bellwether.data.akshare_provider import AkshareCNProvider, AkshareHKProvider
from bellwether.data.base import ProviderRegistry


def test_akshare_cn_ohlcv_parsing(monkeypatch):
    akshare = pytest.importorskip("akshare")

    def fake_hist(**kwargs):
        return pd.DataFrame(
            {
                "日期": ["2026-01-02", "2026-01-03", "2026-01-06"],
                "开盘": [1700.0, 1710.0, 1720.0],
                "收盘": [1710.0, 1720.0, 1730.0],
                "最高": [1715.0, 1725.0, 1735.0],
                "最低": [1695.0, 1705.0, 1715.0],
                "成交量": [1000.0, 1100.0, 1200.0],
                "成交额": [1.0, 2.0, 3.0],  # 多余列应被丢弃
            }
        )

    monkeypatch.setattr(akshare, "stock_zh_a_hist", fake_hist)
    monkeypatch.setattr(
        "bellwether.data.akshare_provider.cached_dataframe",
        lambda key, ttl, loader: loader(),
    )

    df = AkshareCNProvider().get_ohlcv("600519", date(2026, 1, 1), date(2026, 1, 7))
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df["close"].iloc[-1] == 1730.0
    assert str(df.index[-1].date()) == "2026-01-06"
    assert len(df) == 3


def test_akshare_hk_ohlcv_parsing(monkeypatch):
    akshare = pytest.importorskip("akshare")

    def fake_hk_daily(**kwargs):
        # 新浪源：英文列 + 返回全历史，含多余的 amount 列
        return pd.DataFrame(
            {
                "date": ["2026-01-02", "2026-01-05", "2026-01-06"],
                "open": [500.0, 505.0, 510.0],
                "high": [508.0, 512.0, 518.0],
                "low": [498.0, 503.0, 508.0],
                "close": [505.0, 510.0, 515.0],
                "volume": [10000.0, 11000.0, 12000.0],
                "amount": [1.0, 2.0, 3.0],
            }
        )

    monkeypatch.setattr(akshare, "stock_hk_daily", fake_hk_daily)
    monkeypatch.setattr(
        "bellwether.data.akshare_provider.cached_dataframe",
        lambda key, ttl, loader: loader(),
    )

    df = AkshareHKProvider().get_ohlcv("00700", date(2026, 1, 1), date(2026, 1, 7))
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df["close"].iloc[-1] == 515.0
    assert "amount" not in df.columns  # 多余列被丢弃
    assert str(df.index[-1].date()) == "2026-01-06"


def test_cn_code_normalization():
    p = AkshareCNProvider()
    assert p._to_code("600519.SH") == "600519"
    assert p._to_code("600519") == "600519"


def test_hk_code_normalization():
    p = AkshareHKProvider()
    assert p._to_code("00700.HK") == "00700"
    assert p._to_code("700") == "00700"
    assert p._to_code("0700") == "00700"


def test_trading_rules():
    cn = AkshareCNProvider().trading_rules()
    assert cn.market == "CN" and cn.has_price_limit is True and cn.settlement == "T+1"
    hk = AkshareHKProvider().trading_rules()
    assert hk.market == "HK" and hk.has_price_limit is False and hk.settlement == "T+2"


def test_market_routing_to_providers():
    assert ProviderRegistry.for_symbol("600519").market == "CN"
    assert ProviderRegistry.for_symbol("00700.HK").market == "HK"
    assert ProviderRegistry.for_symbol("00700").market == "HK"


def test_eastmoney_bypasses_proxy(monkeypatch):
    from bellwether.data.akshare_provider import _bypass_proxy_for_eastmoney

    monkeypatch.setenv("NO_PROXY", "")
    _bypass_proxy_for_eastmoney()
    assert "eastmoney.com" in os.environ["NO_PROXY"]

    # 已存在的 NO_PROXY 条目应保留
    monkeypatch.setenv("NO_PROXY", "localhost")
    _bypass_proxy_for_eastmoney()
    assert "localhost" in os.environ["NO_PROXY"] and "eastmoney.com" in os.environ["NO_PROXY"]


def test_akshare_cn_news_parsing(monkeypatch):
    akshare = pytest.importorskip("akshare")

    def fake_news(**kwargs):
        return pd.DataFrame(
            {
                "关键词": ["600519", "600519"],
                "新闻标题": ["白酒板块飘红", "茅台发布财报"],
                "新闻内容": ["板块全线上涨……", "Q2 营收增长……"],
                "发布时间": ["2026-07-15 11:21:00", "2026-07-14 09:00:00"],
                "文章来源": ["21世纪", "证券时报"],
                "新闻链接": ["http://x", "http://y"],
            }
        )

    monkeypatch.setattr(akshare, "stock_news_em", fake_news)
    items = AkshareCNProvider().get_news("600519", limit=5)
    assert len(items) == 2
    assert items[0].title == "白酒板块飘红"
    assert items[0].published_at is not None
    assert items[0].url == "http://x"


def test_akshare_hk_news_parsing(monkeypatch):
    akshare = pytest.importorskip("akshare")

    def fake_news(**kwargs):
        return pd.DataFrame(
            {
                "关键词": ["00700"],
                "新闻标题": ["腾讯控股连续回购"],
                "新闻内容": ["累计斥资5.13亿港元……"],
                "发布时间": ["2026-07-15 10:00:00"],
                "文章来源": ["财联社"],
                "新闻链接": ["http://z"],
            }
        )

    monkeypatch.setattr(akshare, "stock_news_em", fake_news)
    items = AkshareHKProvider().get_news("00700", limit=5)
    assert len(items) == 1
    assert items[0].title == "腾讯控股连续回购"
