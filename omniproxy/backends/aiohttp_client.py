"""aiohttp-based backend."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from aiohttp import ClientTimeout, TCPConnector  # type: ignore
from aiohttp.client import ClientSession  # type: ignore

from ..constants import DEFAULT_BACKEND_TIMEOUT
from ..proxy import Proxy
from .base import BackendResponse, BaseBackend


class AiohttpBackend(BaseBackend):
    name = "aiohttp"

    def get(self, url, proxy, *, timeout=10.0, **kwargs):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.aget(url, proxy, timeout=timeout, **kwargs))
        # If a loop is running, this is a genuine limitation — raise clearly
        raise RuntimeError(
            "AiohttpBackend.get() cannot be called from an async context; use aget() instead."
        )

    async def aget(
        self, url: str, proxy: Proxy, *, timeout: float = DEFAULT_BACKEND_TIMEOUT, **kwargs: Any
    ) -> BackendResponse:
        try:
            from aiohttp_socks import ProxyConnector  # type: ignore
        except ImportError as e:
            raise ImportError(
                "Install with 'uv add omniproxy --extra aiohttp' (needs aiohttp-socks for SOCKS)."
            ) from e

        to = ClientTimeout(total=timeout)
        if "socks" in proxy.protocol:
            connector = ProxyConnector.from_url(proxy.url)
            proxy_url = None
        else:
            connector = TCPConnector()
            proxy_url = proxy.url

        async with ClientSession(connector=connector, timeout=to) as session:
            async with session.get(url, proxy=proxy_url, **kwargs) as resp:
                text = await resp.text()
                jd = None
                with contextlib.suppress(Exception):
                    jd = await resp.json(content_type=None)
                return BackendResponse(
                    status_code=resp.status,
                    headers=dict(resp.headers.items()),
                    json_data=jd,
                    text=text,
                )

    def request_direct(
        self, method: str, url: str, *, timeout: float = DEFAULT_BACKEND_TIMEOUT, **kwargs: Any
    ) -> BackendResponse:
        return asyncio.run(self.arequest_direct(method, url, timeout=timeout, **kwargs))

    async def arequest_direct(
        self, method: str, url: str, *, timeout: float = DEFAULT_BACKEND_TIMEOUT, **kwargs: Any
    ) -> BackendResponse:
        to = ClientTimeout(total=timeout)
        async with ClientSession(connector=TCPConnector(), timeout=to) as session:
            async with session.request(method.upper(), url, **kwargs) as resp:
                text = await resp.text()
                jd = None
                with contextlib.suppress(Exception):
                    jd = await resp.json(content_type=None)
                return BackendResponse(
                    status_code=resp.status,
                    headers=dict(resp.headers.items()),
                    json_data=jd,
                    text=text,
                )
