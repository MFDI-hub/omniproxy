"""aiohttp-based backend."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from aiohttp import ClientTimeout, TCPConnector
from aiohttp.client import ClientSession

from ..constants import DEFAULT_BACKEND_TIMEOUT
from ..proxy import Proxy
from .base import BackendResponse, BaseBackend


class AiohttpBackend(BaseBackend):
    """:class:`BaseBackend` powered by :mod:`aiohttp` with optional ``aiohttp_socks`` connectors.

    SOCKS proxies require ``ProxyConnector`` from the ``aiohttp_socks`` distribution. The sync
    :meth:`get` helper refuses to run when an asyncio loop is already executing to avoid nested
    loop bugs—use :meth:`aget` from coroutines instead.

    Attributes
    ----------
    name: :class:`str`
        Constant ``aiohttp``.
    """

    name = "aiohttp"

    def get(self, url, proxy, *, timeout=10.0, **kwargs):
        """Sync wrapper that runs :meth:`aget` via ``asyncio.run`` when no loop is running.

        Args:
            url: Target URL.
            proxy: :class:`~omniproxy.proxy.Proxy` instance.
            timeout (float): Total client timeout seconds.
            **kwargs: Forwarded to aiohttp request APIs.

        Returns:
            BackendResponse: Parsed response.

        Raises:
            RuntimeError: If called from inside a running event loop.

        Example:
            >>> AiohttpBackend.get.__name__
            'get'
        """
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
        """GET *url* through *proxy* using aiohttp (SOCKS via ProxyConnector).

        Args:
            url (str): Target URL.
            proxy (Proxy): Proxy with ``protocol`` driving connector choice.
            timeout (float): ``ClientTimeout(total=...)`` value.
            **kwargs (Any): Extra ``session.get`` kwargs.

        Returns:
            BackendResponse: Parsed response.

        Raises:
            ImportError: If SOCKS is requested but aiohttp-socks is missing.

        Example:
            >>> AiohttpBackend.aget.__name__
            'aget'
        """
        try:
            from aiohttp_socks import ProxyConnector
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
        """Run :meth:`arequest_direct` in a fresh event loop via ``asyncio.run``.

        Args:
            method (str): HTTP verb.
            url (str): Target URL.
            timeout (float): Total timeout seconds.
            **kwargs (Any): Forwarded to aiohttp.

        Returns:
            BackendResponse: Parsed response.

        Example:
            >>> AiohttpBackend.request_direct.__name__
            'request_direct'
        """
        return asyncio.run(self.arequest_direct(method, url, timeout=timeout, **kwargs))

    async def arequest_direct(
        self, method: str, url: str, *, timeout: float = DEFAULT_BACKEND_TIMEOUT, **kwargs: Any
    ) -> BackendResponse:
        """Direct aiohttp request without proxying.

        Args:
            method (str): HTTP verb.
            url (str): Target URL.
            timeout (float): Total timeout seconds.
            **kwargs (Any): Forwarded to ``session.request``.

        Returns:
            BackendResponse: Parsed response.

        Example:
            >>> AiohttpBackend.arequest_direct.__name__
            'arequest_direct'
        """
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
