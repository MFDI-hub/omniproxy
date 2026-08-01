# ADR-0008: Pluggable selection strategies and optional EMA scoring

- **Status:** Accepted
- **Date:** 2026-07-16
- **Tags:** pool, selection

## Context

Different workloads need different pick policies:

- even spread (round-robin)
- cheap randomness
- prefer historically good proxies (weighted)
- prefer low latency

Hard-coding one policy would force forks. Weighted / latency policies need a shared success/latency signal without requiring every strategy to invent its own stats store.

## Decision

1. Define selection behind a **`SelectionStrategy` protocol**, built from `PoolConfig.strategy`.
2. Ship built-ins: round-robin, random, weighted, lowest-latency.
3. Maintain optional **EMA scoring state** (`EMAState`) updated on `mark_success` / `mark_failed` (and health outcomes for eligibility/eviction).
4. Require scoring config when the chosen strategy needs it (validated on `PoolConfig`).
5. Keep the in-memory container as a `deque[Proxy]` with eligibility filtering (cooldown, caps, tags, anonymity, sessions) **before** strategy selection.

## Consequences

- **Positive:** Workload-appropriate picking without pool forks; scoring is reusable across strategies and eviction.
- **Negative:** Cold start needs `min_samples` / grace behavior; misconfigured weighted strategy without scoring is a config error.
- **Neutral:** Custom strategies can be added later without changing acquire’s outer loop.

## Alternatives considered

| Alternative | Why not |
|-------------|---------|
| Single hard-coded strategy | Insufficient for scraping vs LB vs latency-sensitive APIs |
| External load balancer only | Leaves the library without in-process orchestration |
| Strategy-owned stats maps | Duplicates state and complicates mark_* paths |

## Evidence

- `omniproxy/strategies.py`, `omniproxy/scoring.py`
- `omniproxy/config.py` (`PoolStrategy`, `ScoringConfig`, validators)
- `omniproxy/pool.py` (`_select`, eligibility pipeline)
