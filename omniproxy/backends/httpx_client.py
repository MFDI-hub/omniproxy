"""httpx-based backend and wrapped Client / AsyncClient."""

from __future__ import annotations

import contextlib
from typing import Any

import httpx  # type: ignore
from httpx_socks import AsyncProxyTransport, SyncProxyTransport  # type: ignore

from ..constants import DEFAULT_BACKEND_TIMEOUT
from ..proxy import Proxy
from .base import BackendResponse, BaseBackend


class Client(httpx.Client):
    def __init__(self, *args, proxy: Proxy | str | None = None, follow_redirects=True, **kwargs):
        if proxy:
            proxy = Proxy(proxy)
            if "http" in proxy.protocol:
                kwargs["proxy"] = proxy.url
            elif "socks" in proxy.protocol:
                kwargs["transport"] = SyncProxyTransport.from_url(proxy.url)
            else:
                raise ValueError(f'Unsupported proxy protocol "{proxy.protocol}".')
        super().__init__(*args, follow_redirects=follow_redirects, **kwargs)


class AsyncClient(httpx.AsyncClient):
    def __init__(self, *args, proxy: Proxy | str | None = None, follow_redirects=True, **kwargs):
        if proxy:
            proxy = Proxy(proxy)
            if "http" in proxy.protocol:
                kwargs["proxy"] = proxy.url
            elif "socks" in proxy.protocol:
                kwargs["transport"] = AsyncProxyTransport.from_url(proxy.url)
            else:
                raise ValueError(f'Unsupported proxy protocol "{proxy.protocol}".')
        super().__init__(*args, follow_redirects=follow_redirects, **kwargs)


class HttpxBackend(BaseBackend):
    name = "httpx"

    def get(
        self, url: str, proxy: Proxy, *, timeout: float = DEFAULT_BACKEND_TIMEOUT, **kwargs: Any
    ) -> BackendResponse:
        with Client(proxy=proxy, timeout=timeout, **kwargs) as client:
            r = client.get(url)
            return self._from_httpx_response(r)

    async def aget(
        self, url: str, proxy: Proxy, *, timeout: float = DEFAULT_BACKEND_TIMEOUT, **kwargs: Any
    ) -> BackendResponse:
        async with AsyncClient(proxy=proxy, timeout=timeout, **kwargs) as client:
            r = await client.get(url)
            return self._from_httpx_response(r)

    def request_direct(
        self, method: str, url: str, *, timeout: float = DEFAULT_BACKEND_TIMEOUT, **kwargs: Any
    ) -> BackendResponse:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            r = client.request(method.upper(), url, **kwargs)
            return self._from_httpx_response(r)

    async def arequest_direct(
        self, method: str, url: str, *, timeout: float = DEFAULT_BACKEND_TIMEOUT, **kwargs: Any
    ) -> BackendResponse:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            r = await client.request(method.upper(), url, **kwargs)
            return self._from_httpx_response(r)

    @staticmethod
    def _from_httpx_response(r: httpx.Response) -> BackendResponse:
        jd = None
        with contextlib.suppress(Exception):
            jd = r.json()
        return BackendResponse(
            status_code=r.status_code,
            headers=dict(r.headers),
            json_data=jd,
            text=r.text,
        )
