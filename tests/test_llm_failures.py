"""LLM 侧故障注入（ROADMAP D2 验收面之二）：异常翻译、重试、模型档降级、明示失败。

用假 client 注入 SDK 异常脚本，不打网；tenacity sleep 由 conftest 打桩。
"""

import anthropic
import httpx
import pytest

from bellwether.agent.llm import ResilientLLM, translate_anthropic_error
from bellwether.agent.router import ModelRouter
from bellwether.core.exceptions import (
    LLMAuthError,
    LLMConnectionError,
    LLMError,
    LLMRateLimitError,
    ModelNotFoundError,
)
from bellwether.models import ModelConfig, ModelParams, ModelSpec


def _api_error(cls, status: int, body=None):
    """构造带 httpx 响应的 anthropic SDK 异常。"""
    request = httpx.Request("POST", "https://api.test/v1/messages")
    response = httpx.Response(status, request=request, json=body or {})
    return cls(f"HTTP {status}", response=response, body=body)


# ─────────────────────────── SDK 异常翻译 ───────────────────────────
@pytest.mark.parametrize(
    ("sdk_exc", "expected"),
    [
        (_api_error(anthropic.AuthenticationError, 401), LLMAuthError),
        (_api_error(anthropic.PermissionDeniedError, 403), LLMAuthError),
        (_api_error(anthropic.NotFoundError, 404), ModelNotFoundError),
        (_api_error(anthropic.RateLimitError, 429), LLMRateLimitError),
        (_api_error(anthropic.OverloadedError, 529), LLMRateLimitError),
        (_api_error(anthropic.InternalServerError, 500), LLMConnectionError),
        (
            anthropic.APITimeoutError(
                request=httpx.Request("POST", "https://api.test/v1/messages")
            ),
            LLMConnectionError,
        ),
    ],
)
def test_translate_by_type(sdk_exc, expected):
    assert isinstance(translate_anthropic_error(sdk_exc), expected)


def test_translate_relay_model_not_found_in_body():
    # 中转用 503 报 model_not_found（HANDOFF §5），只能看 body 辨认
    exc = _api_error(
        anthropic.InternalServerError,
        503,
        body={"error": {"type": "model_not_found", "message": "no such model"}},
    )
    assert isinstance(translate_anthropic_error(exc), ModelNotFoundError)


def test_translate_other_4xx_is_plain_llm_error():
    translated = translate_anthropic_error(_api_error(anthropic.BadRequestError, 400))
    assert type(translated) is LLMError  # 编程性错误：不可重试、不降级掩盖


# ─────────────────────────── 假 client ───────────────────────────
class _EndTurn:
    """messages.create 成功返回的最小替身。"""

    stop_reason = "end_turn"

    def __init__(self, text="研判内容"):
        self.content = [type("T", (), {"type": "text", "text": text})()]


class FakeClient:
    """按脚本依次返回/抛出；记录每次调用的 model id。"""

    def __init__(self, script):
        self._script = list(script)
        self.calls: list[str] = []
        self.messages = self  # duck-typing: client.messages.create

    def create(self, **kwargs):
        self.calls.append(kwargs["model"])
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _chain():
    return [
        ModelSpec(model="primary-model", params=ModelParams(max_tokens=8192)),
        ModelSpec(model="fallback-model", params=ModelParams(max_tokens=8192)),
    ]


# ─────────────────────────── 重试 ───────────────────────────
def test_retries_rate_limit_then_succeeds():
    client = FakeClient(
        [
            _api_error(anthropic.RateLimitError, 429),
            _api_error(anthropic.RateLimitError, 429),
            _EndTurn(),
        ]
    )
    resp, used = ResilientLLM(client).create(_chain(), messages=[])
    assert resp.stop_reason == "end_turn"
    assert used.model == "primary-model"
    assert client.calls == ["primary-model"] * 3  # 同档内退避重试，未降级


