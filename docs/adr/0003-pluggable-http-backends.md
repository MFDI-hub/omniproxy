# ADR-0003: Pluggable HTTP backends via optional extras

- **Status:** Accepted
- **Date:** 2026-07-16
- **Tags:** networking, packaging

## Context

Proxy checks and anonymity probes need an HTTP client. Users already standardize on different stacks (httpx, aiohttp, requests, curl-cffi, tls-client) for TLS fingerprinting, SOCKS support, or sync vs async style.

Bundling every client as a hard dependency bloats installs and creates version conflicts. Pinning a single client forces stack churn on adopters.

## Decision

1. Keep the **runtime wheel lean**: `msgspec`, `orjson`, `typing-extensions`.
2. Expose a **`BaseBackend` adapter** and **`get_backend(name)` factory**.
3. Ship each HTTP client as an **optional extra** (`[httpx]`, `[aiohttp]`, `[requests]`, `[curl_cffi]`, `[tls_client]`, plus `[re2]` / `[scrape]` / `[all]`).
4. Raise a clear `ImportError` with install guidance when the chosen backend is missing.
5. Prefer **`curl_cffi` as the library default** (`DEFAULT_BACKEND`) for stronger default TLS fingerprint behavior; docs/examples may still showcase httpx as a common install path.

## Consequences

- **Positive:** Users install only what they need; one check API across backends; SOCKS/TLS strategies stay swappable.
- **Negative:** “Works after `pip install omniproxy`” does not imply checks work until an extra is installed.
- **Neutral:** Backend feature parity is best-effort; some clients are sync-only or lack certain transports.

## Alternatives considered

| Alternative | Why not |
|-------------|---------|
| Single pinned HTTP client | Rejects large share of existing stacks |
| Fat wheel with all clients | Heavy, conflict-prone installs |
| Require user-supplied transport callbacks only | Too much glue for the common case |

## Evidence

- `omniproxy/backends/base.py`, `omniproxy/backends/factory.py`
- `omniproxy/constants.py` (`DEFAULT_BACKEND`, `SUPPORTED_BACKENDS`)
- `pyproject.toml` (`optional-dependencies`)
