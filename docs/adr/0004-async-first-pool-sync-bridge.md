# ADR-0004: Async-first pool; sync via event-loop bridge

- **Status:** Accepted
- **Date:** 2026-07-16
- **Tags:** concurrency, pool

## Context

Pool orchestration needs concurrent background work: health checks, refresh, dead-letter retry, metrics. Maintaining separate sync and async pool implementations doubles bugs and drifts behavior.

Many callers still use synchronous code (scripts, requests-based pipelines).

## Decision

- Implement **`AsyncProxyPool` as the sole pool engine** (leases, filters, strategies, circuit breaker, background tasks).
- Provide **`SyncProxyPool` as a thin wrapper**: a daemon thread owning a private asyncio event loop, delegating every public call into `AsyncProxyPool`.
- Keep the historical alias `ProxyPool = SyncProxyPool` for older callers.

## Consequences

- **Positive:** One source of truth for pool semantics; async apps get native performance; sync apps keep a familiar blocking API.
- **Negative:** Sync path pays thread + loop overhead and cross-thread scheduling complexity.
- **Neutral:** Sync and async APIs must stay intentionally mirrored; new features land on async first.

## Alternatives considered

| Alternative | Why not |
|-------------|---------|
| Dual sync/async implementations | Drift and double maintenance |
| Async-only public API | Breaks large sync audience |
| Multiprocess pool | Overkill for in-process lease management |

## Evidence

- `omniproxy/pool.py` (`AsyncProxyPool`, `SyncProxyPool`)
- `omniproxy/__init__.py` (`ProxyPool` alias)
- `docs/for_mine_and_only_my.md`, `docs/diagrams/omniproxy.puml`
