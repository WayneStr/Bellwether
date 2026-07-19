"""基本面分析模块：整理估值指标 + 简版 DCF → FundamentalReport。

不给买卖指令；DCF 用通用假设，仅作粗略参考并透明标注。
"""

from __future__ import annotations

from ..core.context import AnalysisContext
from ..data.base import MarketDataProvider
from ..models import FundamentalReport
from .valuation import DEFAULT_DCF, simple_dcf


def _pct(value: float | None) -> float | None:
    """小数比率转百分比数值（0.4786 -> 47.86），便于 LLM 无歧义解读。"""
    return round(value * 100, 2) if value is not None else None


class FundamentalModule:
    def compute(
        self, symbol: str, provider: MarketDataProvider, *, context: AnalysisContext
    ) -> FundamentalReport:
        d = provider.get_fundamentals(symbol, context=context)
        return self.build_report(symbol, d, context=context)

    def build_report(self, symbol: str, d, *, context: AnalysisContext) -> FundamentalReport:
        """以已取得的原始数据组装报告（tool 证据化路径复用，避免二次取数）。"""
        metrics: dict[str, float | None] = {
            "PE": d.pe,
            "PB": d.pb,
            "PEG": d.peg,
            "PS": d.ps,
            "EPS": d.eps,
            "ROE(%)": _pct(d.roe),
            "毛利率(%)": _pct(d.gross_margins),
            "market_cap": d.market_cap,
        }

        net_debt = 0.0
        if d.total_debt is not None or d.total_cash is not None:
            net_debt = (d.total_debt or 0.0) - (d.total_cash or 0.0)

        fair = simple_dcf(d.free_cashflow, d.shares_outstanding, net_debt=net_debt)
        note = (
            "DCF 基于通用假设，仅供粗略参考，非精确估值"
            if fair is not None
            else "DCF 未计算：缺自由现金流/股数，或假设不成立"
        )

        return FundamentalReport(
            symbol=symbol,
            name=d.name,
            metrics=metrics,
            dcf_fair_value=round(fair, 2) if fair is not None else None,
            dcf_assumptions=dict(DEFAULT_DCF),
            dcf_note=note,
            fetched_at=context.clock.now(),
            source=d.source,
        )
