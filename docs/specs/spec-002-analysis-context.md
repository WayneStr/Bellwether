# Spec-002: AnalysisContext

> 状态: Frozen (M1) · 2026-07-17 · 变更须 ADR · 来源: RFC-000/001/002/003

## 1. Contract

`AnalysisContext` is the analysis session's immutable logical clock. Every business
date, capture timestamp, trace timestamp, and cassette key MUST flow from it; code
outside the approved operational whitelist MUST NOT call `datetime.now()` or
`date.today()` directly.

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol

class Clock(Protocol):
    def now(self) -> datetime: ...       # MUST return a UTC-aware datetime

@dataclass(frozen=True, slots=True)
class AnalysisContext:
    as_of: datetime
    capture_policy: Literal["live", "cassette", "silver"]
    clock: Clock

    def __post_init__(self) -> None:
        if self.as_of.tzinfo is None or self.as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
```

- CLI MUST explicitly create one context at command entry. With no `--as-of`, it MUST
  read `SystemClock.now()` once, normalize it to UTC, and use that same value as
  `as_of`; `capture_policy` defaults to `live`.
- `as_of` is frozen selection time: history end dates, canonical cassette arguments,
  strict PIT queries, and report context MUST use it. `clock.now()` supplies capture,
  generated, and trace event timestamps. For cassette/silver replay, the injected
  frozen clock SHOULD return `as_of` so a replay is deterministic.
- A user-supplied `--as-of` MUST be UTC-normalized before construction. A live command
  MAY have a moving system clock after construction, but MUST NOT change `as_of`.

## 2. Required signature propagation

| Hop | Frozen signature change |
|---|---|
| CLI | `analyze(..., as_of: datetime | None = None, capture_policy="live")`; construct `context`, then call orchestrator. `snapshot` and `portfolio` do the same. |
| Orchestrator | `analyze(symbol, *, context: AnalysisContext, deep=False, ...)`; pass the identical object into every tool invocation and trace event. |
| Tools | `execute_tool(name, tool_input, provider, *, context: AnalysisContext, trace: TraceRecorder | None = None)` and every private handler accept `context`. |
| Analysis | `TechnicalModule.compute(..., *, context)`; `FundamentalModule.compute(..., *, context)`; `PortfolioModule.compute(..., *, context)`. |
| Provider | `resolve_symbol(query, *, context)` and all `get_ohlcv/get_fundamentals/get_news(..., *, context)` methods; the abstract base and every live/cassette implementation MUST match. |
| Trace/cassette | `record_tool_call(..., context)` writes `as_of=context.as_of`; recorder and `CassetteProvider` derive timestamps and canonical arguments only from `context`. |
| Snapshot | `run_snapshot(..., context)` and `snapshot_symbol(..., context)` use the same object for request windows and manifest capture times. |

No hop MAY construct a replacement context. Helpers that need only the date SHOULD
receive `context.as_of.date()`, not a new clock or `datetime` default.

## 3. `qfq` anchor rule

The pure M3 interface is `adjust(bars, actions, mode, anchor_date, price_basis)`.
For `mode="qfq"`, its only permitted call site is
`anchor_date=context.as_of.date()`. `anchor_date` MUST be recorded in Evidence/report
metadata. The current provider-default qfq view is transitional comparison data only;
it MUST NOT be presented as a reproducible qfq Evidence value until it has passed this
pure-function boundary. `hfq` uses its first-trading-day anchor and does not read time.

## 4. Current direct-time census (15 hits; `date.today()` = 0)

| Location | Current purpose | Required migration |
|---|---|---|
| `agent/tools.py:132` | price-history end date | `context.as_of.date()` |
| `agent/tools.py:211` | OHLCV summary `fetched_at` | `context.clock.now()` |
| `analysis/technical.py:27` | technical history end date | `context.as_of.date()` |
| `analysis/technical.py:56` | report `fetched_at` | `context.clock.now()` |
| `analysis/fundamental.py:53` | report `fetched_at` | `context.clock.now()` |
| `analysis/portfolio.py:33` | portfolio history end date | `context.as_of.date()` |
| `analysis/portfolio.py:84` | report `fetched_at` | `context.clock.now()` |
| `data/yfinance_provider.py:76` | fundamentals capture time | `context.clock.now()` |
| `data/akshare_provider.py:178` | CN fundamentals capture time | `context.clock.now()` |
| `data/akshare_provider.py:233` | HK fundamentals capture time | `context.clock.now()` |
| `snapshot.py:111` | snapshot request end date | `context.as_of.date()` |
| `snapshot.py:173` | snapshot partition day | `context.as_of.date()`; `date_str` becomes an explicit compatibility override and MUST be recorded. |
| `snapshot.py:174` | run-id clock component | `context.clock.now()`; retain random suffix for collision resistance under frozen tests. |
| `snapshot.py:182` | manifest `created_at` / first-seen basis | `context.clock.now()` |
| `snapshot.py:215` | `last_status.finished_at` operational completion stamp | Retain direct system time in the named whitelist: it is a non-replay status log, never a PIT/cassette/trace input. |

The whitelist MUST contain only `snapshot.py:215` and its reason above. A CI guard
MUST scan the package for both forms and fail any new unlisted occurrence.

## 5. Verification gates

1. A frozen-clock cassette run MUST produce identical canonical provider arguments and
   `as_of` trace fields on two executions.
2. A qfq test MUST assert its anchor equals `context.as_of.date()`.
3. A static test MUST reconcile this table with `rg 'datetime\\.now\\(|date\\.today\\('`.
4. Quick and deep paths MUST preserve object identity (`is`) for the context passed to
   each tool and trace event.
