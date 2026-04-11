"""requests-based sync/async backend."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from ..constants import DEFAULT_BACKEND_TIMEOUT
from ..proxy import Proxy
from .base import BackendResponse, BaseBackend


class RequestsBackend(BaseBackend):
    """Blocking :class:`BaseBackend` built on :mod:`requests` with :meth:`asyncio.to_thread` async shims.

    ``proxies`` dicts are derived from :meth:`omniproxy.proxy.Proxy.as_requests_proxies`. Async
    methods simply offload the synchronous implementations to the default thread pool.

    Attributes
    ----------
    name: :class:`str`
        Constant ``requests``.
    """

    name = "requests"

    def get(
        self, url: str, proxy: Proxy, *, timeout: float = DEFAULT_BACKEND_TIMEOUT, **kwargs: Any
    ) -> BackendResponse:
        """Synchronous GET through *proxy* using the ``requests`` library.

        Args:
            url (str): Target URL.
            proxy (Proxy): Proxy whose URL is mapped to ``proxies=``.
            timeout (float): Per-request timeout seconds.
            **kwargs (Any): Forwarded to :func:`requests.get`.

        Returns:
            BackendResponse: Parsed response.

        Example:
            >>> RequestsBackend.get.__name__
            'get'
        """
        import requests  # type: ignore

        proxies = proxy.as_requests_proxies()
        r = requests.get(url, proxies=proxies, timeout=timeout, **kwargs)
        jd = None
        with contextlib.suppress(Exception):
            jd = r.json()
        return BackendResponse(
            status_code=r.status_code,
            headers=dict(r.headers.items()),
            json_data=jd,
            text=r.text,
        )

    async def aget(
        self, url: str, proxy: Proxy, *, timeout: float = DEFAULT_BACKEND_TIMEOUT, **kwargs: Any
    ) -> BackendResponse:
        """Offload :meth:`get` to a worker thread for asyncio compatibility.

        Args:
            url (str): Target URL.
            proxy (Proxy): Proxy to use.
            timeout (float): Timeout seconds.
            **kwargs (Any): Forwarded to :meth:`get`.

        Returns:
            BackendResponse: Parsed response.

        Example:
            >>> RequestsBackend.aget.__name__
            'aget'
        """
        return await asyncio.to_thread(self.get, url, proxy, timeout=timeout, **kwargs)

    def request_direct(
        self, method: str, url: str, *, timeout: float = DEFAULT_BACKEND_TIMEOUT, **kwargs: Any
    ) -> BackendResponse:
        """Direct ``requests.request`` without a proxy.

        Args:
            method (str): HTTP verb.
            url (str): Target URL.
            timeout (float): Timeout seconds.
            **kwargs (Any): Forwarded to :func:`requests.request`.

        Returns:
            BackendResponse: Parsed response.

        Example:
            >>> RequestsBackend.request_direct.__name__
            'request_direct'
        """
        import requests  # type: ignore

        r = requests.request(method.upper(), url, timeout=timeout, **kwargs)
        jd = None
        with contextlib.suppress(Exception):
            jd = r.json()
        return BackendResponse(
            status_code=r.status_code,
            headers=dict(r.headers.items()),
            json_data=jd,
            text=r.text,
        )

    async def arequest_direct(
        self, method: str, url: str, *, timeout: float = DEFAULT_BACKEND_TIMEOUT, **kwargs: Any
    ) -> BackendResponse:
        """Thread-pooled async wrapper around :meth:`request_direct`.

        Args:
            method (str): HTTP verb.
            url (str): Target URL.
            timeout (float): Timeout seconds.
            **kwargs (Any): Forwarded to :meth:`request_direct`.

        Returns:
            BackendResponse: Parsed response.

        Example:
            >>> RequestsBackend.arequest_direct.__name__
            'arequest_direct'
        """
        return await asyncio.to_thread(
            lambda: self.request_direct(method, url, timeout=timeout, **kwargs)
        )
