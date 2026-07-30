"""加载 config.toml + 环境变量，产出强类型配置对象 AppConfig。

配置优先级（针对模型选择，见 agent/router.py 的三级覆盖）：
    CLI 运行时参数  >  config.toml  >  代码内置默认
本模块负责后两级；CLI 覆盖在 ModelRouter.resolve 处生效。
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from pydantic import BaseModel, Field

from .models import ModelConfig


class ApiConfig(BaseModel):
    # LLM 供应商 API 形态："anthropic"（Messages API，含官方/Anthropic 兼容中转）
    # 或 "openai"（chat/completions，含官方/OpenAI 格式中转）。决定客户端与 key 槽。
    provider: str = "anthropic"
    # 自定义 API 请求地址（代理 / 中转 / 兼容网关）。留空用该 provider 官方默认。
    base_url: str | None = None
    # prompt caching（M2）：system+tools 加 cache_control，多轮 tool-use 大幅省 input token。
    # 个别中转不支持 cache_control 字段时置 false 关闭。（OpenAI 端自动缓存，此开关无效）
    prompt_caching: bool = True


class DataConfig(BaseModel):
    default_market: str = "US"
    cache_ttl_days: int = 1


class ReportConfig(BaseModel):
    disclaimer: bool = True
    language: str = "zh"


class BudgetConfig(BaseModel):
    """单次分析成本硬上限（ADR-0004 默认值；config.toml 的 [budget] 可覆盖）。"""

    quick_usd: float = 0.35
    deep_usd: float = 1.50


class AppConfig(BaseModel):
    models: ModelConfig = Field(default_factory=ModelConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    data: DataConfig = Field(default_factory=DataConfig)
    report: ReportConfig = Field(default_factory=ReportConfig)
    # price-book 覆盖（core/costs.py 的内置定价仅占位；中转模型名不同时须在此覆盖）
    pricing: dict[str, dict[str, float]] = Field(default_factory=dict)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)

    # 密钥不进配置文件：环境变量优先，其次系统钥匙串（keyring，可选依赖）。
    # 按 provider 选 env 变量与钥匙串槽，两家 key 互不干扰。
    @property
    def api_key(self) -> str | None:
        return os.environ.get(provider_env_var(self.api.provider)) or _keyring_get_api_key(
            provider_keyring_user(self.api.provider)
        )

    @property
    def base_url(self) -> str | None:
        # config 显式值 > provider 对应 *_BASE_URL 环境变量 > None（用官方默认）
        return self.api.base_url or os.environ.get(provider_base_url_var(self.api.provider)) or None

    # 向后兼容别名（历史调用点仍用 anthropic_* 命名；语义已 provider 感知）。
    @property
    def anthropic_api_key(self) -> str | None:
        return self.api_key

    @property
    def anthropic_base_url(self) -> str | None:
        return self.base_url


KEYRING_SERVICE = "bellwether"
KEYRING_USERNAME = "anthropic_api_key"  # 向后兼容常量（= anthropic 槽）

_ENV_KEY = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}
_ENV_BASE = {"anthropic": "ANTHROPIC_BASE_URL", "openai": "OPENAI_BASE_URL"}
_KEYRING_USER = {"anthropic": "anthropic_api_key", "openai": "openai_api_key"}


def provider_env_var(provider: str) -> str:
    """provider 对应的 API key 环境变量名（未知 provider 回落 anthropic）。"""
    return _ENV_KEY.get(provider, "ANTHROPIC_API_KEY")


def provider_base_url_var(provider: str) -> str:
    return _ENV_BASE.get(provider, "ANTHROPIC_BASE_URL")


def provider_keyring_user(provider: str) -> str:
    """provider 对应的钥匙串 username 槽（两家 key 隔离存储）。"""
    return _KEYRING_USER.get(provider, KEYRING_USERNAME)


def _keyring_get_api_key(username: str = KEYRING_USERNAME) -> str | None:
    """从系统钥匙串读 key（D6）。keyring 未安装或后端不可用（无桌面环境等）
    一律静默返回 None——钥匙串只是便利项，环境变量永远可用。"""
    try:
        import keyring
    except ImportError:
        return None
    try:
        return keyring.get_password(KEYRING_SERVICE, username)
    except Exception:
        return None


def api_key_source(provider: str = "anthropic") -> str:
    """指定 provider 的 key 来源（诊断用，绝不返回 key 本身）：env / keyring / none。"""
    if os.environ.get(provider_env_var(provider)):
        return "env"
    if _keyring_get_api_key(provider_keyring_user(provider)):
        return "keyring"
    return "none"


def load_config(path: str | os.PathLike[str] | None = None) -> AppConfig:
    """加载配置。显式 path > ./config.toml > 内置默认；找不到文件即用默认。"""
    candidate: Path | None = Path(path) if path else Path("config.toml")
    if candidate and candidate.exists():
        with open(candidate, "rb") as f:
            raw = tomllib.load(f)
        return AppConfig.model_validate(raw)
    return AppConfig()  # 全默认，保证零配置也能跑
