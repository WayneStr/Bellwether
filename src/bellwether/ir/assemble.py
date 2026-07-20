"""StructuredReport 组装器（spec-001 v1.1 §3 的组装环节）。

LLM 经 submit_report 工具提交结构化草稿（claims 是**带 [E12] 令牌的纯文本**——
evidence_ids 由代码从文本机械派生，双写一致因此构造性成立，LLM 无从写错）；
本模块执行 R1（裸数字）与 R6（段落判据：主体段必须引证据）校验，通过后组装
闭包并构造 StructuredReport（schema 层自动执行 R2-R4 与策略绑定校验）。

违规不抛异常而是收集返回：orchestrator 把违规反馈给 LLM 重写；重试耗尽后走
lenient 模式——违规条目 drop（计入 dropped_claims 并记 trace），绝不带病出报告。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from ..core.context import AnalysisContext
from .models import (
    AnalysisContextRef,
    Claim,
    CoverageDimension,
    CoverageReport,
    ReportMeta,
    Scenario,
    Section,
    StructuredReport,
    _tokens,
)
from .store import EvidenceStore
from .verify import scan_naked_numbers

# 观点/展望段标记：这些段落里允许无证据的 interpretation（R6 段落判据的唯一豁免区）
_OPINION_MARKERS = ("观点", "展望", "看法", "opinion", "outlook")

# LLM 侧提交草稿的 tool schema：claims 是纯文本（令牌内嵌），结构最小化
SUBMIT_REPORT_TOOL = {
    "name": "submit_report",
    "description": (
        "提交最终结构化报告（分析完成后必须调用，不要用普通文本输出终稿）。"
        "所有数字一律以 [E12] 证据令牌书写（来自工具返回的 eid），禁止任何裸数字；"
        "观点/展望段之外的每条陈述必须至少引用一个令牌。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "sections": {
                "type": "array",
                "description": "报告主体段落（如 概览/技术面/基本面/消息面/综合研判）",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "段落标题（不含数字）"},
                        "claims": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "该段的陈述列表，数字一律用 [E12] 令牌",
                        },
                    },
                    "required": ["title", "claims"],
                },
            },
            "scenarios": {
                "type": "array",
                "description": "情景分析（bull/base/bear 各一段叙述）",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "enum": ["bull", "base", "bear"]},
                        "narrative": {"type": "string"},
                    },
                    "required": ["name", "narrative"],
                },
            },
            "risks": {
                "type": "array",
                "items": {"type": "string"},
                "description": "主要风险列表（每条必须引用 [E12] 令牌）",
            },
        },
        "required": ["sections"],
    },
}


@dataclass
class AssemblyResult:
    report: StructuredReport | None
    violations: list[str] = field(default_factory=list)
    dropped: list[dict[str, str]] = field(default_factory=list)  # {text, reason} → trace


def _check_claim_text(text: str, store: EvidenceStore) -> str | None:
    """单条陈述的 R1 + 引用存在性检查。返回违规原因或 None。"""
    if not text.strip():
        return "空白陈述"
    if scan_naked_numbers(text):
        return "含裸数字（数字必须以 [E12] 令牌引用）"
    for eid in _tokens(text):
        try:
            store.get(eid)
        except KeyError:
            return f"引用了不存在的证据 {eid}"
    return None


def _is_opinion_section(title: str) -> bool:
    lowered = title.lower()
    return any(marker in lowered for marker in _OPINION_MARKERS)


def assemble_report(
    draft: dict[str, Any],
    *,
    store: EvidenceStore,
    context: AnalysisContext,
    symbol: str,
    market: str,
    tier: str,
    model_versions: dict[str, str],
    prompt_versions: dict[str, str],
    provenance_ref: str,
    data_types_present: set[str],
    lenient: bool = False,
    cost_usd: float | None = None,
) -> AssemblyResult:
    """草稿 → StructuredReport。strict 模式违规即拒（返回违规清单给 LLM 重写）；
    lenient 模式违规条目 drop（重试耗尽后的兜底，诚实计数绝不带病输出）。"""
    violations: list[str] = []
    dropped: list[dict[str, str]] = []

    def _reject(text: str, reason: str, where: str) -> None:
        if lenient:
            dropped.append({"text": text, "reason": reason})
        else:
            violations.append(f"{where}: {reason} — {text[:80]!r}")

    sections: list[Section] = []
    for s_idx, raw_section in enumerate(draft.get("sections") or []):
        title = str(raw_section.get("title") or "").strip()
        if not title or scan_naked_numbers(title):
            _reject(title or "(无标题)", "段落标题为空或含裸数字", f"sections[{s_idx}]")
            continue
        opinion = _is_opinion_section(title)
        claims: list[Claim] = []
        for c_idx, text in enumerate(raw_section.get("claims") or []):
            text = str(text)
            where = f"sections[{s_idx}].claims[{c_idx}]"
            reason = _check_claim_text(text, store)
            if reason is None and not opinion and not _tokens(text):
                reason = "主体段陈述必须至少引用一个证据令牌（R6 段落判据）"
            if reason is not None:
                _reject(text, reason, where)
                continue
            claims.append(
                Claim(
                    claim_id=f"s{s_idx}-c{c_idx}",
                    kind="interpretation" if opinion and not _tokens(text) else "fact",
                    text=text,
                    evidence_ids=_tokens(text),
                )
            )
        if claims:
            sections.append(Section(section_id=f"s{s_idx}", title=title, claims=claims))

    scenarios: list[Scenario] = []
    for name in ("bull", "base", "bear"):
        raw = next(
            (s for s in draft.get("scenarios") or [] if s.get("name") == name),
            None,
        )
        if raw is None:
            continue
        narrative = str(raw.get("narrative") or "")
        reason = _check_claim_text(narrative, store)
        if reason is not None:
            _reject(narrative, reason, f"scenarios[{name}]")
            continue
        scenarios.append(
            Scenario(name=name, assumption_eids=_tokens(narrative), narrative=narrative)
        )

    risks: list[Claim] = []
    for r_idx, text in enumerate(draft.get("risks") or []):
        text = str(text)
        where = f"risks[{r_idx}]"
        reason = _check_claim_text(text, store)
        if reason is None and not _tokens(text):
            reason = "风险条目必须引用证据令牌"
        if reason is not None:
            _reject(text, reason, where)
            continue
        risks.append(
            Claim(claim_id=f"r{r_idx}", kind="risk", text=text, evidence_ids=_tokens(text))
        )

    if violations:
        return AssemblyResult(report=None, violations=violations, dropped=dropped)
    if not sections:
        return AssemblyResult(
            report=None,
            violations=["报告没有任何合法段落（sections 为空或全部被拒）"],
            dropped=dropped,
        )

    cited = {eid for s in sections for c in s.claims for eid in c.evidence_ids}
    cited |= {eid for s in scenarios for eid in s.assumption_eids}
    cited |= {eid for c in risks for eid in c.evidence_ids}
    evidence = store.closure(sorted(cited))

    meta = ReportMeta(
        symbol=symbol,
        market=market,  # type: ignore[arg-type]
        tier=tier,  # type: ignore[arg-type]
        generated_at=context.clock.now(),
        analysis_context=AnalysisContextRef(
            as_of=context.as_of, capture_policy=context.capture_policy
        ),
        model_versions=model_versions,
        prompt_versions=prompt_versions,
        coverage=derive_coverage(symbol, market, context, data_types_present),
        dropped_claims=len(dropped),
        cost_usd=cost_usd,
    )
    try:
        report = StructuredReport(
            meta=meta,
            sections=sections,
            scenarios=scenarios,
            risks=risks,
            evidence=evidence,
            provenance_ref=provenance_ref,
        )
    except ValidationError as exc:
        return AssemblyResult(report=None, violations=[f"schema 校验失败：{exc}"], dropped=dropped)
    return AssemblyResult(report=report, violations=[], dropped=dropped)


def derive_coverage(
    symbol: str, market: str, context: AnalysisContext, data_types_present: set[str]
) -> CoverageReport:
    """coverage 机械推导（ADR-0006 P8）：status 由实际注册证据决定，reason 无数字。"""

    def dim(name: str) -> CoverageDimension:
        if name in data_types_present:
            return CoverageDimension(status="available", as_of=context.as_of)
        return CoverageDimension(status="missing", reason="该维度无已注册证据")

    return CoverageReport(
        market=market,  # type: ignore[arg-type]
        symbol=symbol,
        as_of=context.as_of,
        dims={
            "ohlcv": dim("ohlcv"),
            "fundamentals": dim("fundamentals"),
            "news": dim("news"),
            # M2 数据面尚无以下三维（fundamentals_period 期次/filings 公告/actions 公司行动）
            "fundamentals_period": CoverageDimension(
                status="missing", reason="数据面尚未接入（后续里程碑）"
            ),
            "filings": CoverageDimension(status="missing", reason="数据面尚未接入（后续里程碑）"),
            "actions": CoverageDimension(status="missing", reason="数据面尚未接入（后续里程碑）"),
        },
    )
