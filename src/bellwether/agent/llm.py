"""LLM 调用可靠性层（ROADMAP D2）：SDK 异常翻译 + 重试 + 模型档降级链。

翻译原则：按异常类型决定重试/降级，不做字符串匹配。唯一例外是中转的
「503 model_not_found」——非标准状态码下只能看 body 辨认（HANDOFF §5：用户经
第三方中转调 API，行为与官方有差异）。

降级原则：仅在「换模型有意义」的失败（重试耗尽的限流/瞬态、模型不存在）时
降档；LLMAuthError 换模型救不了坏 key，立即明示失败。
"""

from __future__ import annotations

from typing import Any

import anthropic
from anthropic.types import Message

from ..config import AppConfig
from ..core.exceptions import (
    LLMAuthError,
    LLMConnectionError,
    LLMError,
    LLMRateLimitError,
    ModelNotFoundError,
)
from ..core.redact import redact
from ..core.retry import llm_retry
from ..models import ModelSpec


def translate_anthropic_error(exc: anthropic.APIError) -> LLMError:
    """anthropic SDK 异常 → Bellwether 类型化异常（可重试性见 exceptions.py 约定）。

    消息统一脱敏（D6）：劣质中转可能在错误 body 里回显收到的 key。
    """
    cls, msg = _classify(exc)
    return cls(redact(msg))


def _classify(exc: anthropic.APIError) -> tuple[type[LLMError], str]:
    if isinstance(exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
        return LLMAuthError, (
            f"认证失败（HTTP {exc.status_code}）：检查 ANTHROPIC_API_KEY 与中转地址。{exc}"
        )
    if isinstance(exc, anthropic.NotFoundError):
        return ModelNotFoundError, f"模型不存在或中转不支持（HTTP 404）：{exc}"
    if isinstance(exc, (anthropic.RateLimitError, anthropic.OverloadedError)):
        return LLMRateLimitError, f"限流/过载（HTTP {exc.status_code}）：{exc}"
    if isinstance(exc, anthropic.APIStatusError):
        body = str(getattr(exc, "body", None) or exc)
        if "model_not_found" in body:  # 中转用 503/4xx 报模型错，只能看 body
            return ModelNotFoundError, f"模型不可用（HTTP {exc.status_code}）：{body[:200]}"
        if exc.status_code >= 500:
            return LLMConnectionError, f"服务端瞬态错误（HTTP {exc.status_code}）：{body[:200]}"
        return LLMError, f"LLM 调用失败（HTTP {exc.status_code}）：{body[:200]}"
    if isinstance(exc, anthropic.APIConnectionError):  # 含 APITimeoutError
        return LLMConnectionError, f"连接失败/超时：{exc}"
    return LLMError, str(exc)


class ResilientLLM:
    """带重试与降级链的 messages.create 包装。orchestrator 每轮经此调用。"""

    def __init__(self, client: anthropic.Anthropic):
        self._client = client

    def create(self, chain: list[ModelSpec], **create_kwargs: Any) -> tuple[Message, ModelSpec]:
        """按链依次尝试，返回 (响应, 实际使用的 spec)。

        每档内部先按 llm_retry 退避重试；档失败（不可重试或重试耗尽）换下一档。
        全链失败抛最后一个类型化异常（明示失败）。
        """
        last_error: LLMError | None = None
        for spec in chain:
            try:
                return self._create_once(spec, **create_kwargs), spec
            except LLMAuthError:
                raise  # 坏 key 换档无意义：立即明示，不掩盖配置问题
            except LLMError as exc:
                last_error = exc
        assert last_error is not None  # chain 非空（resolve_chain 至少返回主选）
        raise last_error

    @llm_retry
    def _create_once(self, spec: ModelSpec, **create_kwargs: Any) -> Message:
        try:
            return self._client.messages.create(
                model=spec.model,
                max_tokens=spec.params.max_tokens,
                temperature=spec.params.temperature,
                **create_kwargs,
            )
        except anthropic.APIError as exc:
            raise translate_anthropic_error(exc) from exc


def build_llm_client(config: AppConfig, *, timeout: float = 300.0) -> Any:
    """按 [api].provider 选客户端：openai→Responses 适配器（httpx）；anthropic→官方 SDK。

    二者都鸭子暴露 `.messages.create`，供 ResilientLLM 统一包裹；两侧的瞬态退避都归
    core/retry.py（故 SDK 内置重试关闭），异常都翻译成同一套 LLMError。
    """
    if config.api.provider == "openai":
        from .openai_adapter import OpenAIResponsesClient

        return OpenAIResponsesClient(
            api_key=config.api_key, base_url=config.base_url, timeout=timeout
        )
    return anthropic.Anthropic(
        api_key=config.api_key,
        base_url=config.base_url,
        timeout=timeout,
        max_retries=0,
    )
