"""tls_client backend."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from ..constants import DEFAULT_BACKEND_TIMEOUT
from ..proxy import Proxy
from .base import BackendResponse, BaseBackend


class TlsClientBackend(BaseBackend):
    """:class:`BaseBackend` that shells out to :mod:`tls_client` sessions with JA3-like behaviour.

    HTTP proxies reuse :meth:`~omniproxy.proxy.Proxy.as_requests_proxies`; SOCKS URLs are passed
    through the same mapping style curl_cffi expects. Only a subset of HTTP verbs is supported for
    :meth:`request_direct`.

    Attributes
    ----------
    name: :class:`str`
        Constant ``tls_client``.
    """

    name = "tls_client"

    def get(
        self, url: str, proxy: Proxy, *, timeout: float = DEFAULT_BACKEND_TIMEOUT, **kwargs: Any
    ) -> BackendResponse:
        """Synchronous GET through *proxy* using a :class:`tls_client.Session`.

        Args:
            url (str): Target URL.
            proxy (Proxy): Proxy configuration.
            timeout (float): Mapped to ``timeout_seconds`` as int for tls_client.
            **kwargs (Any): Session and request kwargs (``client_identifier``, etc.).

        Returns:
            BackendResponse: Parsed response.

        Raises:
            ImportError: If tls_client is not installed.

        Example:
            >>> TlsClientBackend.get.__name__
            'get'
        """
        try:
            import tls_client
        except ImportError as e:
            raise ImportError("Install with 'uv add omniproxy --extra tls_client'") from e

        session = tls_client.Session(
            client_identifier=kwargs.pop("client_identifier", "chrome_120"),
            random_tls_extension_order=kwargs.pop("random_tls_extension_order", True),
        )
        proxies = (
            proxy.as_requests_proxies()
            if "http" in proxy.protocol
            else {"http": proxy.url, "https": proxy.url}
        )
        r = session.get(
            url,
            proxies=proxies,
            timeout_seconds=int(timeout),
            **kwargs,
        )
        jd = None
        with contextlib.suppress(Exception):
            jd = r.json()
        hdrs = r.headers if isinstance(r.headers, dict) else dict(r.headers)
        return BackendResponse(
            status_code=r.status_code,
            headers=hdrs,
            json_data=jd,
            text=r.text if hasattr(r, "text") else str(r.content or ""),
        )

    async def aget(
        self, url: str, proxy: Proxy, *, timeout: float = DEFAULT_BACKEND_TIMEOUT, **kwargs: Any
    ) -> BackendResponse:
        """Thread-pooled async wrapper around :meth:`get`.

        Args:
            url (str): Target URL.
            proxy (Proxy): Proxy to use.
            timeout (float): Timeout seconds.
            **kwargs (Any): Forwarded to :meth:`get`.

        Returns:
            BackendResponse: Parsed response.

        Example:
            >>> TlsClientBackend.aget.__name__
            'aget'
        """
        return await asyncio.to_thread(self.get, url, proxy, timeout=timeout, **kwargs)

    def request_direct(
        self, method: str, url: str, *, timeout: float = DEFAULT_BACKEND_TIMEOUT, **kwargs: Any
    ) -> BackendResponse:
        """Direct tls_client call without proxying (method dispatch map).

        Args:
            method (str): Supported HTTP verb for tls_client.
            url (str): Target URL.
            timeout (float): ``timeout_seconds`` int for tls_client.
            **kwargs (Any): Forwarded to the tls_client verb method.

        Returns:
            BackendResponse: Parsed response.

        Raises:
            ImportError: If tls_client is missing.
            ValueError: If *method* is unsupported.

        Example:
            >>> TlsClientBackend.request_direct.__name__
            'request_direct'
        """
        try:
            import tls_client
        except ImportError as e:
            raise ImportError("Install with 'uv add omniproxy --extra tls_client'") from e

        session = tls_client.Session(
            client_identifier=kwargs.pop("client_identifier", "chrome_120"),
            random_tls_extension_order=kwargs.pop("random_tls_extension_order", True),
        )

        METHOD_MAP = {
            "GET": session.get,
            "POST": session.post,
            "PUT": session.put,
            "PATCH": session.patch,
            "DELETE": session.delete,
            "HEAD": session.head,
            "OPTIONS": session.options,
        }

        m = method.upper()
        caller = METHOD_MAP.get(m)
        if caller is None:
            raise ValueError(
                f"tls_client backend does not support method {method!r}. "
                f"Supported: {', '.join(METHOD_MAP)}"
            )

        r = caller(url, proxies=None, timeout_seconds=int(timeout), **kwargs)
        jd = None
        with contextlib.suppress(Exception):
            jd = r.json()
        hdrs = r.headers if isinstance(r.headers, dict) else dict(r.headers)
        return BackendResponse(
            status_code=r.status_code,
            headers=hdrs,
            json_data=jd,
            text=r.text if hasattr(r, "text") else str(r.content or ""),
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
            >>> TlsClientBackend.arequest_direct.__name__
            'arequest_direct'
        """
        return await asyncio.to_thread(
            lambda: self.request_direct(method, url, timeout=timeout, **kwargs)
        )
