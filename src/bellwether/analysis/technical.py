"""技术面分析模块：拿 OHLCV → 算指标（确定性）→ 组装 TechnicalReport。

不产出买卖指令，只输出客观指标快照与中性状态描述，交由 LLM 解读。
"""

from __future__ import annotations

import pandas as pd

from ..core.context import AnalysisContext
from ..data.base import MarketDataProvider, period_to_start
from ..models import TechnicalReport
from . import indicators as ind


def _last(series: pd.Series) -> float | None:
    """取序列最后一个非 NaN 值，四舍五入到 4 位；全 NaN 返回 None。"""
    valid = series.dropna()
    return round(float(valid.iloc[-1]), 4) if len(valid) else None


class TechnicalModule:
    def compute(
        self,
        symbol: str,
        provider: MarketDataProvider,
        period: str = "6mo",
        *,
        context: AnalysisContext,
    ) -> TechnicalReport:
        end = context.as_of.date()
        start = period_to_start(period, end)
        df = provider.get_ohlcv(symbol, start, end, context=context)
        return self.build_report(
            symbol, period, df, getattr(provider, "source", "unknown"), context=context
        )

    def build_report(
        self,
        symbol: str,
        period: str,
        df: pd.DataFrame,
        source: str,
        *,
        context: AnalysisContext,
    ) -> TechnicalReport:
        """以已取得的 OHLCV 组装报告（tool 证据化路径复用，避免二次取数）。"""
        close = df["close"]

        macd_line, macd_sig, macd_hist = ind.macd(close)
        boll_mid, boll_up, boll_low = ind.bollinger(close, 20)
        last_close = round(float(close.iloc[-1]), 4)

        snapshot: dict[str, float | None] = {
            "last_close": last_close,
            "MA20": _last(ind.sma(close, 20)),
            "MA50": _last(ind.sma(close, 50)),
            "RSI14": _last(ind.rsi(close, 14)),
            "MACD": _last(macd_line),
            "MACD_signal": _last(macd_sig),
            "MACD_hist": _last(macd_hist),
            "BOLL_mid": _last(boll_mid),
            "BOLL_upper": _last(boll_up),
            "BOLL_lower": _last(boll_low),
        }

        return TechnicalReport(
            symbol=symbol,
            period=period,
            last_close=last_close,
            data_asof=str(df.index[-1].date()),
            indicators=snapshot,
            signals=self._describe(last_close, snapshot, macd_hist),
            fetched_at=context.clock.now(),
            source=source,
        )

    @staticmethod
    def _describe(last_close: float, snap: dict, macd_hist: pd.Series) -> list[str]:
        """确定性规则 → 中性状态描述（客观陈述，非买卖建议）。"""
        out: list[str] = []

        ma20, ma50 = snap["MA20"], snap["MA50"]
        if ma20 is not None:
            out.append(f"收盘价位于 MA20（{ma20}）{'上方' if last_close >= ma20 else '下方'}")
        if ma20 is not None and ma50 is not None:
            out.append(
                "均线呈" + ("多头排列（MA20>MA50）" if ma20 > ma50 else "空头排列（MA20<MA50）")
            )

        rsi = snap["RSI14"]
        if rsi is not None:
            zone = "超买区（≥70）" if rsi >= 70 else "超卖区（≤30）" if rsi <= 30 else "中性区"
            out.append(f"RSI14={rsi}，处于{zone}")

        hist = macd_hist.dropna()
        if len(hist) >= 2:
            prev, curr = float(hist.iloc[-2]), float(hist.iloc[-1])
            if prev <= 0 < curr:
                out.append("MACD 柱由负转正（近期金叉）")
            elif prev >= 0 > curr:
                out.append("MACD 柱由正转负（近期死叉）")

        boll_up, boll_low = snap["BOLL_upper"], snap["BOLL_lower"]
        if boll_up is not None and last_close >= boll_up:
            out.append("收盘价触及/突破布林带上轨")
        elif boll_low is not None and last_close <= boll_low:
            out.append("收盘价触及/跌破布林带下轨")

        return out
