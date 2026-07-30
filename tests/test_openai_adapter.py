"""OpenAI Responses 适配器单测：翻译逻辑纯函数、离线可复现（合成 payload，不触网）。

覆盖：请求组装（messages/system/tools/tool_choice/参数映射）、响应解析（文本/工具
调用/截断/usage+cache）、错误翻译（各 HTTP 码→类型化异常）、往返（resp.content 塞回
messages 能重新翻译成 Responses input）、temperature 被拒的兜底重试。
"""

from __future__ import annotations

import pytest

from bellwether.agent.openai_adapter import (
    Message,
    OpenAIResponsesClient,
    TextBlock,
    ToolUseBlock,
    build_responses_request,
    parse_responses_response,
    translate_openai_http_error,
)
from bellwether.core.exceptions import (
    LLMAuthError,
    LLMConnectionError,
    LLMError,
    LLMRateLimitError,
    ModelNotFoundError,
)

SYS = "你是股票研究助手。"
TOOLS = [
    {
        "name": "get_price_history",
        "description": "取行情",
        "input_schema": {"type": "object", "properties": {"symbol": {"type": "string"}}},
        "cache_control": {"type": "ephemeral"},  # 应被丢弃
    }
]


# ───────────────────── 请求组装 ─────────────────────
def test_build_request_basic_params():
    body = build_responses_request(
        model="gpt-5.6-terra",
        max_tokens=4096,
        temperature=0.2,
        system=SYS,
        tools=TOOLS,
        messages=[{"role": "user", "content": "分析 AAPL"}],
        tool_choice=None,
    )
    assert body["model"] == "gpt-5.6-terra"
    assert body["max_output_tokens"] == 4096  # max_tokens → max_output_tokens
    assert body["temperature"] == 0.2
    assert body["store"] is False
    assert body["instructions"] == SYS
    assert body["input"] == [{"role": "user", "content": "分析 AAPL"}]


def test_build_request_tools_flattened_and_cache_control_stripped():
    body = build_responses_request(
        model="m",
        max_tokens=8,
        temperature=None,
        system=None,
        tools=TOOLS,
        messages=[{"role": "user", "content": "hi"}],
        tool_choice=None,
    )
    (tool,) = body["tools"]
    assert tool["type"] == "function"
    assert tool["name"] == "get_price_history"
    assert tool["parameters"] == TOOLS[0]["input_schema"]
    assert "cache_control" not in tool
    assert "temperature" not in body  # None 不下发


def test_build_request_system_blocks_with_cache_control():
    """system 为带 cache_control 的 text 块列表（prompt_caching 开时的形态）→ 拼成 instructions。"""
    body = build_responses_request(
        model="m",
        max_tokens=8,
        temperature=0.0,
        system=[{"type": "text", "text": SYS, "cache_control": {"type": "ephemeral"}}],
        tools=None,
        messages=[{"role": "user", "content": "hi"}],
        tool_choice=None,
    )
    assert body["instructions"] == SYS


def test_build_request_tool_choice_forced():
    body = build_responses_request(
        model="m",
        max_tokens=8,
        temperature=0.0,
        system=None,
        tools=TOOLS,
        messages=[{"role": "user", "content": "hi"}],
        tool_choice={"type": "tool", "name": "submit_report"},
    )
    assert body["tool_choice"] == {"type": "function", "name": "submit_report"}


def test_build_request_translates_assistant_blocks_and_tool_results():
    """往返关键：上一轮 resp.content 块塞回 assistant + tool_result → Responses input。"""
    prior = [
        TextBlock(text="我来取数据"),
        ToolUseBlock(id="call_1", name="get_price_history", input={"symbol": "AAPL"}),
    ]
    messages = [
        {"role": "user", "content": "分析 AAPL"},
        {"role": "assistant", "content": prior},
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "call_1", "content": '{"last": 321}'}
            ],
        },
    ]
    body = build_responses_request(
        model="m",
        max_tokens=8,
        temperature=None,
        system=None,
        tools=None,
        messages=messages,
        tool_choice=None,
    )
    assert body["input"] == [
        {"role": "user", "content": "分析 AAPL"},
        {"role": "assistant", "content": "我来取数据"},
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "get_price_history",
            "arguments": '{"symbol": "AAPL"}',
        },
        {"type": "function_call_output", "call_id": "call_1", "output": '{"last": 321}'},
    ]


# ───────────────────── 响应解析 ─────────────────────
def test_parse_text_response():
    data = {
        "status": "completed",
        "output": [
            {"type": "reasoning", "summary": []},  # 内部项应跳过
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "AAPL 收于 321"}],
            },
        ],
        "usage": {
            "input_tokens": 100,
            "output_tokens": 20,
            "input_tokens_details": {"cached_tokens": 64},
        },
    }
    msg = parse_responses_response(data, "gpt-5.5")
    assert isinstance(msg, Message)
    assert msg.stop_reason == "end_turn"
    assert [b.type for b in msg.content] == ["text"]
    assert msg.content[0].text == "AAPL 收于 321"
    assert msg.usage.input_tokens == 100
    assert msg.usage.output_tokens == 20
    assert msg.usage.cache_read_input_tokens == 64  # cached_tokens → cache_read
    assert msg.usage.cache_creation_input_tokens == 0


