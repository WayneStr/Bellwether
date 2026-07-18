# Spec-001: Evidence IR

> 状态: **Frozen v1.1** · 2026-07-17 (v1.0) → 2026-07-18 (v1.1, 双盲评审修订, ADR-0006) · 变更须 ADR · 来源: RFC-000/001/002/003

## 1. Contract

`report.json` MUST validate as `StructuredReport`; terminal and Markdown output MUST
be stateless views. All datetimes MUST be UTC-aware.

**Pipeline invariant (constructive guarantee closure).** Schema validity is a
*necessary but not sufficient* condition. A `report.json` MUST only be produced by
the pipeline **after** `verify_constructive` (§3) has passed; the sole render entry
point MUST consume only verified reports. Any code path that constructs a
`StructuredReport` and renders it without the verifier (tests excepted, and tests
MUST NOT ship reports) violates this contract.

**Source pointers.** For file-based snapshot captures, `snapshot_ref` is the pointer:
`{YYYY-MM-DD}/{run_id}/{MARKET}/{SYMBOL}/{file}#{sha256}`; legacy imports use
`run-legacy`, and cassette uses recording date/cassette version segments. **Live
capture:** from M2 on, the live analysis path MUST persist every tool response as an
addressable capture (`capture_id` + canonicalized bytes + sha256) in the session's
capture store referenced from the provenance trace; live-path Evidence uses
`snapshot_ref=None` and MUST be resolvable via `capture_id` (verified by R7, §3).
A report whose source Evidence cannot be resolved to real captures MUST NOT be
produced.

`available_at` is producer-derived, never freely edited: authoritative = `published_at`;
observed = `max(first_seen_at, released_at?)`; replay = `None` and strict PIT excludes it.
`captured_at` MUST be the moment the bytes were **first actually fetched** from the
producer — never the Evidence-construction time. The cache layer MUST persist the
original capture time alongside the bytes and restore it on cache hits; Evidence built
from bytes older than the freshness threshold MUST carry `confidence="stale"`.

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
    upstream_source: str | None = None  # 同 provider 降级子源（"eastmoney"/"sina"）；降级链捕获 MUST 填
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
    op: Literal["add", "sub", "mul", "div", "pct_change", "ratio", "cagr", "yoy_pct", "model"]
    inputs: list[Eid] = Field(min_length=1)
    params: dict[str, float] = Field(default_factory=dict)  # 仅确定性模块可写（§3 P12）；LLM 可调 tool 禁传
    formula: Annotated[str, Field(min_length=1)]
    @model_validator(mode="after")
    def valid(self):
        if len(set(self.inputs)) != len(self.inputs): raise ValueError("inputs must be unique")
        if self.op == "model" and not self.params: raise ValueError("model op requires params (assumption set)")
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
    anchor_date: date | None = None  # 复权锚点（spec-002 §3 落点）；qfq/split_and_dividend_adjusted 必填
    source: SourceRef | None = None  # 取数类必填；派生类必为 None（与 derivation 互斥，见校验）
    derivation: Derivation | None = None
    confidence: Confidence
    fingerprint: Sha256 | None = None
    @model_validator(mode="after")
    def origin(self):
        # 恰一来源：captured（source 非空、derivation 空）XOR derived（derivation 非空、source 空）。
        # 假设类 Evidence M2 不开放（ADR-0006 S1）；场景假设只能引用 captured/derived。
        if (self.source is None) == (self.derivation is None): raise ValueError("exactly one of source/derivation")
        return self
    @model_validator(mode="after")
    def kind_value(self):
        if self.kind in {"metric", "series_stat"} and not isinstance(self.value, float): raise ValueError("numeric kind requires float value")
        if self.kind in {"news", "doc_quote"} and not isinstance(self.value, str): raise ValueError("text kind requires str value")
        return self
    @model_validator(mode="after")
    def pit_and_price(self):
        if self.pit_class == "authoritative" and (self.published_at is None or self.available_at != self.published_at): raise ValueError("authoritative available_at = published_at")
        if self.pit_class == "observed":
            if self.first_seen_at is None: raise ValueError("observed requires first_seen_at")
            if self.available_at != max(t for t in (self.first_seen_at, self.released_at) if t is not None): raise ValueError("bad observed available_at")
        if self.pit_class == "replay" and self.available_at is not None: raise ValueError("replay has no available_at")
        if self.source is not None:
            if (self.source.data_type == "ohlcv") != (self.price_basis is not None): raise ValueError("price_basis only and always for ohlcv")
            if self.source.data_type == "news" and self.pit_class == "authoritative": raise ValueError("news is never authoritative")
        if self.price_basis in {"qfq", "split_and_dividend_adjusted"} and self.anchor_date is None: raise ValueError("adjusted price requires anchor_date")
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
    @model_validator(mode="after")
    def unique(self):
        if len(set(self.assumption_eids)) != len(self.assumption_eids): raise ValueError("assumption_eids must be unique")
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
        for ev in self.evidence.values():  # 预检：派生输入必须在册且拓扑有序（父 eid 序号 < 自身），拒环/前向引用
            for parent in (ev.derivation.inputs if ev.derivation else []):
                if parent not in self.evidence: raise ValueError("evidence must be exact closure")
                if int(parent[1:]) >= int(ev.eid[1:]): raise ValueError("derivation inputs must precede eid")
        closure: set[str] = set()
        def visit(eid: str) -> None:
            if eid not in closure:
                closure.add(eid)
                for parent in (self.evidence[eid].derivation.inputs if self.evidence[eid].derivation else []): visit(parent)
        for eid in direct: visit(eid)
        if set(self.evidence) != closure or any(k != v.eid for k, v in self.evidence.items()): raise ValueError("evidence must be exact closure")
        return self
    @model_validator(mode="after")
    def policy_pit(self):
        # capture_policy 与 pit_class 绑定（ADR-0006 S9）：cassette 全 replay；live/silver 无 replay
        policy = self.meta.analysis_context.capture_policy
        replays = [e.eid for e in self.evidence.values() if e.pit_class == "replay"]
        if policy == "cassette" and len(replays) != len(self.evidence): raise ValueError("cassette report must be all-replay")
        if policy != "cassette" and replays: raise ValueError("replay evidence requires cassette policy")
        return self
