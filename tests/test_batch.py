"""C3 跑批执行器：编排/预算中止/失败如实记录/null 统计落盘（_analyze_one 打桩）。"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bellwether.config import AppConfig
from bellwether.evals import batch as batch_mod
from bellwether.evals.batch import BatchConfig, run_batch, symbols_from_manifest
from bellwether.evals.models import CaseResult, DimensionResult
from bellwether.evals.stats import null_distribution
from bellwether.ir.models import (
    AnalysisContextRef,
    Claim,
    CoverageDimension,
    CoverageReport,
    ReportMeta,
    Section,
    StructuredReport,
)

AS_OF = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


def _minimal_report(symbol: str) -> StructuredReport:
    """最小合法 StructuredReport（零证据：纯 interpretation 段）——跑批编排测试用。"""
    missing = CoverageDimension(status="missing", reason="测试桩")
    return StructuredReport(
        meta=ReportMeta(
            symbol=symbol,
            market="US",
            tier="quick",
            generated_at=AS_OF,
            analysis_context=AnalysisContextRef(as_of=AS_OF, capture_policy="live"),
            coverage=CoverageReport(
                market="US",
                symbol=symbol,
                as_of=AS_OF,
                dims={
                    name: missing
                    for name in (
                        "ohlcv",
                        "fundamentals",
                        "fundamentals_period",
                        "news",
                        "filings",
                        "actions",
                    )
                },
            ),
        ),
        sections=[
            Section(
                section_id="s0",
                title="观点",
                claims=[Claim(claim_id="c0", kind="interpretation", text="整体保持稳健")],
            )
        ],
        evidence={},
        provenance_ref="stub-trace",
    )


def _cassette_dir(tmp_path: Path, symbols: list[str]) -> Path:
    root = tmp_path / "cassette"
    root.mkdir()
    manifest = {
        "as_of": "2026-07-26T13:09:16+00:00",
        "entries": {
            f"k{i}": {"provider_id": "yfinance", "method": "get_ohlcv", "args": {"symbol": s}}
            for i, s in enumerate(symbols)
        },
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def _stub_analyze(reports_dir: Path, *, cost: float = 0.1, fail: set[str] = frozenset()):
    counter = {"n": 0}

    def stub(config, manifest, cassette_root, symbol):
        if symbol in fail:
            return symbol, None, cost, "LLMConnectionError: 桩故障"
        counter["n"] += 1
        path = reports_dir / f"stub-{counter['n']}-{symbol}-report.json"
        path.write_text(_minimal_report(symbol).model_dump_json(), encoding="utf-8")
        return symbol, path, cost, None

    return stub


def test_symbols_from_manifest_smoke():
    manifest = {
        "entries": {
            f"e{i}": {"provider_id": "x", "method": "get_ohlcv", "args": {"symbol": s}}
            for i, s in enumerate(["AAPL", "MSFT", "GOOGL", "AMZN", "600519", "600036"])
        }
    }
    full = symbols_from_manifest(manifest)
    assert len(full) == 6
    smoke = symbols_from_manifest(manifest, smoke=True)
    assert len(smoke) == 5  # US 取前 3 + CN 全部 2 只


def test_run_batch_k2_produces_runs_and_null(tmp_path, monkeypatch):
    reports = tmp_path / "reports"
    reports.mkdir()
    monkeypatch.setattr(batch_mod, "_analyze_one", _stub_analyze(reports))
    out = tmp_path / "out"
    summary = run_batch(
        AppConfig(),
        BatchConfig(
            cassette_root=_cassette_dir(tmp_path, ["AAPL", "MSFT"]),
            out_dir=out,
            k=2,
            judge=False,
        ),
    )
    assert summary["runs"] == 2
    assert (out / "run-1.json").exists() and (out / "run-2.json").exists()
    assert (out / "null.json").exists() and (out / "batch-meta.json").exists()
    meta = json.loads((out / "batch-meta.json").read_text(encoding="utf-8"))
    assert meta["aborted"] is None
    assert [r["cases_ok"] for r in meta["runs"]] == [2, 2]
    assert meta["total_cost_usd"] == pytest.approx(0.4)  # 4 例 × $0.1，judge 关


def test_run_batch_budget_abort_is_honest(tmp_path, monkeypatch):
    reports = tmp_path / "reports"
    reports.mkdir()
    monkeypatch.setattr(batch_mod, "_analyze_one", _stub_analyze(reports, cost=50.0))
    out = tmp_path / "out"
    summary = run_batch(
        AppConfig(),
        BatchConfig(
            cassette_root=_cassette_dir(tmp_path, ["AAPL", "MSFT", "GOOGL"]),
            out_dir=out,
            k=3,
            judge=False,
            budget_usd=80.0,
            concurrency=1,
        ),
    )
    meta = summary["meta"]
    assert meta["aborted"] is not None and "预算" in meta["aborted"]
    assert len(meta["runs"]) == 1  # 第 1 轮触限即停，不进第 2 轮
    assert meta["total_cost_usd"] >= 80.0


def test_run_batch_records_failures_and_continues(tmp_path, monkeypatch):
    reports = tmp_path / "reports"
    reports.mkdir()
    monkeypatch.setattr(batch_mod, "_analyze_one", _stub_analyze(reports, fail={"MSFT"}))
    out = tmp_path / "out"
    summary = run_batch(
        AppConfig(),
        BatchConfig(
            cassette_root=_cassette_dir(tmp_path, ["AAPL", "MSFT"]),
            out_dir=out,
            k=1,
            judge=False,
        ),
    )
    run1 = summary["meta"]["runs"][0]
    assert run1["cases_ok"] == 1
    assert run1["failures"][0]["symbol"] == "MSFT"
    assert "桩故障" in run1["failures"][0]["error"]


def test_run_batch_early_aborts_on_sustained_outage(tmp_path, monkeypatch):
    """端点持续中断（连续失败达阈值）→ 早停止损，不空转全部例子与后续轮。"""
    reports = tmp_path / "reports"
    reports.mkdir()
    syms = [f"S{i:02d}" for i in range(15)]  # 15 只，全失败（模拟上游宕机）
    monkeypatch.setattr(batch_mod, "_analyze_one", _stub_analyze(reports, fail=set(syms)))
    out = tmp_path / "out"
    summary = run_batch(
        AppConfig(),
        BatchConfig(
            cassette_root=_cassette_dir(tmp_path, syms),
            out_dir=out,
            k=5,
            judge=False,
            concurrency=1,  # 顺序完成，连续失败计数确定
        ),
    )
    meta = summary["meta"]
    assert meta["aborted"] is not None and "持续中断" in meta["aborted"]
    assert len(meta["runs"]) == 1  # 第 1 轮就早停，不进后续轮
    assert len(meta["runs"][0]["failures"]) < len(syms)  # 未把 15 只全跑完即止损


# ─────────────────────────── null_distribution 单元 ───────────────────────────
def _case_with_reasoning(symbol: str, score: float) -> CaseResult:
    return CaseResult(
        report_path=f"{symbol}.json",
        symbol=symbol,
        market="US",
        tier="quick",
        dimensions=[
            DimensionResult(name="factual", status="pass"),
            DimensionResult(name="completeness", status="pass", score=1.0),
            DimensionResult(name="compliance", status="pass"),
            DimensionResult(name="reasoning", status="pass", score=score),
        ],
    )


def test_null_distribution_pairwise_means():
    run_a = [_case_with_reasoning("AAPL", 80.0), _case_with_reasoning("MSFT", 90.0)]
    run_b = [_case_with_reasoning("AAPL", 82.0), _case_with_reasoning("MSFT", 88.0)]
    run_c = [_case_with_reasoning("AAPL", 80.0), _case_with_reasoning("MSFT", 90.0)]
    stats = null_distribution([run_a, run_b, run_c], "reasoning")
    assert stats["pairs"] == 3  # C(3,2)
    # a-b: mean(-2,+2)=0；a-c: 0；b-c: mean(+2,-2)=0
    assert stats["pair_means"] == [0.0, 0.0, 0.0]
    assert stats["abs_max"] == 0.0


def test_null_distribution_detects_spread():
    run_a = [_case_with_reasoning("AAPL", 90.0)]
    run_b = [_case_with_reasoning("AAPL", 80.0)]
    stats = null_distribution([run_a, run_b], "reasoning")
    assert stats["pairs"] == 1 and stats["abs_max"] == 10.0


def test_run_batch_survives_unexpected_exception(tmp_path, monkeypatch):
    """单例抛任意异常（非 BellwetherError）绝不炸整批——如实记 failure 继续。"""
    reports = tmp_path / "reports"
    reports.mkdir()
    good = _stub_analyze(reports)

    def sometimes_crash(config, manifest, cassette_root, symbol):
        if symbol == "MSFT":
            raise AttributeError("'str' object has no attribute 'get'")
        return good(config, manifest, cassette_root, symbol)

    monkeypatch.setattr(batch_mod, "_analyze_one", sometimes_crash)
    out = tmp_path / "out"
    summary = run_batch(
        AppConfig(),
        BatchConfig(
            cassette_root=_cassette_dir(tmp_path, ["AAPL", "MSFT"]),
            out_dir=out,
            k=1,
            judge=False,
        ),
    )
    run1 = summary["meta"]["runs"][0]
    assert run1["cases_ok"] == 1
    assert run1["failures"][0]["symbol"] == "MSFT"
    assert "未预期异常 AttributeError" in run1["failures"][0]["error"]
