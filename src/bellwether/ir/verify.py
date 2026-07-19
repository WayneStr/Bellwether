"""verify_constructive 的规则实现（spec-001 v1.1 §3）——本文件从 R1 起步。

R1 裸数字/量词扫描：Claim 文本、场景 narrative、coverage.reason 等渲染自由文本
中，[E12] 令牌是唯一合法的数字载体；令牌外的阿拉伯数字与中文数字一律违规。
模糊定量语言（翻倍/三成/由盈转亏…）是 spec 明示的声明式残差，不在本扫描器
职责内（ADR-0006 P11），归 C1 推理维度与审稿人。

白名单可扩展但默认为空：宁可误杀（claim 被 drop 重写）不可漏放（红线 2/3）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

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
