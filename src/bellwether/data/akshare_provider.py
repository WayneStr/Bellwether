"""A股 / 港股数据 provider，基于 akshare（免费，中文数据源）。

akshare 为可选依赖（仅分析 A股/港股时需要）：延迟 import，未安装给友好提示。
注意：akshare 数据源在国内访问最稳，国外网络可能需代理（Surge 等让相关域名走直连）。
"""

from __future__ import annotations

import os
import socket
from datetime import UTC, date, datetime

import pandas as pd

from ..core.circuit import breaker_for
from ..core.exceptions import (
    BellwetherError,
    ConfigError,
    DataUnavailableError,
    RateLimitError,
)
from ..core.retry import datasource_retry
from ..models import FundamentalData, NewsItem, TradingRules
from .base import MarketDataProvider, ProviderRegistry, call_source
from .cache import DEFAULT_TTL_DAYS, cached_dataframe

_OHLCV_COLS = ["open", "high", "low", "close", "volume"]
_CN_COL_MAP = {
    "日期": "date",
    "开盘": "open",
    "收盘": "close",
    "最高": "high",
    "最低": "low",
    "成交量": "volume",
}


def _bypass_proxy_for_eastmoney() -> None:
    """把东财加入 NO_PROXY，让国内数据源绕过系统代理（如 Surge）直连——直连最稳。

    只影响 *.eastmoney.com；Anthropic API / 中转仍走原代理，不受影响。
    """
    for var in ("NO_PROXY", "no_proxy"):
        current = os.environ.get(var, "")
        if "eastmoney.com" not in current:
            os.environ[var] = f"{current},eastmoney.com" if current else "eastmoney.com"


def _ensure_socket_timeout() -> None:
    """akshare 不暴露 HTTP timeout 参数，requests 默认无超时会挂死；socket 级默认超时
    是唯一兜底（spec-003：provider 拥有底层超时）。只在未设置时设定，不覆盖显式配置；
    httpx（anthropic SDK）自带显式超时不受此影响。"""
    if socket.getdefaulttimeout() is None:
        socket.setdefaulttimeout(60.0)


def _require_akshare():
    try:
        import akshare as ak
    except ImportError as exc:  # pragma: no cover
        raise ConfigError("分析 A股/港股需要 akshare，请先安装：uv pip install akshare") from exc
    _bypass_proxy_for_eastmoney()
    _ensure_socket_timeout()
    return ak


def _f(value) -> float | None:
    try:
        if value is None or pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_hist(df: pd.DataFrame) -> pd.DataFrame:
    """akshare 中文列 hist DataFrame → 标准 OHLCV（日期索引，丢弃价格缺失行）。"""
    if df is None or df.empty:
        raise DataUnavailableError("数据源返回空行情")
    df = df.rename(columns=_CN_COL_MAP)
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").reindex(columns=_OHLCV_COLS)
    return df.dropna(subset=["open", "high", "low", "close"])


def _load_cn_em(ak, code: str, start: date, end: date, ak_adjust: str) -> pd.DataFrame:
    """A股 东财日线（主源）。"""
    return _normalize_hist(
        ak.stock_zh_a_hist(
            symbol=code,
            period="daily",
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
            adjust=ak_adjust,
        )
    )


def _load_cn_sina(ak, code: str, start: date, end: date, ak_adjust: str) -> pd.DataFrame:
    """A股 新浪日线兜底（东财 push2his 间歇不可达时降级）：英文列，需 sh/sz 前缀。"""
    prefix = "sh" if code.startswith("6") else "sz"
    df = ak.stock_zh_a_daily(
        symbol=f"{prefix}{code}",
        start_date=start.strftime("%Y%m%d"),
        end_date=end.strftime("%Y%m%d"),
        adjust=ak_adjust,
    )
    if df is None or df.empty:
        raise DataUnavailableError("数据源返回空行情")
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").reindex(columns=_OHLCV_COLS)
    return df.dropna(subset=["open", "high", "low", "close"])


def _merge_chain_error(em_err: BellwetherError, sina_err: BellwetherError) -> BellwetherError:
    """双源都失败时合并为一个类型化异常：任一源可重试 → 整链可重试（退避后网络
    抖动可能恢复）；两侧都不可重试（空数据/熔断）→ 不重试。"""
    msg = f"东财与新浪均失败：em={em_err}; sina={sina_err}"
    if isinstance(em_err, RateLimitError) or isinstance(sina_err, RateLimitError):
        return RateLimitError(msg)
    return DataUnavailableError(msg)


def _fetch_em_news(ak, code: str, limit: int) -> list[NewsItem]:
    """东财个股新闻（A股/港股通用），解析为 NewsItem 列表。"""
    try:
        df = ak.stock_news_em(symbol=code)
    except Exception:
        return []
    if df is None or df.empty:
        return []
    items: list[NewsItem] = []
    for _, row in df.head(limit).iterrows():
        published = None
        ts = row.get("发布时间")
        if ts:
            try:
                published = pd.to_datetime(ts)
            except Exception:
                published = None
        items.append(
            NewsItem(
                title=str(row.get("新闻标题") or ""),
                url=row.get("新闻链接"),
                published_at=published,
                summary=(str(row.get("新闻内容") or "")[:200] or None),
            )
        )
    return items


