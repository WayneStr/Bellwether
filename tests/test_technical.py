"""TechnicalModule.compute 的单测：用假 provider 喂固定数据，不打网络。"""

import pandas as pd

from bellwether.analysis.technical import TechnicalModule


class _FakeProvider:
    """喂一段单调上涨的 60 日行情，指标结果可预期。"""

    source = "fake"

    def get_ohlcv(self, symbol, start, end, interval="1d", context=None):
        idx = pd.date_range("2025-01-01", periods=60, freq="D")
        close = pd.Series(range(100, 160), index=idx, dtype=float)
        return pd.DataFrame(
            {"open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 1000.0},
            index=idx,
        )


def test_technical_compute_basic(ctx):
    report = TechnicalModule().compute("TEST", _FakeProvider(), period="3mo", context=ctx)
    assert report.symbol == "TEST"
    assert report.last_close == 159.0
    assert report.source == "fake"
    assert report.indicators["MA20"] is not None
    assert report.indicators["RSI14"] == 100.0  # 单调上涨无下跌 → RSI=100
    assert report.data_asof == "2025-03-01"  # 第 60 天


def test_technical_signals_are_neutral_descriptions(ctx):
    report = TechnicalModule().compute("TEST", _FakeProvider(), period="3mo", context=ctx)
    joined = " ".join(report.signals)
    assert "多头排列" in joined  # MA20 > MA50 且上涨
    # 中性描述里不应出现买卖指令字样
    for word in ("买入", "卖出", "buy", "sell"):
        assert word not in joined
