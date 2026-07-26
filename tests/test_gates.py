"""C3 门禁引擎：三种 gate 判定、B9 版本指纹、bootstrap 可复现性、劣化拦截演练。"""

import json
from datetime import UTC, datetime

import pytest
import yaml

from bellwether.evals.gates import DEFAULT_GATES, evaluate_gates, load_gates
from bellwether.evals.models import CaseResult, DimensionResult, EvalReport
from bellwether.evals.stats import bootstrap_upper_bound, dimension_score, paired_diffs

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
FP = {"models": ["synthesis:m1"], "prompts": ["system:abc"], "judge": None}


def _case(
    symbol, *, factual="pass", completeness=1.0, compliance="pass", reasoning=None, error=None
):
    dims = [
        DimensionResult(name="factual", status=factual),
        DimensionResult(
            name="completeness",
            status="pass" if completeness >= 1.0 else "fail",
            score=completeness,
        ),
        DimensionResult(name="compliance", status=compliance),
        DimensionResult(name="reasoning", status="pass", score=reasoning)
        if reasoning is not None
        else DimensionResult(name="reasoning", status="skip", note="未启用 judge"),
    ]
    return CaseResult(
        report_path=f"{symbol}-report.json",
        symbol=symbol,
        market="US",
        tier="quick",
        dimensions=dims,
        error=error,
    )


def _report(cases, fingerprint=FP):
    return EvalReport(generated_at=NOW, n_cases=len(cases), cases=cases, fingerprint=fingerprint)


# ─────────────────────────── dimension_score 映射 ───────────────────────────
def test_dimension_score_mapping():
    case = _case("AAPL", completeness=0.8, reasoning=70.0)
    assert dimension_score(case, "factual") == 100.0
    assert dimension_score(case, "completeness") == pytest.approx(80.0)
    assert dimension_score(case, "reasoning") == 70.0
    # composite = 可用维度等权均值
    assert dimension_score(case, "composite") == pytest.approx((100 + 80 + 100 + 70) / 4)


def test_dimension_score_skip_and_error():
    case = _case("AAPL")  # reasoning skip
    assert dimension_score(case, "reasoning") is None
    assert dimension_score(case, "composite") == pytest.approx(100.0)  # skip 不拉低
    broken = _case("MSFT", error="ValidationError: bad")
    assert dimension_score(broken, "factual") == 0.0
    assert dimension_score(broken, "composite") == 0.0


# ─────────────────────────── bootstrap ───────────────────────────
def test_bootstrap_reproducible_and_directional():
    degraded = [-20.0] * 20
    mean, upper = bootstrap_upper_bound(degraded, seed=7)
    mean2, upper2 = bootstrap_upper_bound(degraded, seed=7)
    assert (mean, upper) == (mean2, upper2)  # 同 seed 逐位一致
    assert upper < 0  # 显著退化

    healthy = [1.0, -1.0] * 10
    _, upper_ok = bootstrap_upper_bound(healthy, seed=7)
    assert upper_ok > 0  # 无退化证据


def test_paired_diffs_reports_unmatched():
    cand = [_case("AAPL", reasoning=70.0), _case("TSLA", reasoning=60.0)]
    base = [_case("AAPL", reasoning=90.0)]  # TSLA 无配对
    diffs, unmatched = paired_diffs(cand, base, "reasoning")
    assert diffs == [-20.0] and unmatched == 1


# ─────────────────────────── 门禁判定（劣化拦截演练） ───────────────────────────
def test_zero_tolerance_blocks_single_violation():
    """M2 验收演练：一例事实性违规的「劣化 PR」必须被拦截。"""
    cand = _report([_case("AAPL"), _case("MSFT", factual="fail")])
    result = evaluate_gates(cand)
    assert result.verdict == "fail"
    factual = next(c for c in result.checks if c.dimension == "factual")
    assert factual.verdict == "fail" and "MSFT" in factual.detail


def test_schema_rejected_case_blocks():
    cand = _report([_case("AAPL", error="ValidationError: x")])
    assert evaluate_gates(cand).verdict == "fail"


def test_absolute_min_floor():
    cand = _report([_case("AAPL", completeness=0.9)])
    result = evaluate_gates(cand)
    comp = next(c for c in result.checks if c.dimension == "completeness")
    assert comp.verdict == "fail" and "0.9" in comp.detail


