# omniproxy

**omniproxy** is a Python library for parsing proxy strings into rich, string-like **`Proxy`** objects, checking them against pluggable HTTP backends, and orchestrating pools with rotation, cooldowns, and optional health monitoring. A small **CLI** covers bulk checks and scraping proxy lists from URLs.

---

## Why omniproxy?

- **Familiar ergonomics:** `Proxy` subclasses `str`, so it drops into logs, files, and templates while exposing structured fields (`ip`, `port`, `username`, `password`, `protocol`, and more).
- **Broad input formats:** From bare `host:port` to authenticated URLs and mobile rotation URLs in trailing brackets.
- **Pluggable checks:** Use `httpx`, `aiohttp`, `requests`, `curl_cffi`, or `tls_client`—sync or async—with shared configuration via `settings`.
- **Pools and monitoring:** `SyncProxyPool` and `AsyncProxyPool` provide round-robin or random selection, filters, optional RPS limits, refresh-on-exhaustion, and background health checks.

---

## Installation

Install the core package from PyPI:

```bash
pip install omniproxy
```

Optional HTTP backends are extras—install the stack you use in production:

```bash
pip install "omniproxy[httpx]"
pip install "omniproxy[aiohttp]"
pip install "omniproxy[requests]"
pip install "omniproxy[curl_cffi]"
pip install "omniproxy[tls_client]"
pip install "omniproxy[all]"
```

If you use **uv** in your own project:

```bash
uv add omniproxy
# e.g. with httpx backend
uv add "omniproxy[httpx]"
```

---

## Quick start

Parse a proxy string and inspect canonical URLs:

```python
from omniproxy import Proxy

proxy = Proxy("login:password@210.173.88.77:3001")

print(proxy)        # http://login:password@210.173.88.77:3001
print(proxy.url)    # http://login:password@210.173.88.77:3001
print(proxy.server) # http://210.173.88.77:3001
print(proxy.playwright)
print(proxy.as_requests_proxies())
```

### Supported string shapes

- `host:port`
- `host:port:login:password`
- `login:password@host:port`
- `host:port|login:password`
- `http://login:password@host:port`
- `socks5://login:password@host:port`

Mobile proxies with a rotation API URL:

```python
Proxy("login:password@host:port[https://rotate.example/api]")
```

---

## Check reachability

```python
from omniproxy import Proxy, check_proxy, check_proxies

single = Proxy("10.0.0.1:8000")
proxy, ok = check_proxy(single)

good, bad = check_proxies(
    ["10.0.0.1:8000", "10.0.0.2:8000"],
    backend="httpx",
    detect_anonymity=True,
)
```

Async helpers include `acheck_proxy`, `acheck_proxies`, `await proxy.acheck(...)`, and `await proxy.aget_info(...)`.

---

## HTTP client helpers

```python
from omniproxy import AsyncClient, Client, Proxy

proxy = Proxy("socks5://login:password@127.0.0.1:9050")

with Client(proxy=proxy) as client:
    print(client.get("https://httpbin.org/ip").status_code)
```

---

## Proxy pool

```python
from omniproxy import ProxyPool

pool = ProxyPool(["10.0.0.1:8000", "10.0.0.2:8000"], strategy="round_robin")
print(pool.get_next())
```

!!! note "Prefer explicit pool imports for new code"
    `ProxyPool` is a deprecated alias of `SyncProxyPool`. For asyncio workloads, use `AsyncProxyPool` from `omniproxy.pool`. See the [Pools](reference/pools.md) reference page for monitoring, configuration, and migration notes.

---

## Global defaults

Tune backends, timeouts, and default check URLs from one place:

```python
from omniproxy import settings

settings.default_backend = "httpx"
settings.default_timeout = 10.0
settings.default_check_urls = ["https://api.ipify.org/?format=json"]
```

For URL list semantics, retries, and health-check overrides, see [Configuration](reference/configuration.md) and [Checking](reference/checking.md).

---

## Command line

```bash
omniproxy check proxies.txt --backend httpx --timeout 8 --output-good good.txt
omniproxy scrape https://example.com/proxies -o scraped.txt
```

Full flag reference: [CLI](cli.md).

---

## Documentation map

| I want to… | Read |
|------------|------|
| High-level architecture and module map | [Reference overview](reference/overview.md) |
| `Proxy` fields, metadata, rotation, formatting | [Proxy](reference/proxy.md) |
| `OmniproxyParser` and regex pipeline | [Parser](reference/parser.md) |
| HTTP backends and `BackendResponse` | [Backends](reference/backends.md) |
| Checks, anonymity, metadata, health drivers | [Checking](reference/checking.md) |
| Files and `fetch_proxies` / scrape behavior | [File and network I/O](reference/io.md) |
| Pools, `PoolConfig`, `HealthMonitor`, errors | [Pools](reference/pools.md) |
| `settings` and URL list validation | [Configuration](reference/configuration.md) |
| Imports and lazy clients | [Package layout and exports](reference/exports.md) |
| Console `omniproxy` command | [CLI](cli.md) |
| Docstrings and signatures | [API Reference](api.md) |

The repository also keeps a dense reviewer checklist in [`FEATURES.md`](https://github.com/MFDI-hub/omniproxy/blob/main/FEATURES.md) at the project root; the **Reference** section above is the narrative counterpart for the docs site.

---

## Development (this repository)

```bash
uv run ruff check omniproxy tests
uv run ty check
uv run pytest
uv build
```

Preview documentation locally:

```bash
uv sync --group dev --group httpx
uv run mkdocs serve
```
