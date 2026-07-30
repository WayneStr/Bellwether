"""OpenAI Responses API 适配器：把 Anthropic Messages 形状翻译成 `/v1/responses`。

设计（鸭子类型收敛）：对外暴露 `.messages.create(...)`，接 Anthropic 风格入参
（model/max_tokens/temperature/system/tools/messages/tool_choice），内部转 Responses
请求、POST、再把响应解析成一个「长得像 anthropic.types.Message」的对象
（`.content` 块列表 / `.stop_reason` / `.usage`）。这样 orchestrator、ResilientLLM、
trace、成本逻辑几乎不动——全部翻译收敛在本模块。

错误统一翻译成项目现有 `LLMError` 子类：429→LLMRateLimitError、5xx/上游→
LLMConnectionError（二者被 llm_retry 退避重试）、401/403→LLMAuthError、404→
ModelNotFoundError（由降级链决定是否换档）。与 llm.py 的 anthropic 版一一对应。

翻译逻辑（build/parse/translate）是纯函数，dict 进 dict 出，可完全离线单测；
只有 `_post` 触网。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ..core.exceptions import (
    LLMAuthError,
    LLMConnectionError,
    LLMError,
    LLMRateLimitError,
    ModelNotFoundError,
)
from ..core.redact import redact


# ─────────── 鸭子类型：够 orchestrator/trace/ledger 用即可 ───────────
@dataclass
class TextBlock:
    text: str
    type: str = "text"


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]
    type: str = "tool_use"


@dataclass
class Usage:
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0  # OpenAI 端自动缓存，无「写入」概念，恒 0


@dataclass
class Message:
    content: list[TextBlock | ToolUseBlock]
    stop_reason: str
    usage: Usage
    model: str = ""
    role: str = "assistant"
    type: str = "message"


# ─────────────────────── 纯翻译函数 ───────────────────────
def _system_to_instructions(system: Any) -> str | None:
    """Anthropic system（str 或 [{type:text,text,cache_control}]）→ Responses instructions。"""
    if system is None:
        return None
    if isinstance(system, str):
        return system or None
    parts = [
        b.get("text", "") if isinstance(b, dict) else str(b)
        for b in system
        if (isinstance(b, dict) and b.get("type") == "text") or isinstance(b, str)
    ]
    joined = "\n".join(p for p in parts if p)
    return joined or None


def _tools_to_responses(tools: Any) -> list[dict[str, Any]] | None:
    """Anthropic tools（{name,description,input_schema[,cache_control]}）→ Responses 扁平 function
    工具（{type:function,name,description,parameters}）。cache_control 等无关键丢弃。"""
    if not tools:
        return None
    out = []
    for t in tools:
        out.append(
            {
                "type": "function",
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
            }
        )
    return out


def _tool_choice_to_responses(tool_choice: Any) -> Any:
    """{type:tool,name:X} → {type:function,name:X}；auto/required/none 原样透传。"""
    if tool_choice is None:
        return None
    if isinstance(tool_choice, dict) and tool_choice.get("type") == "tool":
        return {"type": "function", "name": tool_choice["name"]}
    return tool_choice


def _block_type(block: Any) -> str | None:
    return block.get("type") if isinstance(block, dict) else getattr(block, "type", None)


def _messages_to_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Anthropic messages → Responses input 数组。

    - {"role":"user"/"assistant","content": str} → 角色消息项
    - assistant content 里的本模块 TextBlock/ToolUseBlock（上一轮 resp.content 塞回）
      → 文本消息项 / function_call 项（call_id 保留以便 function_call_output 对齐）
    - user content 里的 {"type":"tool_result",tool_use_id,content} → function_call_output 项
    """
    items: list[dict[str, Any]] = []
    for m in messages:
        role = m["role"]
        content = m["content"]
        if isinstance(content, str):
            items.append({"role": role, "content": content})
            continue
        for block in content:
            btype = _block_type(block)
            if btype == "tool_use":
                items.append(
                    {
                        "type": "function_call",
                        "call_id": block.id,
                        "name": block.name,
                        "arguments": json.dumps(block.input, ensure_ascii=False),
                    }
                )
            elif btype == "text":
                text = block.get("text", "") if isinstance(block, dict) else block.text
                if text:
                    items.append({"role": role, "content": text})
            elif btype == "tool_result":
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": block["tool_use_id"],
                        "output": block["content"],
                    }
                )
    return items


def build_responses_request(
    *,
    model: str,
    max_tokens: int,
    temperature: float | None,
    system: Any,
    tools: Any,
    messages: list[dict[str, Any]],
    tool_choice: Any = None,
) -> dict[str, Any]:
    """组装 `/v1/responses` 请求体。store=False：不在服务端留存，状态由本地 messages 维护。"""
    body: dict[str, Any] = {
        "model": model,
        "input": _messages_to_input(messages),
        "max_output_tokens": max_tokens,
        "store": False,
    }
    instructions = _system_to_instructions(system)
    if instructions:
        body["instructions"] = instructions
    rtools = _tools_to_responses(tools)
    if rtools:
        body["tools"] = rtools
    tc = _tool_choice_to_responses(tool_choice)
    if tc is not None:
        body["tool_choice"] = tc
    if temperature is not None:
        body["temperature"] = temperature
    return body


