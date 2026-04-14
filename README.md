# omniproxy

[![CI](https://img.shields.io/github/actions/workflow/status/MFDI-hub/omniproxy/ci.yml?branch=main&logo=github&label=CI)](https://github.com/MFDI-hub/omniproxy/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/omniproxy)](https://pypi.org/project/omniproxy/)
[![Python versions](https://img.shields.io/pypi/pyversions/omniproxy)](https://pypi.org/project/omniproxy/)
[![Documentation](https://img.shields.io/badge/docs-mfdi.github.io-526fff?logo=materialformkdocs&logoColor=white)](https://mfdi.github.io/omniproxy/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**omniproxy** parses proxy strings into a **`str`** subclass with structured fields (`ip`, `port`, credentials, protocol, optional rotation URL), checks proxies through pluggable HTTP clients (**httpx**, **aiohttp**, **requests**, **curl_cffi**, **tls_client**), and ships sync/async **proxy pools** with cooldowns, filters, optional rate limits, and background health monitoring. A small **CLI** bulk-checks files and scrapes proxy-like lines from URLs.

Use it when you want one canonical type for “proxy as string” in configs and logs, but still need reachability checks, anonymity hints, and pool orchestration without rewriting glue code each time.

---

## Key features

- **String-like `Proxy` type** — behaves as a canonical proxy string while exposing structured data and metadata (latency, anonymity, optional geo-style fields).
- **Many input formats** — `host:port`, colon/pipe auth variants, full URLs, SOCKS, bracketed IPv6, trailing `[rotation_url]` for mobile proxies.
- **Multi-backend checks** — sync and async APIs; optional anonymity classification; configurable default URL lists and retries.
- **I/O helpers** — read/write proxy lists; `fetch_proxies` to scrape pages over HTTPS.
- **Pools** — `SyncProxyPool` / `AsyncProxyPool` with round-robin or random selection, lifecycle hooks, optional `HealthMonitor`.
- **CLI** — `omniproxy check` and `omniproxy scrape` for quick operational workflows.
- **Typed** — `py.typed` marker; suitable for strict typing in downstream projects.

---

## Documentation

Full narrative docs, CLI details, and an **API reference** (MkDocs + Material + mkdocstrings) are published from `main`:

**[https://mfdi.github.io/omniproxy/](https://mfdi.github.io/omniproxy/)**

---

## Installation

**Python:** 3.10 or newer (`requires-python = ">=3.10"`).

**Runtime dependencies** (always installed with the wheel): **msgspec**, **orjson**. HTTP clients are **optional extras**—install at least one backend you intend to use for checks or the built-in httpx helpers.

### pip

```bash
pip install omniproxy
```

With an HTTP backend (recommended for checks and `Client` / `AsyncClient`):

```bash
pip install "omniproxy[httpx]"
```

Other extras:

```bash
pip install "omniproxy[aiohttp]"
pip install "omniproxy[requests]"
pip install "omniproxy[curl_cffi]"
pip install "omniproxy[tls_client]"
pip install "omniproxy[all]"
```

### uv

```bash
uv add omniproxy
```

With extras:

```bash
uv add "omniproxy[httpx]"
uv add "omniproxy[all]"
```

---

## Quickstart

```python
from omniproxy import Proxy

proxy = Proxy("login:password@210.173.88.77:3001")

print(proxy)         # http://login:password@210.173.88.77:3001
print(proxy.url)     # canonical URL
print(proxy.server)  # protocol://ip:port (no credentials)
print(proxy.as_requests_proxies())
```

Supported shapes include `host:port`, `host:port:login:password`, `login:password@host:port`, `host:port|login:password`, and `http://` / `socks5://` URLs. Mobile rotation API in brackets:

```python
Proxy("login:password@host:port[https://rotate.example/api]")
```

---

## Full usage

### Checking proxies

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

Async: `acheck_proxy`, `acheck_proxies`, `await proxy.acheck(...)`, `await proxy.aget_info(...)`.

### HTTP client wrappers (httpx)

Requires `omniproxy[httpx]`:

```python
from omniproxy import AsyncClient, Client, Proxy

proxy = Proxy("socks5://login:password@127.0.0.1:9050")

with Client(proxy=proxy) as client:
    print(client.get("https://httpbin.org/ip").status_code)
```

### Proxy pool

```python
from omniproxy import ProxyPool

pool = ProxyPool(["10.0.0.1:8000", "10.0.0.2:8000"], strategy="round_robin")
print(pool.get_next())
```

For new code, prefer `SyncProxyPool` / `AsyncProxyPool` from `omniproxy.pool` (see [docs — Pools](https://mfdi.github.io/omniproxy/reference/pools/)).

### Global configuration

```python
from omniproxy import settings

settings.default_backend = "httpx"
settings.default_timeout = 10.0
settings.default_check_urls = ["https://api.ipify.org/?format=json"]
```

`default_check_urls` is a **non-empty list** used for reachability checks (with rotation across entries on retry). Templates for `with_info=True` live in `settings.default_check_info_url_templates` (each must contain `{fields}`). Details: [Configuration](https://mfdi.github.io/omniproxy/reference/configuration/).

### CLI

```bash
omniproxy check proxies.txt --backend httpx --timeout 8 --output-good good.txt
omniproxy scrape https://example.com/proxies -o scraped.txt
```

See [CLI documentation](https://mfdi.github.io/omniproxy/cli/) and `omniproxy --help`.

---

## Project structure

```
omniproxy/
├── omniproxy/              # Library package
│   ├── backends/           # httpx, aiohttp, requests, curl_cffi, tls_client
│   ├── cli.py
│   ├── config.py
│   ├── extended_proxy.py   # Proxy subclass, checks, bulk helpers
│   ├── io.py
│   ├── pool.py
│   ├── proxy.py
│   ├── utils.py
│   └── ...
├── tests/
├── docs/                   # MkDocs site source
├── examples/               # Runnable examples
├── scripts/
├── .github/workflows/      # CI (ruff, ty), docs deploy
├── mkdocs.yml
├── pyproject.toml
├── uv.lock
├── README.md
├── LICENSE
└── FEATURES.md             # Architecture checklist (reviewer map)
```

---

## Contributing

1. **Fork** the repository and create a **feature branch** from `main`.
2. Make your changes; keep commits focused and messages clear.
3. **Style:** this repo uses **[Ruff](https://docs.astral.sh/ruff/)** for linting (and formatting where configured). Run `uv run ruff check omniproxy tests` before opening a PR.
4. **Types:** run `uv run ty check` when you touch typing-sensitive code ([ty](https://docs.astral.sh/ty/) reads `[tool.ty]` in `pyproject.toml`).
5. **Tests:** run `uv run pytest` locally; fix failures and avoid regressions.
6. Open a **pull request** against `main` with a short description of the change and any trade-offs.

---

## Changelog

There is no root `CHANGELOG.md` yet. **Release history** and tags are tracked on GitHub:

**[https://github.com/MFDI-hub/omniproxy/releases](https://github.com/MFDI-hub/omniproxy/releases)**

For semantic versioning, follow the version in `omniproxy/__init__.py` and PyPI.

---

## Development (from a clone)

```bash
uv sync --group dev --group httpx   # or rely on [tool.uv] default-groups
uv run ruff check omniproxy tests
uv run ty check
uv run pytest
uv build
```

Preview documentation:

```bash
uv sync --group dev --group httpx
uv run mkdocs serve
```

---

## License

This project is licensed under the **MIT License** — see **[LICENSE](LICENSE)**.
