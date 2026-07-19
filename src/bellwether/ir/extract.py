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
