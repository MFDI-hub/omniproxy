"""aiohttp-based backend."""

from __future__ import annotations

import asyncio
from typing import Any

from aiohttp import ClientTimeout, TCPConnector
from aiohttp.client import ClientSession

from ..proxy import Proxy
from .base import BackendResponse, BaseBackend


class AiohttpBackend(BaseBackend):
    name = "aiohttp"

    def get(self, url: str, proxy: Proxy, *, timeout: float = 10.0, **kwargs: Any) -> BackendResponse:
        return asyncio.run(self.aget(url, proxy, timeout=timeout, **kwargs))

    async def aget(self, url: str, proxy: Proxy, *, timeout: float = 10.0, **kwargs: Any) -> BackendResponse:
        try:
            from aiohttp_socks import ProxyConnector
        except ImportError as e:
            raise ImportError("Install with 'uv add proxystr --extra aiohttp' (needs aiohttp-socks for SOCKS).") from e

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
                try:
                    jd = await resp.json(content_type=None)
                except Exception:
                    pass
                return BackendResponse(
                    status_code=resp.status,
                    headers={k: v for k, v in resp.headers.items()},
                    json_data=jd,
                    text=text,
                )
