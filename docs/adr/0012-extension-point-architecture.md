
# ADR-0012: Extension point architecture (`StateStore`, `MetricsExporter`, custom fetchers)

- **Status:** Accepted
- **Date:** 2026-07-16
- **Tags:** architecture, plugins, persistence

## Context

ADR‑0001 scopes omniproxy as an embeddable library without built‑in persistence or metric pipelines. Yet multiple extension hooks are already implied: `StateStore` for dead‑letter durability (ADR‑0010), `MetricsExporter` for structured observability, and the `ProxyFetcher` protocol (ADR‑0010). Without a unified extension policy, third‑party integrations may depend on private APIs or drift as the library evolves.

## Decision

1. Define a **small, stable set of `typing.Protocol`** interfaces that form the public extension surface:
   - `ProxyFetcher` (already present, `async def fetch() -> list[Proxy | str]`)
   - `StateStore` (optional persistence for dead‑letter, sessions, or scoring)
   - `MetricsExporter` (optional hook for structured metrics emission)

2. Accept these interfaces in `PoolConfig` fields (e.g. `state_store_factory: Callable[[], StateStore]`), not via global registration. The pool calls them at well‑defined lifecycle events and never caches or mutates them outside the documented contract.

3. **No discovery mechanism** (entry points, setuptools plugins) is shipped in the library itself. Third‑party packages can advertise their implementations through their own docs; the integrator explicitly passes instances to the pool.

4. Internal implementation details (e.g. the concrete class behind `StateStore`) are **not** part of the public API and may change. The protocol alone defines backward compatibility.

5. Custom selection strategies (ADR‑0008) follow the same pattern: implement the `SelectionStrategy` protocol and pass it to `PoolConfig.strategy`.

## Consequences

- **Positive:** Clear contract for external extensions; core team avoids bundling Redis/Postgres while still enabling them; integrators know exactly what they can swap.
- **Negative:** No automatic plugin discovery; users must wire components manually.
- **Neutral:** The set of protocols may grow slowly; each new protocol requires an ADR amendment.

## Alternatives considered

| Alternative | Why not |
|-------------|---------|
| Abstract base classes | Heavier; `Protocol` matches the “anything with the right methods” philosophy. |
| Global registry + entry points | Adds unnecessary framework weight; violates “library, not platform” scope. |
| No defined interfaces (duck typing only) | Fails to provide discoverability; callers guess method signatures. |

## Evidence

- `omniproxy/fetchers/base.py` (`ProxyFetcher`)
- `omniproxy/dead_letter.py` (comments referencing `StateStore`)
- `omniproxy/config.py` (`PoolConfig` fields for `state_store_factory` and `metrics_exporter`)