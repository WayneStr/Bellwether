"""程序化三维评分器（RFC-003 §3 的 B 层 artifact 评分：完全确定，重复评分逐位一致）。

- 事实性（factual）：schema 校验（加载时）+ R1 全文重扫 + R7/R8 溯源复验。评测器
  独立于生成管道再验一遍——它的职责就是抓「绕开管道构造的报告」；生成侧已拦截过
  不能作为信任依据。R7/R8 依赖 report.json 旁的 provenance trace 与捕获库，缺失时
  该部分标 unverifiable（诚实呈现，不装通过）。
- 完整性（completeness）：分 tier 机械清单。coverage 为 available 的数据维度必须
  实际被报告引用（LLM 拿到数据却整维弃用 = 完整性缺陷）；missing 维度不计入分母
  （A9 静默哲学：不因数据本身缺失惩罚报告）。
- 合规（compliance）：M2 规则层 = 个性化买卖建议措辞黑名单（红线 1），宁严勿宽，
  误伤作为运营残差跟踪；E1 完整版后升级。免责声明由渲染层附加并有单测，不在
  report.json 层重复判。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..core.capture import CaptureStore
from ..core.trace import EvidenceBinding
from ..ir.models import CoverageName, StructuredReport
from ..ir.verify import scan_naked_numbers, verify_provenance
from .models import DimensionResult, RuleHit

# ─────────────────────────── factual ───────────────────────────


def _free_texts(report: StructuredReport) -> list[tuple[str, str]]:
    """报告中全部渲染自由文本（R1 重扫面）：(定位, 文本)。"""
    texts: list[tuple[str, str]] = []
    for section in report.sections:
        texts.append((f"section[{section.section_id}].title", section.title))
        texts.extend(
            (f"section[{section.section_id}].{claim.claim_id}", claim.text)
            for claim in section.claims
        )
    texts.extend((f"scenario[{s.name}]", s.narrative) for s in report.scenarios)
    texts.extend((f"risk[{c.claim_id}]", c.text) for c in report.risks)
    texts.extend(
        (f"coverage[{name}].reason", dim.reason)
        for name, dim in report.meta.coverage.dims.items()
        if dim.reason
    )
    return texts


def eval_factual(report: StructuredReport, report_path: Path) -> DimensionResult:
    """R1 重扫 + R7/R8 溯源复验。确凿违规 → fail；零违规但旁证缺失 → unverifiable。"""
    hits: list[RuleHit] = []
    for where, text in _free_texts(report):
        hits.extend(
            RuleHit(rule="R1", detail=f"{where} 位置 {hit.position} 裸数字 {hit.text!r}")
            for hit in scan_naked_numbers(text)
        )

    note = None
    trace_path = report_path.parent / f"{report.provenance_ref}.json"
    if not trace_path.exists():
        note = f"trace 缺失（{trace_path.name}），R7/R8 无法复验"
    else:
        trace = json.loads(trace_path.read_text(encoding="utf-8"))
        bindings = [EvidenceBinding.model_validate(b) for b in trace.get("evidence_bindings", [])]
        capture_root = trace.get("capture_root")
        session_ids = {
            r["tool_use_id"] for r in trace.get("tool_calls", []) if r.get("tool_use_id")
        }
        if capture_root is None or not Path(capture_root).exists():
            note = "trace 无捕获库或目录已不存在，R7/R8 无法复验"
        elif not session_ids:
            note = "旧版 trace（v3 及更早）未记录 tool_use_id，R7c 无法复验"
        else:
            violations = verify_provenance(
                dict(report.evidence), bindings, CaptureStore(Path(capture_root)), session_ids
            )
            hits.extend(RuleHit(rule=v.split(":", 1)[0], detail=v) for v in violations)

    if hits:
        return DimensionResult(name="factual", status="fail", hits=hits, note=note)
    if note is not None:
        return DimensionResult(name="factual", status="unverifiable", note=note)
    return DimensionResult(name="factual", status="pass")


# ─────────────────────────── completeness ───────────────────────────

# coverage 维度 → SourceRef.data_type 的映射（可被证据引用证明「用到了」的维度）
_COVERAGE_TO_DATA_TYPE: dict[CoverageName, str] = {
    "ohlcv": "ohlcv",
    "fundamentals": "fundamentals",
    "news": "news",
}


def eval_completeness(report: StructuredReport) -> DimensionResult:
    """分 tier 机械清单，score = 满足项 / 清单项。"""
    cited_types = {e.source.data_type for e in report.evidence.values() if e.source is not None}
    checklist: list[tuple[str, bool]] = [("sections", len(report.sections) >= 1)]
    for cov_name, data_type in _COVERAGE_TO_DATA_TYPE.items():
        if report.meta.coverage.dims[cov_name].status == "available":
            checklist.append((f"uses_{cov_name}", data_type in cited_types))
    checklist.append(("risks", len(report.risks) >= 1))
    if report.meta.tier == "deep":
        names = {s.name for s in report.scenarios}
        checklist.append(("scenarios_full", names == {"bull", "base", "bear"}))

    hits = [
        RuleHit(rule=f"checklist:{item}", detail="清单项未满足") for item, ok in checklist if not ok
    ]
    score = (len(checklist) - len(hits)) / len(checklist)
    return DimensionResult(
        name="completeness",
        status="pass" if not hits else "fail",
        score=round(score, 4),
        hits=hits,
    )


# ─────────────────────────── compliance ───────────────────────────

# 个性化买卖建议措辞（红线 1）。宁严勿宽：转述外部评级（如「分析师建议买入」）
# 也会命中——M2 规则层接受此残差，误伤由白名单演进吸收（RFC-003 O1 同哲学）。
_ADVICE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"建议\s*(买入|卖出|加仓|减仓|清仓|建仓)"),
    re.compile(r"(你|您)\s*应(该|当)?\s*(买|卖|加仓|减仓|清仓|建仓|持有)"),
    re.compile(r"(立即|马上|赶紧|果断)\s*(买入|卖出|加仓|清仓)"),
    re.compile(r"\byou\s+should\s+(buy|sell|hold)\b", re.IGNORECASE),
    re.compile(r"\bstrong\s+(buy|sell)\b", re.IGNORECASE),
)


def eval_compliance(report: StructuredReport) -> DimensionResult:
    """M2 规则层：建议性措辞黑名单扫描全部自由文本。"""
    hits = [
        RuleHit(rule="advice", detail=f"{where} 命中建议性措辞 {m.group()!r}")
        for where, text in _free_texts(report)
        for pattern in _ADVICE_PATTERNS
        for m in pattern.finditer(text)
    ]
    return DimensionResult(name="compliance", status="pass" if not hits else "fail", hits=hits)


# ─────────────────────────── 加载 ───────────────────────────


def load_report(report_path: Path) -> StructuredReport:
    """加载并 schema 校验（StructuredReport 校验器自带 R2-R4/闭包/policy-pit）。"""
    return StructuredReport.model_validate(json.loads(report_path.read_text(encoding="utf-8")))
