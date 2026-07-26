"""C1 评测运行器：程序化维度确定性 + 播种违规样本全拦截 + judge 统计 + CLI。

fixture 走真实管道（execute_tool 捕获注册 → assemble_report 组装 → report/trace
落盘），评测器随后独立复验——守门员测试按 RFC-003 §1.4 KPI 口径：播种
值篡改/裸数字/假 eid/删捕获/违规措辞，验证 verifier 在位且全拦截。
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from bellwether.agent import tools as tools_mod
from bellwether.core.capture import CaptureStore
from bellwether.core.context import AnalysisContext, FrozenClock
from bellwether.core.costs import CostLedger
from bellwether.core.exceptions import LLMConnectionError
from bellwether.evals.judge import judge_report
from bellwether.evals.runner import discover_reports, run_eval
from bellwether.ir.assemble import assemble_report
from bellwether.ir.recorder import ToolRecorder
from bellwether.ir.store import EvidenceStore
from bellwether.models import FundamentalData, ModelSpec, NewsItem, TradingRules

AS_OF = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
TRACE_ID = "trace123"


class FakeProvider:
    market = "CN"
    source = "akshare"

    def resolve_symbol(self, query, *, context):
        return query.strip().upper()

    def get_ohlcv(self, symbol, start, end, interval="1d", adjust="default", *, context):
        df = pd.DataFrame(
            {
                "open": [10.0, 11.0],
                "high": [11.5, 12.5],
                "low": [9.5, 10.5],
                "close": [11.0, 12.0],
                "volume": [100.0, 200.0],
            },
            index=pd.to_datetime(["2026-07-16", "2026-07-17"]),
        )
        df.attrs["upstream_source"] = "sina"
        df.attrs["captured_at"] = AS_OF.isoformat()
        return df

    def get_fundamentals(self, symbol, *, context):
        return FundamentalData(
            symbol=symbol,
            currency="CNY",
            pe=25.5,
            roe=0.4786,
            fetched_at=AS_OF,
            source=self.source,
        )

    def get_news(self, symbol, limit=20, *, context):
        return [NewsItem(title="公司发布年报", url="http://x", published_at=None, summary=None)]

    def trading_rules(self):
        return TradingRules(
            market="CN", timezone="Asia/Shanghai", has_price_limit=True, settlement="T+1"
        )


def _make_session(tmp_path: Path, *, with_news: bool = False):
    """真实管道产一份合法 report.json + trace（bindings/captures/tool_use_id 齐备）。"""
    context = AnalysisContext(as_of=AS_OF, capture_policy="live", clock=FrozenClock(AS_OF))
    capture_root = tmp_path / "captures"
    recorder = ToolRecorder(
        context=context, evidence=EvidenceStore("600519"), captures=CaptureStore(capture_root)
    )
    recorder.current_tool_call_id = "tc_1"
    price = json.loads(
        tools_mod.execute_tool(
            "get_price_history",
            {"symbol": "600519"},
            FakeProvider(),
            context=context,
            trace=recorder,
        )
    )
    recorder.current_tool_call_id = "tc_2"
    fund = json.loads(
        tools_mod.execute_tool(
            "get_fundamentals",
            {"symbol": "600519"},
            FakeProvider(),
            context=context,
            trace=recorder,
        )
    )
    data_types = {"ohlcv", "fundamentals"}
    if with_news:
        data_types.add("news")
    e_close = price["last_close"]["eid"]
    e_pe = fund["metrics"]["PE"]["eid"]
    draft = {
        "sections": [
            {"title": "概览", "claims": [f"最新收盘价为 [{e_close}]，市盈率 [{e_pe}]"]},
            {"title": "观点与展望", "claims": ["整体基本面保持稳健"]},
        ],
        "scenarios": [{"name": "base", "narrative": f"围绕 [{e_close}] 水平震荡"}],
        "risks": [f"估值 [{e_pe}] 若持续抬升需警惕回调"],
    }
    result = assemble_report(
        draft,
        store=recorder.evidence,
        context=context,
        symbol="600519",
        market="CN",
        tier="quick",
        model_versions={"synthesis": "m1"},
        prompt_versions={"system": "abc"},
        provenance_ref=TRACE_ID,
        data_types_present=data_types,
    )
    assert result.report is not None, result.violations

    out_dir = tmp_path / "reports"
    out_dir.mkdir()
    trace = {
        "trace_id": TRACE_ID,
        "capture_root": str(capture_root),
        "evidence_bindings": [b.model_dump() for b in recorder.bindings],
        "tool_calls": [
            {"name": "get_price_history", "input": {}, "output": "", "tool_use_id": "tc_1"},
            {"name": "get_fundamentals", "input": {}, "output": "", "tool_use_id": "tc_2"},
        ],
    }
    (out_dir / f"{TRACE_ID}.json").write_text(
        json.dumps(trace, ensure_ascii=False, default=str), encoding="utf-8"
    )
    report_path = out_dir / f"{TRACE_ID}-report.json"
    report_path.write_text(result.report.model_dump_json(), encoding="utf-8")
    return report_path


def _dims(case):
    return {d.name: d for d in case.dimensions}


def _mutate_report(report_path: Path, fn) -> None:
    raw = json.loads(report_path.read_text(encoding="utf-8"))
    fn(raw)
    report_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")


# ─────────────────────────── 干净会话与确定性 ───────────────────────────
def test_clean_session_all_programmatic_pass(tmp_path):
    report_path = _make_session(tmp_path)
    result = run_eval([report_path])
    case = result.cases[0]
    assert case.error is None and not case.hard_fail
    dims = _dims(case)
    assert dims["factual"].status == "pass"  # R1 重扫 + R7/R8 真实复验全过
    assert dims["completeness"].status == "pass" and dims["completeness"].score == 1.0
    assert dims["compliance"].status == "pass"
    assert dims["reasoning"].status == "skip"  # 未启用 judge，诚实标注


def test_programmatic_scores_are_deterministic(tmp_path):
    report_path = _make_session(tmp_path)
    a = run_eval([report_path])
    b = run_eval([report_path])
    # generated_at 是唯一的操作性时间字段；cases 内容必须逐位一致（RFC-003 §3 B 层）
    assert [c.model_dump() for c in a.cases] == [c.model_dump() for c in b.cases]
    assert a.summary == b.summary


# ─────────────────────────── 播种违规（守门员） ───────────────────────────
def test_tampered_value_caught_by_r8(tmp_path):
    report_path = _make_session(tmp_path)

    def tamper(raw):
        eid = next(iter(raw["evidence"]))
        raw["evidence"][eid]["value"] = raw["evidence"][eid]["value"] + 1.0

    _mutate_report(report_path, tamper)
    case = run_eval([report_path]).cases[0]
    factual = _dims(case)["factual"]
    assert factual.status == "fail" and case.hard_fail
    assert any(h.rule == "R8" for h in factual.hits)


def test_naked_number_caught_by_r1(tmp_path):
    report_path = _make_session(tmp_path)
    _mutate_report(
        report_path,
        lambda raw: raw["sections"][1]["claims"].__setitem__(
            0, dict(raw["sections"][1]["claims"][0], text="预计明年增长 30%")
        ),
    )
    case = run_eval([report_path]).cases[0]
    factual = _dims(case)["factual"]
    assert factual.status == "fail" and any(h.rule == "R1" for h in factual.hits)


def test_deleted_capture_caught_by_r7(tmp_path):
    report_path = _make_session(tmp_path)
    for obj in (tmp_path / "captures" / "objects").iterdir():
        obj.unlink()
    case = run_eval([report_path]).cases[0]
    factual = _dims(case)["factual"]
    assert factual.status == "fail" and any(h.rule == "R7" for h in factual.hits)


def test_fake_eid_rejected_by_schema(tmp_path):
    report_path = _make_session(tmp_path)

    def inject(raw):
        claim = raw["sections"][0]["claims"][0]
        claim["text"] += " 另见 [E99]"
        claim["evidence_ids"].append("E99")

    _mutate_report(report_path, inject)
    case = run_eval([report_path]).cases[0]
    assert case.error is not None and "ValidationError" in case.error
    assert case.hard_fail


def test_missing_trace_is_unverifiable_not_pass(tmp_path):
    report_path = _make_session(tmp_path)
    (report_path.parent / f"{TRACE_ID}.json").unlink()
    case = run_eval([report_path]).cases[0]
    factual = _dims(case)["factual"]
    assert factual.status == "unverifiable" and not case.hard_fail
    assert "trace 缺失" in (factual.note or "")


def test_advice_wording_fails_compliance(tmp_path):
    report_path = _make_session(tmp_path)
    _mutate_report(
        report_path,
        lambda raw: raw["sections"][1]["claims"].__setitem__(
            0, dict(raw["sections"][1]["claims"][0], text="当前位置建议买入并持有")
        ),
    )
    case = run_eval([report_path]).cases[0]
    compliance = _dims(case)["compliance"]
    assert compliance.status == "fail" and case.hard_fail
    assert any(h.rule == "advice" for h in compliance.hits)


# ─────────────────────────── completeness 清单 ───────────────────────────
def test_deep_without_full_scenarios_loses_score(tmp_path):
    report_path = _make_session(tmp_path)
    _mutate_report(report_path, lambda raw: raw["meta"].__setitem__("tier", "deep"))
    case = run_eval([report_path]).cases[0]
    completeness = _dims(case)["completeness"]
    assert completeness.status == "fail" and completeness.score < 1.0
    assert any(h.rule == "checklist:scenarios_full" for h in completeness.hits)


def test_available_dim_unused_loses_score(tmp_path):
    # news 注册过（coverage=available）但报告未引用任何 news 证据 → 完整性缺陷
    report_path = _make_session(tmp_path, with_news=True)
    case = run_eval([report_path]).cases[0]
    completeness = _dims(case)["completeness"]
    assert completeness.status == "fail"
    assert any(h.rule == "checklist:uses_news" for h in completeness.hits)


# ─────────────────────────── judge（打桩） ───────────────────────────
class _StubLLM:
    def __init__(self, scores):
        self._scores = list(scores)
        self.calls = 0

    def create(self, chain, **kwargs):
        self.calls += 1
        score = self._scores.pop(0)
        block = SimpleNamespace(type="tool_use", input={"score": score, "rationale": "ok"})
        resp = SimpleNamespace(
            content=[block], usage=SimpleNamespace(input_tokens=100, output_tokens=10)
        )
        return resp, chain[0]


def _load_report(report_path):
    from bellwether.evals.dimensions import load_report

    return load_report(report_path)


def test_judge_mean_and_ci(tmp_path):
    report = _load_report(_make_session(tmp_path))
    ledger = CostLedger()
    dim = judge_report(
        report,
        llm=_StubLLM([80, 90, 100]),
        spec=ModelSpec(model="judge-x"),
        ledger=ledger,
        n_judge=3,
    )
    assert dim.status == "pass" and dim.score == 90.0 and dim.n == 3
    lo, hi = dim.ci95
    assert lo == pytest.approx(90 - 4.303 * 10 / 3**0.5, abs=0.01)
    assert hi == pytest.approx(90 + 4.303 * 10 / 3**0.5, abs=0.01)
    assert ledger.calls == 3  # judge 花费入账（未知模型计 $0 不瞎估）


def test_judge_single_run_has_no_ci(tmp_path):
    report = _load_report(_make_session(tmp_path))
    dim = judge_report(
        report, llm=_StubLLM([77]), spec=ModelSpec(model="judge-x"), ledger=CostLedger(), n_judge=1
    )
    assert dim.status == "pass" and dim.score == 77.0 and dim.ci95 is None


def test_judge_llm_failure_marks_dimension_fail(tmp_path):
    report = _load_report(_make_session(tmp_path))

    class _Broken:
        def create(self, chain, **kwargs):
            raise LLMConnectionError("中转不可达")

    dim = judge_report(
        report, llm=_Broken(), spec=ModelSpec(model="judge-x"), ledger=CostLedger(), n_judge=2
    )
    assert dim.status == "fail" and "judge 调用失败" in (dim.note or "")


def test_run_eval_with_judge_attaches_meta(tmp_path):
    report_path = _make_session(tmp_path)
    result = run_eval(
        [report_path],
        llm=_StubLLM([88]),
        judge_spec=ModelSpec(model="judge-x"),
        ledger=CostLedger(),
        n_judge=1,
    )
    dims = _dims(result.cases[0])
    assert dims["reasoning"].status == "pass" and dims["reasoning"].score == 88.0
    assert result.judge_meta["model"] == "judge-x"


# ─────────────────────────── runner 发现与 CLI ───────────────────────────
def test_discover_reports_dir_and_file(tmp_path):
    report_path = _make_session(tmp_path)
    assert discover_reports([report_path.parent]) == [report_path]
    assert discover_reports([report_path]) == [report_path]


def test_cli_eval_pass_and_json(tmp_path):
    from typer.testing import CliRunner

    from bellwether.cli import app

    report_path = _make_session(tmp_path)
    runner = CliRunner()
    ok = runner.invoke(app, ["eval", str(report_path.parent)])
    assert ok.exit_code == 0, ok.output
    assert "C1 评测分数报告" in ok.output

    as_json = runner.invoke(app, ["eval", str(report_path), "--json"])
    assert as_json.exit_code == 0
    payload = json.loads(as_json.stdout)
    assert payload["n_cases"] == 1
    assert payload["summary"]["hard_fail_cases"] == 0


def test_cli_eval_hard_fail_exits_one(tmp_path):
    from typer.testing import CliRunner

    from bellwether.cli import app

    report_path = _make_session(tmp_path)
    _mutate_report(
        report_path,
        lambda raw: raw["evidence"].__setitem__(
            next(iter(raw["evidence"])),
            dict(
                raw["evidence"][next(iter(raw["evidence"]))],
                value=raw["evidence"][next(iter(raw["evidence"]))]["value"] + 1.0,
            ),
        ),
    )
    runner = CliRunner()
    result = runner.invoke(app, ["eval", str(report_path)])
    assert result.exit_code == 1


def test_cli_eval_empty_target_errors(tmp_path):
    from typer.testing import CliRunner

    from bellwether.cli import app

    empty = tmp_path / "empty"
    empty.mkdir()
    result = runner_result = CliRunner().invoke(app, ["eval", str(empty)])
    assert runner_result.exit_code == 1
    assert "未发现" in result.output
