"""数据质量框架（M3-A4）：OHLCV 合理性校验 + 脏数据隔离告警。

对已归一化的 OHLCV（列 open/high/low/close/volume、日期索引）做 **provider 无关**的
合理性检查——脏数据静默入库会污染所有下游指标/估值/评测，必须在源头拦截。

判据：
- 价格为正、high≥low、high≥max(open,close)、low≤min(open,close)、volume≥0 → 违者**丢弃**（隔离）。
- 日期唯一、单调递增 → 去重（保留最后一条）+ 排序。
- 单日收盘跳变 > 阈值 → **仅告警不丢弃**（可能真跳、也可能拆股未复权/错价，交由下游甄别）。

不合格必记 structlog 警告（A9 教训：静默降级要有告警面）。纯函数，完全离线可测。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd
import structlog

_log = structlog.get_logger(__name__)

# 单日收盘涨跌幅超此比例视为可疑（拆股未复权/错价）；给 A股 10% 涨跌停留足余量
_MAX_DAILY_MOVE = 0.6

_PRICE_COLS = ["open", "high", "low", "close"]


@dataclass
class QualityReport:
    """一次 OHLCV 校验的结果摘要（可入 trace / 告警）。"""

    n_input: int
    n_dropped: int
    issues: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.n_dropped == 0 and not self.issues


def validate_ohlcv(
    df: pd.DataFrame, *, symbol: str = "", source: str = ""
) -> tuple[pd.DataFrame, QualityReport]:
    """校验并隔离不合格 OHLCV 行。返回 (清洗后 df, 质量报告)；None/空原样返回。"""
    if df is None or df.empty:
        return df, QualityReport(0, 0)

    n = len(df)
    issues: list[str] = []
    o, h, low_, c = (df["open"], df["high"], df["low"], df["close"])

    valid = (
        (df[_PRICE_COLS] > 0).all(axis=1)
        & (h >= low_)
        & (h >= o)
        & (h >= c)
        & (low_ <= o)
        & (low_ <= c)
    )
    if "volume" in df.columns:
        valid &= df["volume"].fillna(0) >= 0
    bad = int((~valid).sum())
    if bad:
        issues.append(f"{bad} 行 OHLC 不合理（价非正 / high<low / 越界 / 负量）")
    clean = df[valid]

    if not clean.index.is_unique:
        dup = int(clean.index.duplicated().sum())
        issues.append(f"{dup} 个重复日期")
        clean = clean[~clean.index.duplicated(keep="last")]
    if not clean.index.is_monotonic_increasing:
        issues.append("日期非单调，已排序")
        clean = clean.sort_index()

    if len(clean) >= 2:
        spikes = int((clean["close"].pct_change().abs() > _MAX_DAILY_MOVE).sum())
        if spikes:
            issues.append(f"{spikes} 处单日跳变>{_MAX_DAILY_MOVE:.0%}（疑似拆股/错价，仅告警）")

    report = QualityReport(n_input=n, n_dropped=n - len(clean), issues=issues)
    if issues:
        try:
            _log.warning(
                "ohlcv_quality",
                symbol=symbol,
                source=source,
                n_input=n,
                dropped=report.n_dropped,
                issues=issues,
            )
        except Exception:  # noqa: BLE001 —— 日志写失败（如流被关）绝不能中断数据校验
            pass
    # 保留 df.attrs（captured_at / upstream_source）——捕获与证据层依赖，布尔索引可能丢失
    if clean is not df:
        clean.attrs = dict(df.attrs)
    return clean, report
