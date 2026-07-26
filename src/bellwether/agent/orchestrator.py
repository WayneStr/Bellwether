"""最小 Claude tool-use loop：规划 → 调 tool 取数 → 综合研判。

模型选择全部经 ModelRouter（三级覆盖）。deep=True 走 deep_report 角色。
可靠性（D2）：显式超时 + SDK 重试关闭（重试统一由 tenacity 管，见 core/retry.py）
+ 模型档降级链（降级发生时在报告中明示）。
溯源（E4 最小）：每次 analyze 落一个 provenance trace（成功/失败都落，旁路不阻塞）。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import structlog
from anthropic import Anthropic
from anthropic.types import Message

from ..config import AppConfig
from ..core.capture import CaptureStore
from ..core.context import AnalysisContext
from ..core.costs import CostLedger
from ..core.exceptions import BellwetherError, BudgetExceededError, ToolError
from ..core.trace import (
    DEFAULT_CAPTURE_ROOT,
    AnalysisTrace,
    LLMCallRecord,
    ToolCallRecord,
    new_trace,
    prompt_version,
    write_report_json,
    write_trace,
)
from ..data.base import MarketDataProvider, ProviderRegistry
from ..ir.assemble import SUBMIT_REPORT_TOOL, assemble_report
from ..ir.recorder import ToolRecorder
from ..ir.render import render_report
from ..ir.store import EvidenceStore
from ..models import ModelSpec
from . import tools as tools_mod
from .llm import ResilientLLM
from .prompts import SYSTEM_PROMPT, analyze_prompt
from .router import ModelRouter

_MAX_TURNS = 10  # 取数轮 + submit/重写轮共用预算（M2 报告边界后从 6 提高）
_MAX_SUBMIT_REJECTIONS = 2  # 违规重写机会；耗尽后 lenient 模式 drop 违规条目
_LLM_TIMEOUT_SECONDS = 300.0  # deep 报告 8k tokens 生成可能超过默认值，显式给足

_log = structlog.get_logger()


def _data_types(recorder: ToolRecorder) -> set[str]:
    """本会话已注册证据覆盖的数据类型（coverage 机械推导的输入）。"""
    types: set[str] = set()
    for binding in recorder.bindings:
        evidence = recorder.evidence.get(binding.eid)
        if evidence.source is not None:
            types.add(evidence.source.data_type)
    return types


class Orchestrator:
    def __init__(self, config: AppConfig):
        self.config = config
        self.router = ModelRouter(config.models)
        self.client = Anthropic(
            api_key=config.anthropic_api_key,
            base_url=config.anthropic_base_url,  # None 时 SDK 用官方默认地址
            timeout=_LLM_TIMEOUT_SECONDS,
            max_retries=0,  # SDK 内置重试关闭：退避策略统一在 core/retry.py，可测可控
        )
        self.llm = ResilientLLM(self.client)
        self.last_trace_path: Path | None = None
        self.last_report_path: Path | None = None
        self.last_cost: dict | None = None

    def analyze(
        self,
        symbol: str,
        *,
        context: AnalysisContext,
        deep: bool = False,
        model_override: str | None = None,
        provider: MarketDataProvider | None = None,
        **param_overrides: Any,
    ) -> str:
        provider = provider or ProviderRegistry.for_symbol(symbol)
        role = "deep_report" if deep else "synthesis"
        chain = self.router.resolve_chain(role, model=model_override, **param_overrides)

        trace = new_trace(
            symbol, deep, [s.model for s in chain], prompt_version(SYSTEM_PROMPT), context
        )
        # M2-B0：会话证据化基座——捕获库 + 证据库 + 记录器（bindings 随 trace 落盘）
        capture_root = DEFAULT_CAPTURE_ROOT / trace.created_at.strftime("%Y-%m-%d") / trace.trace_id
        recorder = ToolRecorder(
            context=context,
            evidence=EvidenceStore(symbol),
            captures=CaptureStore(capture_root),
        )
        trace.capture_root = str(capture_root)
        self.last_trace_path = None
        ledger = CostLedger(self.config.pricing)
        try:
            return self._run_loop(symbol, deep, provider, chain, trace, context, recorder, ledger)
        except BellwetherError as exc:
            trace.outcome = f"error:{type(exc).__name__}"
            raise
        finally:
            trace.evidence_bindings = recorder.bindings
            summary = ledger.summary()
            trace.cost = summary
            self.last_cost = summary
            self.last_trace_path = write_trace(trace)

    def _run_loop(
        self,
        symbol: str,
        deep: bool,
        provider: MarketDataProvider,
        chain: list[ModelSpec],
        trace: AnalysisTrace,
        context: AnalysisContext,
        recorder: ToolRecorder,
        ledger: CostLedger,
    ) -> str:
        primary_model = chain[0].model
        role = "deep_report" if deep else "synthesis"
        budget_usd = self.config.budget.deep_usd if deep else self.config.budget.quick_usd
        messages: list[dict] = [{"role": "user", "content": analyze_prompt(symbol, deep)}]
        tools = [*tools_mod.TOOL_SCHEMAS, SUBMIT_REPORT_TOOL]
        # prompt caching（M2/RFC-003 D3）：system+tools 逐轮不变，缓存断点打在 tools 末尾
        # （覆盖 tools+system 前缀）。中转不支持 cache_control 时经 [api].prompt_caching 关闭。
        system_param: Any = SYSTEM_PROMPT
        if self.config.api.prompt_caching:
            system_param = [
                {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
            ]
            tools = [*tools[:-1], {**tools[-1], "cache_control": {"type": "ephemeral"}}]
        submit_rejections = 0
        force_submit = False
        last_violations: list[str] | None = None

        for _ in range(_MAX_TURNS):
            create_kwargs: dict[str, Any] = {
                "system": system_param,
                "tools": tools,
                "messages": messages,
            }
            if force_submit:
                create_kwargs["tool_choice"] = {"type": "tool", "name": "submit_report"}
            if ledger.total_usd >= budget_usd:
                raise BudgetExceededError(
                    f"预算超限：已花费 ${ledger.total_usd:.4f}，"
                    f"{'deep' if deep else 'quick'} 档上限 ${budget_usd:.2f}"
                )
            call_start = time.monotonic()
            resp, used = self.llm.create(chain, **create_kwargs)
            latency_s = time.monotonic() - call_start
            usage = getattr(resp, "usage", None)
            input_tokens = usage.input_tokens if usage is not None else 0
            output_tokens = usage.output_tokens if usage is not None else 0
            cache_read = (getattr(usage, "cache_read_input_tokens", 0) or 0) if usage else 0
            cache_write = (getattr(usage, "cache_creation_input_tokens", 0) or 0) if usage else 0
            ledger.record_llm(
                used.model,
                input_tokens,
                output_tokens,
                latency_s,
                cache_read_tokens=cache_read,
                cache_write_tokens=cache_write,
            )
            trace.llm_calls.append(
                LLMCallRecord(
                    model=used.model,
                    stop_reason=resp.stop_reason,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_read_tokens=cache_read,
                    cache_write_tokens=cache_write,
                    latency_s=latency_s,
                )
            )
            _log.info(
                "llm_call",
                model=used.model,
                stop_reason=resp.stop_reason,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_s=round(latency_s, 3),
            )
            trace.final_model = used.model
            # 降级后从降级档继续：之前的档已证明失败，后续轮次不再逐轮撞它
            if used is not chain[0]:
                trace.degraded = True
                chain = chain[chain.index(used) :]

            if resp.stop_reason != "tool_use":
                # 契约要求终稿必须经 submit_report 管道（spec-001 §1）：追加提醒并强制一轮
                if not force_submit:
                    force_submit = True
                    messages.append({"role": "assistant", "content": resp.content})
                    messages.append(
                        {
                            "role": "user",
                            "content": "请调用 submit_report 工具提交结构化终稿"
                            "（数字一律用 [E12] 证据令牌）。",
                        }
                    )
                    continue
                trace.outcome = "unstructured"
                text = _collect_text(resp) + (
                    "\n\n> ⚠️ 本次输出未经构造性核验管道（模型未提交结构化报告），"
                    "数字未经证据令牌核验。"
                )
                return _finalize(text, primary_model, used.model)

            messages.append({"role": "assistant", "content": resp.content})
            tool_results = []
            submitted_report = None
            for block in resp.content:
                if getattr(block, "type", None) != "tool_use":
                    continue
                tool_name = block.name  # type: ignore[union-attr]
                tool_input = block.input  # type: ignore[union-attr]
                tool_use_id = block.id  # type: ignore[union-attr]

                if tool_name == "submit_report":
                    lenient = submit_rejections >= _MAX_SUBMIT_REJECTIONS
                    result = assemble_report(
                        dict(tool_input),
                        store=recorder.evidence,
                        context=context,
                        symbol=symbol,
                        market=provider.market,
                        tier="deep" if deep else "quick",
                        model_versions={role: used.model},
                        prompt_versions={"system": prompt_version(SYSTEM_PROMPT)},
                        provenance_ref=trace.trace_id,
                        data_types_present=_data_types(recorder),
                        lenient=lenient,
                        cost_usd=ledger.total_usd if ledger.total_usd > 0 else None,
                    )
                    outcome = (
                        "accepted"
                        if result.report is not None
                        else f"rejected: {'; '.join(result.violations)}"
                    )
                    trace.tool_calls.append(
                        ToolCallRecord(
                            name="submit_report",
                            input=dict(tool_input),
                            output=outcome[:2000],
                            tool_use_id=tool_use_id,
                        )
                    )
                    _log.info("submit", accepted=result.report is not None)
                    if result.report is None:
                        # 止损（2026-07-26 回放踩坑）：连续两次逐字相同的拒绝原因，
                        # 说明重写改变不了结果（多为管道级 schema 错误，LLM 无法修复）
                        # ——继续烧轮次只浪费预算，立即诚实失败并留 trace 定位。
                        if result.violations == last_violations:
                            raise ToolError(
                                "报告校验错误重写后原样复现（疑似管道实现缺陷，非草稿问题）："
                                + "; ".join(result.violations)[:500]
                            )
                        last_violations = result.violations
                        submit_rejections += 1
                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": tool_use_id,
                                "content": json.dumps(
                                    {"status": "rejected", "violations": result.violations},
                                    ensure_ascii=False,
                                ),
                            }
                        )
                        continue
                    trace.dropped_claims = [
                        f"{d['reason']}：{d['text'][:100]}" for d in result.dropped
                    ]
                    submitted_report = result.report
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_use_id,
                            "content": '{"status": "accepted"}',
                        }
                    )
                    continue

                recorder.current_tool_call_id = tool_use_id  # RFC-000 §6 tool_call_id
                output = tools_mod.execute_tool(
                    tool_name, tool_input, provider, context=context, trace=recorder
                )
                _log.info("tool_call", name=tool_name)
                trace.tool_calls.append(
                    ToolCallRecord(
                        name=tool_name,
                        input=dict(tool_input),
                        output=output,
                        tool_use_id=tool_use_id,
                    )
                )
                tool_results.append(
                    {"type": "tool_result", "tool_use_id": tool_use_id, "content": output}
                )

            messages.append({"role": "user", "content": tool_results})
            if submitted_report is not None:
                trace.outcome = "ok"
                self.last_report_path = write_report_json(submitted_report, trace.trace_id)
                return _finalize(
                    render_report(submitted_report), primary_model, trace.final_model or ""
                )

        trace.outcome = "max_turns"
        return "（已达到最大工具调用轮次，未能得出最终研判。可稍后重试。）"


def _finalize(text: str, primary_model: str, used_model: str) -> str:
    """降级发生时在报告尾部明示（红线 4 的数据时效标注同级要求：诚实呈现）。"""
    if used_model != primary_model:
        text += (
            f"\n\n> ⚠️ 模型降级说明：原定模型 `{primary_model}` 持续不可用，"
            f"本报告（或其部分轮次）由降级模型 `{used_model}` 生成。"
        )
    return text


def _collect_text(resp: Message) -> str:
    texts = [b for b in resp.content if getattr(b, "type", None) == "text"]
    parts = [t.text for t in texts]  # type: ignore[union-attr]
    return "\n".join(parts).strip()
