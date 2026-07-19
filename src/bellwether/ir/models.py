"""Evidence IR 规范模型 —— spec-001 Frozen v1.1 §2 的实现。

语义与 spec-001 §2 的规范 Pydantic 代码块逐字对应（tests/test_ir_models.py 的
spec 同步守卫负责报警漂移），仅按仓库 lint 规范展开了格式。变更须 ADR。
"""

from __future__ import annotations

import re
from datetime import date
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

Eid = Annotated[str, Field(pattern=r"^E[1-9][0-9]*$")]
SnapshotRef = Annotated[
    str,
    Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}/[^/]+/[A-Z]+/[^/]+/[^#]+#[0-9a-f]{64}$"),
]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
PriceBasis = Literal[
    "unadjusted",
    "split_adjusted",
    "split_adjusted_plus_action_columns",
    "qfq",
    "split_and_dividend_adjusted",
]
Confidence = Literal["reported", "derived", "estimated", "stale", "missing"]
EvidenceKind = Literal["metric", "series_stat", "news", "doc_quote"]
ClaimKind = Literal["fact", "interpretation", "scenario", "risk"]
CapturePolicy = Literal["live", "cassette", "silver"]
CoverageStatus = Literal["available", "degraded", "missing"]
CoverageName = Literal["ohlcv", "fundamentals", "fundamentals_period", "news", "filings", "actions"]

_TOKEN = re.compile(r"\[E([1-9][0-9]*)\]")


def _tokens(text: str) -> list[str]:
    """按首现顺序提取去重的 [E12] 令牌（R3 双写一致的文本侧）。"""
    return list(dict.fromkeys(f"E{n}" for n in _TOKEN.findall(text)))


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceRef(StrictModel):
    provider_id: Annotated[str, Field(min_length=1)]
    provider_version: str | None = None
    # 同 provider 降级子源（"eastmoney"/"sina"）；降级链捕获必填（ADR-0006 S2）
    upstream_source: str | None = None
    tool_name: Annotated[str, Field(min_length=1)]
    tool_call_id: Annotated[str, Field(min_length=1)]
    data_type: Literal["ohlcv", "fundamentals", "news", "filing", "derived"]
    capture_id: Annotated[str, Field(min_length=1)]
    canonical_request: dict[str, Any] = Field(default_factory=dict)
    response_sha256: Sha256
    captured_at: AwareDatetime
    license_tag: Annotated[str, Field(min_length=1)]
    url: str | None = None
    snapshot_ref: SnapshotRef | None = None


class Period(StrictModel):
    kind: Literal["instant", "range", "fiscal"]
    start: date | None = None
    end: date | None = None
    label: str | None = None

    @model_validator(mode="after")
    def valid(self) -> Period:
        if self.kind == "range" and (self.start is None or self.end is None):
            raise ValueError("range requires start/end")
        if self.start and self.end and self.start > self.end:
            raise ValueError("start exceeds end")
        if self.kind == "fiscal" and not self.label:
            raise ValueError("fiscal requires label")
        return self


class Derivation(StrictModel):
    op: Literal["add", "sub", "mul", "div", "pct_change", "ratio", "cagr", "yoy_pct", "model"]
    inputs: list[Eid] = Field(min_length=1)
    # 仅确定性模块可写（spec-001 §3 P12）；LLM 可调 tool 禁传
    params: dict[str, float] = Field(default_factory=dict)
    formula: Annotated[str, Field(min_length=1)]

    @model_validator(mode="after")
    def valid(self) -> Derivation:
        if len(set(self.inputs)) != len(self.inputs):
            raise ValueError("inputs must be unique")
        if self.op == "model" and not self.params:
            raise ValueError("model op requires params (assumption set)")
        return self


class Evidence(StrictModel):
    eid: Eid
    kind: EvidenceKind
    value: float | str
    unit: str | None = None
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")] | None = None
    period: Period | None = None
    valid_at: AwareDatetime | None = None
    published_at: AwareDatetime | None = None
    first_seen_at: AwareDatetime | None = None
    released_at: AwareDatetime | None = None
    available_at: AwareDatetime | None = None
    pit_class: Literal["authoritative", "observed", "replay"]
    price_basis: PriceBasis | None = None
    # 复权锚点（spec-002 §3 落点）；qfq / split_and_dividend_adjusted 必填（ADR-0006 S3）
    anchor_date: date | None = None
    # 取数类必填；派生类必为 None（与 derivation 互斥 XOR，ADR-0006 S1）
    source: SourceRef | None = None
    derivation: Derivation | None = None
    confidence: Confidence
    fingerprint: Sha256 | None = None

    @model_validator(mode="after")
    def origin(self) -> Evidence:
        # 恰一来源：captured（source 非空）XOR derived（derivation 非空）。
        # 假设类 Evidence M2 不开放；场景假设只能引用 captured/derived（ADR-0006 S1）。
        if (self.source is None) == (self.derivation is None):
            raise ValueError("exactly one of source/derivation")
        return self

    @model_validator(mode="after")
    def kind_value(self) -> Evidence:
        if self.kind in {"metric", "series_stat"} and not isinstance(self.value, float):
            raise ValueError("numeric kind requires float value")
        if self.kind in {"news", "doc_quote"} and not isinstance(self.value, str):
            raise ValueError("text kind requires str value")
        return self

    @model_validator(mode="after")
    def pit_and_price(self) -> Evidence:
        if self.pit_class == "authoritative" and (
            self.published_at is None or self.available_at != self.published_at
        ):
            raise ValueError("authoritative available_at = published_at")
        if self.pit_class == "observed":
            if self.first_seen_at is None:
                raise ValueError("observed requires first_seen_at")
            if self.available_at != max(
                t for t in (self.first_seen_at, self.released_at) if t is not None
            ):
                raise ValueError("bad observed available_at")
        if self.pit_class == "replay" and self.available_at is not None:
            raise ValueError("replay has no available_at")
        if self.source is not None:
            if (self.source.data_type == "ohlcv") != (self.price_basis is not None):
                raise ValueError("price_basis only and always for ohlcv")
            if self.source.data_type == "news" and self.pit_class == "authoritative":
                raise ValueError("news is never authoritative")
        if self.price_basis in {"qfq", "split_and_dividend_adjusted"} and self.anchor_date is None:
            raise ValueError("adjusted price requires anchor_date")
        return self


