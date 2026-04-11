# omniproxy

`omniproxy` is a proxy parsing and utility library with string-like proxy objects, multi-backend checking, rotation helpers, and a small CLI.

## Installation

Core package:

```bash
pip install omniproxy
```

With optional HTTP backends:

```bash
pip install "omniproxy[httpx]"
pip install "omniproxy[aiohttp]"
pip install "omniproxy[requests]"
pip install "omniproxy[curl_cffi]"
pip install "omniproxy[tls_client]"
pip install "omniproxy[all]"
```

## Quick start

```python
from omniproxy import Proxy

proxy = Proxy("login:password@210.173.88.77:3001")

print(proxy)        # http://login:password@210.173.88.77:3001
print(proxy.url)    # http://login:password@210.173.88.77:3001
print(proxy.server) # http://210.173.88.77:3001
print(proxy.playwright)
print(proxy.as_requests_proxies())
```

Supported string formats include:

- `host:port`
- `host:port:login:password`
- `login:password@host:port`
- `host:port|login:password`
- `http://login:password@host:port`
- `socks5://login:password@host:port`

You can append a rotation URL for mobile proxies:

```python
Proxy("login:password@host:port[https://rotate.example/api]")
```

## Checking proxies

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

Async APIs are available too:

- `acheck_proxy(...)`
- `acheck_proxies(...)`
- `await proxy.acheck(...)`
- `await proxy.aget_info(...)`

## Built-in client wrappers

```python
from omniproxy import AsyncClient, Client, Proxy

proxy = Proxy("socks5://login:password@127.0.0.1:9050")

with Client(proxy=proxy) as client:
    print(client.get("https://httpbin.org/ip").status_code)
```

## Proxy pool

```python
from omniproxy import ProxyPool

pool = ProxyPool(["10.0.0.1:8000", "10.0.0.2:8000"], strategy="round_robin")
print(pool.get_next())
```

## CLI

```bash
omniproxy check proxies.txt --backend httpx --timeout 8 --output-good good.txt
omniproxy scrape https://example.com/proxies -o scraped.txt
```

## Configuration

```python
from omniproxy import settings

settings.default_backend = "httpx"
settings.default_timeout = 10.0
settings.default_check_url = "https://api.ipify.org/?format=json"
```

## Development

```bash
uv run ruff check omniproxy tests
uv run mypy omniproxy
uv run pytest
uv build
```
