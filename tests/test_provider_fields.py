"""provider fundamentals / news 字段解析补测（monkeypatch 底层 SDK，不打网）。"""

import pandas as pd
import pytest

from bellwether.data.akshare_provider import AkshareCNProvider, AkshareHKProvider, _fetch_em_news
from bellwether.data.yfinance_provider import YFinanceProvider


# ─────────────────────────── yfinance：get_fundamentals ───────────────────────────
class _FakeTickerInfo:
    def __init__(self, info):
        self.info = info


def test_yfinance_get_fundamentals_maps_fields(monkeypatch):
    info = {
        "shortName": "Apple Inc.",
        "currency": "USD",
        "marketCap": 3_000_000_000_000,
        "trailingPE": 30.5,
        "priceToBook": 45.2,
        "trailingEps": 6.1,
        "totalRevenue": 400_000_000_000,
        "netIncomeToCommon": 100_000_000_000,
        "returnOnEquity": 1.5,
        "freeCashflow": 90_000_000_000,
        "sharesOutstanding": 15_000_000_000,
        "totalDebt": 120_000_000_000,
        "totalCash": 60_000_000_000,
        "trailingPegRatio": 2.1,
        "priceToSalesTrailing12Months": 7.8,
        "grossMargins": 0.45,
    }
    monkeypatch.setattr(
        "bellwether.data.yfinance_provider.yf.Ticker", lambda symbol: _FakeTickerInfo(info)
    )
    fund = YFinanceProvider().get_fundamentals("AAPL")
    assert fund.symbol == "AAPL"
    assert fund.name == "Apple Inc."
    assert fund.currency == "USD"
    assert fund.market_cap == 3_000_000_000_000
    assert fund.pe == 30.5
    assert fund.pb == 45.2
    assert fund.eps == 6.1
    assert fund.revenue == 400_000_000_000
    assert fund.net_income == 100_000_000_000
    assert fund.roe == 1.5
    assert fund.free_cashflow == 90_000_000_000
    assert fund.shares_outstanding == 15_000_000_000
    assert fund.total_debt == 120_000_000_000
    assert fund.total_cash == 60_000_000_000
    assert fund.peg == 2.1
    assert fund.ps == 7.8
    assert fund.gross_margins == 0.45
    assert fund.source == "yfinance"


def test_yfinance_get_fundamentals_missing_info_returns_shell(monkeypatch):
    for info in (None, {}):
        monkeypatch.setattr(
            "bellwether.data.yfinance_provider.yf.Ticker",
            lambda symbol, _info=info: _FakeTickerInfo(_info),
        )
        fund = YFinanceProvider().get_fundamentals("AAPL")
        assert fund.symbol == "AAPL"
        assert fund.name is None
        assert fund.market_cap is None
        assert fund.pe is None
        assert fund.source == "yfinance"


# ─────────────────────────── yfinance：get_news ───────────────────────────
def test_yfinance_get_news_parses_nested_content_and_skips_missing_title(monkeypatch):
    raw = [
        {
            "content": {
                "title": "苹果创新高",
                "canonicalUrl": {"url": "http://example.com/a"},
                "summary": "摘要文本",
            },
            "providerPublishTime": 1720000000,
        },
        {"content": {"summary": "无标题"}},  # 缺 title，应被跳过
    ]

    class _FakeTickerNews:
        def __init__(self, symbol):
            self.news = raw

    monkeypatch.setattr("bellwether.data.yfinance_provider.yf.Ticker", _FakeTickerNews)
    items = YFinanceProvider().get_news("AAPL", limit=10)
    assert len(items) == 1
    assert items[0].title == "苹果创新高"
    assert items[0].url == "http://example.com/a"
    assert items[0].summary == "摘要文本"
    assert items[0].published_at is not None


# ─────────────────────────── akshare CN：get_fundamentals ───────────────────────────
def test_akshare_cn_get_fundamentals_parses_indicator(monkeypatch):
    akshare = pytest.importorskip("akshare")

    def fake_indicator(**kwargs):
        return pd.DataFrame({"pe_ttm": [10.0, 12.5], "pb": [1.1, 1.3], "ps_ttm": [2.0, 2.2]})

    # raising=False：本机装的 akshare 版本已不带 stock_a_indicator_lg 属性，
    # 这里只验证「给定该接口标准返回，解析正确」，不依赖它在当前版本真实存在。
    monkeypatch.setattr(akshare, "stock_a_indicator_lg", fake_indicator, raising=False)
    fund = AkshareCNProvider().get_fundamentals("600519")
    assert fund.symbol == "600519"
    assert fund.currency == "CNY"
    assert fund.pe == 12.5
    assert fund.pb == 1.3
    assert fund.ps == 2.2
    assert fund.source == "akshare"


def test_akshare_cn_get_fundamentals_exception_leaves_blank(monkeypatch):
    akshare = pytest.importorskip("akshare")

    def boom(**kwargs):
        raise RuntimeError("估值接口不稳")

    monkeypatch.setattr(akshare, "stock_a_indicator_lg", boom, raising=False)
    fund = AkshareCNProvider().get_fundamentals("600519")
    assert fund.pe is None
    assert fund.pb is None
    assert fund.ps is None
    assert fund.currency == "CNY"


# ─────────────────────────── akshare HK：get_fundamentals ───────────────────────────
def test_akshare_hk_get_fundamentals_returns_shell():
    fund = AkshareHKProvider().get_fundamentals("00700")
    assert fund.symbol == "00700"
    assert fund.currency == "HKD"
    assert fund.pe is None
    assert fund.source == "akshare"


# ─────────────────────────── _fetch_em_news ───────────────────────────
def test_fetch_em_news_exception_returns_empty(monkeypatch):
    akshare = pytest.importorskip("akshare")

    def boom(**kwargs):
        raise RuntimeError("接口挂了")

    monkeypatch.setattr(akshare, "stock_news_em", boom)
    assert _fetch_em_news(akshare, "600519", 10) == []


def test_fetch_em_news_empty_df_returns_empty(monkeypatch):
    akshare = pytest.importorskip("akshare")

    monkeypatch.setattr(akshare, "stock_news_em", lambda **kw: pd.DataFrame())
    assert _fetch_em_news(akshare, "600519", 10) == []


def test_fetch_em_news_none_returns_empty(monkeypatch):
    akshare = pytest.importorskip("akshare")

    monkeypatch.setattr(akshare, "stock_news_em", lambda **kw: None)
    assert _fetch_em_news(akshare, "600519", 10) == []
