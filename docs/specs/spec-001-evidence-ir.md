# Spec-001: Evidence IR

> 状态: Frozen (M1) · 2026-07-17 · 变更须 ADR · 来源: RFC-000/001/002/003

## 1. Contract

`report.json` MUST validate as `StructuredReport`; terminal and Markdown output MUST
be stateless views. All datetimes MUST be UTC-aware. `snapshot_ref` is the sole
source pointer: `{YYYY-MM-DD}/{run_id}/{MARKET}/{SYMBOL}/{file}#{sha256}`; legacy
imports use `run-legacy`, and cassette uses recording date/cassette version segments.
`available_at` is producer-derived, never freely edited: authoritative = `published_at`;
observed = `max(first_seen_at, released_at?)`; replay = `None` and strict PIT excludes it.

EvidenceStore is one per session. Its single serialized allocator MUST issue monotonic,
never-reused `E1`, `E2`, ... IDs and expose `register`, `get`, and transitive `closure`.
**Stage prefixes are forbidden**: RFC-000 selects cross-stage closure rather than ID
namespacing. Thus `[E12]`, not `[analysis-E12]`, is the only reference token format.

## 2. Normative Pydantic definitions

```python
from __future__ import annotations
import re
from datetime import date
from typing import Annotated, Any, Literal
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
Eid = Annotated[str, Field(pattern=r"^E[1-9][0-9]*$")]
SnapshotRef = Annotated[str, Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}/[^/]+/[A-Z]+/[^/]+/[^#]+#[0-9a-f]{64}$")]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
PriceBasis = Literal["unadjusted", "split_adjusted", "split_adjusted_plus_action_columns", "qfq", "split_and_dividend_adjusted"]
Confidence = Literal["reported", "derived", "estimated", "stale", "missing"]
EvidenceKind = Literal["metric", "series_stat", "news", "doc_quote"]
ClaimKind = Literal["fact", "interpretation", "scenario", "risk"]
CapturePolicy = Literal["live", "cassette", "silver"]
CoverageStatus = Literal["available", "degraded", "missing"]
CoverageName = Literal["ohlcv", "fundamentals", "fundamentals_period", "news", "filings", "actions"]
_TOKEN = re.compile(r"\[E([1-9][0-9]*)\]")
def _tokens(text: str) -> list[str]: return list(dict.fromkeys(f"E{n}" for n in _TOKEN.findall(text)))
class StrictModel(BaseModel): model_config = ConfigDict(extra="forbid")
class SourceRef(StrictModel):
    provider_id: Annotated[str, Field(min_length=1)]
    provider_version: str | None = None
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
    def valid(self):
        if self.kind == "range" and (self.start is None or self.end is None): raise ValueError("range requires start/end")
        if self.start and self.end and self.start > self.end: raise ValueError("start exceeds end")
        if self.kind == "fiscal" and not self.label: raise ValueError("fiscal requires label")
        return self
class Derivation(StrictModel):
    op: Literal["add", "sub", "mul", "div", "pct_change", "ratio", "cagr", "yoy_pct"]
    inputs: list[Eid] = Field(min_length=1)
    params: dict[str, float] = Field(default_factory=dict)
    formula: Annotated[str, Field(min_length=1)]
    @model_validator(mode="after")
    def valid(self):
        if len(set(self.inputs)) != len(self.inputs): raise ValueError("inputs must be unique")
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
    source: SourceRef
    derivation: Derivation | None = None
    confidence: Confidence
    fingerprint: Sha256 | None = None
    @model_validator(mode="after")
    def pit_and_price(self):
        if self.pit_class == "authoritative" and (self.published_at is None or self.available_at != self.published_at): raise ValueError("authoritative available_at = published_at")
        if self.pit_class == "observed":
            if self.first_seen_at is None: raise ValueError("observed requires first_seen_at")
            if self.available_at != max(t for t in (self.first_seen_at, self.released_at) if t is not None): raise ValueError("bad observed available_at")
        if self.pit_class == "replay" and self.available_at is not None: raise ValueError("replay has no available_at")
        if (self.source.data_type == "ohlcv") != (self.price_basis is not None): raise ValueError("price_basis only and always for ohlcv")
        return self
class Claim(StrictModel):
    claim_id: Annotated[str, Field(min_length=1)]
    kind: ClaimKind
    text: Annotated[str, Field(min_length=1)]
    evidence_ids: list[Eid] = Field(default_factory=list)
    contains_external_facts: bool = False
    @model_validator(mode="after")
    def citations(self):
        if len(set(self.evidence_ids)) != len(self.evidence_ids) or self.evidence_ids != _tokens(self.text): raise ValueError("evidence_ids must equal unique text tokens")
        if (self.kind in {"fact", "risk"} or self.contains_external_facts) and not self.evidence_ids: raise ValueError("claim needs Evidence")
        return self
class Section(StrictModel):
    section_id: Annotated[str, Field(min_length=1)]
    title: Annotated[str, Field(min_length=1)]
    claims: list[Claim] = Field(default_factory=list)
class Scenario(StrictModel):
    name: Literal["bull", "base", "bear"]
    assumption_eids: list[Eid] = Field(default_factory=list)
    narrative: Annotated[str, Field(min_length=1)]
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
    def complete(self):
        if set(self.dims) != {"ohlcv", "fundamentals", "fundamentals_period", "news", "filings", "actions"}: raise ValueError("declare all coverage dimensions")
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
    def exact_closure(self):
        claims = [c for s in self.sections for c in s.claims] + self.risks
        if any(c.kind != "risk" for c in self.risks): raise ValueError("risks must be risk claims")
        direct = {eid for c in claims for eid in c.evidence_ids} | {eid for s in self.scenarios for eid in s.assumption_eids + _tokens(s.narrative)}
        if not direct <= set(self.evidence): raise ValueError("cited eid missing")
        closure: set[str] = set()
        def visit(eid: str) -> None:
            if eid not in closure:
                closure.add(eid)
                for parent in (self.evidence[eid].derivation.inputs if self.evidence[eid].derivation else []): visit(parent)
        for eid in direct: visit(eid)
        if set(self.evidence) != closure or any(k != v.eid for k, v in self.evidence.items()): raise ValueError("evidence must be exact closure")
        return self
```

