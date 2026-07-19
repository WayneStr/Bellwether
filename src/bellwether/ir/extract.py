"""确定性抽取器注册表（spec-001 v1.1 §3 R8 / ADR-0006 P3）。

构造性保证的承重件：Evidence.value **只能**由这里注册的确定性函数从规范化捕获
payload 产出——构建时抽取、核验时用同一函数重算比对，值与源字节机械绑定。

payload 形态约定（与 ToolRecorder.capture 的落盘对象一致）：
- ohlcv:        {"records": [{"date": "YYYY-MM-DD", "open": f, "high": f, "low": f,
                 "close": f, "volume": f}, ...]}（升序）
- fundamentals: FundamentalData.model_dump() 的 dict（datetime 已 ISO 化）
- news:         {"items": [{"title": s, "published_at": s|None, "summary": s|None,
                 "url": s|None}, ...]}
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pandas as pd

from ..analysis import indicators as ind

ExtractorFn = Callable[..., float | str]

_REGISTRY: dict[str, ExtractorFn] = {}


def register_extractor(extractor_id: str) -> Callable[[ExtractorFn], ExtractorFn]:
    def deco(fn: ExtractorFn) -> ExtractorFn:
        if extractor_id in _REGISTRY:
            raise ValueError(f"duplicate extractor: {extractor_id}")
        _REGISTRY[extractor_id] = fn
        return fn

    return deco


def run_extractor(extractor_id: str, payload: dict[str, Any], **args: Any) -> float | str:
    """按 id 执行抽取（构建与 R8 核验共用同一入口）。未注册 id 直接 KeyError。"""
    return _REGISTRY[extractor_id](payload, **args)


def known_extractors() -> list[str]:
    return sorted(_REGISTRY)


# ─────────────────────────── OHLCV ───────────────────────────
def _closes(payload: dict[str, Any]) -> list[float]:
    return [float(r["close"]) for r in payload["records"]]


@register_extractor("ohlcv.last_close")
def _last_close(payload: dict[str, Any]) -> float:
    return _closes(payload)[-1]


@register_extractor("ohlcv.first_close")
def _first_close(payload: dict[str, Any]) -> float:
    return _closes(payload)[0]


@register_extractor("ohlcv.period_return_pct")
def _period_return_pct(payload: dict[str, Any]) -> float:
    closes = _closes(payload)
    return (closes[-1] / closes[0] - 1) * 100


@register_extractor("ohlcv.bars_count")
def _bars_count(payload: dict[str, Any]) -> float:
    return float(len(payload["records"]))


@register_extractor("ohlcv.period_high")
def _period_high(payload: dict[str, Any]) -> float:
    return max(float(r["high"]) for r in payload["records"])


@register_extractor("ohlcv.period_low")
def _period_low(payload: dict[str, Any]) -> float:
    return min(float(r["low"]) for r in payload["records"])


# ─────────────────────────── OHLCV 技术指标（复用 indicators.py 纯函数）───────────────────────────
def _close_series(payload: dict[str, Any]) -> pd.Series:
    return pd.Series(_closes(payload))


def _last_or_raise(series: pd.Series) -> float:
    """序列末位非 NaN 值，4 位四舍五入（与 TechnicalModule._last 口径一致，R8 忠实重算）。

    预热窗口不足时全 NaN：raise ValueError——register_value 借此返回 None 跳过，
    不静默造值。
    """
    valid = series.dropna()
    if valid.empty:
        raise ValueError("insufficient data: all-NaN window")
    return round(float(valid.iloc[-1]), 4)


@register_extractor("ohlcv.sma_last")
def _sma_last(payload: dict[str, Any], *, window: int) -> float:
    return _last_or_raise(ind.sma(_close_series(payload), window))


@register_extractor("ohlcv.rsi_last")
def _rsi_last(payload: dict[str, Any], *, period: int = 14) -> float:
    return _last_or_raise(ind.rsi(_close_series(payload), period))


_MACD_COMPONENTS = ("macd", "signal", "hist")  # 对齐 indicators.macd 的返回顺序


@register_extractor("ohlcv.macd_last")
def _macd_last(payload: dict[str, Any], *, component: str) -> float:
    by_component = dict(zip(_MACD_COMPONENTS, ind.macd(_close_series(payload)), strict=True))
    return _last_or_raise(by_component[component])


_BOLL_BANDS = ("middle", "upper", "lower")  # 对齐 indicators.bollinger 的返回顺序


@register_extractor("ohlcv.boll_last")
def _boll_last(payload: dict[str, Any], *, band: str) -> float:
    by_band = dict(zip(_BOLL_BANDS, ind.bollinger(_close_series(payload), 20), strict=True))
    return _last_or_raise(by_band[band])


# ─────────────────────────── fundamentals ───────────────────────────
@register_extractor("fund.field")
def _fund_field(payload: dict[str, Any], *, field: str) -> float:
    value = payload[field]
    if value is None:
        raise ValueError(f"fundamentals field {field!r} is None; not registrable")
    return float(value)


@register_extractor("fund.field_pct")
def _fund_field_pct(payload: dict[str, Any], *, field: str) -> float:
    """小数比率 → 百分比数值（报告呈现口径，与 FundamentalReport 的 _pct 一致）。"""
    value = payload[field]
    if value is None:
        raise ValueError(f"fundamentals field {field!r} is None; not registrable")
    return round(float(value) * 100, 2)


# ─────────────────────────── news ───────────────────────────
@register_extractor("news.title")
def _news_title(payload: dict[str, Any], *, index: int) -> str:
    return str(payload["items"][index]["title"])