def parse_responses_response(data: dict[str, Any], model: str) -> Message:
    """Responses 响应 → 鸭子 Message。output 类型化项：message(output_text)/function_call；
    reasoning 等内部项跳过。有 function_call → stop_reason=tool_use（触发工具循环）。"""
    content: list[TextBlock | ToolUseBlock] = []
    for item in data.get("output") or []:
        itype = item.get("type")
        if itype == "message":
            for part in item.get("content") or []:
                if part.get("type") in ("output_text", "text"):
                    content.append(TextBlock(text=part.get("text", "")))
        elif itype == "function_call":
            try:
                parsed = json.loads(item.get("arguments") or "{}")
            except (json.JSONDecodeError, TypeError):
                parsed = {}
            content.append(
                ToolUseBlock(
                    id=item.get("call_id") or item.get("id") or "",
                    name=item.get("name", ""),
                    input=parsed if isinstance(parsed, dict) else {},
                )
            )
    # 兜底：未抽到文本但有便利字段 output_text
    if not any(b.type == "text" for b in content) and data.get("output_text"):
        content.append(TextBlock(text=str(data["output_text"])))

    if any(b.type == "tool_use" for b in content):
        stop_reason = "tool_use"
    else:
        incomplete = (data.get("incomplete_details") or {}).get("reason")
        stop_reason = (
            "max_tokens"
            if data.get("status") == "incomplete"
            and incomplete in ("max_output_tokens", "max_tokens")
            else "end_turn"
        )

    u = data.get("usage") or {}
    cached = (u.get("input_tokens_details") or {}).get("cached_tokens") or 0
    usage = Usage(
        input_tokens=u.get("input_tokens", 0) or 0,
        output_tokens=u.get("output_tokens", 0) or 0,
        cache_read_input_tokens=cached,
    )
    return Message(content=content, stop_reason=stop_reason, usage=usage, model=model)


def translate_openai_http_error(status: int, body: Any) -> LLMError:
    """OpenAI/中转 HTTP 错误 → 类型化 LLMError（可重试性见 core/exceptions & retry）。

    消息统一脱敏（D6）：劣质中转可能在错误体里回显 key。
    """
    msg = ""
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            msg = err.get("message", "")
        else:
            msg = body.get("message") or (str(err) if err else "")
    msg = redact(str(msg))[:300]
    if status in (401, 403):
        return LLMAuthError(f"认证失败（HTTP {status}）：检查 API key 与端点地址。{msg}")
    if status == 404:
        return ModelNotFoundError(f"模型不存在或端点不支持（HTTP 404）：{msg}")
    if status == 429:
        return LLMRateLimitError(f"限流（HTTP 429）：{msg}")
    if status >= 500:
        return LLMConnectionError(f"服务端/上游瞬态错误（HTTP {status}）：{msg}")
    return LLMError(f"LLM 调用失败（HTTP {status}）：{msg}")


def _is_param_unsupported(body: Any) -> bool:
    """判断 400 是否为「参数不被支持」（推理模型常拒非默认 temperature）。"""
    if not isinstance(body, dict):
        return False
    err = body.get("error")
    if not isinstance(err, dict):
        err = {}
    text = f"{err.get('message', '')} {err.get('param', '')}".lower()
    return "temperature" in text or "unsupported" in text or "not supported" in text


# ─────────────────────── 触网客户端 ───────────────────────
def _responses_url(base_url: str | None) -> str:
    base = (base_url or "https://api.openai.com").rstrip("/")
    return base + ("/responses" if base.endswith("/v1") else "/v1/responses")


class _Messages:
    def __init__(self, parent: OpenAIResponsesClient) -> None:
        self._p = parent

    def create(
        self,
        *,
        model: str,
        max_tokens: int,
        temperature: float | None = None,
        system: Any = None,
        tools: Any = None,
        messages: list[dict[str, Any]],
        tool_choice: Any = None,
        **_ignored: Any,
    ) -> Message:
        return self._p._create(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            tools=tools,
            messages=messages,
            tool_choice=tool_choice,
        )


class OpenAIResponsesClient:
    """鸭子兼容 anthropic.Anthropic 的最小面：`.messages.create(...)`。供 ResilientLLM 直接包裹。"""

    def __init__(self, *, api_key: str | None, base_url: str | None = None, timeout: float = 300.0):
        self._url = _responses_url(base_url)
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        self._timeout = timeout
        self.messages = _Messages(self)

    def _post(self, body: dict[str, Any]) -> tuple[int, Any]:
        import httpx

        try:
            r = httpx.post(self._url, headers=self._headers, json=body, timeout=self._timeout)
        except httpx.TimeoutException as exc:
            raise LLMConnectionError(f"请求超时：{redact(str(exc))}") from exc
        except httpx.HTTPError as exc:
            raise LLMConnectionError(f"连接失败：{redact(str(exc))}") from exc
        try:
            return r.status_code, r.json()
        except (json.JSONDecodeError, ValueError):
            return r.status_code, {"error": {"message": r.text[:200]}}

    def _create(
        self,
        *,
        model: str,
        max_tokens: int,
        temperature: float | None,
        system: Any,
        tools: Any,
        messages: list[dict[str, Any]],
        tool_choice: Any,
    ) -> Message:
        body = build_responses_request(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            tools=tools,
            messages=messages,
            tool_choice=tool_choice,
        )
        status, data = self._post(body)
        # 推理模型常拒非默认 temperature：去掉重试一次（非瞬态错，不走 llm_retry）
        if status == 400 and "temperature" in body and _is_param_unsupported(data):
            body.pop("temperature")
            status, data = self._post(body)
        if status != 200:
            raise translate_openai_http_error(status, data)
        return parse_responses_response(data, model)
