
# ADR-0013: Error taxonomy and exception hierarchy

- **Status:** Accepted
- **Date:** 2026-07-16
- **Tags:** errors, api-design

## Context

The library performs I/O (checks, fetchers, rotation URLs), enforces internal constraints (circuit open, max leases, cooldowns), and validates configuration. Callers need to catch specific errors without importing every internal exception type. Currently exceptions exist ad‑hoc; without a documented hierarchy, additions could break existing `except` blocks.

## Decision

1. Root exception: **`OmniproxyError`** (inherits from `Exception`). All library‑raised exceptions are descendants of this.

2. First‑level families:
   - **`ProxyCheckError`** – failure during a proxy reachability / anonymity check.
   - **`PoolError`** – any pool‑related runtime error (subtypes: `PoolCircuitOpenError`, `PoolExhaustedError`, `PoolLeaseError`).
   - **`FetcherError`** – failure inside a `ProxyFetcher` implementation (wraps underlying I/O or parse issues).
   - **`BackendError`** – when an HTTP backend raises an exception; always wraps the original client library exception.
   - **`ConfigError`** – invalid pool or global configuration (raised at construction time).

3. **Wrapping rule:** backend‑specific exceptions (e.g. `httpx.ConnectError`, `aiohttp.ClientConnectorError`) are **never** allowed to leak directly to the caller. They must be caught and re‑raised as `BackendError` with `cause` chaining.

4. **Safe stringification:** exceptions that hold a `Proxy` must use `proxy.safe_url` in their `__str__` to prevent credential leakage into logs.

5. **Payload access:** all exceptions carry a machine‑readable attribute where relevant (e.g. `proxy` string, `backend_name`, `retry_count`).

## Consequences

- **Positive:** Callers catch one stable hierarchy; backend swaps do not ripple into `except` clauses; credential‑safe logging is enforced.
- **Negative:** Additional wrapping layer may obscure original tracebacks if not properly chained (mitigated by `raise ... from`).
- **Neutral:** New error subtypes may be added later; major version bumps will only follow removal or renaming.

## Alternatives considered

| Alternative | Why not |
|-------------|---------|
| Flat list of exceptions with no root | Harder to catch “any omniproxy error” in library wrappers. |
| Re‑exporting backend exceptions directly | Leaks implementation details; breaks when backend changes. |
| Generic `Exception` in all public APIs | Sacrifices granularity needed for retry / circuit‑breaker decisions. |

## Evidence

- `omniproxy/exceptions.py` (definition of all exception classes)
- `omniproxy/backends/` (wrapping patterns in each adapter)
- `omniproxy/pool.py` (`PoolCircuitOpenError` usage)