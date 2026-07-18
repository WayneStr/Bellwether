"""重试策略（tenacity）——ROADMAP D2。

按异常类型退避重试：只重试「可重试」异常，不可重试的立即上抛。
指数退避上限有意保守（尊重免费源），配合 A0 已有的礼貌限流与降级链。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .exceptions import LLMConnectionError, LLMRateLimitError, RateLimitError

_T = TypeVar("_T")

# 数据源：限流/连接类退避重试（1→2→4…最多 30s，4 次）；空数据类不在此列，立即上抛。
datasource_retry: Callable[[Callable[..., _T]], Callable[..., _T]] = retry(
    retry=retry_if_exception_type(RateLimitError),
    wait=wait_exponential(multiplier=1, min=1, max=30),
    stop=stop_after_attempt(4),
    reraise=True,
)

# LLM：429/overloaded 与连接/服务端瞬态退避重试（2→4→8…最多 60s，3 次）；
# 认证/模型不存在立即上抛（由降级链决定是否换档）。
llm_retry: Callable[[Callable[..., _T]], Callable[..., _T]] = retry(
    retry=retry_if_exception_type((LLMRateLimitError, LLMConnectionError)),
    wait=wait_exponential(multiplier=2, min=2, max=60),
    stop=stop_after_attempt(3),
    reraise=True,
)
