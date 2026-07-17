"""纯确定性技术指标计算（pandas 实现）。

刻意不引入 pandas-ta：它在 numpy>=2.0 下有 `from numpy import NaN` 的 import 崩溃，
且是黑盒。这里的经典指标算法简单、可控、可逐个单测——符合「确定性计算必须可验证」。

约定：输入价格 Series，输出等长 Series（前若干位为 NaN，属正常预热期）。
"""

from __future__ import annotations

import pandas as pd


def sma(series: pd.Series, period: int) -> pd.Series:
    """简单移动平均。"""
    return series.rolling(window=period, min_periods=period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    """指数移动平均（adjust=False，首值等于首个价格）。"""
    return series.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """相对强弱指标，Wilder 平滑。全程无下跌时为 100，无上涨时为 0。"""
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def macd(
    series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """返回 (macd 线, signal 线, 柱状 hist)。hist = macd - signal 恒成立。"""
    macd_line = ema(series, fast) - ema(series, slow)
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def bollinger(
    series: pd.Series, period: int = 20, n_std: float = 2.0
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """布林带，返回 (中轨, 上轨, 下轨)。中轨即 period 期 SMA，用总体标准差(ddof=0)。"""
    mid = sma(series, period)
    std = series.rolling(window=period, min_periods=period).std(ddof=0)
    return mid, mid + n_std * std, mid - n_std * std
