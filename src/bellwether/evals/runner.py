"""评测运行器：发现 report.json → 逐例四维评分 → EvalReport（RFC-003 C1）。

C1 只读 report.json（RFC-003 D5：唯一事实源），永不做自由文本数字抽取。
程序化三维总是运行；推理质量维仅在显式启用 judge 时运行（花费额度）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..core.context import SystemClock
from ..core.costs import CostLedger
from ..models import ModelSpec
from .dimensions import eval_completeness, eval_compliance, eval_factual, load_report
from .judge import judge_report
from .models import CaseResult, DimensionResult, EvalReport


def discover_reports(targets: list[Path]) -> list[Path]:
    """target 为文件 → 直接采用；为目录 → 收集其下 *-report.json（按名排序）。"""
    paths: list[Path] = []
    for target in targets:
        if target.is_dir():
            paths.extend(sorted(target.glob("*-report.json")))
        else:
            paths.append(target)
    return paths


def run_eval(
    paths: list[Path],
    *,
    llm: Any | None = None,  # ResilientLLM；None = 不跑推理质量维
    judge_spec: ModelSpec | None = None,
    ledger: CostLedger | None = None,
    n_judge: int = 1,
) -> EvalReport:
    """逐例评分并聚合。generated_at 是唯一时间字段（cases 内容确定性可复现）。"""
    judging = llm is not None and judge_spec is not None
    cases: list[CaseResult] = []
    for path in paths:
        try:
            report = load_report(path)
        except Exception as exc:  # json/文件/ValidationError 统一诚实呈现
            cases.append(CaseResult(report_path=str(path), error=f"{type(exc).__name__}: {exc}"))
            continue
        dims = [eval_factual(report, path), eval_completeness(report), eval_compliance(report)]
        if judging:
            assert judge_spec is not None and ledger is not None
            dims.append(
                judge_report(report, llm=llm, spec=judge_spec, ledger=ledger, n_judge=n_judge)
            )
        else:
            dims.append(
                DimensionResult(name="reasoning", status="skip", note="未启用 judge（--judge）")
            )
        cases.append(
            CaseResult(
                report_path=str(path),
                symbol=report.meta.symbol,
                market=report.meta.market,
                tier=report.meta.tier,
                dimensions=dims,
            )
        )

    judge_meta = None
    if judging:
        assert judge_spec is not None and ledger is not None
        judge_meta = {"model": judge_spec.model, "n_judge": n_judge, "cost": ledger.summary()}
    return EvalReport(
        generated_at=SystemClock().now(),  # 操作性时间戳，经 spec-002 认可的时钟入口
        n_cases=len(cases),
        cases=cases,
        summary=_summarize(cases),
        judge_meta=judge_meta,
    )


def _summarize(cases: list[CaseResult]) -> dict:
    summary: dict[str, Any] = {}
    for name in ("factual", "completeness", "compliance", "reasoning"):
        dims = [d for c in cases for d in c.dimensions if d.name == name]
        entry: dict[str, Any] = {}
        for dim in dims:
            entry[dim.status] = entry.get(dim.status, 0) + 1
        scores = [d.score for d in dims if d.score is not None]
        if scores:
            entry["mean_score"] = round(sum(scores) / len(scores), 4)
            entry["min_score"] = min(scores)
        summary[name] = entry
    summary["error_cases"] = sum(1 for c in cases if c.error is not None)
    summary["hard_fail_cases"] = sum(1 for c in cases if c.hard_fail)
    return summary