# ─────────────────────────── 降级链 ───────────────────────────
def test_model_not_found_falls_back_to_next_tier():
    client = FakeClient([_api_error(anthropic.NotFoundError, 404), _EndTurn()])
    resp, used = ResilientLLM(client).create(_chain(), messages=[])
    assert used.model == "fallback-model"
    assert client.calls == ["primary-model", "fallback-model"]  # 不可重试 → 立即换档


def test_auth_error_fails_fast_no_fallback():
    client = FakeClient([_api_error(anthropic.AuthenticationError, 401)])
    with pytest.raises(LLMAuthError):
        ResilientLLM(client).create(_chain(), messages=[])
    assert client.calls == ["primary-model"]  # 坏 key 不降级：换模型救不了


def test_whole_chain_fails_raises_typed_error():
    client = FakeClient(
        [
            _api_error(anthropic.NotFoundError, 404),
            _api_error(anthropic.NotFoundError, 404),
        ]
    )
    with pytest.raises(ModelNotFoundError):
        ResilientLLM(client).create(_chain(), messages=[])
    assert client.calls == ["primary-model", "fallback-model"]  # 明示失败前每档都试过


def test_retryable_exhausted_then_falls_back():
    # 主档持续 429 重试 3 次耗尽 → 降级档成功
    client = FakeClient(
        [
            _api_error(anthropic.RateLimitError, 429),
            _api_error(anthropic.RateLimitError, 429),
            _api_error(anthropic.RateLimitError, 429),
            _EndTurn(),
        ]
    )
    resp, used = ResilientLLM(client).create(_chain(), messages=[])
    assert used.model == "fallback-model"
    assert client.calls == ["primary-model"] * 3 + ["fallback-model"]


# ─────────────────────────── resolve_chain ───────────────────────────
def test_resolve_chain_orders_tiers_and_keeps_params():
    router = ModelRouter(ModelConfig())
    chain = router.resolve_chain("deep_report")
    config = ModelConfig()
    assert [s.model for s in chain] == [
        config.deep_report.model,
        config.synthesis.model,
        config.parse.model,
    ]
    # 降级换的只是模型 id：任务参数保留 deep_report 的
    assert all(s.params.max_tokens == config.deep_report.params.max_tokens for s in chain)


def test_resolve_chain_explicit_model_disables_fallback():
    chain = ModelRouter(ModelConfig()).resolve_chain("deep_report", model="user-picked")
    assert [s.model for s in chain] == ["user-picked"]


def test_resolve_chain_dedupes_repeated_models():
    config = ModelConfig()
    config.synthesis.model = config.deep_report.model  # 用户把两个角色配成同一模型
    chain = ModelRouter(config).resolve_chain("deep_report")
    assert [s.model for s in chain] == [config.deep_report.model, config.parse.model]


# ─────────────────────────── orchestrator 端到端降级 ───────────────────────────
def test_orchestrator_degradation_is_disclosed(monkeypatch, ctx):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    from bellwether.agent.orchestrator import Orchestrator
    from bellwether.config import AppConfig

    orch = Orchestrator(AppConfig())
    # 文本终稿（未 submit）：force-submit 追问一轮后仍 end_turn → unstructured 回退
    fake = FakeClient(
        [_api_error(anthropic.NotFoundError, 404), _EndTurn("最终研判"), _EndTurn("最终研判")]
    )
    orch.llm = ResilientLLM(fake)

    report = orch.analyze("AAPL", deep=True, context=ctx)
    assert "最终研判" in report
    assert "模型降级说明" in report  # 降级必须明示，不得静默
    assert AppConfig().models.synthesis.model in report


def test_orchestrator_no_disclosure_without_degradation(monkeypatch, ctx):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    from bellwether.agent.orchestrator import Orchestrator
    from bellwether.config import AppConfig

    orch = Orchestrator(AppConfig())
    orch.llm = ResilientLLM(FakeClient([_EndTurn("正常研判"), _EndTurn("正常研判")]))

    report = orch.analyze("AAPL", context=ctx)
    assert "正常研判" in report
    assert "模型降级说明" not in report
