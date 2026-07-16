"""模型解析的唯一入口：角色化 + 三级覆盖。

设计目标（DESIGN.md §3）：代码任何地方都不硬编码模型名，一律经此解析。
    resolve("deep_report")                       # 用 config/默认
    resolve("deep_report", model="claude-opus-4-8", temperature=0.2)  # CLI 覆盖
"""

from __future__ import annotations

from ..models import ModelConfig, ModelSpec

VALID_ROLES = ("parse", "synthesis", "deep_report")


class ModelRouter:
    def __init__(self, config: ModelConfig):
        self._config = config

    def resolve(self, role: str, *, model: str | None = None, **param_overrides) -> ModelSpec:
        """返回某角色最终生效的模型规格。

        role: parse / synthesis / deep_report
        model: CLI 层覆盖模型 id（最高优先级）
        param_overrides: 覆盖采样参数（temperature/max_tokens/...）；值为 None 则忽略
        """
        if role not in VALID_ROLES:
            raise ValueError(f"未知模型角色: {role!r}，可选 {VALID_ROLES}")

        # 深拷贝，避免运行时覆盖污染共享的 config
        resolved: ModelSpec = getattr(self._config, role).model_copy(deep=True)

        if model:
            resolved.model = model

        for key, value in param_overrides.items():
            if value is None:
                continue
            if hasattr(resolved.params, key) and key != "extra":
                setattr(resolved.params, key, value)
            else:
                resolved.params.extra[key] = value

        return resolved
