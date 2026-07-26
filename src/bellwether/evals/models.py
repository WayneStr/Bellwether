"""评测结果模型（C1 产物；不属于 Evidence IR，schema 独立演进）。

确定性约定（RFC-003 §3）：`CaseResult` 与其内的维度结果不含任何时间戳——
程序化维度对同一 report.json 的重复评分必须逐位一致。`EvalReport.generated_at`
是唯一的操作性时间字段（对齐 spec-001 的操作性元数据豁免）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

EVAL_VERSION = 1

DimensionName = Literal["factual", "completeness", "compliance", "reasoning"]
# unverifiable：非确凿违规，但核验所需的旁证（trace/捕获库）缺失，无法证明也无法证伪
DimensionStatus = Literal["pass", "fail", "skip", "unverifiable"]


class RuleHit(BaseModel):
    """一次违规/缺项命中：规则名 + 定位明细（诊断与门禁呈现用）。"""

    rule: str  # "R1" / "R7" / "R8" / "checklist:<item>" / "advice"
    detail: str


class DimensionResult(BaseModel):
    name: DimensionName
    status: DimensionStatus
    score: float | None = None  # completeness ∈ [0,1]；reasoning ∈ [0,100] 均值
    ci95: tuple[float, float] | None = None  # reasoning n≥2 时的 95% t 区间
    n: int | None = None  # reasoning 的评审次数
    hits: list[RuleHit] = Field(default_factory=list)
    note: str | None = None  # skip / unverifiable 的原因说明


class CaseResult(BaseModel):
    report_path: str
    symbol: str | None = None
    market: str | None = None
    tier: str | None = None
    dimensions: list[DimensionResult] = Field(default_factory=list)
    error: str | None = None  # 加载失败 / schema 校验拒绝 → 整例确凿不合格

    @property
    def hard_fail(self) -> bool:
        """确凿不合格：schema 拒绝，或任一零容忍程序化维度 fail。

        unverifiable 不算 fail（无法证伪），由呈现层明示、C3 门禁再定语义。
        """
        if self.error is not None:
            return True
        return any(
            d.status == "fail" and d.name in ("factual", "compliance") for d in self.dimensions
        )


class EvalReport(BaseModel):
    eval_version: int = EVAL_VERSION
    generated_at: datetime  # 操作性元数据（唯一时间字段）
    n_cases: int
    cases: list[CaseResult]
    summary: dict = Field(default_factory=dict)  # 每维度聚合视图
    judge_meta: dict | None = None  # {"model":…, "n_judge":…, "cost":…}；未启用 judge 为 None
