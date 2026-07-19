"""把数据层与分析模块能力封装为 Claude 可调用的 tool：schema + 执行分发。

原则（DESIGN.md §1）：tool 只取数/算数，返回客观事实（结构化 JSON），不含结论。
"""

from __future__ import annotations

import json
from datetime import datetime

import pandas as pd

from ..analysis.fundamental import FundamentalModule
from ..analysis.technical import TechnicalModule
from ..core.context import AnalysisContext
from ..core.exceptions import BellwetherError, LLMRateLimitError, RateLimitError
from ..data.base import MarketDataProvider, period_to_start
from ..ir.models import Evidence
from ..ir.recorder import ToolRecorder, ref
from ..models import OHLCVBar, OHLCVSummary

_period_to_start = period_to_start  # 向后兼容别名（tests 引用）
_TECH = TechnicalModule()
_FUND = FundamentalModule()

# ─────────────────────────── Tool schema（Anthropic 格式）───────────────────────────
TOOL_SCHEMAS = [
    {
        "name": "get_price_history",
        "description": (
            "获取某股票在给定区间的历史 K 线（OHLCV）摘要，含最近若干日与区间收益。"
            "返回客观数据，不含结论。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "股票代码，如 AAPL"},
                "period": {
                    "type": "string",
                    "description": "回溯区间，如 1mo / 6mo / 1y，默认 6mo",
                },
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "get_technical_analysis",
        "description": (
            "计算某股票的技术指标（MA20/MA50、RSI14、MACD、布林带）"
            "并给出中性的量价状态描述。返回客观数据，不含买卖建议。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "股票代码，如 AAPL"},
                "period": {
                    "type": "string",
                    "description": "回溯区间，如 3mo / 6mo / 1y，默认 6mo",
                },
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "get_fundamentals",
        "description": (
            "获取某股票的基本面分析：估值指标（PE/PB/PEG/PS/ROE/毛利率/市值）+"
            " 简版 DCF 内在价值估算（含透明假设）。返回客观数据，不含买卖建议。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "股票代码，如 AAPL"},
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "compare_peers",
        "description": (
            "对比主标的与若干同行的估值/盈利指标（PE/PB/ROE/市值）。同行代码由你依据行业知识提供。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "主标的代码，如 AAPL"},
                "peers": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": '同行代码列表，如 ["MSFT", "GOOGL"]',
                },
            },
            "required": ["symbol", "peers"],
        },
    },
    {
        "name": "get_news",
        "description": (
            "获取某股票近期新闻（标题/时间/摘要），用于识别催化剂与风险事件。"
            "返回客观新闻列表，不含结论。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "股票代码，如 AAPL"},
                "limit": {"type": "integer", "description": "最多返回条数，默认 10"},
            },
            "required": ["symbol"],
        },
    },
]


# ─────────────────────────── 执行分发 ───────────────────────────
def execute_tool(
    name: str,
    tool_input: dict,
    provider: MarketDataProvider,
    *,
    context: AnalysisContext,
    trace: ToolRecorder | None = None,
) -> str:
    """执行工具，返回作为 tool_result 内容的 JSON 字符串。异常也转成结构化错误返回。

    trace 非 None 时走证据化路径（M2-B0）：provider 响应落可寻址捕获、可上报值经
    抽取器注册为 Evidence、数值以 {v, eid} 形态返回给 LLM。trace=None 保持 M1 行为
    （portfolio 等非 LLM 路径）。technical/compare_peers 的证据化在下一批接入。
    """
    try:
        if name == "get_price_history":
            return _get_price_history(tool_input, provider, context=context, trace=trace)
        if name == "get_technical_analysis":
            return _get_technical_analysis(tool_input, provider, context=context)
        if name == "get_fundamentals":
            return _get_fundamentals(tool_input, provider, context=context, trace=trace)
        if name == "compare_peers":
            return _compare_peers(tool_input, provider, context=context)
        if name == "get_news":
            return _get_news(tool_input, provider, context=context, trace=trace)
        return json.dumps({"error": f"未知工具: {name}"}, ensure_ascii=False)
    except BellwetherError as exc:  # 类型化失败：让 LLM 知道错误种类与是否值得换参重试
        return json.dumps(
            {
                "error": str(exc),
                "error_type": type(exc).__name__,
                "retryable": isinstance(exc, (RateLimitError, LLMRateLimitError)),
            },
            ensure_ascii=False,
        )
    except Exception as exc:  # 让 LLM 看到失败原因而非直接崩溃
        return json.dumps(
            {"error": str(exc), "error_type": "unexpected", "retryable": False},
            ensure_ascii=False,
        )