`register` MUST allocate IDs and reject caller-supplied IDs. Derived Evidence MUST carry
`Derivation`; source Evidence MUST set it to `None`.

## 3. Rendering and verifier

- Claims/scenarios MUST use `[E12]` only; literal Arabic/Chinese numbers and quantity
  words are forbidden outside the verifier whitelist. Renderer replacement (`value +
  unit/currency`) is the final report's only numeric source.
- `verify_constructive` MUST check token existence, Claim double-write, exact closure,
  and renderer reverse mapping; retry a bad claim at most twice, then drop it and add
  one to `meta.dropped_claims`.
- Naked-number/render exemptions are exactly `ReportMeta.schema_version`, `generated_at`,
  `analysis_context`, `model_versions`, `prompt_versions`, `coverage`, `dropped_claims`,
  `cost_usd`, `provenance_ref`, and trace IDs (`session_id`, `llm_call_id`,
  `tool_call_id`, `snapshot_ref`). They NEVER apply to Claim text or scenario narrative.

## 4. Current-model mapping and M2 checklist

| Current `models.py` type | Disposition | M2 action |
|---|---|---|
| `AnalysisResult` | Obsolete report boundary | Replace quick free-text return/rendering with `StructuredReport`. |
| `OHLCVSummary`, `TechnicalReport`, `FundamentalReport`, `PortfolioReport` | Retain internal | Register each reportable value as Evidence; never emit as final report JSON. |
| `OHLCVBar`, `FundamentalData`, `NewsItem` | Retain/evolve | Add capture metadata only at Evidence conversion, not raw DTOs. |
| `TradingRules`, `ModelParams`, `ModelSpec`, `ModelConfig` | Retain | Provenance/config only; outside IR. |

M2 MUST add these models/EvidenceStore; return reportable tool values as `{v, eid}`;
build IR before rendering; replace `render_analysis`/`export_markdown`; and add seeded
R1--R6 verifier tests.