def test_paired_ci_detects_significant_regression():
    base = _report([_case(f"S{i}", reasoning=90.0) for i in range(20)])
    cand = _report([_case(f"S{i}", reasoning=70.0) for i in range(20)])
    result = evaluate_gates(cand, base)
    reasoning = next(c for c in result.checks if c.dimension == "reasoning")
    assert reasoning.verdict == "fail" and "显著退化" in reasoning.detail
    assert reasoning.waivable  # 豁免通道声明（人工流程，机器判定不变）
    assert result.verdict == "fail"


def test_paired_ci_passes_when_no_regression():
    base = _report([_case(f"S{i}", reasoning=80.0) for i in range(20)])
    cand = _report([_case(f"S{i}", reasoning=80.0 + (1 if i % 2 else -1)) for i in range(20)])
    result = evaluate_gates(cand, base)
    reasoning = next(c for c in result.checks if c.dimension == "reasoning")
    assert reasoning.verdict == "pass"
    assert result.verdict == "pass"


def test_no_baseline_skips_paired_dimensions():
    result = evaluate_gates(_report([_case("AAPL")]))
    paired = [c for c in result.checks if c.gate == "paired_ci"]
    assert paired and all(c.verdict == "skip" for c in paired)
    assert result.verdict == "pass"  # 程序化维度全绿时不因缺基线而红


# ─────────────────────────── B9 版本指纹 ───────────────────────────
def test_fingerprint_mismatch_requires_rebaseline():
    base = _report([_case("AAPL", reasoning=90.0)], fingerprint=FP)
    cand = _report(
        [_case("AAPL", reasoning=90.0)],
        fingerprint={**FP, "models": ["synthesis:m2-new"]},
    )
    result = evaluate_gates(cand, base)
    assert result.requires_rebaseline and result.verdict == "fail"
    assert any("models" in d for d in result.fingerprint_diff)
    # paired 维度不判分（拒绝不可比的比较），zero_tolerance 照常执行
    reasoning = next(c for c in result.checks if c.dimension == "reasoning")
    assert reasoning.verdict == "skip" and "重定基线" in reasoning.detail
    factual = next(c for c in result.checks if c.dimension == "factual")
    assert factual.verdict == "pass"


def test_missing_fingerprint_treated_as_mismatch():
    base = _report([_case("AAPL")], fingerprint=None)
    cand = _report([_case("AAPL")], fingerprint=FP)
    assert evaluate_gates(cand, base).requires_rebaseline


# ─────────────────────────── 配置同步守卫与 CLI ───────────────────────────
def test_gates_yaml_matches_builtin_default():
    """eval/gates.yaml 是治理权威；内置 DEFAULT_GATES 漂移即报警。"""
    from pathlib import Path

    repo_yaml = Path(__file__).parent.parent / "eval" / "gates.yaml"
    assert yaml.safe_load(repo_yaml.read_text(encoding="utf-8")) == DEFAULT_GATES
    assert load_gates(None) == DEFAULT_GATES


def test_cli_gate_pass_and_fail(tmp_path):
    from typer.testing import CliRunner

    from bellwether.cli import app

    runner = CliRunner()
    good = tmp_path / "good.json"
    good.write_text(_report([_case("AAPL")]).model_dump_json(), encoding="utf-8")
    ok = runner.invoke(app, ["gate", str(good)])
    assert ok.exit_code == 0, ok.output
    assert "门禁：通过" in ok.output

    bad = tmp_path / "bad.json"
    bad.write_text(_report([_case("AAPL", factual="fail")]).model_dump_json(), encoding="utf-8")
    blocked = runner.invoke(app, ["gate", str(bad)])
    assert blocked.exit_code == 1
    assert "门禁：不通过" in blocked.output


def test_cli_gate_with_baseline_regression(tmp_path):
    from typer.testing import CliRunner

    from bellwether.cli import app

    base = tmp_path / "base.json"
    cand = tmp_path / "cand.json"
    base.write_text(
        _report([_case(f"S{i}", reasoning=90.0) for i in range(20)]).model_dump_json(),
        encoding="utf-8",
    )
    cand.write_text(
        _report([_case(f"S{i}", reasoning=70.0) for i in range(20)]).model_dump_json(),
        encoding="utf-8",
    )
    result = CliRunner().invoke(app, ["gate", str(cand), "--baseline", str(base)])
    assert result.exit_code == 1
    assert "显著退化" in result.output


def test_eval_report_fingerprint_roundtrip(tmp_path):
    report = _report([_case("AAPL")])
    loaded = EvalReport.model_validate(json.loads(report.model_dump_json()))
    assert loaded.fingerprint == FP
