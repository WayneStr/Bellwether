"""数据层与确定性辅助函数的单测（不打网络）。"""

from datetime import date

import pandas as pd

from bellwether.agent.tools import _period_to_start, _summarize_ohlcv
from bellwether.data.base import ProviderRegistry
from bellwether.data.yfinance_provider import YFinanceProvider


def test_registry_resolves_us():
    provider = ProviderRegistry.for_market("US")
    assert isinstance(provider, YFinanceProvider)
    assert provider.market == "US"


def test_for_symbol_defaults_us():
    assert ProviderRegistry.for_symbol("AAPL").market == "US"


def test_trading_rules_us():
    rules = YFinanceProvider().trading_rules()
    assert rules.market == "US"
    assert rules.has_price_limit is False


def test_period_to_start():
    end = date(2026, 7, 15)
    assert _period_to_start("1y", end) == date(2025, 7, 15)
    assert _period_to_start("6mo", end).year == 2026
    assert (end - _period_to_start("10d", end)).days == 10


def test_summarize_ohlcv():
    idx = pd.to_datetime(["2026-01-02", "2026-01-03", "2026-01-06"])
    df = pd.DataFrame(
        {
            "open": [1, 2, 3],
            "high": [2, 3, 4],
            "low": [0.5, 1.5, 2.5],
            "close": [1.0, 2.0, 4.0],
            "volume": [100, 200, 300],
        },
        index=idx,
    )
    summary = _summarize_ohlcv(df, "TEST", "1d", "unit", recent=2)
    assert summary.symbol == "TEST"
    assert summary.bars_count == 3
    assert summary.last_close == 4.0
    assert summary.period_return_pct == 300.0  # (4/1 - 1) * 100
    assert len(summary.recent_bars) == 2
    assert summary.recent_bars[-1].date == "2026-01-06"
