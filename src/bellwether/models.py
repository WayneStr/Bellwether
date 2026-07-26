"""所有 Pydantic 数据结构的集中定义。

分三类：
1. 模型配置（ModelParams/ModelSpec/ModelConfig）—— 模型可配置的核心。
2. 数据层结构 —— provider 产出的原始数据。
3. 输出结构 —— agent 综合后的分析结果。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ────────────────────────────── 1. 模型配置 ──────────────────────────────
class ModelParams(BaseModel):
    """单个模型调用的采样参数。extra 透传 SDK 其他参数，保持向前兼容。"""

    temperature: float = 0.3
    max_tokens: int = 4096
    extra: dict[str, Any] = Field(default_factory=dict)


class ModelSpec(BaseModel):
    """一个「模型 id + 参数」的组合。model 是自由字符串，从不硬编码在逻辑里。"""

    model: str
    params: ModelParams = Field(default_factory=ModelParams)


class ModelConfig(BaseModel):
    """角色化模型配置。任何调用 LLM 的地方都必须经 ModelRouter 从这里解析。

    config.toml 的 [models.<role>] 段直接映射到这些字段，加载即校验。
    """

    parse: ModelSpec = Field(
        default_factory=lambda: ModelSpec(
            model="claude-haiku-4-5-20251001",
            params=ModelParams(temperature=0.0, max_tokens=2048),
        )
    )
    synthesis: ModelSpec = Field(
        default_factory=lambda: ModelSpec(
            model="claude-sonnet-5",
            params=ModelParams(temperature=0.2, max_tokens=4096),
        )
    )
    deep_report: ModelSpec = Field(
        default_factory=lambda: ModelSpec(
            model="claude-opus-4-8",
            params=ModelParams(temperature=0.3, max_tokens=8192),
        )
    )
    judge: ModelSpec = Field(
        default_factory=lambda: ModelSpec(
            model="claude-haiku-4-5-20251001",
            params=ModelParams(temperature=0.0, max_tokens=1024),
        )
    )


# ────────────────────────────── 2. 数据层 ──────────────────────────────
class TradingRules(BaseModel):
    """市场差异（时区/涨跌停/结算）封装在这里，上层模块对市场无感知。"""

    market: str
    timezone: str
    has_price_limit: bool
    settlement: str  # 如 "T+0" / "T+1"


class OHLCVBar(BaseModel):
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float


class OHLCVSummary(BaseModel):
    """K 线的可序列化摘要（喂给 LLM 的事实，不含结论）。"""

    symbol: str
    interval: str
    start: str
    end: str
    bars_count: int
    last_close: float
    period_return_pct: float | None = None
    recent_bars: list[OHLCVBar] = Field(default_factory=list)
    fetched_at: datetime
    source: str


class FundamentalData(BaseModel):
    symbol: str
    name: str | None = None
    currency: str | None = None
    market_cap: float | None = None
    pe: float | None = None
    pb: float | None = None
    eps: float | None = None
    revenue: float | None = None
    net_income: float | None = None
    roe: float | None = None
    free_cashflow: float | None = None
    shares_outstanding: float | None = None
    total_debt: float | None = None
    total_cash: float | None = None
    peg: float | None = None
    ps: float | None = None
    gross_margins: float | None = None
    fetched_at: datetime
    source: str


class NewsItem(BaseModel):
    title: str
    url: str | None = None
    published_at: datetime | None = None
    summary: str | None = None


# ────────────────────────────── 3. 输出 ──────────────────────────────
class AnalysisResult(BaseModel):
    """agent 综合后的分析结果。P0 主要用 verdict，其余字段随模块完善逐步填充。"""

    symbol: str
    verdict: str
    evidence: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    data_asof: datetime | None = None
    disclaimer: str = ""


class TechnicalReport(BaseModel):
    """技术面分析模块的结构化输出（确定性指标 + 中性状态描述）。"""

    symbol: str
    period: str
    last_close: float
    data_asof: str  # 最后一根 K 线日期
    indicators: dict[str, float | None]  # 最新指标快照 {"RSI14": 62.3, "MACD": ...}
    signals: list[str]  # 确定性规则识别的中性状态描述，非买卖指令
    fetched_at: datetime
    source: str


class FundamentalReport(BaseModel):
    """基本面分析模块的结构化输出（估值指标 + 简版 DCF）。"""

    symbol: str
    name: str | None = None
    metrics: dict[str, float | None]  # PE/PB/PEG/PS/EPS/ROE/毛利率/市值
    dcf_fair_value: float | None  # 简版 DCF 每股内在价值
    dcf_assumptions: dict[str, float]  # DCF 假设，透明可审
    dcf_note: str | None = None  # 数据缺失/口径说明
    fetched_at: datetime
    source: str


class PortfolioReport(BaseModel):
    """组合/风险分析的结构化输出（确定性指标）。"""

    symbols: list[str]
    weights: dict[str, float]  # 各标的权重（归一化）
    period: str
    common_days: int  # 参与计算的共同交易日数
    correlation: dict[str, dict[str, float]]  # 相关性矩阵
    annualized_volatility: float | None  # 组合年化波动率(%)
    annualized_returns: dict[str, float | None]  # 各标的年化收益(%)
    max_drawdowns: dict[str, float | None]  # 各标的最大回撤(%)
    concentration_hhi: float  # 赫芬达尔集中度指数
    fetched_at: datetime
