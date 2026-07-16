"""技术指标的确定性单测：用已知/可手算数据验证数值（守住防幻觉底线）。"""

import numpy as np
import pandas as pd

from bellwether.analysis.indicators import bollinger, ema, macd, rsi, sma


def test_sma_basic():
    s = pd.Series([1, 2, 3, 4, 5], dtype=float)
    result = sma(s, 3)
    assert np.isnan(result.iloc[0]) and np.isnan(result.iloc[1])
    assert result.iloc[2] == 2.0  # (1+2+3)/3
    assert result.iloc[3] == 3.0  # (2+3+4)/3
    assert result.iloc[4] == 4.0  # (3+4+5)/3


def test_ema_first_value_equals_price():
    s = pd.Series([10, 20, 30], dtype=float)
    # adjust=False 时首个 EMA 值等于首个价格
    assert ema(s, 2).iloc[0] == 10.0


def test_bollinger_mid_is_sma_and_bands_ordered():
    s = pd.Series(range(1, 25), dtype=float)
    mid, upper, lower = bollinger(s, period=20, n_std=2)
    assert mid.iloc[19] == sma(s, 20).iloc[19]
    assert upper.iloc[19] > mid.iloc[19] > lower.iloc[19]


def test_rsi_all_gains_is_100():
    s = pd.Series(range(1, 30), dtype=float)  # 单调上涨，无下跌
    assert rsi(s, 14).iloc[-1] == 100.0


def test_rsi_stays_in_range():
    rng = np.random.default_rng(0)  # 固定种子，结果确定
    s = pd.Series(rng.normal(0, 1, 200).cumsum() + 1000)
    result = rsi(s, 14).dropna()
    assert (result >= 0).all() and (result <= 100).all()


def test_macd_hist_equals_line_minus_signal():
    s = pd.Series(np.linspace(100, 200, 60))
    macd_line, signal_line, hist = macd(s)
    pd.testing.assert_series_equal(hist, macd_line - signal_line, check_names=False)