class Claim(StrictModel):
    claim_id: Annotated[str, Field(min_length=1)]
    kind: ClaimKind
    text: Annotated[str, Field(min_length=1)]
    evidence_ids: list[Eid] = Field(default_factory=list)
    contains_external_facts: bool = False

    @model_validator(mode="after")
    def citations(self) -> Claim:
        if len(set(self.evidence_ids)) != len(self.evidence_ids) or self.evidence_ids != _tokens(
            self.text
        ):
            raise ValueError("evidence_ids must equal unique text tokens")
        if (
            self.kind in {"fact", "risk"} or self.contains_external_facts
        ) and not self.evidence_ids:
            raise ValueError("claim needs Evidence")
        return self


class Section(StrictModel):
    section_id: Annotated[str, Field(min_length=1)]
    title: Annotated[str, Field(min_length=1)]
    claims: list[Claim] = Field(default_factory=list)


class Scenario(StrictModel):
    name: Literal["bull", "base", "bear"]
    assumption_eids: list[Eid] = Field(default_factory=list)
    narrative: Annotated[str, Field(min_length=1)]

    @model_validator(mode="after")
    def unique(self) -> Scenario:
        if len(set(self.assumption_eids)) != len(self.assumption_eids):
            raise ValueError("assumption_eids must be unique")
        return self


class CoverageDimension(StrictModel):
    status: CoverageStatus
    as_of: AwareDatetime | None = None
    quality_score: Annotated[float, Field(ge=0, le=100)] | None = None
    reason: str | None = None


class CoverageReport(StrictModel):
    market: Literal["US", "CN", "HK"]
    symbol: Annotated[str, Field(min_length=1)]
    as_of: AwareDatetime
    dims: dict[CoverageName, CoverageDimension]

    @model_validator(mode="after")
    def complete(self) -> CoverageReport:
        if set(self.dims) != {
            "ohlcv",
            "fundamentals",
            "fundamentals_period",
            "news",
            "filings",
            "actions",
        }:
            raise ValueError("declare all coverage dimensions")
        return self


class AnalysisContextRef(StrictModel):
    as_of: AwareDatetime
    capture_policy: CapturePolicy


class ReportMeta(StrictModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    symbol: Annotated[str, Field(min_length=1)]
    market: Literal["US", "CN", "HK"]
    tier: Literal["quick", "deep"]
    generated_at: AwareDatetime
    analysis_context: AnalysisContextRef
    model_versions: dict[str, str] = Field(default_factory=dict)
    prompt_versions: dict[str, str] = Field(default_factory=dict)
    coverage: CoverageReport
    dropped_claims: Annotated[int, Field(ge=0)] = 0
    cost_usd: Annotated[float, Field(ge=0)] | None = None


class StructuredReport(StrictModel):
    meta: ReportMeta
    sections: list[Section] = Field(default_factory=list)
    scenarios: list[Scenario] = Field(default_factory=list)
    risks: list[Claim] = Field(default_factory=list)
    evidence: dict[Eid, Evidence]
    provenance_ref: Annotated[str, Field(min_length=1)]

    @model_validator(mode="after")
    def exact_closure(self) -> StructuredReport:
        claims = [c for s in self.sections for c in s.claims] + self.risks
        if any(c.kind != "risk" for c in self.risks):
            raise ValueError("risks must be risk claims")
        direct = {eid for c in claims for eid in c.evidence_ids} | {
            eid for s in self.scenarios for eid in s.assumption_eids + _tokens(s.narrative)
        }
        if not direct <= set(self.evidence):
            raise ValueError("cited eid missing")
        # 预检：派生输入必须在册且拓扑有序（父 eid 序号 < 自身），拒环/前向引用（ADR-0006 S7）
        for ev in self.evidence.values():
            for parent in ev.derivation.inputs if ev.derivation else []:
                if parent not in self.evidence:
                    raise ValueError("evidence must be exact closure")
                if int(parent[1:]) >= int(ev.eid[1:]):
                    raise ValueError("derivation inputs must precede eid")
        closure: set[str] = set()

        def visit(eid: str) -> None:
            if eid not in closure:
                closure.add(eid)
                ev = self.evidence[eid]
                for parent in ev.derivation.inputs if ev.derivation else []:
                    visit(parent)

        for eid in direct:
            visit(eid)
        if set(self.evidence) != closure or any(k != v.eid for k, v in self.evidence.items()):
            raise ValueError("evidence must be exact closure")
        return self

    @model_validator(mode="after")
    def policy_pit(self) -> StructuredReport:
        # capture_policy 与 pit_class 绑定（ADR-0006 S9）：cassette 全 replay；live/silver 无 replay
        policy = self.meta.analysis_context.capture_policy
        replays = [e.eid for e in self.evidence.values() if e.pit_class == "replay"]
        if policy == "cassette" and len(replays) != len(self.evidence):
            raise ValueError("cassette report must be all-replay")
        if policy != "cassette" and replays:
            raise ValueError("replay evidence requires cassette policy")
        return self
