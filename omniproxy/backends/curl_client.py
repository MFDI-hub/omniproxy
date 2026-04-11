"""curl_cffi backend for TLS fingerprinting / stealth checks."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from ..constants import DEFAULT_BACKEND_TIMEOUT
from ..proxy import Proxy
from .base import BackendResponse, BaseBackend


class CurlBackend(BaseBackend):
    name = "curl_cffi"

    def get(
        self, url: str, proxy: Proxy, *, timeout: float = DEFAULT_BACKEND_TIMEOUT, **kwargs: Any
    ) -> BackendResponse:
        try:
            from curl_cffi import requests as curl_requests  # type: ignore
        except ImportError as e:
            raise ImportError("Install with 'uv add omniproxy --extra curl_cffi'") from e

        if "http" in proxy.protocol or "socks" in proxy.protocol:
            proxies = {"http": proxy.url, "https": proxy.url}
        else:
            raise ValueError(
                f'Unsupported proxy protocol "{proxy.protocol}" for curl_cffi backend.'
            )
        impersonate = kwargs.pop("impersonate", "chrome")

        r = curl_requests.get(
            url,
            proxies=proxies,
            timeout=int(timeout) if timeout else None,
            impersonate=impersonate,
            **kwargs,
        )
        jd = None
        with contextlib.suppress(Exception):
            jd = r.json()
        return BackendResponse(
            status_code=r.status_code,
            headers=dict(r.headers) if hasattr(r.headers, "items") else {},
            json_data=jd,
            text=getattr(r, "text", "") or "",
        )

    async def aget(
        self, url: str, proxy: Proxy, *, timeout: float = DEFAULT_BACKEND_TIMEOUT, **kwargs: Any
    ) -> BackendResponse:
        return await asyncio.to_thread(self.get, url, proxy, timeout=timeout, **kwargs)

    def request_direct(
        self, method: str, url: str, *, timeout: float = DEFAULT_BACKEND_TIMEOUT, **kwargs: Any
    ) -> BackendResponse:
        try:
            from curl_cffi import requests as curl_requests
        except ImportError as e:
            raise ImportError("Install with 'uv add omniproxy --extra curl_cffi'") from e

        impersonate = kwargs.pop("impersonate", "chrome")
        r = curl_requests.request(
            method.upper(),
            url,
            proxies=None,
            timeout=int(timeout) if timeout else None,
            impersonate=impersonate,
            **kwargs,
        )
        jd = None
        with contextlib.suppress(Exception):
            jd = r.json()
        return BackendResponse(
            status_code=r.status_code,
            headers=dict(r.headers) if hasattr(r.headers, "items") else {},
            json_data=jd,
            text=getattr(r, "text", "") or "",
        )

    async def arequest_direct(
        self, method: str, url: str, *, timeout: float = DEFAULT_BACKEND_TIMEOUT, **kwargs: Any
    ) -> BackendResponse:
        return await asyncio.to_thread(
            lambda: self.request_direct(method, url, timeout=timeout, **kwargs)
        )
