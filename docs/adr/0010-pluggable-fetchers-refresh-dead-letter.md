# ADR-0010: Pluggable fetchers, refresh, warmup, and dead-letter

- **Status:** Accepted
- **Date:** 2026-07-16
- **Tags:** replenishment, resilience

## Context

Pools shrink as proxies die, cool down, or get evicted. Callers source replacements from files, HTTP lists, or HTML pages. Coupling the pool to one source format makes reuse hard.

Failed proxies also need a holding area so they are not immediately re-served, with optional retry and optional durable storage.

## Decision

1. Define a **`ProxyFetcher` protocol** (`async def fetch() -> list[Proxy | str]`).
2. Ship built-ins: file, URL, and scrape fetchers.
3. Drive replenishment through **background refresh** and **on-demand refresh** when below `min_size`, merging under locks with generation coalescing.
4. Offer optional **warmup** before the pool becomes ready.
5. Keep removed/failing proxies in a **dead-letter queue** with retry worker; persistence is optional via a `StateStore` protocol / factory—not a built-in database.

## Consequences

- **Positive:** Any source can plug in; pool size stays healthy without app timers; dead-letter avoids hot-looping bad endpoints.
- **Negative:** Fetcher quality and scrape fragility remain the caller’s/source’s problem.
- **Neutral:** In-memory dead-letter is the default; durable stores are opt-in integrations.

## Alternatives considered

| Alternative | Why not |
|-------------|---------|
| Hard-coded file-only refresh | Ignores URL/list providers and scrapers |
| Built-in Redis/SQLite dead-letter | Violates lean library scope; wrong for many embeds |
| No dead-letter (hard delete only) | Loses recoverable proxies and observability |

## Evidence

- `omniproxy/fetchers/` (`base.py`, file/url/scrape fetchers)
- `omniproxy/refresh.py`, `omniproxy/warmup.py`, `omniproxy/dead_letter.py`
- `omniproxy/config.py` (`RefreshConfig`, `WarmupConfig`, `DeadLetterConfig`, `StateStore`)
