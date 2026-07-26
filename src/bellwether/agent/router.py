"""模型解析的唯一入口：角色化 + 三级覆盖。

设计目标（DESIGN.md §3）：代码任何地方都不硬编码模型名，一律经此解析。
    resolve("deep_report")                       # 用 config/默认
    resolve("deep_report", model="claude-opus-4-8", temperature=0.2)  # CLI 覆盖
"""

from __future__ import annotations

from typing import Any

from ..models import ModelConfig, ModelSpec

VALID_ROLES = ("parse", "synthesis", "deep_report", "judge")

# LLM 故障降级顺序（D2）：换的只是模型 id，任务参数（temperature/max_tokens）保留
# 原角色的——降级是「换一台发动机」，不是换任务。parse 已是最低档，无处可降。
# judge 不降级：评审模型漂移会破坏评测可比性（RFC-003 §4.1 重测触发条件），失败即明示。
_FALLBACK_ROLE = {"deep_report": "synthesis", "synthesis": "parse", "parse": None, "judge": None}


class ModelRouter:
    def __init__(self, config: ModelConfig):
        self._config = config

    def resolve(self, role: str, *, model: str | None = None, **param_overrides: Any) -> ModelSpec:
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

    def resolve_chain(
        self, role: str, *, model: str | None = None, **param_overrides: Any
    ) -> list[ModelSpec]:
        """主选 + 降级档的模型链（D2 LLM 降级）。

        用户显式 --model 覆盖时链长为 1：明确指定的模型失败就明示失败，
        不偷偷换成别的。降级档与已在链中的模型 id 重复时跳过（用户可能把
        多个角色配成同一模型）。
        """
        primary = self.resolve(role, model=model, **param_overrides)
        if model:
            return [primary]

        chain = [primary]
        fallback_role = _FALLBACK_ROLE.get(role)
        while fallback_role:
            fb_model = getattr(self._config, fallback_role).model
            if fb_model not in [spec.model for spec in chain]:
                spec = primary.model_copy(deep=True)
                spec.model = fb_model
                chain.append(spec)
            fallback_role = _FALLBACK_ROLE.get(fallback_role)
        return chain
