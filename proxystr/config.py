"""Global defaults for proxystr (backends, timeouts, check URLs)."""

from __future__ import annotations

# Default HTTP client backend for checks and CLI (httpx | aiohttp | requests | curl_cffi | tls_client)
default_backend: str = "httpx"

# Default timeouts in seconds
default_timeout: float = 10.0
default_connect_timeout: float | None = None

# Optional override for check targets (extended_proxy sets concrete defaults)
default_check_url: str = "https://api.ipify.org/?format=json"
