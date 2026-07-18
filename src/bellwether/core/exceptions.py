"""Bellwether 统一异常层级（ROADMAP D2 / RFC-000）。

分类目的：让调用方按**异常类型**决定重试/降级/上抛，而不是靠字符串匹配错误信息
（现状 provider 里 `except Exception` + 字符串拼接是脆弱的，D2 逐步替换为这套类型）。

可重试性约定（retry.py 依赖此语义）：
- RateLimitError / LLMRateLimitError / LLMConnectionError → 退避后可重试
- DataUnavailableError / CircuitOpenError / LLMAuthError / ModelNotFoundError
  → 不可重试（重试无意义、需人工、或熔断要求快速失败）
"""

from __future__ import annotations


class BellwetherError(Exception):
    """所有 Bellwether 领域异常的基类。"""


class ConfigError(BellwetherError):
    """配置或密钥缺失/非法（如 ANTHROPIC_API_KEY 未设、config.toml 非法）。"""


# ─────────────────────────── 数据源 ───────────────────────────
class DataSourceError(BellwetherError):
    """数据源类错误的基类。"""


class DataUnavailableError(DataSourceError):
    """数据源可达但无有效数据（空结果/字段缺失）——重试通常无意义。"""


class RateLimitError(DataSourceError):
    """被限流/拒绝/连接被断（如东财 RemoteDisconnected）——退避后可重试。"""


class CircuitOpenError(DataSourceError):
    """熔断器打开：该数据源近期连续失败，跳过调用快速失败——不重试，等冷却。"""


# ─────────────────────────── LLM ───────────────────────────
class LLMError(BellwetherError):
    """LLM 调用类错误的基类。"""


class LLMAuthError(LLMError):
    """认证失败（401 / invalid x-api-key）——不可重试，需人工换 key。"""


class LLMRateLimitError(LLMError):
    """LLM 限流（429 / overloaded）——退避后可重试。"""


class LLMConnectionError(LLMError):
    """网络/服务端瞬态故障（连接失败/超时/5xx 非 model_not_found）——退避后可重试。"""


class ModelNotFoundError(LLMError):
    """模型 id 不存在 / 中转不支持（503 model_not_found）——不可重试，需换模型。"""


# ─────────────────────────── 工具 ───────────────────────────
class ToolError(BellwetherError):
    """agent tool 执行失败。"""
