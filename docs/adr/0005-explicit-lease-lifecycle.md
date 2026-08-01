# ADR-0005: Explicit acquire / release | mark_success | mark_failed lifecycle

- **Status:** Accepted
- **Date:** 2026-07-16
- **Tags:** pool, api

## Context

A pool that “hands out” proxies must track active leases, connection caps, scoring, cooldowns, and circuit-breaker signals. Silent auto-release on GC is unreliable. Implicit success-on-any-return conflates “done using” with “request succeeded.”

Callers also need different outcomes:

- finished without a quality signal (`release`)
- successful use (`mark_success`)
- failed use with exception context (`mark_failed`)

## Decision

Treat pool membership as an **explicit lease**:

1. `acquire(...)` returns a `Proxy` and increments the lease counter.
2. Exactly **one** of `release`, `mark_success`, or `mark_failed` must follow per acquisition.
3. `mark_*` updates scores / cooldowns / circuit breaker **and** returns the lease (do not also call `release`).
4. Optional convenience flags (`auto_mark_success_on_exit`, `auto_mark_failed_on_exception`) may automate this inside context helpers without changing the underlying model.
5. Filters and sticky sessions are expressed via `AcquireOptions` at acquire time.

## Consequences

- **Positive:** Deterministic accounting; clear failure semantics; breaker and scoring stay accurate.
- **Negative:** Misuse (double release, or release + mark_success) is a footgun; docs must stress the rule.
- **Neutral:** Context-manager helpers can reduce boilerplate but must map cleanly onto the same lifecycle.

## Alternatives considered

| Alternative | Why not |
|-------------|---------|
| Auto-release via finalizers | Non-deterministic; hides failures |
| Single `done(success: bool)` API | Loses distinction between “released unused” and “succeeded” |
| Checkout without feedback | Cannot drive cooldown / scoring / breaker |

## Evidence

- `omniproxy/pool.py` (`acquire`, `release`, `mark_success`, `mark_failed`, `AcquireOptions`)
- `docs/for_mine_and_only_my.md` (usage lifecycle)
