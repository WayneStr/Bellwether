"""M2-B0 批 B：组装器（R1/R6/lenient-drop）与渲染器（token 替换/格式/R5 不变量）。"""

from datetime import UTC, datetime

import pytest

from bellwether.core.context import AnalysisContext, FrozenClock
from bellwether.ir.assemble import assemble_report, derive_coverage
from bellwether.ir.models import SourceRef
from bellwether.ir.render import format_value, render_report
from bellwether.ir.store import EvidenceStore

AS_OF = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
CTX = AnalysisContext(as_of=AS_OF, capture_policy="live", clock=FrozenClock(AS_OF))


def _store_with_evidence():
    store = EvidenceStore("600519")
    source = SourceRef(
        provider_id="akshare",
        upstream_source="sina",
        tool_name="get_price_history",
        tool_call_id="tc1",
        data_type="ohlcv",
        capture_id="c1",
        response_sha256="a" * 64,
        captured_at=AS_OF,
        license_tag="private-ok-backup-ok",
    )
    e1 = store.register(  # E1 close
        metric_name="last_close",
        kind="series_stat",
        value=1710.5,
        pit_class="observed",
        first_seen_at=AS_OF,
        available_at=AS_OF,
        price_basis="qfq",
        anchor_date=AS_OF.date(),
        source=source,
        confidence="reported",
    )
    e2 = store.register(  # E2 pe
        metric_name="pe",
        kind="metric",
        value=25.5,
        pit_class="observed",
        first_seen_at=AS_OF,
        available_at=AS_OF,
        source=source.model_copy(update={"data_type": "fundamentals", "capture_id": "c2"}),
        confidence="reported",
    )
    return store, e1, e2


def _assemble(draft, store, **kwargs):
    defaults = dict(
        store=store,
        context=CTX,
        symbol="600519",
        market="CN",
        tier="quick",
        model_versions={"synthesis": "m1"},
        prompt_versions={"system": "abc"},
        provenance_ref="t" * 32,
        data_types_present={"ohlcv", "fundamentals"},
    )
    defaults.update(kwargs)
    return assemble_report(draft, **defaults)


def _clean_draft():
    return {
        "sections": [
            {"title": "概览", "claims": ["收盘价为 [E1]，估值 [E2] 处于合理区间"]},
            {"title": "观点与展望", "claims": ["整体基本面保持稳健"]},
        ],
        "scenarios": [{"name": "base", "narrative": "围绕 [E1] 水平震荡"}],
        "risks": ["估值 [E2] 若持续抬升需警惕回调"],
    }


# ─────────────────────────── 组装 strict ───────────────────────────
def test_assemble_clean_draft():
    store, e1, e2 = _store_with_evidence()
    result = _assemble(_clean_draft(), store)
    assert result.violations == []
    report = result.report
    assert report is not None
    assert set(report.evidence) == {e1.eid, e2.eid}  # 精确闭包
    assert report.sections[0].claims[0].evidence_ids == ["E1", "E2"]  # 双写代码派生
    assert report.sections[1].claims[0].kind == "interpretation"  # 观点段无令牌合法
    assert report.meta.coverage.dims["ohlcv"].status == "available"
    assert report.meta.coverage.dims["news"].status == "missing"


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda d: d["sections"][0]["claims"].__setitem__(0, "涨到 45 元"), "裸数字"),
        (lambda d: d["sections"][0]["claims"].__setitem__(0, "见 [E99]"), "不存在的证据"),
        (lambda d: d["sections"][0]["claims"].__setitem__(0, "走势平稳"), "R6 段落判据"),
        (lambda d: d["sections"][0].__setitem__("title", "2026 展望"), "标题为空或含裸数字"),
        (lambda d: d["risks"].__setitem__(0, "存在系统性风险"), "必须引用证据令牌"),
        (lambda d: d["scenarios"][0].__setitem__("narrative", "涨三成"), "裸数字"),
    ],
)
def test_assemble_rejects_violations(mutate, expected):
    store, *_ = _store_with_evidence()
    draft = _clean_draft()
    mutate(draft)
    result = _assemble(draft, store)
    assert result.report is None
    assert any(expected in v for v in result.violations)


