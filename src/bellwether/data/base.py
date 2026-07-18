"""数据层抽象：MarketDataProvider 接口 + ProviderRegistry。

「多市场」的可插拔性全部落在这一个抽象上：新增市场 = 写一个子类并
@ProviderRegistry.register，上层模块零改动。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import date, timedelta
from typing import TypeVar

import pandas as pd

from ..core.circuit import CircuitBreaker
from ..core.exceptions import BellwetherError, DataUnavailableError, RateLimitError
from ..models import FundamentalData, NewsItem, TradingRules

_T = TypeVar("_T")


def classify_provider_error(exc: Exception) -> BellwetherError:
    """把数据源底层异常翻译为类型化异常（D2）：按类型决定可重试性，不做字符串匹配。

    连接/超时类（东财 RemoteDisconnected 属 ConnectionError 家族；requests 的
    ConnectionError/Timeout 也继承 OSError）→ RateLimitError 退避可重试；
    其余（空数据、解析失败如 akshare 改列名）→ DataUnavailableError 不重试。
    """
    if isinstance(exc, BellwetherError):
        return exc
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return RateLimitError(f"{type(exc).__name__}: {exc}")
    return DataUnavailableError(f"{type(exc).__name__}: {exc}")


def call_source(breaker: CircuitBreaker, fetch: Callable[[], _T]) -> _T:
    """单个数据源的守护调用：熔断 + 异常类型化。provider 内每个源都经此入口。"""
    try:
        return breaker.call(fetch)
    except BellwetherError:
        raise
    except Exception as exc:
        raise classify_provider_error(exc) from exc


def period_to_start(period: str, end: date) -> date:
    """把 '6mo' / '1y' / '10d' 之类的回溯区间解析为起始日期（默认按月）。"""
    p = period.strip().lower()
    digits = "".join(c for c in p if c.isdigit())
    n = int(digits) if digits else 6
    if p.endswith("y"):
        return end - timedelta(days=365 * n)
    if p.endswith("d"):
        return end - timedelta(days=n)
    return end - timedelta(days=30 * n)


def detect_market(symbol: str) -> str:
    """按代码形态识别市场：字母→US；6 位数字或 .SS/.SH/.SZ→CN；4-5 位数字或 .HK→HK。"""
    s = symbol.strip().upper()
    if s.endswith(".HK"):
        return "HK"
    if s.endswith((".SS", ".SH", ".SZ")):
        return "CN"
    head = s.split(".")[0]
    if head.isdigit():
        if len(head) == 6:
            return "CN"
        if len(head) in (4, 5):
            return "HK"
    return "US"


class MarketDataProvider(ABC):
    market: str = "UNKNOWN"
    source: str = "unknown"

    @abstractmethod
    def get_ohlcv(
        self,
        symbol: str,
        start: date,
        end: date,
        interval: str = "1d",
        adjust: str = "default",
    ) -> pd.DataFrame:
        """返回 OHLCV，索引为日期。

        adjust="default"：复权视图（CN/HK qfq、US auto_adjust），供分析模块使用；
        adjust="raw"：不复权原始价（A0 事实层，US 额外含 dividends/stock splits 列）。
        """

    @abstractmethod
    def get_fundamentals(self, symbol: str) -> FundamentalData: ...

    @abstractmethod
    def get_news(self, symbol: str, limit: int = 20) -> list[NewsItem]: ...

    @abstractmethod
    def trading_rules(self) -> TradingRules:
        """市场差异（时区/涨跌停/结算）封装在这里。"""

    def resolve_symbol(self, query: str) -> str:
        """名称/代码归一化，默认原样大写，子类可覆盖。"""
        return query.strip().upper()


class ProviderRegistry:
    _by_market: dict[str, type[MarketDataProvider]] = {}

    @classmethod
    def register(cls, market: str):
        def deco(provider_cls: type[MarketDataProvider]) -> type[MarketDataProvider]:
            cls._by_market[market.upper()] = provider_cls
            return provider_cls

        return deco

    @classmethod
    def for_market(cls, market: str) -> MarketDataProvider:
        key = market.upper()
        if key not in cls._by_market:
            raise KeyError(f"未注册的市场: {market!r}，已注册 {list(cls._by_market)}")
        return cls._by_market[key]()

    @classmethod
    def for_symbol(cls, symbol: str) -> MarketDataProvider:
        return cls.for_market(detect_market(symbol))
