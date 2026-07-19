"""EvidenceStore —— RFC-000 §4 会话语义 + spec-001 §2 register 约定。

- session 级单例：quick 与 deep 共用同一实例与语义（由 orchestrator 每次分析构造并持有）
- eid 全会话唯一、从不复用：单一串行分配器（锁保护，deep fan-out 并行注册排队）
- register 分配 eid 并拒绝调用方自带 eid；同时计算 fingerprint（spec-001 §2：
  语义键 symbol|kind|metric-name|period|price_basis|upstream_source 的规范化哈希，
  排除 value 与时间戳——同一事实跨运行对齐，回放错绑守卫 R-P 的基础）
- closure(eids)：跨阶段可解析的 derivation 传递闭包
"""

from __future__ import annotations

import hashlib
import threading
from typing import Any

from .models import Evidence, Period


def semantic_fingerprint(
    symbol: str,
    kind: str,
    metric_name: str,
    period: Period | None,
    price_basis: str | None,
    upstream_source: str | None,
) -> str:
    """spec-001 §2 钉死的 fingerprint preimage（排除 value 与所有时间戳）。"""
    period_key = (
        f"{period.kind}|{period.start or ''}|{period.end or ''}|{period.label or ''}"
        if period
        else ""
    )
    preimage = "|".join(
        [symbol, kind, metric_name, period_key, price_basis or "", upstream_source or ""]
    )
    return hashlib.sha256(preimage.encode("utf-8")).hexdigest()


class EvidenceStore:
    def __init__(self, symbol: str):
        self._symbol = symbol
        self._lock = threading.Lock()
        self._next = 1
        self._by_eid: dict[str, Evidence] = {}

    def register(self, *, metric_name: str, **evidence_fields: Any) -> Evidence:
        """分配 eid 并注册一条 Evidence；调用方 MUST NOT 自带 eid/fingerprint。

        metric_name 是该值的语义名（如 "close"、"pe_ttm"、"rsi14"、"dcf_intrinsic"），
        只进 fingerprint preimage，不落 Evidence 字段。
        """
        if "eid" in evidence_fields:
            raise ValueError("register allocates eids; caller-supplied eid rejected")
        if "fingerprint" in evidence_fields:
            raise ValueError("fingerprint is store-computed; caller-supplied value rejected")
        with self._lock:
            eid = f"E{self._next}"
            candidate = Evidence(eid=eid, **evidence_fields)
            source = candidate.source
            fingerprint = semantic_fingerprint(
                self._symbol,
                candidate.kind,
                metric_name,
                candidate.period,
                candidate.price_basis,
                source.upstream_source if source else None,
            )
            evidence = candidate.model_copy(update={"fingerprint": fingerprint})
            self._next += 1
            self._by_eid[eid] = evidence
            return evidence

    def get(self, eid: str) -> Evidence:
        return self._by_eid[eid]

    def closure(self, eids: list[str]) -> dict[str, Evidence]:
        """给定直接引用的 eid 集合，返回含 derivation 输入的传递闭包（渲染/verifier 用）。"""
        result: dict[str, Evidence] = {}

        def visit(eid: str) -> None:
            if eid in result:
                return
            evidence = self._by_eid[eid]
            result[eid] = evidence
            for parent in evidence.derivation.inputs if evidence.derivation else []:
                visit(parent)

        for eid in eids:
            visit(eid)
        return result

    def __len__(self) -> int:
        return len(self._by_eid)
