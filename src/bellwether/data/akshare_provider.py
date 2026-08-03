"""A股 / 港股数据 provider，基于 akshare（免费，中文数据源）。

akshare 为可选依赖（仅分析 A股/港股时需要）：延迟 import，未安装给友好提示。
注意：akshare 数据源在国内访问最稳，国外网络可能需代理（Surge 等让相关域名走直连）。
"""

from __future__ import annotations

import os
import socket
from datetime import date

import pandas as pd
import structlog

from ..core.circuit import breaker_for
from ..core.context import AnalysisContext
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
from .quality import validate_ohlcv

_log = structlog.get_logger(__name__)

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


def _baidu_valuation(fetch, code: str) -> tuple[float | None, float | None, float | None]:
    """从百度股市通估值取 (pe_ttm, pb, market_cap)。A股/港股同一形态，只是 fetch 不同
    （stock_zh_valuation_baidu / stock_hk_valuation_baidu）。总市值单位为「亿」→ ×1e8 归一到原币。

    A9：逐指标独立抓取，任一失败**记 structlog 警告不静默吞**（此前 `except: pass` 让 A股/港股
    估值长期空缺却无人知）；字段留 None、报告优雅降级，绝不因估值缺失炸掉整份分析。
    """

    def one(indicator: str) -> float | None:
        try:
            df = fetch(symbol=code, indicator=indicator, period="近一年")
        except Exception as exc:  # noqa: BLE001 —— 网络/接口不稳，记警后留空
            _log.warning(
                "baidu_valuation_failed", code=code, indicator=indicator, error=str(exc)[:120]
            )
            return None
        if df is None or df.empty or "value" not in df.columns:
            _log.warning("baidu_valuation_empty", code=code, indicator=indicator)
            return None
        return _f(df.iloc[-1]["value"])

    pe, pb, mc = one("市盈率(TTM)"), one("市净率"), one("总市值")
    return pe, pb, (mc * 1e8 if mc is not None else None)


def _pct_to_frac(value: float | None) -> float | None:
    """百分数（10.06）→ 小数（0.1006）——fundamental._pct 展示时会再 ×100 还原。"""
    return value / 100 if value is not None else None


def _cn_financials(fetch, code: str, year: int) -> tuple[float | None, float | None, float | None]:
    """新浪财务分析指标 → (eps, roe, gross_margins)。取最新报告期一行；roe/毛利率 %→小数。
    失败/缺列记 structlog 警告不静默；nan 经 _f 归 None（如茅台此源无毛利率）。"""
    try:
        df = fetch(symbol=code, start_year=str(year - 1))
    except Exception as exc:  # noqa: BLE001 —— 网络/接口不稳，记警后留空
        _log.warning("cn_financials_failed", code=code, error=str(exc)[:120])
        return None, None, None
    if df is None or df.empty or "日期" not in df.columns:
        _log.warning("cn_financials_empty", code=code)
        return None, None, None
    # 选最新「年报」（12-31）：季报的 eps/roe 是单季数，与年化/trailing 口径不可比、会误导
    df = df.assign(_d=df["日期"].astype(str))
    annual = df[df["_d"].str.endswith("-12-31")]
    last = (annual if not annual.empty else df).sort_values("_d").iloc[-1]
    return (
        _f(last.get("摊薄每股收益(元)")),
        _pct_to_frac(_f(last.get("净资产收益率(%)"))),
        _pct_to_frac(_f(last.get("销售毛利率(%)"))),
    )


