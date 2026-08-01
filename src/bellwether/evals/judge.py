"""推理质量维（RFC-003 §3 D 层）：LLM 评审 + rubric，n 次均值 ± 95% CI。

评审对象是渲染后的报告文本（用户可见形态）。judge 不重复核数——数字真实性
已由 B 层程序化硬判，rubric 只评推理与呈现质量。judge 角色不降级（换评审模型
= 评测可比性破坏）。judge **调用失败**（中转挂/未按契约提交）标 unverifiable 而非
fail——那是「拿不到分」不是「质量 0」，须排除出统计，否则污染 null 与门禁。
"""

from __future__ import annotations

import statistics
from typing import Any

from ..core.costs import CostLedger
from ..core.exceptions import BellwetherError
from ..ir.models import StructuredReport
from ..ir.render import render_report
from ..models import ModelSpec
from .models import DimensionResult, RuleHit

# 双侧 95% t 临界值（df = n-1，n ≤ 10 足够：release 档 n_judge=3）
_T_95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262}

JUDGE_SYSTEM = (
    "你是股票研究报告的质量评审员。你只评估推理与呈现质量，不核对数字的真实性"
    "（数字已由程序化核查器验证，报告中形如 1710 或 47.86% 的数值可视为可信）。"
    "按以下 rubric 打一个 0-100 的综合分：\n"
    "- 逻辑连贯（35 分）：从证据到结论的推理是否成立，有无跳跃或自相矛盾；\n"
    "- 证据使用（30 分）：引用的数据是否切题、是否支撑所在论点，有无堆砌；\n"
    "- 结构清晰（20 分）：段落组织、要点归纳是否便于读者把握；\n"
    "- 风险平衡（15 分）：风险与不确定性是否与机会同等严肃地呈现。\n"
    "必须调用 submit_judgement 工具提交分数与一段简短理由，不要输出其他内容。"
)

JUDGE_TOOL = {
    "name": "submit_judgement",
    "description": "提交评审结果",
    "input_schema": {
        "type": "object",
        "properties": {
            "score": {"type": "integer", "minimum": 0, "maximum": 100},
            "rationale": {"type": "string", "description": "评分理由（三句以内）"},
        },
        "required": ["score", "rationale"],
    },
}


def _mean_ci95(scores: list[float]) -> tuple[float, tuple[float, float] | None]:
    mean = statistics.fmean(scores)
    if len(scores) < 2:
        return mean, None
    half = _T_95[len(scores) - 1] * statistics.stdev(scores) / len(scores) ** 0.5
    return mean, (round(mean - half, 2), round(mean + half, 2))


def judge_report(
    report: StructuredReport,
    *,
    llm: Any,  # ResilientLLM 协议：create(chain, **kwargs) -> (Message, ModelSpec)
    spec: ModelSpec,
    ledger: CostLedger,
    n_judge: int = 1,
) -> DimensionResult:
    """n_judge 次独立评审（互不可见），返回均值 ±95% CI 的 reasoning 维度结果。"""
    text = render_report(report)
    prompt = f"待评审报告（{report.meta.symbol} · {report.meta.tier} 档）：\n\n{text}"
    scores: list[float] = []
    rationales: list[RuleHit] = []
    for i in range(n_judge):
        try:
            resp, used = llm.create(
                [spec],
                system=JUDGE_SYSTEM,
                messages=[{"role": "user", "content": prompt}],
                tools=[JUDGE_TOOL],
                tool_choice={"type": "tool", "name": "submit_judgement"},
            )
        except BellwetherError as exc:
            return DimensionResult(
                name="reasoning",
                status="unverifiable",
                n=len(scores),
                note=f"judge 调用失败（第 {i + 1} 次）：{type(exc).__name__}: {exc}",
            )
        usage = getattr(resp, "usage", None)
        ledger.record_llm(
            used.model,
            usage.input_tokens if usage else 0,
            usage.output_tokens if usage else 0,
        )
        block = next((b for b in resp.content if getattr(b, "type", None) == "tool_use"), None)
        if block is None:
            return DimensionResult(
                name="reasoning",
                status="unverifiable",
                n=len(scores),
                note=f"judge 未按契约提交结构化评分（第 {i + 1} 次）",
            )
        score = float(min(100, max(0, int(block.input["score"]))))
        scores.append(score)
        rationale = str(block.input.get("rationale", "")).strip()
        if rationale:
            rationales.append(RuleHit(rule=f"judge[{i + 1}]", detail=rationale[:300]))

    mean, ci = _mean_ci95(scores)
    return DimensionResult(
        name="reasoning",
        status="pass",
        score=round(mean, 2),
        ci95=ci,
        n=n_judge,
        hits=rationales,
    )