@ProviderRegistry.register("CN")
class AkshareCNProvider(MarketDataProvider):
    market = "CN"
    source = "akshare"

    @staticmethod
    def _to_code(symbol: str) -> str:
        return symbol.strip().upper().split(".")[0]  # 600519.SH -> 600519

    def resolve_symbol(self, query: str) -> str:
        return query.strip().upper()

    @datasource_retry
    def get_ohlcv(
        self,
        symbol: str,
        start: date,
        end: date,
        interval: str = "1d",
        adjust: str = "default",
    ) -> pd.DataFrame:
        ak = _require_akshare()
        code = self._to_code(symbol)
        key = f"ohlcv:CN:{code}:{start}:{end}:{interval}:{adjust}"
        ak_adjust = "" if adjust == "raw" else "qfq"  # ""=不复权原始价（A0 事实层）

        def _load() -> pd.DataFrame:
            # 降级链：东财（主源，单源熔断）→ 新浪；东财熔断打开时直接走新浪不再白等。
            try:
                return call_source(
                    breaker_for("eastmoney", "kline_cn"),
                    lambda: _load_cn_em(ak, code, start, end, ak_adjust),
                )
            except BellwetherError as em_err:
                try:
                    return call_source(
                        breaker_for("sina", "kline_cn"),
                        lambda: _load_cn_sina(ak, code, start, end, ak_adjust),
                    )
                except BellwetherError as sina_err:
                    raise _merge_chain_error(em_err, sina_err) from sina_err

        return cached_dataframe(key, DEFAULT_TTL_DAYS, _load)

    def get_fundamentals(self, symbol: str) -> FundamentalData:
        ak = _require_akshare()
        code = self._to_code(symbol)
        pe = pb = ps = None
        try:
            ind = ak.stock_a_indicator_lg(symbol=code)
            if ind is not None and not ind.empty:
                last = ind.iloc[-1]
                pe = _f(last.get("pe_ttm"))
                pb = _f(last.get("pb"))
                ps = _f(last.get("ps_ttm"))
        except Exception:
            pass  # akshare 估值接口不稳，拿不到就留空
        return FundamentalData(
            symbol=symbol,
            currency="CNY",
            pe=pe,
            pb=pb,
            ps=ps,
            fetched_at=datetime.now(UTC),
            source=self.source,
        )

    def get_news(self, symbol: str, limit: int = 20) -> list[NewsItem]:
        return _fetch_em_news(_require_akshare(), self._to_code(symbol), limit)

    def trading_rules(self) -> TradingRules:
        return TradingRules(
            market="CN", timezone="Asia/Shanghai", has_price_limit=True, settlement="T+1"
        )


@ProviderRegistry.register("HK")
class AkshareHKProvider(MarketDataProvider):
    market = "HK"
    source = "akshare"

    @staticmethod
    def _to_code(symbol: str) -> str:
        return symbol.strip().upper().split(".")[0].zfill(5)  # 700 -> 00700

    def resolve_symbol(self, query: str) -> str:
        return query.strip().upper()

    @datasource_retry
    def get_ohlcv(
        self,
        symbol: str,
        start: date,
        end: date,
        interval: str = "1d",
        adjust: str = "default",
    ) -> pd.DataFrame:
        ak = _require_akshare()
        code = self._to_code(symbol)
        key = f"ohlcv:HK:{code}:{start}:{end}:{interval}:{adjust}"
        ak_adjust = "" if adjust == "raw" else "qfq"

        def _fetch() -> pd.DataFrame:
            # 东财港股接口（33.push2his）部分网络不可达，改用新浪源（英文列、返回全历史）
            df = ak.stock_hk_daily(symbol=code, adjust=ak_adjust)
            if df is None or df.empty:
                raise DataUnavailableError(f"未取到 {symbol} 的港股行情")
            df = df.copy()
            df["date"] = pd.to_datetime(df["date"])
            df = df.set_index("date").loc[str(start) : str(end)]
            return df.reindex(columns=_OHLCV_COLS).dropna(subset=["open", "high", "low", "close"])

        def _load() -> pd.DataFrame:
            return call_source(breaker_for("sina", "kline_hk"), _fetch)

        return cached_dataframe(key, DEFAULT_TTL_DAYS, _load)

    def get_fundamentals(self, symbol: str) -> FundamentalData:
        # 港股估值 akshare 接口未统一，先返回基础壳（估值待接入）
        return FundamentalData(
            symbol=symbol,
            currency="HKD",
            fetched_at=datetime.now(UTC),
            source=self.source,
        )

    def get_news(self, symbol: str, limit: int = 20) -> list[NewsItem]:
        return _fetch_em_news(_require_akshare(), self._to_code(symbol), limit)

    def trading_rules(self) -> TradingRules:
        return TradingRules(
            market="HK", timezone="Asia/Hong_Kong", has_price_limit=False, settlement="T+2"
        )