def _hk_financials(
    fetch, code: str
) -> tuple[float | None, float | None, float | None, float | None, float | None]:
    """东财港股财务指标（本机间歇可达）→ (eps_ttm, roe, gross_margins, revenue, net_income)。
    最新报告期在首行；roe/毛利率 %→小数。失败记警不静默、字段留 None、报告优雅降级。"""
    try:
        df = fetch(symbol=code, indicator="报告期")
    except Exception as exc:  # noqa: BLE001 —— 东财港股接口本机间歇不可达
        _log.warning("hk_financials_failed", code=code, error=str(exc)[:120])
        return None, None, None, None, None
    if df is None or df.empty or "REPORT_DATE" not in df.columns:
        _log.warning("hk_financials_empty", code=code)
        return None, None, None, None, None
    # 选最新年报（REPORT_DATE 含 12-31）：季报的 roe/营收/净利是单季数、不可比
    df = df.assign(_d=df["REPORT_DATE"].astype(str))
    annual = df[df["_d"].str.contains("-12-31")]
    last = (annual if not annual.empty else df).sort_values("_d").iloc[-1]
    return (
        _f(last.get("EPS_TTM")),
        _pct_to_frac(_f(last.get("ROE_AVG"))),
        _pct_to_frac(_f(last.get("GROSS_PROFIT_RATIO"))),
        _f(last.get("OPERATE_INCOME")),
        _f(last.get("HOLDER_PROFIT")),
    )


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

    @datasource_retry
    def get_ohlcv(
        self,
        symbol: str,
        start: date,
        end: date,
        interval: str = "1d",
        adjust: str = "default",
        *,
        context: AnalysisContext,
    ) -> pd.DataFrame:
        ak = _require_akshare()
        code = self._to_code(symbol)
        key = f"ohlcv:CN:{code}:{start}:{end}:{interval}:{adjust}"
        ak_adjust = "" if adjust == "raw" else "qfq"  # ""=不复权原始价（A0 事实层）

        def _load() -> pd.DataFrame:
            # 降级链：东财（主源，单源熔断）→ 新浪；东财熔断打开时直接走新浪不再白等。
            # df.attrs 标注实际子源与真实捕获时刻（B9/B8）：缓存命中随 pickle 回填原值。
            try:
                df = call_source(
                    breaker_for("eastmoney", "kline_cn"),
                    lambda: _load_cn_em(ak, code, start, end, ak_adjust),
                )
                df.attrs["upstream_source"] = "eastmoney"
            except BellwetherError as em_err:
                try:
                    df = call_source(
                        breaker_for("sina", "kline_cn"),
                        lambda: _load_cn_sina(ak, code, start, end, ak_adjust),
                    )
                    df.attrs["upstream_source"] = "sina"
                except BellwetherError as sina_err:
                    raise _merge_chain_error(em_err, sina_err) from sina_err
            df.attrs["captured_at"] = context.clock.now().isoformat()
            df, _ = validate_ohlcv(df, symbol=symbol, source=df.attrs.get("upstream_source", ""))
            return df

        return cached_dataframe(key, DEFAULT_TTL_DAYS, _load)

    def get_fundamentals(self, symbol: str, *, context: AnalysisContext) -> FundamentalData:
        ak = _require_akshare()
        code = self._to_code(symbol)
        pe, pb, market_cap = _baidu_valuation(ak.stock_zh_valuation_baidu, code)
        eps, roe, gross_margins = _cn_financials(
            ak.stock_financial_analysis_indicator, code, context.clock.now().year
        )
        return FundamentalData(
            symbol=symbol,
            currency="CNY",
            pe=pe,
            pb=pb,
            market_cap=market_cap,
            eps=eps,
            roe=roe,
            gross_margins=gross_margins,
            fetched_at=context.clock.now(),
            source=self.source,
        )

    def get_news(self, symbol: str, limit: int = 20, *, context: AnalysisContext) -> list[NewsItem]:
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

    @datasource_retry
    def get_ohlcv(
        self,
        symbol: str,
        start: date,
        end: date,
        interval: str = "1d",
        adjust: str = "default",
        *,
        context: AnalysisContext,
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
            df = call_source(breaker_for("sina", "kline_hk"), _fetch)
            df.attrs["upstream_source"] = "sina"
            df.attrs["captured_at"] = context.clock.now().isoformat()
            df, _ = validate_ohlcv(df, symbol=symbol, source="sina")
            return df

        return cached_dataframe(key, DEFAULT_TTL_DAYS, _load)

    def get_fundamentals(self, symbol: str, *, context: AnalysisContext) -> FundamentalData:
        ak = _require_akshare()
        code = self._to_code(symbol)
        pe, pb, market_cap = _baidu_valuation(ak.stock_hk_valuation_baidu, code)
        eps, roe, gross_margins, revenue, net_income = _hk_financials(
            ak.stock_financial_hk_analysis_indicator_em, code
        )
        return FundamentalData(
            symbol=symbol,
            currency="HKD",
            pe=pe,
            pb=pb,
            market_cap=market_cap,
            eps=eps,
            roe=roe,
            gross_margins=gross_margins,
            revenue=revenue,
            net_income=net_income,
            fetched_at=context.clock.now(),
            source=self.source,
        )

    def get_news(self, symbol: str, limit: int = 20, *, context: AnalysisContext) -> list[NewsItem]:
        return _fetch_em_news(_require_akshare(), self._to_code(symbol), limit)

    def trading_rules(self) -> TradingRules:
        return TradingRules(
            market="HK", timezone="Asia/Hong_Kong", has_price_limit=False, settlement="T+2"
        )