def _get_price_history(
    tool_input: dict,
    provider: MarketDataProvider,
    *,
    context: AnalysisContext,
    trace: ToolRecorder | None = None,
) -> str:
    symbol = provider.resolve_symbol(tool_input["symbol"], context=context)
    period = tool_input.get("period", "6mo")
    end = context.as_of.date()
    start = period_to_start(period, end)
    df = provider.get_ohlcv(symbol, start, end, context=context)
    summary = _summarize_ohlcv(df, symbol, "1d", provider.source, context=context)
    if trace is None:
        return summary.model_dump_json()

    captured = trace.capture(
        provider_id=provider.source,
        tool_name="get_price_history",
        method="get_ohlcv",
        canonical_args={
            "symbol": symbol,
            "start": str(start),
            "end": str(end),
            "interval": "1d",
            "adjust": "default",
        },
        payload={"records": _df_records(df)},
        captured_at=_attrs_captured_at(df),
        upstream_source=df.attrs.get("upstream_source"),
        data_type="ohlcv",
    )
    # provider-default 复权视图的口径诚实标注（spec-001 §4）：CN/HK qfq、US 分红拆股复权，
    # 锚点=as_of；历史序列值不注册（可复现 qfq 待 M3 anchored 边界），仅注册摘要统计。
    basis = "qfq" if provider.market in ("CN", "HK") else "split_and_dividend_adjusted"

    def _reg(metric: str, extractor: str, kind: str = "series_stat") -> Evidence | None:
        return trace.register_value(
            captured,
            metric_name=metric,
            extractor_id=extractor,
            kind=kind,
            price_basis=basis,
            anchor_date=context.as_of.date(),
        )

    out = summary.model_dump(mode="json")
    for field_name, metric, extractor in (
        ("bars_count", "bars_count", "ohlcv.bars_count"),
        ("last_close", "last_close", "ohlcv.last_close"),
        ("period_return_pct", "period_return_pct", "ohlcv.period_return_pct"),
    ):
        evidence = _reg(metric, extractor)
        if evidence is not None:
            out[field_name] = ref(evidence)
    for metric, extractor in (
        ("period_high", "ohlcv.period_high"),
        ("period_low", "ohlcv.period_low"),
    ):
        evidence = _reg(metric, extractor)
        if evidence is not None:
            out[metric] = ref(evidence)
    return json.dumps(out, ensure_ascii=False)


def _get_technical_analysis(
    tool_input: dict, provider: MarketDataProvider, *, context: AnalysisContext
) -> str:
    symbol = provider.resolve_symbol(tool_input["symbol"], context=context)
    period = tool_input.get("period", "6mo")
    return _TECH.compute(symbol, provider, period, context=context).model_dump_json()


# 报告 metrics 键 → (raw 字段, 抽取器, unit, 是否挂币种)；与 FundamentalReport 呈现口径一致
_FUND_METRIC_MAP: dict[str, tuple[str, str, str | None, bool]] = {
    "PE": ("pe", "fund.field", None, False),
    "PB": ("pb", "fund.field", None, False),
    "PEG": ("peg", "fund.field", None, False),
    "PS": ("ps", "fund.field", None, False),
    "EPS": ("eps", "fund.field", None, True),
    "ROE(%)": ("roe", "fund.field_pct", "%", False),
    "毛利率(%)": ("gross_margins", "fund.field_pct", "%", False),
    "market_cap": ("market_cap", "fund.field", None, True),
}


def _get_fundamentals(
    tool_input: dict,
    provider: MarketDataProvider,
    *,
    context: AnalysisContext,
    trace: ToolRecorder | None = None,
) -> str:
    symbol = provider.resolve_symbol(tool_input["symbol"], context=context)
    if trace is None:
        return _FUND.compute(symbol, provider, context=context).model_dump_json()

    raw = provider.get_fundamentals(symbol, context=context)  # 单次取数，捕获与报告同源
    report = _FUND.build_report(symbol, raw, context=context)
    captured = trace.capture(
        provider_id=provider.source,
        tool_name="get_fundamentals",
        method="get_fundamentals",
        canonical_args={"symbol": symbol},
        payload=raw.model_dump(mode="json"),
        captured_at=raw.fetched_at,
        data_type="fundamentals",
    )
    currency = raw.currency if raw.currency and len(raw.currency) == 3 else None
    out = report.model_dump(mode="json")
    for metric_key, (field, extractor, unit, has_currency) in _FUND_METRIC_MAP.items():
        evidence = trace.register_value(
            captured,
            metric_name=field,
            extractor_id=extractor,
            extractor_args={"field": field},
            kind="metric",
            unit=unit,
            currency=currency if has_currency else None,
        )
        if evidence is not None:
            out["metrics"][metric_key] = ref(evidence)
    # dcf_fair_value 是 op="model" 的派生证据，随下一批（假设集证据化）接入；本批保持裸值
    return json.dumps(out, ensure_ascii=False)


