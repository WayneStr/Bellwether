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
    # 自定义 Anthropic API 请求地址（代理 / 中转 / 兼容网关）。留空用官方默认。
    base_url: str | None = None


class DataConfig(BaseModel):
    default_market: str = "US"
    cache_ttl_days: int = 1


class ReportConfig(BaseModel):
    disclaimer: bool = True
    language: str = "zh"


class AppConfig(BaseModel):
    models: ModelConfig = Field(default_factory=ModelConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    data: DataConfig = Field(default_factory=DataConfig)
    report: ReportConfig = Field(default_factory=ReportConfig)

    # 密钥不进配置文件：环境变量优先，其次系统钥匙串（keyring，可选依赖）。
    @property
    def anthropic_api_key(self) -> str | None:
        return os.environ.get("ANTHROPIC_API_KEY") or _keyring_get_api_key()

    @property
    def anthropic_base_url(self) -> str | None:
        # config.toml 显式设置优先，其次环境变量 ANTHROPIC_BASE_URL，最后 None（SDK 用官方默认）
        return self.api.base_url or os.environ.get("ANTHROPIC_BASE_URL") or None


KEYRING_SERVICE = "bellwether"
KEYRING_USERNAME = "anthropic_api_key"


def _keyring_get_api_key() -> str | None:
    """从系统钥匙串读 key（D6）。keyring 未安装或后端不可用（无桌面环境等）
    一律静默返回 None——钥匙串只是便利项，环境变量永远可用。"""
    try:
        import keyring
    except ImportError:
        return None
    try:
        return keyring.get_password(KEYRING_SERVICE, KEYRING_USERNAME)
    except Exception:
        return None


def api_key_source() -> str:
    """当前 key 的来源（诊断用，绝不返回 key 本身）：env / keyring / none。"""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "env"
    if _keyring_get_api_key():
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
