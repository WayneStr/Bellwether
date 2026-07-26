"""CostLedger：三本账合一的成本计量（RFC-000 §7 / ADR-0004）。

price-book 版本化：内置定价仅为占位（以官方价目为准）；用户经中转的模型名与
官方定价可能不同，**必须**在 config.toml 的 [pricing] 覆盖才能得到准确金额。
未登记的模型一律计 $0 并记入 unknown_models —— 诚实呈现，不瞎估成本。
"""

from __future__ import annotations

PRICE_BOOK_VERSION = "pb-v1-2026-07"

# 占位定价（USD / 百万 token）。以官方价目为准；用户经中转的模型名不同，
# 必须在 config.toml 的 [pricing] 覆盖才有准确金额。
_BUILTIN_PRICES: dict[str, dict[str, float]] = {
    "claude-opus-4-8": {"input_per_mtok": 15.0, "output_per_mtok": 75.0},
    "claude-sonnet-5": {"input_per_mtok": 3.0, "output_per_mtok": 15.0},
    "claude-haiku-4-5-20251001": {"input_per_mtok": 0.8, "output_per_mtok": 4.0},
}

# prompt caching 的官方计价结构（相对 input 价的比例）：写缓存 1.25×，读缓存 0.1×。
# 模型未显式配 cache_*_per_mtok 时按此比例从 input_per_mtok 派生；中转比例不同时
# 可在 config.toml [pricing] 里显式覆盖 cache_read_per_mtok / cache_write_per_mtok。
_CACHE_WRITE_RATIO = 1.25
_CACHE_READ_RATIO = 0.1


class CostLedger:
    """会话级成本账本：累计每次 LLM 调用的 token 用量与折算金额。

    价目表 = 内置占位价 与 config.toml [pricing] 覆盖合并（覆盖优先）。
    """

    def __init__(self, overrides: dict[str, dict[str, float]] | None = None):
        self._prices: dict[str, dict[str, float]] = {**_BUILTIN_PRICES, **(overrides or {})}
        self.total_usd: float = 0.0
        self.total_input_tokens: int = 0
        self.total_output_tokens: int = 0
        self.total_cache_read_tokens: int = 0
        self.total_cache_write_tokens: int = 0
        self.calls: int = 0
        self.unknown_models: set[str] = set()

    def record_llm(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        latency_s: float | None = None,
        *,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> float:
        """记一次 LLM 调用，返回本次折算金额。

        模型不在价目表（内置 + 覆盖）中时计 $0 并记入 unknown_models（不瞎估）。
        cache 读/写 tokens 单列累计；金额按显式 cache_*_per_mtok 或官方比例派生价折算。
        latency_s 仅供调用方留痕（trace 落盘），账本本身不用它计费。
        """
        self.calls += 1
        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens
        self.total_cache_read_tokens += cache_read_tokens
        self.total_cache_write_tokens += cache_write_tokens
        price = self._prices.get(model)
        if price is None:
            self.unknown_models.add(model)
            return 0.0
        read_price = price.get("cache_read_per_mtok", price["input_per_mtok"] * _CACHE_READ_RATIO)
        write_price = price.get(
            "cache_write_per_mtok", price["input_per_mtok"] * _CACHE_WRITE_RATIO
        )
        cost = (
            input_tokens / 1_000_000 * price["input_per_mtok"]
            + output_tokens / 1_000_000 * price["output_per_mtok"]
            + cache_read_tokens / 1_000_000 * read_price
            + cache_write_tokens / 1_000_000 * write_price
        )
        self.total_usd += cost
        return cost

    def summary(self) -> dict:
        return {
            "price_book_version": PRICE_BOOK_VERSION,
            "total_usd": self.total_usd,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_cache_read_tokens": self.total_cache_read_tokens,
            "total_cache_write_tokens": self.total_cache_write_tokens,
            "calls": self.calls,
            "unknown_models": sorted(self.unknown_models),
        }
