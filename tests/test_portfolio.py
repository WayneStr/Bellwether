"""PortfolioModule.compute 确定性单测（注入假 provider，不打网）。"""

import numpy as np
import pandas as pd
import pytest

from bellwether.analysis.portfolio import PortfolioModule


def _fake_provider(series: pd.Series):
    class _P:
        source = "fake"

        def resolve_symbol(self, query):
            return query.upper()

        def get_ohlcv(self, symbol, start, end, interval="1d"):
            return pd.DataFrame({"close": series}, index=series.index)

    return _P()


def test_portfolio_perfectly_correlated():
    idx = pd.date_range("2025-01-01", periods=120, freq="D")
    a = pd.Series(np.linspace(100, 200, 120), index=idx)
    b = pd.Series(np.linspace(50, 100, 120), index=idx)  # b = a/2 → 完全正相关
    providers = {"A": _fake_provider(a), "B": _fake_provider(b)}

    r = PortfolioModule().compute(
        ["A", "B"], period="6mo", provider_for=lambda s: providers[s.upper()]
    )
    assert r.symbols == ["A", "B"]
    assert r.weights == {"A": 0.5, "B": 0.5}  # 默认等权
    assert r.concentration_hhi == 0.5  # 2 只等权 HHI = 0.5
    assert abs(r.correlation["A"]["B"] - 1.0) < 1e-6
    assert r.common_days == 120


def test_portfolio_custom_weights_and_drawdown():
    idx = pd.date_range("2025-01-01", periods=60, freq="D")
    a = pd.Series(np.linspace(100, 120, 60), index=idx)
    b = pd.Series(np.linspace(100, 90, 60), index=idx)  # 单调下跌
    providers = {"A": _fake_provider(a), "B": _fake_provider(b)}

    r = PortfolioModule().compute(
        ["A", "B"], period="3mo", weights={"A": 3, "B": 1},
        provider_for=lambda s: providers[s.upper()],
    )
    assert r.weights == {"A": 0.75, "B": 0.25}  # 3:1 归一化
    assert r.concentration_hhi == round(0.75**2 + 0.25**2, 4)  # 0.625
    assert r.max_drawdowns["B"] < 0  # 单调下跌，回撤为负


def test_portfolio_needs_two_symbols():
    with pytest.raises(ValueError):
        PortfolioModule().compute(["A"])