def test_parse_function_call_response_sets_tool_use_stop():
    data = {
        "status": "completed",
        "output": [
            {
                "type": "function_call",
                "call_id": "call_9",
                "name": "get_price_history",
                "arguments": '{"symbol": "600519"}',
            },
        ],
        "usage": {"input_tokens": 50, "output_tokens": 10},
    }
    msg = parse_responses_response(data, "m")
    assert msg.stop_reason == "tool_use"  # 触发 orchestrator 工具循环
    (block,) = msg.content
    assert isinstance(block, ToolUseBlock)
    assert block.id == "call_9"
    assert block.name == "get_price_history"
    assert block.input == {"symbol": "600519"}


def test_parse_incomplete_maps_to_max_tokens():
    data = {
        "status": "incomplete",
        "incomplete_details": {"reason": "max_output_tokens"},
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "截断"}],
            }
        ],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    assert parse_responses_response(data, "m").stop_reason == "max_tokens"


def test_parse_falls_back_to_output_text_convenience():
    data = {
        "status": "completed",
        "output": [],
        "output_text": "便利字段文本",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    msg = parse_responses_response(data, "m")
    assert msg.content[0].text == "便利字段文本"


def test_parse_malformed_function_args_tolerated():
    data = {
        "status": "completed",
        "output": [{"type": "function_call", "call_id": "c", "name": "t", "arguments": "not json"}],
        "usage": {},
    }
    msg = parse_responses_response(data, "m")
    assert msg.content[0].input == {}  # 解析失败退化空 dict，不炸


# ───────────────────── 错误翻译 ─────────────────────
@pytest.mark.parametrize(
    "status,exc",
    [
        (401, LLMAuthError),
        (403, LLMAuthError),
        (404, ModelNotFoundError),
        (429, LLMRateLimitError),
        (500, LLMConnectionError),
        (502, LLMConnectionError),  # 中转上游断
        (503, LLMConnectionError),  # 中转过载
        (400, LLMError),
    ],
)
def test_translate_http_errors(status, exc):
    assert isinstance(translate_openai_http_error(status, {"error": {"message": "boom"}}), exc)


def test_translate_error_redacts_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-leaked-in-body")
    err = translate_openai_http_error(
        401, {"error": {"message": "bad key sk-secret-leaked-in-body"}}
    )
    assert "sk-secret-leaked-in-body" not in str(err)


# ───────────────────── 触网 _create（mock _post）─────────────────────
def _client() -> OpenAIResponsesClient:
    return OpenAIResponsesClient(api_key="k", base_url="https://relay.example.xyz")


def test_create_success(monkeypatch):
    client = _client()
    ok = {
        "status": "completed",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "pong"}],
            }
        ],
        "usage": {"input_tokens": 3, "output_tokens": 1},
    }
    monkeypatch.setattr(client, "_post", lambda body: (200, ok))
    msg = client.messages.create(
        model="gpt-5.5",
        max_tokens=16,
        temperature=0.2,
        system=SYS,
        tools=None,
        messages=[{"role": "user", "content": "ping"}],
    )
    assert msg.content[0].text == "pong"


def test_create_retries_without_temperature_on_param_reject(monkeypatch):
    """推理模型拒 temperature：适配器去掉重试一次并成功。"""
    client = _client()
    calls: list[dict] = []
    ok = {"status": "completed", "output": [], "output_text": "ok", "usage": {}}
    reject = {
        "error": {
            "message": "Unsupported value: 'temperature' must be default",
            "param": "temperature",
        }
    }

    def fake_post(body):
        calls.append(dict(body))
        return (200, ok) if "temperature" not in body else (400, reject)

    monkeypatch.setattr(client, "_post", fake_post)
    msg = client.messages.create(
        model="gpt-5.6-sol",
        max_tokens=8,
        temperature=0.3,
        system=None,
        tools=None,
        messages=[{"role": "user", "content": "hi"}],
    )
    assert msg.content[0].text == "ok"
    assert len(calls) == 2  # 首次带 temperature 被拒，去掉后成功
    assert "temperature" in calls[0] and "temperature" not in calls[1]


def test_create_raises_typed_error_on_503(monkeypatch):
    """中转上游 503 → LLMConnectionError（被 llm_retry 重试、降级链兜底）。"""
    client = _client()
    monkeypatch.setattr(
        client,
        "_post",
        lambda body: (503, {"error": {"message": "Service temporarily unavailable"}}),
    )
    with pytest.raises(LLMConnectionError):
        client.messages.create(
            model="gpt-5.5",
            max_tokens=8,
            temperature=None,
            system=None,
            tools=None,
            messages=[{"role": "user", "content": "hi"}],
        )


def test_create_400_non_param_error_not_retried(monkeypatch):
    """非参数类 400 不触发去-temperature 重试，直接抛 LLMError。"""
    client = _client()
    calls = []

    def fake_post(body):
        calls.append(1)
        return (400, {"error": {"message": "bad request: malformed input"}})

    monkeypatch.setattr(client, "_post", fake_post)
    with pytest.raises(LLMError):
        client.messages.create(
            model="m",
            max_tokens=8,
            temperature=0.2,
            system=None,
            tools=None,
            messages=[{"role": "user", "content": "hi"}],
        )
    assert len(calls) == 1  # 只调一次，未误重试


def test_responses_url_normalization():
    from bellwether.agent.openai_adapter import _responses_url

    assert _responses_url("https://x.xyz") == "https://x.xyz/v1/responses"
    assert _responses_url("https://x.xyz/") == "https://x.xyz/v1/responses"
    assert _responses_url("https://api.openai.com/v1") == "https://api.openai.com/v1/responses"
    assert _responses_url(None) == "https://api.openai.com/v1/responses"
