"""组合/风险分析模块：多标的的相关性、组合波动率、回撤、集中度（确定性）。

只算数字、组装结构体，不调用 LLM；跨市场标的按各自 provider 取数、共同交易日对齐。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

import numpy as np
import pandas as pd

from ..data.base import MarketDataProvider, ProviderRegistry, period_to_start
from ..models import PortfolioReport

_TRADING_DAYS = 252


class PortfolioModule:
    def compute(
        self,
        symbols: list[str],
        period: str = "1y",
        weights: dict[str, float] | None = None,
        provider_for: Callable[[str], MarketDataProvider] | None = None,
    ) -> PortfolioReport:
        provider_for = provider_for or ProviderRegistry.for_symbol
        symbols = [s.strip().upper() for s in symbols]
        if len(symbols) < 2:
            raise ValueError("组合分析至少需要 2 只标的")

        end = datetime.now(timezone.utc).date()
        start = period_to_start(period, end)

        closes: dict[str, pd.Series] = {}
        for sym in symbols:
            provider = provider_for(sym)
            df = provider.get_ohlcv(provider.resolve_symbol(sym), start, end)
            closes[sym] = df["close"]

        prices = pd.DataFrame(closes).dropna()  # 共同交易日对齐
        if len(prices) < 2:
            raise ValueError("组合内标的的共同交易日不足，无法计算")
        returns = prices.pct_change().dropna()

        # 权重：默认等权，统一归一化
        raw = weights or {s: 1.0 for s in symbols}
        total_w = sum(raw[s] for s in symbols)
        w = {s: raw[s] / total_w for s in symbols}
        w_vec = np.array([w[s] for s in symbols])

        # 相关性矩阵
        corr = returns.corr().round(3)
        correlation = {a: {b: float(corr.loc[a, b]) for b in symbols} for a in symbols}

        # 组合年化波动率(%)
        port_var = float(w_vec @ returns.cov().values @ w_vec)
        ann_vol = round(float(np.sqrt(port_var * _TRADING_DAYS)) * 100, 2) if port_var > 0 else None

        # 各标的最大回撤(%) 与年化收益(%)
        max_dd: dict[str, float | None] = {}
        ann_ret: dict[str, float | None] = {}
        for s in symbols:
            p = prices[s]
            max_dd[s] = round(float(((p - p.cummax()) / p.cummax()).min()) * 100, 2)
            years = len(p) / _TRADING_DAYS
            if years > 0 and p.iloc[0] > 0:
                cagr = (float(p.iloc[-1]) / float(p.iloc[0])) ** (1 / years) - 1
                ann_ret[s] = round(cagr * 100, 2)
            else:
                ann_ret[s] = None

        return PortfolioReport(
            symbols=symbols,
            weights={s: round(w[s], 4) for s in symbols},
            period=period,
            common_days=len(prices),
            correlation=correlation,
            annualized_volatility=ann_vol,
            annualized_returns=ann_ret,
            max_drawdowns=max_dd,
            concentration_hhi=round(float(np.sum(w_vec**2)), 4),
            fetched_at=datetime.now(timezone.utc),
        )