```

`register` MUST allocate IDs and reject caller-supplied IDs. Derived Evidence MUST carry
`Derivation` with `source=None`; captured Evidence MUST carry `SourceRef` with
`derivation=None` (schema-enforced XOR, ADR-0006 S1).

**Fingerprint (ADR-0006 S4).** `fingerprint = sha256` of the RFC-000 §8-canonicalized
semantic key `symbol|kind|metric-name|period|price_basis|upstream_source` — **excluding**
`value` and all timestamps, so the same fact aligns across runs even when bytes differ.
MUST be set on every Evidence in cassette-backed and eval runs; MAY be omitted on
ad-hoc live runs. Playback compares recorded vs recomputed fingerprints per eid (§3 R-P).

## 3. Rendering and verifier

**Verifier rules.** `verify_constructive` MUST run before any rendering (pipeline
invariant, §1) and MUST enforce, per report:

- **R1 naked-number scan** — Claim text, scenario narratives, and **all rendered free
  text including `coverage.*.reason`** MUST NOT contain literal Arabic/Chinese numbers
  or whitelisted-out quantity words; `[E12]` tokens are the only numeric carriers.
  *Declared residual (ADR-0006 P11):* fuzzy quantity language (翻倍/三成/由盈转亏/腰斩
  …) is NOT constructively covered; it is handled as an explicit residual — listed in
  the report boundary statement and owned by the C1 reasoning dimension + reviewer.
  This spec does not overclaim coverage of it.
- **R2 token existence** / **R3 double-write** (evidence_ids equals ordered unique text
  tokens; ordered equality is intentional — canonical order, accepted drop-rate cost)
  / **R4 exact closure** — as encoded in §2 validators.
- **R5 render reverse-mapping** — enforced **at runtime for every report** (not merely
  renderer unit tests): every numeric span in rendered output MUST map back to exactly
  one token substitution or an exempt field.
- **R6 evidence requirement** — `kind ∈ {fact, risk}` claims and any claim with
  `contains_external_facts=True` MUST cite evidence. **Segment rule (ADR-0006 P7):**
  claims in report body sections (overview/technical/valuation/events and equivalents)
  are ALL held to the fact standard regardless of self-declared `kind`;
  `kind="interpretation"` is only admissible inside explicitly-marked opinion/outlook
  sections. `contains_external_facts` may only tighten, never exempt.
- **R7 provenance resolution (ADR-0006 P2)** — every captured Evidence's pointers MUST
  resolve: `capture_id`/`snapshot_ref` to an existing entry in the capture store /
  snapshot / cassette whose bytes hash to `response_sha256`, and `tool_call_id` to this
  session's trace. Resolution failure ⇒ the citing claim is dropped.
- **R8 value faithfulness (ADR-0006 P3)** — every captured Evidence's `value` MUST be
  produced by a registered deterministic extractor from the canonicalized response, and
  the verifier MUST recompute it with the same extractor and compare. Translated or
  formatted display strings MUST NOT be written back into `value`; `doc_quote` values
  MUST be verbatim substrings of the source.

A claim failing any rule is retried at most twice, then dropped with
`meta.dropped_claims += 1`; the dropped claim's text and failure reason MUST be recorded
in the provenance trace (ADR-0006 P13).

**Rendering.**
- Renderer replacement (`value + unit/currency`) is the final report's only numeric
  source. A **single shared token-substitution renderer** with a frozen number-format
  rule set (precision, thousands separators, unit scaling, locale) backs both the rich
  terminal view and Markdown export (ADR-0006 P6); replay renders MUST be byte-stable.
- `model_versions` records the models **actually used** per role; when any differs from
  the configured primary, the renderer MUST emit a degradation banner (red line 4).
- Confidence presentation (ADR-0006 P10): `stale` Evidence triggers a report-level
  staleness banner; `estimated` Evidence MUST be presented together with its
  assumptions; `missing` belongs to `CoverageDimension.status` only and MUST NOT be
  used on Evidence.
- Coverage (ADR-0006 P8): each dimension's `status` MUST be mechanically derived from
  actually-registered Evidence for that dimension (none ⇒ `missing`, partial ⇒
  `degraded`); silent provider `except: pass` swallowing a gap violates this contract.
  `reason` is prose subject to R1 — quantified degradation details go into Evidence,
  not `reason`.
- Naked-number/render exemptions are exactly `ReportMeta.schema_version`, `generated_at`,
  `analysis_context`, `model_versions`, `prompt_versions`, `dropped_claims`, `cost_usd`,
  `provenance_ref`, trace IDs (`session_id`, `llm_call_id`, `tool_call_id`,
  `snapshot_ref`), and coverage's **structured** fields (`status`, `as_of`,
  `quality_score`) — NOT `coverage.*.reason` free text (ADR-0006 P8), and NEVER Claim
  text or scenario narrative.

**Derivation guard (ADR-0006 P12).** The M2 `derive_metric` tool signature is exactly
`(op, input_eids)` — no numeric literals. `Derivation.params` may be written only by
deterministic analysis modules (`op="model"`, e.g. DCF assumption sets); no LLM-callable
tool may pass params. Numeric-parameter assumption operations remain closed in M2
(RFC-003 O8).

**Playback guard (R-P, ADR-0006 P14).** Cassette playback MUST compare each recorded
eid's `fingerprint` against the recomputed one; any mismatch fails the run (prevents
silent token-to-fact re-binding when registration order shifts).

## 4. Current-model mapping and M2 checklist

| Current `models.py` type | Disposition | M2 action |
|---|---|---|
| `AnalysisResult` | Obsolete report boundary | Replace quick free-text return/rendering with `StructuredReport`. |
| `OHLCVSummary`, `TechnicalReport`, `FundamentalReport`, `PortfolioReport` | Retain internal | Register each reportable value as Evidence; never emit as final report JSON. |
| `OHLCVBar`, `FundamentalData`, `NewsItem` | Retain/evolve | Add capture metadata only at Evidence conversion, not raw DTOs. |
| `TradingRules`, `ModelParams`, `ModelSpec`, `ModelConfig` | Retain | Provenance/config only; outside IR. |

**Registration conventions (ADR-0006 M1).**
- Technical indicators (RSI/MACD/MA/BOLL): `kind="series_stat"`, `derivation=None`,
  `source` points at the underlying OHLCV capture (`data_type="ohlcv"`), `price_basis`
  records the adjustment basis actually used. Until spec-002 §3's anchored-qfq boundary
  lands (M3), CN/HK price Evidence SHOULD be registered on the factual basis
  (`unadjusted` + `anchor_date=None`); provider-default qfq views MUST NOT be presented
  as reproducible qfq Evidence.
- Simple DCF: a derived Evidence with `op="model"`, `inputs` = the fundamental eids
  consumed, `params` = the assumption set written by the deterministic valuation module,
  `formula` naming the model.

**Trace integration (ADR-0006 M2/M3).**
- `provenance_ref` is exactly the session `trace_id` (uuid4 hex, `core/trace.py`).
- `SourceRef.tool_call_id` is the orchestrator's Anthropic `block.id`; M2 MUST thread it
  (plus response hash and `license_tag`) into `ToolCallRecord`.
- The M1 trace-level `snapshot_ref` field is retired — superseded by per-SourceRef
  granularity.
- `canonical_request` MUST be non-empty and RFC-000 §8-canonicalized for snapshot- and
  cassette-backed captures, with time-range fields derived from `context.as_of`; it MAY
  be empty only for ad-hoc live calls that are nonetheless persisted per §1.

**M2 checklist.** Add these models/EvidenceStore; persist live captures (§1) and build
the deterministic extractor registry (R8); return reportable tool values as `{v, eid}`;
build IR before rendering; replace `render_analysis`/`export_markdown` with the single
verified-render pipeline; add seeded R1–R8 + R-P verifier tests (red-team HELD list =
seed cases, see ADR-0006).

## 5. Known limitations (v1.1, recorded per ADR-0006)

`response_sha256` vs `snapshot_ref`'s embedded hash have no equality invariant until
M3 canonicalization; `evidence_ids` ordered-equality is kept as canonical order;
`SnapshotRef` regex is intentionally loose (`{file}` may contain `/`, date segment not
calendar-validated); `ClaimKind="scenario"` keeps its enum slot but M2 uses `Scenario`
objects exclusively; price-book version rides in the provenance trace, not `ReportMeta`;
risk claims live in top-level `risks[]` by preference.
