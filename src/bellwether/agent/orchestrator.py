"""最小 Claude tool-use loop：规划 → 调 tool 取数 → 综合研判。

模型选择全部经 ModelRouter（三级覆盖）。deep=True 走 deep_report 角色。
"""

from __future__ import annotations

from typing import Any

from anthropic import Anthropic
from anthropic.types import Message

from ..config import AppConfig
from ..data.base import ProviderRegistry
from . import tools as tools_mod
from .prompts import SYSTEM_PROMPT, analyze_prompt
from .router import ModelRouter

_MAX_TURNS = 6


class Orchestrator:
    def __init__(self, config: AppConfig):
        self.config = config
        self.router = ModelRouter(config.models)
        self.client = Anthropic(
            api_key=config.anthropic_api_key,
            base_url=config.anthropic_base_url,  # None 时 SDK 用官方默认地址
        )

    def analyze(
        self,
        symbol: str,
        *,
        deep: bool = False,
        model_override: str | None = None,
        **param_overrides: Any,
    ) -> str:
        provider = ProviderRegistry.for_symbol(symbol)
        role = "deep_report" if deep else "synthesis"
        spec = self.router.resolve(role, model=model_override, **param_overrides)

        messages: list[dict] = [{"role": "user", "content": analyze_prompt(symbol, deep)}]

        for _ in range(_MAX_TURNS):
            resp = self.client.messages.create(
                model=spec.model,
                max_tokens=spec.params.max_tokens,
                temperature=spec.params.temperature,
                system=SYSTEM_PROMPT,
                tools=tools_mod.TOOL_SCHEMAS,  # type: ignore[arg-type]
                messages=messages,  # type: ignore[arg-type]
            )

            if resp.stop_reason != "tool_use":
                return _collect_text(resp)

            messages.append({"role": "assistant", "content": resp.content})
            tool_results = []
            for block in resp.content:
                if getattr(block, "type", None) == "tool_use":
                    tool_name = block.name  # type: ignore[union-attr]
                    tool_input = block.input  # type: ignore[union-attr]
                    tool_use_id = block.id  # type: ignore[union-attr]
                    output = tools_mod.execute_tool(tool_name, tool_input, provider)
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_use_id,
                            "content": output,
                        }
                    )
            messages.append({"role": "user", "content": tool_results})

        return "（已达到最大工具调用轮次，未能得出最终研判。可稍后重试。）"


def _collect_text(resp: Message) -> str:
    texts = [b for b in resp.content if getattr(b, "type", None) == "text"]
    parts = [t.text for t in texts]  # type: ignore[union-attr]
    return "\n".join(parts).strip()
