# ADR-0007: Pool-wide circuit breaker fed only by client outcomes

- **Status:** Accepted
- **Date:** 2026-07-16
- **Tags:** resilience, pool

## Context

When many proxies fail, continuing to acquire burns time and amplifies outages. A circuit breaker can shed load and allow recovery probes.

Health checks also produce failures, but they are **synthetic**: probing a check URL is not the same as a caller’s real request failing. Feeding the breaker from health checks can open the circuit during quiet periods or because a probe endpoint is flaky, even when production traffic is fine.

## Decision

- Offer an optional **pool-wide** `CircuitBreaker` (CLOSED / OPEN / HALF_OPEN).
- Feed it **only** from client-reported outcomes via `mark_success` / `mark_failed`.
- **Do not** feed the breaker from the health-check loop; health checks update failure counts, scoring EMA, and cooldowns only.
- In HALF_OPEN, allow a single probe acquisition and gate others with `PoolCircuitOpenError`.

## Consequences

- **Positive:** Breaker reflects real workload health; health probes cannot false-trip shedding.
- **Negative:** If callers forget to `mark_failed`, the breaker stays silent.
- **Neutral:** Per-proxy isolation remains the job of cooldowns / scoring eviction, not the pool-wide breaker.

## Alternatives considered

| Alternative | Why not |
|-------------|---------|
| Health checks trip the breaker | Couples probe endpoint health to traffic shedding |
| Per-proxy circuit breakers only | Misses pool-wide cascading failure signal |
| No breaker | Acquire storms during outages |

## Evidence

- `omniproxy/circuit_breaker.py`
- `omniproxy/pool.py` (`_apply_health_result` notes; `mark_success` / `mark_failed`)
- `docs/diagrams/omniproxy.puml`