def _compare_peers(
    tool_input: dict, provider: MarketDataProvider, *, context: AnalysisContext
) -> str:
    symbol = provider.resolve_symbol(tool_input["symbol"], context=context)
    peers = [provider.resolve_symbol(p, context=context) for p in tool_input.get("peers", [])]
    comparison: dict[str, dict] = {}
    for sym in [symbol, *peers]:
        try:
            d = provider.get_fundamentals(sym, context=context)
            comparison[sym] = {"PE": d.pe, "PB": d.pb, "ROE": d.roe, "market_cap": d.market_cap}
        except Exception as exc:
            comparison[sym] = {"error": str(exc)}
    return json.dumps({"target": symbol, "comparison": comparison}, ensure_ascii=False)


def _get_news(
    tool_input: dict,
    provider: MarketDataProvider,
    *,
    context: AnalysisContext,
    trace: ToolRecorder | None = None,
) -> str:
    symbol = provider.resolve_symbol(tool_input["symbol"], context=context)
    limit = int(tool_input.get("limit", 10))
    items = provider.get_news(symbol, limit, context=context)
    rows = [
        {
            "title": n.title,
            "published_at": n.published_at.isoformat() if n.published_at else None,
            "summary": n.summary,
            "url": n.url,
        }
        for n in items
    ]
    if trace is None:
        return json.dumps({"symbol": symbol, "count": len(rows), "news": rows}, ensure_ascii=False)

    captured = trace.capture(
        provider_id=provider.source,
        tool_name="get_news",
        method="get_news",
        canonical_args={"symbol": symbol, "limit": limit},
        payload={"items": rows},
        data_type="news",
    )
    out_rows = []
    for index, (item, row) in enumerate(zip(items, rows, strict=True)):
        published = item.published_at
        if published is not None and published.tzinfo is None:
            # naive 源时戳不可信（akshare 无时区）：不入 IR。observed 类可得性本就
            # 只看 first_seen_at（RFC-000 §2），tool 输出仍保留原文供 LLM 参考。
            published = None
        evidence = trace.register_value(
            captured,
            metric_name=f"news_title_{index}",
            extractor_id="news.title",
            extractor_args={"index": index},
            kind="news",
            published_at=published,
        )
        out_row: dict = dict(row)
        if evidence is not None:
            out_row["title"] = ref(evidence)
        out_rows.append(out_row)
    return json.dumps(
        {"symbol": symbol, "count": len(out_rows), "news": out_rows}, ensure_ascii=False
    )


# ─────────────────────────── 确定性辅助 ───────────────────────────
def _df_records(df: pd.DataFrame) -> list[dict]:
    """OHLCV DataFrame → 捕获 payload 的规范 records（升序，与抽取器约定一致）。"""
    return [
        {
            "date": str(idx.date()),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row["volume"]),
        }
        for idx, row in df.iterrows()
    ]


def _attrs_captured_at(df: pd.DataFrame) -> datetime | None:
    """provider 经 df.attrs 携带的真实捕获时刻（缓存命中回填原值，B8）；无则 None。"""
    raw = df.attrs.get("captured_at")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
        return parsed if parsed.tzinfo is not None else None
    except ValueError:
        return None


def _summarize_ohlcv(
    df: pd.DataFrame,
    symbol: str,
    interval: str,
    source: str,
    recent: int = 10,
    *,
    context: AnalysisContext,
) -> OHLCVSummary:
    bars = [
        OHLCVBar(
            date=str(idx.date()),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row["volume"]),
        )
        for idx, row in df.tail(recent).iterrows()
    ]
    first_close = float(df["close"].iloc[0])
    last_close = float(df["close"].iloc[-1])
    ret = (last_close / first_close - 1) * 100 if first_close else None
    return OHLCVSummary(
        symbol=symbol,
        interval=interval,
        start=str(df.index[0].date()),
        end=str(df.index[-1].date()),
        bars_count=len(df),
        last_close=last_close,
        period_return_pct=ret,
        recent_bars=bars,
        fetched_at=context.clock.now(),
        source=source,
    )
