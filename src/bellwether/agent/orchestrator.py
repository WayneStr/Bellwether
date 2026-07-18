"""最小 Claude tool-use loop：规划 → 调 tool 取数 → 综合研判。

模型选择全部经 ModelRouter（三级覆盖）。deep=True 走 deep_report 角色。
可靠性（D2）：显式超时 + SDK 重试关闭（重试统一由 tenacity 管，见 core/retry.py）
+ 模型档降级链（降级发生时在报告中明示）。
"""

from __future__ import annotations

from typing import Any

from anthropic import Anthropic
from anthropic.types import Message

from ..config import AppConfig
from ..data.base import ProviderRegistry
from . import tools as tools_mod
from .llm import ResilientLLM
from .prompts import SYSTEM_PROMPT, analyze_prompt
from .router import ModelRouter

_MAX_TURNS = 6
_LLM_TIMEOUT_SECONDS = 300.0  # deep 报告 8k tokens 生成可能超过默认值，显式给足


class Orchestrator:
    def __init__(self, config: AppConfig):
        self.config = config
        self.router = ModelRouter(config.models)
        self.client = Anthropic(
            api_key=config.anthropic_api_key,
            base_url=config.anthropic_base_url,  # None 时 SDK 用官方默认地址
            timeout=_LLM_TIMEOUT_SECONDS,
            max_retries=0,  # SDK 内置重试关闭：退避策略统一在 core/retry.py，可测可控
        )
        self.llm = ResilientLLM(self.client)

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
        chain = self.router.resolve_chain(role, model=model_override, **param_overrides)
        primary_model = chain[0].model

        messages: list[dict] = [{"role": "user", "content": analyze_prompt(symbol, deep)}]

        for _ in range(_MAX_TURNS):
            resp, used = self.llm.create(
                chain,
                system=SYSTEM_PROMPT,
                tools=tools_mod.TOOL_SCHEMAS,
                messages=messages,
            )
            # 降级后从降级档继续：之前的档已证明失败，后续轮次不再逐轮撞它
            if used is not chain[0]:
                chain = chain[chain.index(used) :]

            if resp.stop_reason != "tool_use":
                return _finalize(_collect_text(resp), primary_model, used.model)

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


def _finalize(text: str, primary_model: str, used_model: str) -> str:
    """降级发生时在报告尾部明示（红线 4 的数据时效标注同级要求：诚实呈现）。"""
    if used_model != primary_model:
        text += (
            f"\n\n> ⚠️ 模型降级说明：原定模型 `{primary_model}` 持续不可用，"
            f"本报告（或其部分轮次）由降级模型 `{used_model}` 生成。"
        )
    return text


def _collect_text(resp: Message) -> str:
    texts = [b for b in resp.content if getattr(b, "type", None) == "text"]
    parts = [t.text for t in texts]  # type: ignore[union-attr]
    return "\n".join(parts).strip()
