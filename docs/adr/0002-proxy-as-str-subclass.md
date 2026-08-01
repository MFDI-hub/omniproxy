# ADR-0002: `Proxy` as an immutable `str` subclass

- **Status:** Accepted
- **Date:** 2026-07-16
- **Tags:** core-model, parsing

## Context

Downstream code stores proxies in configs, logs, env vars, and HTTP client `proxies=` maps. A rich object (dataclass / Pydantic model) forces adapters everywhere. A bare string loses structure (protocol, auth, rotation URL, metadata).

Parsing must also accept many wire formats (`host:port`, auth variants, full URLs, SOCKS, bracketed IPv6, trailing `[rotation_url]`).

## Decision

Represent a proxy as an **immutable `str` subclass** (`Proxy`) whose string value is the canonical URL, with structural and metadata fields on `__slots__`.

- Construction goes through `OmniproxyParser` (msgspec `Struct` + compiled format regexes).
- Structural fields are read-only after construction.
- Metadata (latency, anonymity, geo) is written only via internal `_set_attribute` / check helpers.
- Helpers (`url`, `safe_url`, `as_requests_proxies`, Playwright TypedDict) adapt to common clients without changing the core type.

## Consequences

- **Positive:** Drop-in for string APIs; structured access when needed; one canonical form after parse.
- **Negative:** Subclassing `str` has quirks (`isinstance`, hashing, pickle); mutation rules must stay disciplined.
- **Neutral:** Format regexes are a maintenance surface as new vendor formats appear.

## Alternatives considered

| Alternative | Why not |
|-------------|---------|
| Plain dataclass / Pydantic model | Requires adapters for every string-shaped API |
| TypedDict only | Weak runtime validation; poor method surface |
| Separate “raw string” + “info” types | Doubles APIs and invites drift |

## Evidence

- `omniproxy/proxy.py`, `omniproxy/extended_proxy.py`
- `omniproxy/utils.py` (`OmniproxyParser`)
- `omniproxy/constants.py` (`PROXY_FORMATS_REGEXP`)
