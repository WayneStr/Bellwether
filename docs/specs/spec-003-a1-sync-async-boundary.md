# Spec-003: A1 sync/async boundary

> 状态: Frozen (M1) · 2026-07-17 · 变更须 ADR · 来源: RFC-000/001/002/003

## 1. M1 boundary

`MarketDataProvider` remains a synchronous interface in M1: `resolve_symbol`,
`get_ohlcv`, `get_fundamentals`, `get_news`, and `trading_rules` MUST remain normal
`def` methods. Existing yfinance, akshare, snapshot, and cassette providers therefore
remain substitutable. Provider code MUST NOT create an event loop or call
`asyncio.run()`.

An async orchestrator/StageRunner MUST invoke blocking provider work through the
single A1 adapter, conceptually:

```python
async def call_provider(fn, *args, deadline, provider_key, context):
    # acquire ProviderExecutor's global and per-provider leases
    # record start via context.clock; execute await asyncio.to_thread(fn, *args)
    # record result/error/latency; release leases only after the worker exits
    ...
```

Tools and analysis code MUST call this adapter, not `asyncio.to_thread` ad hoc. A
sync-only caller MAY call the provider directly; it MUST not claim concurrent execution
or bypass trace/cassette recording. The adapter is the single location for M1 metrics,
deadlines, leases, and later rate limiting.

## 2. Timeout and cancellation semantics

- `StageRunner` owns the overall stage deadline. `ProviderExecutor` receives the
  remaining deadline and owns conversion of expiry into `ProviderTimeout`; providers
  own their underlying HTTP connect/read timeouts and MUST receive no independent,
  longer deadline.
- On deadline expiry, the awaiter MUST stop waiting, record a timed-out `tool_call`,
  and discard any eventual result. It MUST NOT retry implicitly; the tool/stage retry
  policy owns retries and budget accounting.
- Cancelling an `asyncio.to_thread` awaiter does **not** stop the native worker. The
  adapter MUST propagate cancellation to the caller promptly, mark the work abandoned,
  and attach completion handling for metrics and lease release. It MUST NOT treat a
  late result as a successful tool result or write it into a cancelled cassette/trace.
- A global or provider semaphore MUST remain held until the underlying worker has
  actually exited, including after timeout/cancellation. Releasing it when the awaiter
  exits would silently exceed the configured concurrency cap.
- Providers are read-only at this boundary. Any future provider with side effects MUST
  expose an idempotency/cancellation design before it may use this adapter.

## 3. Capacity, limiter location, and observability

M1 SHOULD set the event loop's default thread executor to **8 workers**. This is a
small fixed ceiling: yfinance/akshare calls are blocking network I/O and can otherwise
consume Python's broad default pool, while eight preserves headroom for LLM/CLI work
without manufacturing provider-side bursts. M1 SHOULD start each provider at a
per-provider concurrency cap of **2**; lower provider-specific limits MAY be configured.

The M3 rate limiter has one frozen mount point now: `ProviderExecutor`, keyed by
`(provider_id, method)`, immediately before a worker lease is started. M1 MUST expose
that key and record it in trace metadata, but MUST NOT add a second limiter inside
providers. M3 will attach token-bucket/backoff enforcement there, so cancellation
cannot refund a request that has already entered a worker.

Every adapter attempt MUST emit provider id, method, `tool_call_id`, context `as_of`,
queue time, worker latency, outcome (`ok`, `timeout`, `cancelled`, `late_completion`,
or `error`), and the limiter key. A late completion is an operational signal, not an
IR artifact.

## 4. M3 all-async migration path

1. Define `AsyncMarketDataProvider` with the same value types and context-bearing
   method signatures; add parity TCKs shared with the sync interface.
2. Implement async HTTP clients provider-by-provider behind `ProviderExecutor`; keep
   the sync provider adapter only for providers not yet migrated.
3. Move cancellation to client request/stream closure, retain the same deadline,
   trace, limiter-key, and cassette semantics, and compare sync/async cassette output.
4. Remove `to_thread` only after every supported provider passes parity and timeout /
   cancellation tests. This is an ADR-gated breaking boundary change, not an M1 cleanup.

M3 MUST preserve the semantic contract above: a caller receives at most one result,
timeouts/cancellations never produce report evidence, and rate limits remain centralized.
