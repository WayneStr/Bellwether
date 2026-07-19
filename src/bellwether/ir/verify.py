"""verify_constructive 的规则实现（spec-001 v1.1 §3）。

- R1 裸数字/量词扫描：Claim 文本、场景 narrative、coverage.reason 等渲染自由文本
  中，[E12] 令牌是唯一合法的数字载体；令牌外的阿拉伯数字与中文数字一律违规。
  模糊定量语言（翻倍/三成/由盈转亏…）是 spec 明示的声明式残差（ADR-0006 P11）。
- R7 溯源解析闭合：每条 source Evidence 的指针必须解析到捕获库中哈希吻合的真实
  字节，tool_call_id 必须属于本会话 trace（ADR-0006 P2——B1/B2/B3 的总开关）。
- R8 value 忠实性：值必须能用注册时的同一确定性抽取器从捕获 payload 逐位重算
  （ADR-0006 P3）。

白名单可扩展但默认为空：宁可误杀（claim 被 drop 重写）不可漏放（红线 2/3）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from ..core.capture import CaptureStore
from ..core.trace import EvidenceBinding
from .extract import run_extractor
from .models import Evidence

_TOKEN = re.compile(r"\[E[1-9][0-9]*\]")
_ARABIC = re.compile(r"[0-9０-９]")
# 中文数字：计数用字（含「两」「〇」）。「一」类单字在成语/连词中的误伤由 drop-rewrite
# 环节吸收——构造性保证宁严勿宽。
_CN_NUM = re.compile(r"[〇零一二两三四五六七八九十百千万亿]")


@dataclass(frozen=True)
class NakedNumber:
    """一次违规命中：位置与命中文本（诊断/重写提示用）。"""

    position: int
    text: str


def scan_naked_numbers(text: str, whitelist: tuple[re.Pattern[str], ...] = ()) -> list[NakedNumber]:
    """R1：返回令牌外的裸数字命中列表；空列表 = 通过。

    实现方式：先把 [E12] 令牌与白名单命中段挖空（等长占位，保位置），再扫描剩余文本。
    """
    masked = list(text)

    def _mask(pattern: re.Pattern[str]) -> None:
        for match in pattern.finditer(text):
            for i in range(match.start(), match.end()):
                masked[i] = "\x00"

    _mask(_TOKEN)
    for pattern in whitelist:
        _mask(pattern)
    remaining = "".join(masked)

    hits = [NakedNumber(position=m.start(), text=m.group()) for m in _ARABIC.finditer(remaining)]
    hits += [NakedNumber(position=m.start(), text=m.group()) for m in _CN_NUM.finditer(remaining)]
    return sorted(hits, key=lambda h: h.position)


# ─────────────────────────── R7 / R8 ───────────────────────────
def verify_provenance(
    evidence: dict[str, Evidence],
    bindings: list[EvidenceBinding],
    captures: CaptureStore,
    session_tool_call_ids: set[str],
) -> list[str]:
    """R7 溯源解析 + R8 值重算。返回违规描述列表；空列表 = 全部通过。

    覆盖每条 source Evidence（derivation 类由 R4 闭包与派生栅栏管辖）：
    R7a binding 存在且 capture_id 一致；R7b 捕获可解析且哈希与 response_sha256 吻合；
    R7c tool_call_id 属于本会话；R8 同一抽取器重算值逐位相等。
    """
    violations: list[str] = []
    by_eid = {b.eid: b for b in bindings}
    for eid, ev in evidence.items():
        if ev.source is None:
            continue
        binding = by_eid.get(eid)
        if binding is None:
            violations.append(f"R7: {eid} 无绑定记录（provenance 缺失）")
            continue
        if binding.capture_id != ev.source.capture_id:
            violations.append(f"R7: {eid} 绑定与 SourceRef 的 capture_id 不一致")
            continue
        if not captures.verify(ev.source.capture_id, ev.source.response_sha256):
            violations.append(
                f"R7: {eid} 捕获不可解析或哈希不吻合（capture_id={ev.source.capture_id}）"
            )
            continue
        if ev.source.tool_call_id not in session_tool_call_ids:
            violations.append(f"R7: {eid} 的 tool_call_id 不属于本会话 trace")
            continue
        body = captures.resolve(ev.source.capture_id)
        assert body is not None  # verify 已通过
        payload = json.loads(body)
        try:
            recomputed = run_extractor(binding.extractor_id, payload, **binding.extractor_args)
        except Exception as exc:
            violations.append(f"R8: {eid} 抽取器重算失败（{binding.extractor_id}: {exc}）")
            continue
        if recomputed != ev.value:
            violations.append(f"R8: {eid} 值与源字节脱节（声称 {ev.value!r}，重算 {recomputed!r}）")
    return violations