def test_assemble_lenient_drops_instead():
    store, e1, e2 = _store_with_evidence()
    draft = _clean_draft()
    draft["sections"][0]["claims"].append("裸写 45 元的违规陈述")
    result = _assemble(draft, store, lenient=True)
    assert result.report is not None
    assert result.report.meta.dropped_claims == 1
    assert result.dropped[0]["reason"].startswith("含裸数字")


def test_assemble_empty_sections_rejected():
    store, *_ = _store_with_evidence()
    result = _assemble({"sections": []}, store)
    assert result.report is None


def test_coverage_reason_has_no_digits():
    coverage = derive_coverage("600519", "CN", CTX, {"ohlcv"})
    from bellwether.ir.verify import scan_naked_numbers

    for dim in coverage.dims.values():
        if dim.reason:
            assert scan_naked_numbers(dim.reason) == []  # P8：reason 禁数字


# ─────────────────────────── 渲染 ───────────────────────────
def test_render_substitutes_tokens_and_formats():
    store, e1, e2 = _store_with_evidence()
    result = _assemble(_clean_draft(), store)
    text = render_report(result.report)
    assert "[E1]" not in text and "[E2]" not in text  # 令牌全部替换
    assert "1710.5" in text  # < 10000 不加千分位（冻结规则）
    assert "25.5" in text


def test_format_value_frozen_rules():
    store, e1, e2 = _store_with_evidence()
    assert format_value(e1) == "1710.5"
    big = e1.model_copy(update={"value": 1234567.0})
    assert format_value(big) == "1,234,567"
    pct = e2.model_copy(update={"value": 47.861, "unit": "%"})
    assert format_value(pct) == "47.86%"
    cur = e2.model_copy(update={"value": 1234.5, "currency": "CNY"})
    assert format_value(cur) == "1234.5 CNY"
    stale = e1.model_copy(update={"confidence": "stale"})
    assert format_value(stale).endswith("⏳")


def test_render_stale_banner():
    store, e1, e2 = _store_with_evidence()
    result = _assemble(_clean_draft(), store)
    report = result.report
    stale_ev = report.evidence["E1"].model_copy(update={"confidence": "stale"})
    stale_report = report.model_copy(update={"evidence": {**report.evidence, "E1": stale_ev}})
    assert "数据时效提示" in render_report(stale_report)


def test_render_r5_invariant_catches_dirty_template():
    store, e1, e2 = _store_with_evidence()
    result = _assemble(_clean_draft(), store)
    report = result.report
    # 绕过组装器直接改 claim 文本（schema 不查裸数字）→ 渲染必须快败而非漏放
    dirty = (
        report.sections[0]
        .claims[0]
        .model_copy(update={"text": "涨了 45%（[E1] [E2]）", "evidence_ids": ["E1", "E2"]})
    )
    bad_section = report.sections[0].model_copy(update={"claims": [dirty]})
    bad = report.model_copy(update={"sections": [bad_section, report.sections[1]]})
    with pytest.raises(RuntimeError, match="naked number"):
        render_report(bad)


# ─────────── 畸形 draft 防御（2026-07-26 smoke：模型给裸字符串 section 炸 assemble） ───────────
@pytest.mark.parametrize(
    "draft",
    [
        {"sections": ["裸字符串段落"]},  # section 非 object（真实发生）
        {"sections": [{"title": "概览", "claims": "非列表"}]},
        {"sections": "整个不是列表"},
        {
            "sections": [{"title": "概览", "claims": ["有 [E1] 的合法陈述"]}],
            "scenarios": ["坏情景"],
        },
        {"sections": [{"title": "概览", "claims": ["引用 [E1]"]}], "risks": "非列表"},
    ],
)
def test_malformed_draft_rejected_not_crash(draft):
    """LLM 输入不可信：任何畸形结构必须转成违规反馈（strict 拒绝），绝不抛异常。"""
    store, _, _ = _store_with_evidence()
    result = _assemble(draft, store)
    assert result.report is None
    assert result.violations  # 有明确违规原因反馈给 LLM 重写


def test_malformed_draft_lenient_drops(tmp_path):
    store, e1, _ = _store_with_evidence()
    draft = {
        "sections": [
            "裸字符串",
            {"title": "概览", "claims": [f"收盘价 [{e1.eid}]"]},
        ],
        "risks": [f"注意 [{e1.eid}] 波动"],
    }
    result = _assemble(draft, store, lenient=True)
    assert result.report is not None  # 合法部分照常出报告
    assert result.report.meta.dropped_claims == 1  # 畸形条目诚实计数
