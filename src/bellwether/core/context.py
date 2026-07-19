"""spec-002: AnalysisContext —— 分析会话的不可变逻辑时钟。

`AnalysisContext` 是全链路时间信号的唯一来源：区间终点、capture/trace 时间戳、
cassette key 全部经它派生。除本文件的 `SystemClock`（真实系统时钟）与
`snapshot.py` 的 `last_status.finished_at`（运营完成戳，spec-002 §4 白名单）外，
全库业务代码不得直连系统时钟（`datetime` 的 now 或 `date` 的 today）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...  # MUST return a UTC-aware datetime


@dataclass(frozen=True, slots=True)
class AnalysisContext:
    as_of: datetime
    capture_policy: Literal["live", "cassette", "silver"]
    clock: Clock

    def __post_init__(self) -> None:
        if self.as_of.tzinfo is None or self.as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")


class SystemClock:
    """真实系统时钟——全库唯一允许直连系统时钟的业务位置之一（另一处见
    snapshot.py 的 last_status.finished_at 白名单）。"""

    def now(self) -> datetime:
        return datetime.now(UTC)


class FrozenClock:
    """恒定时钟：构造时接一个 datetime，now() 恒返回它。供 cassette/评测回放使用，
    使同一 context 下的多次调用产出确定性时间戳（spec-002 §1：replay 的注入冻结钟
    SHOULD 返回 as_of）。"""

    def __init__(self, frozen: datetime):
        self._frozen = frozen

    def now(self) -> datetime:
        return self._frozen
