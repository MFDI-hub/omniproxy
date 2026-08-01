# ADR-0006: Frozen Pydantic config graphs with opinionated presets

- **Status:** Accepted
- **Date:** 2026-07-16
- **Tags:** configuration

## Context

Pool behavior has many interacting knobs (cooldown, limits, sessions, scoring, breaker, refresh, warmup). Mutable nested dicts invite partial updates and racey hot-mutation. Users also need starting points for common workloads (scraping, API gateways, stealth, residential rotation, load balancing).

## Decision

1. Model process defaults as a frozen **`GlobalConfig`** singleton (`settings`).
2. Model pool policy as a frozen **`PoolConfig`** graph of nested config models (`CooldownConfig`, `SessionConfig`, …).
3. Apply changes by **replacement** (`model_copy(update=...)`), not in-place mutation.
4. Ship **opinionated presets** as classmethods:
   - `scraping_preset`
   - `api_gateway_preset`
   - `stealth_preset`
   - `rotating_residential_preset`
   - `load_balancer_preset`
5. Validate cross-field consistency in model validators (e.g. scoring required for weighted / lowest-latency strategies).

## Consequences

- **Positive:** Safe sharing across tasks; clearer diffs when swapping policy; presets encode known-good combinations.
- **Negative:** Nested models are verbose; callers must learn `model_copy` for tweaks.
- **Neutral:** Presets are starting points, not hard product modes—any field can still be overridden via copy.

## Alternatives considered

| Alternative | Why not |
|-------------|---------|
| Mutable singleton config | Racey; hard to reason about mid-flight changes |
| YAML/TOML-only config files | Extra I/O layer; Python call sites still need objects |
| Flat kwargs on the pool constructor | Does not scale to nested policies |

## Evidence

- `omniproxy/config.py` (`GlobalConfig`, `PoolConfig`, presets)
