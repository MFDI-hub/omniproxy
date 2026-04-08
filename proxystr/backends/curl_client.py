"""curl_cffi backend for TLS fingerprinting / stealth checks."""

from __future__ import annotations

import asyncio
from typing import Any

from ..proxy import Proxy
from .base import BackendResponse, BaseBackend


class CurlBackend(BaseBackend):
    name = "curl_cffi"

    def get(self, url: str, proxy: Proxy, *, timeout: float = 10.0, **kwargs: Any) -> BackendResponse:
        try:
            from curl_cffi import requests as curl_requests
        except ImportError as e:
            raise ImportError("Install with 'uv add proxystr --extra curl_cffi'") from e

        proxies = {"http": proxy.url, "https": proxy.url} if "http" in proxy.protocol else None
        if proxies is None and "socks" in proxy.protocol:
            proxies = {"http": proxy.url, "https": proxy.url}
        impersonate = kwargs.pop("impersonate", "chrome")

        r = curl_requests.get(
            url,
            proxies=proxies,
            timeout=int(timeout) if timeout else None,
            impersonate=impersonate,
            **kwargs,
        )
        jd = None
        try:
            jd = r.json()
        except Exception:
            pass
        return BackendResponse(
            status_code=r.status_code,
            headers=dict(r.headers) if hasattr(r.headers, "items") else {},
            json_data=jd,
            text=getattr(r, "text", "") or "",
        )

    async def aget(self, url: str, proxy: Proxy, *, timeout: float = 10.0, **kwargs: Any) -> BackendResponse:
        return await asyncio.to_thread(self.get, url, proxy, timeout=timeout, **kwargs)
