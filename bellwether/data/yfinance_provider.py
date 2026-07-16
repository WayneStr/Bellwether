"""美股数据 provider，基于 yfinance（免费）。P1 会在此之上扩展更完整的财报字段。"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pandas as pd
import yfinance as yf

from ..models import FundamentalData, NewsItem, TradingRules
from .base import MarketDataProvider, ProviderRegistry
from .cache import DEFAULT_TTL_DAYS, cached_dataframe

_OHLCV_COLS = ["open", "high", "low", "close", "volume"]


@ProviderRegistry.register("US")
class YFinanceProvider(MarketDataProvider):
    market = "US"
    source = "yfinance"

    def get_ohlcv(
        self, symbol: str, start: date, end: date, interval: str = "1d"
    ) -> pd.DataFrame:
        key = f"ohlcv:{self.market}:{symbol}:{start}:{end}:{interval}"

        def _load() -> pd.DataFrame:
            df = yf.Ticker(symbol).history(
                start=start, end=end, interval=interval, auto_adjust=True
            )
            df = df.rename(columns=str.lower).reindex(columns=_OHLCV_COLS)
            # 去掉价格缺失的行：yfinance 尾部偶发 NaN（未收盘/停牌），否则污染 last_close 等
            df = df.dropna(subset=["open", "high", "low", "close"])
            if df.empty:
                raise ValueError(f"未取到 {symbol} 的有效行情（yfinance 返回空或全为缺失）")
            return df

        return cached_dataframe(key, DEFAULT_TTL_DAYS, _load)

    def get_fundamentals(self, symbol: str) -> FundamentalData:
        info = yf.Ticker(symbol).info or {}
        return FundamentalData(
            symbol=symbol,
            name=info.get("shortName") or info.get("longName"),
            currency=info.get("currency"),
            market_cap=info.get("marketCap"),
            pe=info.get("trailingPE"),
            pb=info.get("priceToBook"),
            eps=info.get("trailingEps"),
            revenue=info.get("totalRevenue"),
            net_income=info.get("netIncomeToCommon"),
            roe=info.get("returnOnEquity"),
            free_cashflow=info.get("freeCashflow"),
            shares_outstanding=info.get("sharesOutstanding"),
            total_debt=info.get("totalDebt"),
            total_cash=info.get("totalCash"),
            peg=info.get("trailingPegRatio") or info.get("pegRatio"),
            ps=info.get("priceToSalesTrailing12Months"),
            gross_margins=info.get("grossMargins"),
            fetched_at=datetime.now(timezone.utc),
            source=self.source,
        )

    def get_news(self, symbol: str, limit: int = 20) -> list[NewsItem]:
        raw = yf.Ticker(symbol).news or []
        items: list[NewsItem] = []
        for entry in raw[:limit]:
            # yfinance 的 news 结构随版本变化（新版嵌套在 content 下），防御性读取
            content = entry.get("content", entry)
            title = content.get("title") or entry.get("title")
            if not title:
                continue
            url = None
            canonical = content.get("canonicalUrl")
            if isinstance(canonical, dict):
                url = canonical.get("url")
            url = url or entry.get("link")
            published = None
            ts = entry.get("providerPublishTime")
            if ts:
                published = datetime.fromtimestamp(ts, tz=timezone.utc)
            items.append(
                NewsItem(title=title, url=url, published_at=published, summary=content.get("summary"))
            )
        return items

    def trading_rules(self) -> TradingRules:
        return TradingRules(
            market="US",
            timezone="America/New_York",
            has_price_limit=False,  # 无涨跌停（有市场级熔断，属不同机制）
            settlement="T+1",
        )
