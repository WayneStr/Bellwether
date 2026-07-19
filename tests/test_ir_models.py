"""Evidence IR 模型行为单测（spec-001 v1.1 §2 校验器）+ spec 同步守卫。"""

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bellwether.ir.models import (
    AnalysisContextRef,
    Claim,
    CoverageDimension,
    CoverageReport,
    Derivation,
    Evidence,
    ReportMeta,
    Section,
    SourceRef,
    StructuredReport,
)

NOW = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)


def _source(**overrides):
    base = dict(
        provider_id="akshare",
        upstream_source="sina",
        tool_name="get_price_history",
        tool_call_id="tc1",
        data_type="ohlcv",
        capture_id="c1",
        response_sha256="a" * 64,
        captured_at=NOW,
        license_tag="private-ok-backup-ok",
    )
    base.update(overrides)
    return SourceRef(**base)


def _captured(**overrides):
    base = dict(
        eid="E1",
        kind="metric",
        value=1710.0,
        pit_class="observed",
        first_seen_at=NOW,
        available_at=NOW,
        price_basis="unadjusted",
        source=_source(),
        confidence="reported",
    )
    base.update(overrides)
    return Evidence(**base)


def _report(evidence, policy="live", claim_text="收盘价为 [E1]", eids=None):
    coverage = CoverageReport(
        market="CN",
        symbol="600519",
        as_of=NOW,
        dims={
            k: CoverageDimension(status="missing")
            for k in ["ohlcv", "fundamentals", "fundamentals_period", "news", "filings", "actions"]
        },
    )
    meta = ReportMeta(
        symbol="600519",
        market="CN",
        tier="quick",
        generated_at=NOW,
        analysis_context=AnalysisContextRef(as_of=NOW, capture_policy=policy),
        coverage=coverage,
    )
    claim = Claim(claim_id="c1", kind="fact", text=claim_text, evidence_ids=eids or ["E1"])
    return StructuredReport(
        meta=meta,
        sections=[Section(section_id="s1", title="概览", claims=[claim])],
        evidence=evidence,
        provenance_ref="t" * 32,
    )


# ─────────────────────────── Evidence 来源与类型（ADR-0006 S1/S3/S5/S6）───────────────────────────
def test_captured_evidence_valid():
    assert _captured().source is not None


def test_source_xor_derivation_enforced():
    with pytest.raises(ValueError):  # 两者皆空（假设类 M2 不开放）
        _captured(source=None)
    with pytest.raises(ValueError):  # 两者皆有
        _captured(derivation=Derivation(op="div", inputs=["E9"], formula="x"))


def test_adjusted_price_requires_anchor_date():
    with pytest.raises(ValueError):
        _captured(price_basis="qfq")
    ok = _captured(price_basis="qfq", anchor_date=NOW.date())
    assert ok.anchor_date == NOW.date()


def test_news_never_authoritative():
    with pytest.raises(ValueError):
        Evidence(
            eid="E1",
            kind="news",
            value="标题",
            pit_class="authoritative",
            published_at=NOW,
            available_at=NOW,
            source=_source(data_type="news"),
            confidence="reported",
        )


def test_kind_value_typing():
    with pytest.raises(ValueError):  # metric 必须 float
        _captured(value="text")
    with pytest.raises(ValueError):  # news 必须 str
        Evidence(
            eid="E1",
            kind="news",
            value=42.0,
            pit_class="observed",
            first_seen_at=NOW,
            available_at=NOW,
            source=_source(data_type="news"),
            confidence="reported",
        )


def test_model_op_requires_params():
    with pytest.raises(ValueError):
        Derivation(op="model", inputs=["E1"], formula="simple_dcf")
    ok = Derivation(op="model", inputs=["E1"], params={"growth": 0.05}, formula="simple_dcf")
    assert ok.params["growth"] == 0.05


# ─────────────────────────── 报告级闭包与策略绑定（S7/S9）───────────────────────────
def test_report_live_ok():
    assert _report({"E1": _captured()}).meta.symbol == "600519"


def test_forward_reference_derivation_rejected_cleanly():
    bad = Evidence(
        eid="E1",
        kind="metric",
        value=2.0,
        pit_class="observed",
        first_seen_at=NOW,
        available_at=NOW,
        derivation=Derivation(op="div", inputs=["E9"], formula="x"),
        confidence="derived",
    )
    with pytest.raises(ValueError):  # 缺失/前向引用 → ValueError（非 KeyError）
        _report({"E1": bad})


def test_orphan_evidence_rejected():
    extra = _captured(eid="E2")
    with pytest.raises(ValueError):  # E2 未被引用 → 非精确闭包
        _report({"E1": _captured(), "E2": extra})


def test_replay_under_live_policy_rejected():
    replay = Evidence(
        eid="E1",
        kind="metric",
        value=1.0,
        pit_class="replay",
        price_basis="unadjusted",
        source=_source(),
        confidence="reported",
    )
    with pytest.raises(ValueError):
        _report({"E1": replay}, policy="live")


def test_claim_token_double_write():
    with pytest.raises(ValueError):  # 文本 token 与 evidence_ids 不一致
        _report({"E1": _captured()}, claim_text="收盘价上涨", eids=["E1"])


# ─────────────────────────── spec 同步守卫 ───────────────────────────
def test_spec_code_block_stays_in_sync():
    """spec-001 §2 规范代码块可执行，且导出的模型/字段集合与 ir/models.py 一致。

    spec 或实现单边改动（漂移）时此测试报警——变更必须两边同步且走 ADR。
    """
    import bellwether.ir.models as impl

    spec = Path(__file__).parent.parent / "docs" / "specs" / "spec-001-evidence-ir.md"
    code = re.search(r"```python\n(.*?)```", spec.read_text(encoding="utf-8"), re.S).group(1)
    import sys

    module_obj = type(sys)("spec001_block")
    exec(code, module_obj.__dict__)
    sys.modules["spec001_block"] = module_obj
    try:
        model_names = [
            "SourceRef",
            "Period",
            "Derivation",
            "Evidence",
            "Claim",
            "Section",
            "Scenario",
            "CoverageDimension",
            "CoverageReport",
            "AnalysisContextRef",
            "ReportMeta",
            "StructuredReport",
        ]
        for name in model_names:
            spec_model = getattr(module_obj, name)
            impl_model = getattr(impl, name)
            assert set(spec_model.model_fields) == set(impl_model.model_fields), name
    finally:
        del sys.modules["spec001_block"]
