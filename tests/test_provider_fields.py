"""provider fundamentals / news 字段解析补测（monkeypatch 底层 SDK，不打网）。"""

import pandas as pd
import pytest

from bellwether.data.akshare_provider import AkshareCNProvider, AkshareHKProvider, _fetch_em_news
from bellwether.data.yfinance_provider import YFinanceProvider


# ─────────────────────────── yfinance：get_fundamentals ───────────────────────────
class _FakeTickerInfo:
    def __init__(self, info):
        self.info = info


def test_yfinance_get_fundamentals_maps_fields(monkeypatch, ctx):
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
    fund = YFinanceProvider().get_fundamentals("AAPL", context=ctx)
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


def test_yfinance_get_fundamentals_missing_info_returns_shell(monkeypatch, ctx):
    for info in (None, {}):
        monkeypatch.setattr(
            "bellwether.data.yfinance_provider.yf.Ticker",
            lambda symbol, _info=info: _FakeTickerInfo(_info),
        )
        fund = YFinanceProvider().get_fundamentals("AAPL", context=ctx)
        assert fund.symbol == "AAPL"
        assert fund.name is None
        assert fund.market_cap is None
        assert fund.pe is None
        assert fund.source == "yfinance"


# ─────────────────────────── yfinance：get_news ───────────────────────────
def test_yfinance_get_news_parses_nested_content_and_skips_missing_title(monkeypatch, ctx):
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
    items = YFinanceProvider().get_news("AAPL", limit=10, context=ctx)
    assert len(items) == 1
    assert items[0].title == "苹果创新高"
    assert items[0].url == "http://example.com/a"
    assert items[0].summary == "摘要文本"
    assert items[0].published_at is not None


# ─────────────────────────── akshare CN：get_fundamentals（A9 百度估值）──────────────
def _fake_baidu(vals):
    """构造 stock_*_valuation_baidu 打桩：按 indicator 返回 (date,value) 序列，取末行。"""

    def fake(symbol, indicator, period):
        return pd.DataFrame({"date": ["2026-07-31", "2026-08-01"], "value": [0.0, vals[indicator]]})

    return fake


def test_akshare_cn_get_fundamentals_parses_baidu_valuation(monkeypatch, ctx):
    akshare = pytest.importorskip("akshare")
    vals = {"市盈率(TTM)": 20.4, "市净率": 6.2, "总市值": 16883.6}  # 总市值单位「亿」
    monkeypatch.setattr(akshare, "stock_zh_valuation_baidu", _fake_baidu(vals), raising=False)
    fund = AkshareCNProvider().get_fundamentals("600519", context=ctx)
    assert fund.symbol == "600519"
    assert fund.currency == "CNY"
    assert fund.pe == 20.4
    assert fund.pb == 6.2
    assert fund.market_cap == pytest.approx(16883.6e8)  # 亿 → 原币
    assert fund.source == "akshare"


def test_akshare_cn_get_fundamentals_failure_warns_not_silent(monkeypatch, ctx):
    akshare = pytest.importorskip("akshare")
    warned: list[str] = []
    monkeypatch.setattr(
        "bellwether.data.akshare_provider._log.warning",
        lambda event, **kw: warned.append(event),
    )

    def boom(symbol, indicator, period):
        raise RuntimeError("估值接口不稳")

    monkeypatch.setattr(akshare, "stock_zh_valuation_baidu", boom, raising=False)
    fund = AkshareCNProvider().get_fundamentals("600519", context=ctx)
    assert fund.pe is None and fund.pb is None and fund.market_cap is None
    assert fund.currency == "CNY"
    assert "baidu_valuation_failed" in warned  # A9：失败不再静默吞，逐项记警


# ─────────────────────────── akshare HK：get_fundamentals（A9 百度估值）──────────────
def test_akshare_hk_get_fundamentals_parses_baidu_valuation(monkeypatch, ctx):
    akshare = pytest.importorskip("akshare")
    vals = {"市盈率(TTM)": 16.2, "市净率": 3.4, "总市值": 43207.6}
    monkeypatch.setattr(akshare, "stock_hk_valuation_baidu", _fake_baidu(vals), raising=False)
    fund = AkshareHKProvider().get_fundamentals("00700", context=ctx)
    assert fund.symbol == "00700"
    assert fund.currency == "HKD"
    assert fund.pe == 16.2
    assert fund.pb == 3.4
    assert fund.market_cap == pytest.approx(43207.6e8)
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
