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
    """Thin wrapper around :class:`httpx.Client` that understands omniproxy :class:`~omniproxy.proxy.Proxy`.

    HTTP and HTTPS schemes set ``httpx``'s ``proxy`` keyword to the canonical URL string. SOCKS
    schemes install ``httpx_socks`` :class:`SyncProxyTransport` as the client's ``transport``.

    .. note::

        Requires the ``httpx`` extra; SOCKS additionally needs ``httpx_socks`` (pulled in by the
        package extras graph for typical installs).

    Parameters
    ----------
    proxy
        Optional ``str`` or :class:`~omniproxy.proxy.Proxy`. When set, normalised to
        :class:`~omniproxy.proxy.Proxy` and mapped to ``proxy`` or ``transport`` for the underlying
        :class:`httpx.Client`.
    follow_redirects: :class:`bool`
        Forwarded verbatim; defaults to ``True`` matching httpx's common default.
    """

    def __init__(self, *args, proxy: Proxy | str | None = None, follow_redirects=True, **kwargs):
        """Create a client, translating *proxy* into ``proxy=`` or SOCKS transport kwargs.

        Args:
            *args: Forwarded to :class:`httpx.Client`.
            proxy (Proxy | str | None): Optional omniproxy proxy configuration.
            follow_redirects (bool): httpx redirect behaviour.
            **kwargs: Additional :class:`httpx.Client` keyword arguments.

        Returns:
            None

        Raises:
            ValueError: If the proxy protocol is unsupported.

        Example:
            >>> Client.__init__.__name__
            '__init__'
        """
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
    """Async counterpart to :class:`Client` using :class:`httpx.AsyncClient` + :class:`AsyncProxyTransport`.

    SOCKS proxies use the async transport from ``httpx_socks``; HTTP(S) proxies use the standard
    ``proxy`` keyword accepted by httpx.

    Parameters
    ----------
    proxy
        Same semantics as :class:`Client`.
    follow_redirects: :class:`bool`
        Same semantics as :class:`Client`.
    """

    def __init__(self, *args, proxy: Proxy | str | None = None, follow_redirects=True, **kwargs):
        """Create an async client with optional omniproxy *proxy* routing.

        Args:
            *args: Forwarded to :class:`httpx.AsyncClient`.
            proxy (Proxy | str | None): Optional proxy.
            follow_redirects (bool): Redirect policy for httpx.
            **kwargs: Extra httpx client kwargs.

        Returns:
            None

        Raises:
            ValueError: If the proxy protocol is unsupported.

        Example:
            >>> AsyncClient.__init__.__name__
            '__init__'
        """
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
    """Concrete :class:`BaseBackend` that spins up short-lived :class:`Client` / :class:`AsyncClient` instances.

    Each ``get`` / ``aget`` call constructs a disposable client so timeouts and TLS settings can
    vary per invocation without sharing state across threads or tasks.

    Attributes
    ----------
    name: :class:`str`
        Constant ``httpx`` for logging and :func:`~omniproxy.backends.factory.get_backend` dispatch.
    """

    name = "httpx"

    def get(
        self, url: str, proxy: Proxy, *, timeout: float = DEFAULT_BACKEND_TIMEOUT, **kwargs: Any
    ) -> BackendResponse:
        """Synchronous GET via a short-lived :class:`Client`.

        Args:
            url (str): Target URL.
            proxy (Proxy): Proxy to use.
            timeout (float): Client timeout seconds.
            **kwargs (Any): Extra :class:`Client` constructor args.

        Returns:
            BackendResponse: Parsed response.

        Example:
            >>> HttpxBackend.get.__name__
            'get'
        """
        with Client(proxy=proxy, timeout=timeout, **kwargs) as client:
            r = client.get(url)
            return self._from_httpx_response(r)

    async def aget(
        self, url: str, proxy: Proxy, *, timeout: float = DEFAULT_BACKEND_TIMEOUT, **kwargs: Any
    ) -> BackendResponse:
        """Async GET via :class:`AsyncClient`.

        Args:
            url (str): Target URL.
            proxy (Proxy): Proxy to use.
            timeout (float): Client timeout seconds.
            **kwargs (Any): Extra async client kwargs.

        Returns:
            BackendResponse: Parsed response.

        Example:
            >>> HttpxBackend.aget.__name__
            'aget'
        """
        async with AsyncClient(proxy=proxy, timeout=timeout, **kwargs) as client:
            r = await client.get(url)
            return self._from_httpx_response(r)

    def request_direct(
        self, method: str, url: str, *, timeout: float = DEFAULT_BACKEND_TIMEOUT, **kwargs: Any
    ) -> BackendResponse:
        """Perform a direct (non-proxied) HTTP request synchronously.

        Args:
            method (str): HTTP verb.
            url (str): Target URL.
            timeout (float): Client timeout seconds.
            **kwargs (Any): Forwarded to :meth:`httpx.Client.request`.

        Returns:
            BackendResponse: Parsed response.

        Example:
            >>> HttpxBackend.request_direct.__name__
            'request_direct'
        """
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            r = client.request(method.upper(), url, **kwargs)
            return self._from_httpx_response(r)

    async def arequest_direct(
        self, method: str, url: str, *, timeout: float = DEFAULT_BACKEND_TIMEOUT, **kwargs: Any
    ) -> BackendResponse:
        """Async direct HTTP request without a proxy.

        Args:
            method (str): HTTP verb.
            url (str): Target URL.
            timeout (float): Client timeout seconds.
            **kwargs (Any): Forwarded to :meth:`httpx.AsyncClient.request`.

        Returns:
            BackendResponse: Parsed response.

        Example:
            >>> HttpxBackend.arequest_direct.__name__
            'arequest_direct'
        """
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            r = await client.request(method.upper(), url, **kwargs)
            return self._from_httpx_response(r)

    @staticmethod
    def _from_httpx_response(r: httpx.Response) -> BackendResponse:
        """Convert an :class:`httpx.Response` into :class:`BackendResponse`.

        Args:
            r (httpx.Response): Raw httpx response.

        Returns:
            BackendResponse: Normalised structure with best-effort JSON parsing.

        Example:
            >>> HttpxBackend._from_httpx_response.__name__
            '_from_httpx_response'
        """
        jd = None
        with contextlib.suppress(Exception):
            jd = r.json()
        return BackendResponse(
            status_code=r.status_code,
            headers=dict(r.headers),
            json_data=jd,
            text=r.text,
        )
