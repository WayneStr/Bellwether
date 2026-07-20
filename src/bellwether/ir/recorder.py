"""ToolRecorder —— tool 层证据化中枢（spec-001 v1.1 §1/§3，spec-002 §2 的 trace 记录器）。

职责链：捕获（CaptureStore 落盘可寻址字节）→ 注册（值**只能**由抽取器从捕获
payload 产出，杜绝手填——R8 的构造性保证在此成立）→ 绑定记录（eid ↔ capture ↔
extractor，落 provenance trace，R7/R8 核验的依据）。

时间语义（ADR-0006 P9/B8）：captured_at 是字节首次真实获取时刻（provider 经
df.attrs / fetched_at 携带，缓存命中回填原值）；超过 STALE_AFTER 的证据强制
confidence="stale"。live 免费源均为 observed（first_seen_at = captured_at）。

cassette 重放（C2a）：context.capture_policy="cassette" 时 register_value 改走
pit_class="replay"（first_seen_at/available_at 均 None，confidence 恒 "reported"，
跳过 stale 判定）；live/silver 不受影响，行为与此前一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Literal

from ..core.capture import CaptureStore
from ..core.context import AnalysisContext
from ..core.trace import EvidenceBinding
from .extract import run_extractor
from .models import Derivation, Evidence, SourceRef
from .store import EvidenceStore

STALE_AFTER = timedelta(hours=72)  # 覆盖周末缓存；超龄证据必须明示 stale（红线 4）

_DEFAULT_LICENSE_TAG = "private-ok-backup-ok"  # E3/ADR-0005：黄金集免费源词表值


@dataclass(frozen=True)
class CapturedPayload:
    """一次已落盘捕获的完整上下文（注册值时复用，不再重复捕获）。"""

    payload: dict[str, Any]
    capture_id: str
    response_sha256: str
    provider_id: str
    upstream_source: str | None
    tool_name: str
    tool_call_id: str
    canonical_request: dict[str, Any]
    captured_at: datetime
    data_type: Literal["ohlcv", "fundamentals", "news", "filing", "derived"]


@dataclass
class ToolRecorder:
    context: AnalysisContext
    evidence: EvidenceStore
    captures: CaptureStore
    license_tag: str = _DEFAULT_LICENSE_TAG
    bindings: list[EvidenceBinding] = field(default_factory=list)
    tool_call_ids: set[str] = field(default_factory=set)
    # orchestrator 在每次 execute_tool 前设置（LLM block.id）；tool 层经 capture 消费。
    # 单线程会话语义（quick）；M4 deep fan-out 编排升级时须改为显式传参（见 spec-002 §2）。
    current_tool_call_id: str | None = None

    def capture(
        self,
        *,
        tool_call_id: str | None = None,
        provider_id: str,
        tool_name: str,
        method: str,
        canonical_args: dict[str, Any],
        payload: dict[str, Any],
        captured_at: datetime | None = None,
        upstream_source: str | None = None,
        data_type: Literal["ohlcv", "fundamentals", "news", "filing", "derived"],
    ) -> CapturedPayload:
        """把一次 provider 响应持久化为可寻址捕获（R7 的实体）。"""
        resolved_id = tool_call_id or self.current_tool_call_id
        if resolved_id is None:
            raise ValueError("capture requires a tool_call_id (orchestrator sets current)")
        tool_call_id = resolved_id
        self.tool_call_ids.add(tool_call_id)
        canonical_request = {"method": method, **canonical_args}
        at = captured_at or self.context.clock.now()
        capture_id, sha = self.captures.persist(
            payload,
            tool_call_id=tool_call_id,
            provider_id=provider_id,
            tool_name=tool_name,
            canonical_request=canonical_request,
            captured_at=at,
            license_tag=self.license_tag,
            upstream_source=upstream_source,
        )
        return CapturedPayload(
            payload=payload,
            capture_id=capture_id,
            response_sha256=sha,
            provider_id=provider_id,
            upstream_source=upstream_source,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            canonical_request=canonical_request,
            captured_at=at,
            data_type=data_type,
        )

    def register_value(
        self,
        captured: CapturedPayload,
        *,
        metric_name: str,
        extractor_id: str,
        extractor_args: dict[str, Any] | None = None,
        kind: str = "metric",
        unit: str | None = None,
        currency: str | None = None,
        price_basis: str | None = None,
        anchor_date: date | None = None,
        published_at: datetime | None = None,
    ) -> Evidence | None:
        """从捕获注册一条证据。值由抽取器产出（本方法不接受 value 参数）。

        抽取失败（字段缺失/None）返回 None——调用方跳过该值，数据缺口由
        coverage 机械推导呈现（批 B），绝不静默造值。

        PIT 语义按 `context.capture_policy` 分支（C2a）：cassette → `pit_class="replay"`，
        `first_seen_at`/`available_at` 均为 None（spec-001：replay 无 available_at；
        S9 报告级校验要求 cassette 报告全 replay），stale 判定跳过、`confidence` 恒
        `"reported"`（诚实性已由 `capture_policy="cassette"` 语义承担，无需叠加时效
        判断）；live/silver → 现状 observed 语义不变。`SourceRef.captured_at` 两种策略
        下都取 `captured.captured_at`（cassette 下由调用方的捕获路径决定，未特别处理
        「回填录制时刻」——已知简化，见 HANDOFF）。
        """
        args = extractor_args or {}
        try:
            value = run_extractor(extractor_id, captured.payload, **args)
        except (KeyError, IndexError, TypeError, ValueError):
            return None

        if self.context.capture_policy == "cassette":
            pit_class: Literal["authoritative", "observed", "replay"] = "replay"
            first_seen_at: datetime | None = None
            available_at: datetime | None = None
            confidence: Literal["reported", "derived", "estimated", "stale", "missing"] = "reported"
        else:
            pit_class = "observed"
            first_seen_at = captured.captured_at
            # observed: max(first_seen_at)；免费源无 released_at
            available_at = captured.captured_at
            confidence = (
                "stale" if self.context.as_of - captured.captured_at > STALE_AFTER else "reported"
            )
        source = SourceRef(
            provider_id=captured.provider_id,
            upstream_source=captured.upstream_source,
            tool_name=captured.tool_name,
            tool_call_id=captured.tool_call_id,
            data_type=captured.data_type,
            capture_id=captured.capture_id,
            canonical_request=captured.canonical_request,
            response_sha256=captured.response_sha256,
            captured_at=captured.captured_at,
            license_tag=self.license_tag,
        )
        registered = self.evidence.register(
            metric_name=metric_name,
            kind=kind,
            value=value,
            unit=unit,
            currency=currency,
            published_at=published_at,
            first_seen_at=first_seen_at,
            available_at=available_at,
            pit_class=pit_class,
            price_basis=price_basis,
            anchor_date=anchor_date,
            source=source,
            confidence=confidence,
        )
        self.bindings.append(
            EvidenceBinding(
                eid=registered.eid,
                capture_id=captured.capture_id,
                extractor_id=extractor_id,
                extractor_args=args,
                fingerprint=registered.fingerprint,
            )
        )
        return registered

    def register_derived(
        self,
        *,
        metric_name: str,
        value: float,
        derivation: Derivation,
        kind: str = "metric",
        unit: str | None = None,
        currency: str | None = None,
    ) -> Evidence:
        """注册一条确定性派生 Evidence（如 DCF 内在价值）。

        仅供确定性分析模块调用（LLM 不可达——LLM 只能经 tool 层看到 {v, eid} 引用，
        无法自行构造 derivation；spec-001 §3 P12 的 params 栅栏语境：params 只能由
        确定性代码写入，杜绝 LLM 编造假设）。value 由调用方直接传入（确定性代码算出，
        非抽取器产出），与 register_value 的构造性保证不同源。

        不 append EvidenceBinding：bindings 表只装 source 类证据（R7/R8 核验对象），
        derived 无捕获无抽取器可核验，只能靠 derivation.inputs 的传递闭包追溯到其
        source 祖先。fingerprint 仍由 EvidenceStore.register 照常计算。

        confidence 恒为 "derived"；派生证据的 PIT 语义（是否/如何随输入证据的
        stale 状态传播）留待 M3 细化，本方法不处理传播规则。
        """
        now = self.context.clock.now()
        return self.evidence.register(
            metric_name=metric_name,
            kind=kind,
            value=value,
            unit=unit,
            currency=currency,
            first_seen_at=now,
            available_at=now,
            pit_class="observed",
            source=None,
            derivation=derivation,
            confidence="derived",
        )


def ref(evidence: Evidence) -> dict[str, Any]:
    """tool 输出里的 {v, eid} 形态（spec-001 §4）：LLM 引用数字的唯一合法通道。"""
    return {"v": evidence.value, "eid": evidence.eid}
