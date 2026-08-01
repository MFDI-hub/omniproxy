# Architecture Decision Records

This directory captures the significant architectural decisions behind **omniproxy** (v4).

Each ADR follows a short MADR-style template:

| Field | Meaning |
|-------|---------|
| **Status** | Proposed · Accepted · Deprecated · Superseded |
| **Context** | Why a decision was needed |
| **Decision** | What we chose |
| **Consequences** | What follows from that choice |

## Index

| ADR | Title | Status |
|-----|-------|--------|
| [0001](0001-library-scope-not-proxy-server.md) | Library scope: client-side proxy toolkit, not a proxy server | Accepted |
| [0002](0002-proxy-as-str-subclass.md) | `Proxy` as an immutable `str` subclass | Accepted |
| [0003](0003-pluggable-http-backends.md) | Pluggable HTTP backends via optional extras | Accepted |
| [0004](0004-async-first-pool-sync-bridge.md) | Async-first pool; sync via event-loop bridge | Accepted |
| [0005](0005-explicit-lease-lifecycle.md) | Explicit acquire / release \| mark_success \| mark_failed lifecycle | Accepted |
| [0006](0006-frozen-pydantic-config-presets.md) | Frozen Pydantic config graphs with opinionated presets | Accepted |
| [0007](0007-circuit-breaker-client-outcomes-only.md) | Pool-wide circuit breaker fed only by client outcomes | Accepted |
| [0008](0008-selection-strategies-and-ema-scoring.md) | Pluggable selection strategies and optional EMA scoring | Accepted |
| [0009](0009-sticky-sessions-and-rotation-urls.md) | Sticky sessions and rotation URLs for residential/mobile | Accepted |
| [0010](0010-pluggable-fetchers-refresh-dead-letter.md) | Pluggable fetchers, refresh, warmup, and dead-letter | Accepted |

## Related docs

- Package overview: [`README.md`](../../README.md)
- Pool flows: [`../pool-flows/`](../pool-flows/README.md)
- Architecture diagram: [`../diagrams/omniproxy.puml`](../diagrams/omniproxy.puml)
