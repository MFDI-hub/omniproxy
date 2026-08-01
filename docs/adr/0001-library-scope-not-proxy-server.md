# ADR-0001: Library scope — client-side proxy toolkit, not a proxy server

- **Status:** Accepted
- **Date:** 2026-07-16
- **Tags:** scope, product

## Context

Proxy tooling often collapses into one of two products:

1. A **network gateway** (accept inbound traffic, terminate TLS, forward upstream).
2. A **client library** (parse proxy strings, check reachability, orchestrate outbound use).

Callers of omniproxy are scrapers, automation, and API clients that already own their HTTP stack. They need a canonical proxy type, health checks, and pool orchestration—not another hop in the network path.

## Decision

Ship **omniproxy as an embeddable Python library** (plus a small CLI for ops), not as a reverse/forward proxy process.

Responsibilities in scope:

- Parse many proxy string formats into a structured `Proxy`.
- Check proxies through pluggable HTTP clients.
- Lease and manage pools (`AsyncProxyPool` / `SyncProxyPool`).
- Fetch/scrape proxy lists; expose `omniproxy check` / `scrape`.

Out of scope:

- Listening sockets, TLS termination, request rewriting.
- Built-in Redis/Postgres persistence or multi-process control planes.
- Application auth (OAuth/JWT); credentials live in proxy URLs only.

## Consequences

- **Positive:** Thin dependency surface; fits any HTTP stack; clear mental model for library users.
- **Negative:** Operators who want a standalone proxy gateway must use other tools.
- **Neutral:** Persistence and metrics are extension points (`StateStore`, `MetricsExporter`), not core services.

## Evidence

- `README.md`, `omniproxy/__init__.py`
- `pyproject.toml` (`libraries` classifiers; CLI entry points)
