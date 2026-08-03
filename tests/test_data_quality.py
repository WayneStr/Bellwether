"""M3-A4 数据质量校验单测：脏数据注入被拦截 / 隔离 / 告警（纯函数、离线）。"""

from __future__ import annotations

import pandas as pd

from bellwether.data.quality import validate_ohlcv


def _ohlcv(rows: list[tuple], dates: list[str] | None = None) -> pd.DataFrame:
    """rows = [(open, high, low, close, volume), ...]。"""
    dates = dates or [f"2026-01-{i + 1:02d}" for i in range(len(rows))]
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"])
    df.index = pd.to_datetime(dates)
    return df


def test_clean_data_passes_untouched():
    df = _ohlcv([(10, 11, 9, 10.5, 100), (10.5, 12, 10, 11, 120)])
    clean, rep = validate_ohlcv(df, symbol="X")
    assert rep.ok and rep.n_dropped == 0 and rep.issues == []
    assert len(clean) == 2


def test_high_below_low_row_dropped():
    df = _ohlcv([(10, 11, 9, 10.5, 100), (10, 8, 12, 10, 100)])  # 第2行 high<low
    clean, rep = validate_ohlcv(df)
    assert rep.n_dropped == 1 and len(clean) == 1
    assert any("high<low" in i or "不合理" in i for i in rep.issues)


def test_negative_price_and_volume_dropped():
    df = _ohlcv([(10, 11, 9, 10, 100), (-1, 11, 9, 10, 100), (10, 11, 9, 10, -5)])
    clean, rep = validate_ohlcv(df)
    assert rep.n_dropped == 2 and len(clean) == 1


def test_close_outside_high_low_dropped():
    df = _ohlcv([(10, 11, 9, 15, 100)])  # close 15 > high 11
    clean, rep = validate_ohlcv(df)
    assert rep.n_dropped == 1 and clean.empty


def test_duplicate_dates_deduped_keep_last():
    df = _ohlcv(
        [(10, 11, 9, 10, 100), (18, 21, 17, 20, 200)],  # 两行均合法、同日期
        dates=["2026-01-01", "2026-01-01"],
    )
    clean, rep = validate_ohlcv(df)
    assert len(clean) == 1
    assert clean.iloc[0]["close"] == 20  # 保留最后一条
    assert any("重复日期" in i for i in rep.issues)


def test_non_monotonic_dates_sorted():
    df = _ohlcv(
        [(10, 11, 9, 10, 100), (10, 12, 10, 11, 100)],
        dates=["2026-01-05", "2026-01-02"],
    )
    clean, rep = validate_ohlcv(df)
    assert list(clean.index) == sorted(clean.index)
    assert any("单调" in i for i in rep.issues)


def test_spike_flagged_but_not_dropped():
    # 收盘 10 → 30（+200%），疑似拆股未复权：告警但保留（可能真跳）
    df = _ohlcv([(10, 11, 9, 10, 100), (10, 31, 10, 30, 100)])
    clean, rep = validate_ohlcv(df)
    assert len(clean) == 2  # 不丢弃
    assert rep.n_dropped == 0
    assert any("跳变" in i for i in rep.issues)


def test_empty_and_none_are_graceful():
    empty = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    clean, rep = validate_ohlcv(empty)
    assert clean.empty and rep.ok
    none_df, rep2 = validate_ohlcv(None)
    assert none_df is None and rep2.ok
