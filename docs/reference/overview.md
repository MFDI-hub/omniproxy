# Library overview

This section documents **behavior and architecture** as implemented in the codebase: core types, parsing, HTTP backends, checks, I/O, pools, configuration, the CLI, and public exports. Use it together with the [API Reference](../api.md) (generated docstrings) and the [CLI](../cli.md) page.

---

## Scope

**omniproxy** is a Python package for:

- Parsing proxy strings into structured, string-like objects.
- Checking proxies through **pluggable HTTP clients**, with optional **anonymity** classification and optional **geo/ASN-style metadata**.
- **List-based default check URLs** with retry behavior that can rotate among URLs.
- **File and URL ingestion** for bulk proxy lists.
- **Synchronous and asyncio proxy pools** with cooldowns, filtering, optional **per-proxy RPS** limits, optional **refresh** when exhausted, optional **lifecycle hooks**, and optional **background health monitoring**.

---

## How this reference is organized

The original design notes follow this path: **overview → core types → parser → execution (backends, checks, I/O) → orchestration (pools) → CLI → configuration → exports**. These docs mirror that shape:

| Topic | Page |
|--------|------|
| `Proxy` model, rotation, formatting, interop | [Proxy](proxy.md) |
| `OmniproxyParser`, regex pipeline, formatting helpers | [Parser](parser.md) |
| Backend registry and response shape | [Backends](backends.md) |
| Checks, health checks, anonymity, metadata | [Checking](checking.md) |
| `read_proxies`, `save_proxies`, streaming, `fetch_proxies` | [File and network I/O](io.md) |
| Pools, `PoolConfig`, `HealthMonitor`, protocols, errors | [Pools](pools.md) |
| Global `settings` / `OmniproxyConfig` | [Configuration](configuration.md) |
| What to import from where | [Package layout and exports](exports.md) |

---

## Source modules (reviewer map)

User-facing behavior is implemented across:

- `omniproxy/proxy.py` — base string proxy / patterns / Playwright settings
- `omniproxy/extended_proxy.py` — extended `Proxy`, bulk checks, health check drivers, `CheckResult`
- `omniproxy/utils.py` — `OmniproxyParser`, `get_formatted_proxy_string`, `ALLOWED_PROTOCOLS`
- `omniproxy/constants.py` — compiled patterns, anonymity ranks, defaults
- `omniproxy/backends/*` — HTTP client adapters
- `omniproxy/io.py` — file and scrape helpers
- `omniproxy/pool.py` — pools, monitor, protocols, `TokenBucket`
- `omniproxy/config.py` — `settings`, `OmniproxyConfig`, `PoolConfig`, `HealthCheckConfig`
- `omniproxy/errors.py` — pool and filter exceptions
- `omniproxy/cli.py` — `omniproxy` console entrypoint

When the public API or defaults change, update these narrative pages and the root [FEATURES.md](https://github.com/mfdi/omniproxy/blob/main/FEATURES.md) (or its successor) in lockstep.
