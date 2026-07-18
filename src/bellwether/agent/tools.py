"""把数据层与分析模块能力封装为 Claude 可调用的 tool：schema + 执行分发。

原则（DESIGN.md §1）：tool 只取数/算数，返回客观事实（结构化 JSON），不含结论。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pandas as pd

from ..analysis.fundamental import FundamentalModule
from ..analysis.technical import TechnicalModule
from ..core.exceptions import BellwetherError, LLMRateLimitError, RateLimitError
from ..data.base import MarketDataProvider, period_to_start
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
def execute_tool(name: str, tool_input: dict, provider: MarketDataProvider) -> str:
    """执行工具，返回作为 tool_result 内容的 JSON 字符串。异常也转成结构化错误返回。"""
    try:
        if name == "get_price_history":
            return _get_price_history(tool_input, provider)
        if name == "get_technical_analysis":
            return _get_technical_analysis(tool_input, provider)
        if name == "get_fundamentals":
            return _get_fundamentals(tool_input, provider)
        if name == "compare_peers":
            return _compare_peers(tool_input, provider)
        if name == "get_news":
            return _get_news(tool_input, provider)
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


def _get_price_history(tool_input: dict, provider: MarketDataProvider) -> str:
    symbol = provider.resolve_symbol(tool_input["symbol"])
    period = tool_input.get("period", "6mo")
    end = datetime.now(UTC).date()
    start = period_to_start(period, end)
    df = provider.get_ohlcv(symbol, start, end)
    return _summarize_ohlcv(df, symbol, "1d", provider.source).model_dump_json()


def _get_technical_analysis(tool_input: dict, provider: MarketDataProvider) -> str:
    symbol = provider.resolve_symbol(tool_input["symbol"])
    period = tool_input.get("period", "6mo")
    return _TECH.compute(symbol, provider, period).model_dump_json()


def _get_fundamentals(tool_input: dict, provider: MarketDataProvider) -> str:
    symbol = provider.resolve_symbol(tool_input["symbol"])
    return _FUND.compute(symbol, provider).model_dump_json()


def _compare_peers(tool_input: dict, provider: MarketDataProvider) -> str:
    symbol = provider.resolve_symbol(tool_input["symbol"])
    peers = [provider.resolve_symbol(p) for p in tool_input.get("peers", [])]
    comparison: dict[str, dict] = {}
    for sym in [symbol, *peers]:
        try:
            d = provider.get_fundamentals(sym)
            comparison[sym] = {"PE": d.pe, "PB": d.pb, "ROE": d.roe, "market_cap": d.market_cap}
        except Exception as exc:
            comparison[sym] = {"error": str(exc)}
    return json.dumps({"target": symbol, "comparison": comparison}, ensure_ascii=False)


def _get_news(tool_input: dict, provider: MarketDataProvider) -> str:
    symbol = provider.resolve_symbol(tool_input["symbol"])
    limit = int(tool_input.get("limit", 10))
    items = provider.get_news(symbol, limit)
    return json.dumps(
        {
            "symbol": symbol,
            "count": len(items),
            "news": [
                {
                    "title": n.title,
                    "published_at": n.published_at.isoformat() if n.published_at else None,
                    "summary": n.summary,
                    "url": n.url,
                }
                for n in items
            ],
        },
        ensure_ascii=False,
    )


# ─────────────────────────── 确定性辅助 ───────────────────────────
def _summarize_ohlcv(
    df: pd.DataFrame, symbol: str, interval: str, source: str, recent: int = 10
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
        fetched_at=datetime.now(UTC),
        source=source,
    )
