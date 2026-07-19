"""分析溯源最小实现（ROADMAP E4 的 M1 版）：每次 analyze 落一个 provenance 包。

包内容 = 输入哈希 + prompt/模型版本 + 完整 tool 调用记录 + LLM 轮次与降级事实，
为 M2 证据层与 trace playback（回放已录制的 LLM 输出）打底。
snapshot_ref 字段与 spec-001/RFC 对齐：M1 分析路径直连 provider（不读 A0 快照），
恒为 None，M2 接证据层后填充。

落盘默认 ~/.bellwether/traces/YYYY-MM-DD/<trace_id>.json；写入前经 redact 脱敏
（D6 零泄漏），写失败静默——溯源是旁路，绝不影响分析主流程。
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

from .context import AnalysisContext
from .redact import redact

DEFAULT_TRACE_ROOT = Path.home() / ".bellwether" / "traces"
TRACE_VERSION = 2


class ToolCallRecord(BaseModel):
    name: str
    input: dict
    output: str  # tool_result 原文（JSON 字符串），回放渲染的依据


class LLMCallRecord(BaseModel):
    model: str
    stop_reason: str | None = None


class AnalysisTrace(BaseModel):
    trace_version: int = TRACE_VERSION
    trace_id: str
    created_at: datetime
    as_of: datetime
    capture_policy: str
    symbol: str
    deep: bool
    input_hash: str
    prompt_version: str
    model_chain: list[str]
    llm_calls: list[LLMCallRecord] = Field(default_factory=list)
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    degraded: bool = False
    final_model: str | None = None
    outcome: str = "incomplete"  # ok / max_turns / error:<ExcType>
    snapshot_ref: str | None = None  # M1 恒 None（分析不走快照）；M2 证据层填充


def prompt_version(system_prompt: str) -> str:
    """prompt 版本 = 内容哈希前 12 位：客观、不靠人工维护版本号。"""
    return hashlib.sha256(system_prompt.encode()).hexdigest()[:12]


def new_trace(
    symbol: str, deep: bool, model_chain: list[str], prompt_ver: str, context: AnalysisContext
) -> AnalysisTrace:
    input_hash = hashlib.sha256(
        f"{symbol}|{deep}|{prompt_ver}|{','.join(model_chain)}".encode()
    ).hexdigest()[:16]
    return AnalysisTrace(
        trace_id=uuid.uuid4().hex,
        created_at=context.clock.now(),
        as_of=context.as_of,
        capture_policy=context.capture_policy,
        symbol=symbol,
        deep=deep,
        input_hash=input_hash,
        prompt_version=prompt_ver,
        model_chain=model_chain,
    )


def write_trace(trace: AnalysisTrace, root: Path | None = None) -> Path | None:
    """落盘（脱敏后）。任何失败返回 None，不打断主流程。"""
    try:
        base = (root or DEFAULT_TRACE_ROOT) / trace.created_at.strftime("%Y-%m-%d")
        base.mkdir(parents=True, exist_ok=True)
        path = base / f"{trace.trace_id}.json"
        path.write_text(redact(trace.model_dump_json(indent=2)), encoding="utf-8")
        return path
    except Exception:
        return None
