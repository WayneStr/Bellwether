"""C3 门禁引擎（RFC-003 §4.2/§4.3）：按 eval/gates.yaml 对 EvalReport 判定。

三种 gate：
- zero_tolerance：B 层 per-artifact 硬判——任何一例 fail/schema 拒绝即红，无豁免。
- absolute_min：分数地板——任一例低于 floor 即红。
- paired_ci：与 baseline-of-record 逐例配对，case 聚类 bootstrap 单侧上界 < 0 即红；
  比较前提是 **版本指纹一致（B9）**——models/prompts/judge 任一失配即拒绝比较并
  要求重定基线（RFC-003 §4.1 强制重测触发），绝不拿不可比的分数下结论。

waiver 标记只是声明该维度存在人工豁免流程（PR 标签+签字、月报汇总），机器判定不变。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .models import EvalReport
from .stats import bootstrap_upper_bound, dimension_score, paired_diffs

# 与 eval/gates.yaml 逐字段同步（tests/test_gates.py 有守卫）；yaml 是治理权威（改动走 PR）。
DEFAULT_GATES: dict[str, Any] = {
    "dimensions": {
        "factual": {"gate": "zero_tolerance"},
        "compliance": {"gate": "zero_tolerance"},
        "completeness": {"gate": "absolute_min", "floor": 0.95},
        "reasoning": {"gate": "paired_ci", "waiver": True},
    },
    "composite": {"gate": "paired_ci", "waiver": False},
}


def load_gates(path: str | Path | None = None) -> dict[str, Any]:
    """读 gates 配置；未给路径或文件不存在时用内置 DEFAULT_GATES。"""
    if path is None:
        return DEFAULT_GATES
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


@dataclass(frozen=True)
class GateCheck:
    dimension: str
    gate: str
    verdict: str  # "pass" / "fail" / "skip"
    detail: str
    waivable: bool = False


@dataclass
class GateResult:
    verdict: str  # "pass" / "fail"
    checks: list[GateCheck] = field(default_factory=list)
    requires_rebaseline: bool = False
    fingerprint_diff: list[str] = field(default_factory=list)


def _fingerprint_diff(candidate: EvalReport, baseline: EvalReport) -> list[str]:
    """B9：两份评测的版本指纹差异清单；空 = 可比。指纹缺失视为失配（宁拒不猜）。"""
    diffs: list[str] = []
    if candidate.fingerprint is None or baseline.fingerprint is None:
        return ["指纹缺失（旧版评测档案）——无法证明可比性"]
    for key in ("models", "prompts", "judge"):
        if candidate.fingerprint.get(key) != baseline.fingerprint.get(key):
            diffs.append(
                f"{key}: candidate={candidate.fingerprint.get(key)!r} "
                f"baseline={baseline.fingerprint.get(key)!r}"
            )
    return diffs


def _check_zero_tolerance(candidate: EvalReport, dimension: str) -> GateCheck:
    bad = [
        Path(c.report_path).name
        for c in candidate.cases
        if c.error is not None
        or any(d.name == dimension and d.status == "fail" for d in c.dimensions)
    ]
    if bad:
        return GateCheck(
            dimension, "zero_tolerance", "fail", f"{len(bad)} 例违规：{', '.join(bad[:5])}"
        )
    return GateCheck(dimension, "zero_tolerance", "pass", "全部通过")


def _check_absolute_min(candidate: EvalReport, dimension: str, floor: float) -> GateCheck:
    scores = [
        (Path(c.report_path).name, s)
        for c in candidate.cases
        if (s := _raw_dimension_fraction(c, dimension)) is not None
    ]
    if not scores:
        return GateCheck(dimension, "absolute_min", "skip", "无可评分样本")
    below = [(name, s) for name, s in scores if s < floor]
    if below:
        worst = min(below, key=lambda x: x[1])
        return GateCheck(
            dimension,
            "absolute_min",
            "fail",
            f"{len(below)} 例低于地板 {floor}（最低 {worst[1]:.2f}：{worst[0]}）",
        )
    lowest = min(s for _, s in scores)
    return GateCheck(dimension, "absolute_min", "pass", f"最低 {lowest:.2f} ≥ {floor}")


def _raw_dimension_fraction(case: Any, dimension: str) -> float | None:
    """absolute_min 用原始比例（completeness 的 0-1 score），与 floor 同尺度。"""
    if case.error is not None:
        return 0.0
    dim = next((d for d in case.dimensions if d.name == dimension), None)
    if dim is None or dim.status in ("skip", "unverifiable"):
        return None
    if dim.score is not None:
        return dim.score
    return 1.0 if dim.status == "pass" else 0.0


def _check_paired_ci(
    candidate: EvalReport,
    baseline: EvalReport | None,
    dimension: str,
    *,
    waivable: bool,
    confidence: float,
    seed: int,
) -> GateCheck:
    gate = "paired_ci"
    if baseline is None:
        return GateCheck(dimension, gate, "skip", "未提供 baseline-of-record", waivable)
    diffs, unmatched = paired_diffs(candidate.cases, baseline.cases, dimension)
    if not diffs:
        return GateCheck(dimension, gate, "skip", f"无可配对样本（剔除 {unmatched} 例）", waivable)
    mean, upper = bootstrap_upper_bound(diffs, confidence=confidence, seed=seed)
    note = f"mean(d)={mean:+.2f}，单侧{confidence:.0%}上界={upper:+.2f}（n={len(diffs)}"
    note += f"，剔除 {unmatched} 例）" if unmatched else "）"
    if upper < 0:
        suffix = "；可走签字豁免流程（RFC-003 §4.3）" if waivable else ""
        return GateCheck(dimension, gate, "fail", f"显著退化：{note}{suffix}", waivable)
    return GateCheck(dimension, gate, "pass", note, waivable)


def evaluate_gates(
    candidate: EvalReport,
    baseline: EvalReport | None = None,
    config: dict[str, Any] | None = None,
    *,
    confidence: float = 0.95,
    seed: int = 0,
) -> GateResult:
    """按配置逐维判定。paired 维度在版本指纹失配时不判分——要求重定基线。"""
    cfg = config or DEFAULT_GATES
    result = GateResult(verdict="pass")

    if baseline is not None:
        result.fingerprint_diff = _fingerprint_diff(candidate, baseline)
        if result.fingerprint_diff:
            result.requires_rebaseline = True

    entries: list[tuple[str, dict[str, Any]]] = list(cfg.get("dimensions", {}).items())
    if "composite" in cfg:
        entries.append(("composite", cfg["composite"]))

    for dimension, spec in entries:
        gate = spec["gate"]
        waivable = bool(spec.get("waiver", False))
        if gate == "zero_tolerance":
            check = _check_zero_tolerance(candidate, dimension)
        elif gate == "absolute_min":
            check = _check_absolute_min(candidate, dimension, float(spec["floor"]))
        elif gate == "paired_ci":
            if result.requires_rebaseline:
                check = GateCheck(
                    dimension, gate, "skip", "版本指纹失配，须重定基线后再比（B9）", waivable
                )
            else:
                check = _check_paired_ci(
                    candidate,
                    baseline,
                    dimension,
                    waivable=waivable,
                    confidence=confidence,
                    seed=seed,
                )
        else:
            check = GateCheck(dimension, gate, "skip", f"未知 gate 类型 {gate!r}")
        result.checks.append(check)

    if any(c.verdict == "fail" for c in result.checks) or result.requires_rebaseline:
        result.verdict = "fail"
    return result


__all__ = [
    "DEFAULT_GATES",
    "GateCheck",
    "GateResult",
    "dimension_score",
    "evaluate_gates",
    "load_gates",
]
