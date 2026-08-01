# ADR-0015: Logging and observability hooks

- **Status:** Accepted
- **Date:** 2026-07-16
- **Tags:** logging, observability

## Context

Background tasks (health checks, refresh, dead‑letter retry, warmup) run without direct caller interaction. When something goes wrong (fetcher returns 500, circuit opens, cooldown evicts a proxy), operators need visibility. The library must emit diagnostic information without imposing a specific logging framework or structured logging dependency.

## Decision

1. Use **Python’s standard `logging` module** with a root logger name `omniproxy`. Child loggers follow module hierarchy (`omniproxy.pool`, `omniproxy.backends`, etc.).

2. **Log levels by convention:**
   - `DEBUG` – detailed pool events (acquire/release, selection reasoning, health‑check probe outcome).
   - `INFO` – lifecycle transitions (refresh completed, breaker state change, warmup finished).
   - `WARNING` – recoverable issues (fetcher returned empty, rotation URL failed, dead‑letter retry exhausted).
   - `ERROR` – unexpected internal exceptions (should never happen, but logged for bug reports).

3. **Credential safety:** log messages that include a `Proxy` must use `proxy.safe_url` (or `str(proxy.server)`). Raw credential strings are never emitted.

4. **Structured metrics** are deliberately separate from logging. The `MetricsExporter` protocol (ADR‑0012) provides a hook for counter/gauge‑style metrics. The library does **not** emit structured metrics via logging (e.g. JSON lines); it uses the metrics hook for that.

5. No logging configuration is applied by the library at import time. Users control handlers, formatters, and levels through their own `logging.basicConfig` or dict config. The library merely acquires loggers via `logging.getLogger(__name__)`.

## Consequences

- **Positive:** Zero‑dependency observability; easy to integrate with existing logging setups; safe for credential‑heavy environments.
- **Negative:** Without structured logging, complex parsing may be needed to extract metrics from text logs (mitigated by the `MetricsExporter` hook).
- **Neutral:** Debug‑level logging can be verbose; users may need to adjust levels per logger.

## Alternatives considered

| Alternative | Why not |
|-------------|---------|
| Third‑party structured logging (structlog, loguru) | Forced dependency; not all users want it. |
| Silent by default (no logging at all) | Makes operational issues invisible; users insert their own prints. |
| Print‑based output | Interferes with stdout, no level filtering, not thread‑safe. |

## Evidence

- `omniproxy/utils.py` (helper `_log_safe` or equivalent)
- `omniproxy/pool.py`, `omniproxy/refresh.py`, `omniproxy/dead_letter.py` (logger calls)
- `docs/for_mine_and_only_my.md` (logging configuration examples)