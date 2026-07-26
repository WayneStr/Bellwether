"""端到端评测跑批（C3 执行器）：同一 cassette 冻结输入重复 k 次「生成 → 评测」。

null 分布测定（RFC-003 §4.1）与 baseline-of-record 归档共用本执行器：输入全冻结
（cassette + FrozenClock），运行间差异只来自 LLM 生成 × 评审的随机性——正是 null
要量化的对象。

诚实性约定：单例失败（模型抖动 / unstructured 回退）不重试、不造分，逐例记入
failures 并在产物 meta 里如实呈现；总成本硬预算超限即中止余下（已完成产物保留）。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..config import AppConfig
from ..core.context import AnalysisContext, FrozenClock
from ..core.exceptions import BellwetherError
from ..data.base import detect_market
from ..data.cassette import CassetteProvider, provider_id_for_market
from .models import EvalReport
from .runner import run_eval
from .stats import null_distribution


@dataclass
class BatchConfig:
    cassette_root: Path
    out_dir: Path
    k: int = 1
    judge: bool = True
    n_judge: int = 1
    concurrency: int = 4
    budget_usd: float = 80.0
    smoke: bool = False  # 每市场只跑前 3 只（全链验证用）


def _cassette_context(as_of_iso: str) -> AnalysisContext:
    parsed = datetime.fromisoformat(as_of_iso)
    parsed = parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    return AnalysisContext(as_of=parsed, capture_policy="cassette", clock=FrozenClock(parsed))


def symbols_from_manifest(manifest: dict[str, Any], *, smoke: bool = False) -> list[str]:
    """cassette 已录标的（get_ohlcv 条目），排序保证跑批顺序确定。"""
    symbols = sorted(
        {e["args"]["symbol"] for e in manifest["entries"].values() if e["method"] == "get_ohlcv"}
    )
    if not smoke:
        return symbols
    by_market: dict[str, list[str]] = {}
    for s in symbols:
        by_market.setdefault(detect_market(s), []).append(s)
    return [s for group in by_market.values() for s in group[:3]]


def _analyze_one(
    config: AppConfig, manifest: dict[str, Any], cassette_root: Path, symbol: str
) -> tuple[str, Path | None, float, str | None]:
    """单例 cassette 回放分析。返回 (symbol, report_path, cost_usd, error)。"""
    from ..agent.orchestrator import Orchestrator  # 延迟 import：测试打桩点在本模块之外

    market = detect_market(symbol)
    provider = CassetteProvider(
        cassette_root, market=market, inner_source_name=provider_id_for_market(manifest, market)
    )
    orch = Orchestrator(config)
    context = _cassette_context(manifest["as_of"])
    try:
        orch.analyze(symbol, context=context, provider=provider)
    except BellwetherError as exc:
        cost = (orch.last_cost or {}).get("total_usd", 0.0)
        return symbol, None, cost, f"{type(exc).__name__}: {exc}"
    cost = (orch.last_cost or {}).get("total_usd", 0.0)
    if orch.last_report_path is None:  # unstructured / max_turns：无结构化产物
        return symbol, None, cost, "no structured report (unstructured or max_turns)"
    return symbol, orch.last_report_path, cost, None


def run_batch(
    config: AppConfig, batch: BatchConfig, *, log: Callable[[str], None] = lambda s: None
) -> dict[str, Any]:
    """执行 k 轮跑批，落盘 run-<i>.json / null.json / batch-meta.json，返回汇总。"""
    manifest = json.loads((batch.cassette_root / "manifest.json").read_text(encoding="utf-8"))
    symbols = symbols_from_manifest(manifest, smoke=batch.smoke)
    batch.out_dir.mkdir(parents=True, exist_ok=True)

    judge_llm = judge_spec = None
    if batch.judge:
        from anthropic import Anthropic

        from ..agent.llm import ResilientLLM
        from ..agent.router import ModelRouter

        client = Anthropic(
            api_key=config.anthropic_api_key,
            base_url=config.anthropic_base_url,
            timeout=120.0,
            max_retries=0,
        )
        judge_llm = ResilientLLM(client)
        judge_spec = ModelRouter(config.models).resolve("judge")

    total_cost = 0.0
    aborted: str | None = None
    eval_reports: list[EvalReport] = []
    runs_meta: list[dict[str, Any]] = []

    for run_idx in range(1, batch.k + 1):
        failures: list[dict[str, str]] = []
        paths: list[Path] = []
        with ThreadPoolExecutor(max_workers=batch.concurrency) as pool:
            futures = {
                pool.submit(_analyze_one, config, manifest, batch.cassette_root, s): s
                for s in symbols
            }
            for fut in as_completed(futures):
                symbol, path, cost, error = fut.result()
                total_cost += cost
                if error is not None:
                    failures.append({"symbol": symbol, "error": error[:300]})
                    log(f"run {run_idx}/{batch.k} · {symbol} FAIL（累计 ${total_cost:.2f}）")
                else:
                    assert path is not None
                    paths.append(path)
                    log(f"run {run_idx}/{batch.k} · {symbol} ok（累计 ${total_cost:.2f}）")
                if total_cost >= batch.budget_usd and aborted is None:
                    aborted = f"预算硬上限 ${batch.budget_usd:.2f} 已达（run {run_idx}），中止余下"
                    pool.shutdown(cancel_futures=True)
                    break

        if paths:
            from ..core.costs import CostLedger

            ledger = CostLedger(config.pricing)
            report = run_eval(
                sorted(paths),
                llm=judge_llm,
                judge_spec=judge_spec,
                ledger=ledger if batch.judge else None,
                n_judge=batch.n_judge,
            )
            total_cost += ledger.summary()["total_usd"] if batch.judge else 0.0
            (batch.out_dir / f"run-{run_idx}.json").write_text(
                report.model_dump_json(indent=2), encoding="utf-8"
            )
            eval_reports.append(report)
        runs_meta.append(
            {
                "run": run_idx,
                "cases_ok": len(paths),
                "failures": failures,
                "cumulative_cost_usd": round(total_cost, 4),
            }
        )
        log(
            f"run {run_idx}/{batch.k} 完成：{len(paths)}/{len(symbols)} 例成功，"
            f"累计 ${total_cost:.2f}"
        )
        if aborted:
            break

    null_stats = None
    if len(eval_reports) >= 2:
        null_stats = {
            dim: null_distribution([r.cases for r in eval_reports], dim)
            for dim in ("reasoning", "composite")
        }
        (batch.out_dir / "null.json").write_text(
            json.dumps(null_stats, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    meta = {
        "cassette": str(batch.cassette_root),
        "cassette_as_of": manifest["as_of"],
        "k": batch.k,
        "n_symbols": len(symbols),
        "judge": batch.judge,
        "n_judge": batch.n_judge,
        "smoke": batch.smoke,
        "total_cost_usd": round(total_cost, 4),
        "budget_usd": batch.budget_usd,
        "aborted": aborted,
        "runs": runs_meta,
        "fingerprint": eval_reports[0].fingerprint if eval_reports else None,
    }
    (batch.out_dir / "batch-meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {"meta": meta, "null": null_stats, "runs": len(eval_reports)}
