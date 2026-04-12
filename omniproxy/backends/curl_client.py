"""curl_cffi backend for TLS fingerprinting / stealth checks."""

from __future__ import annotations

import contextlib
from typing import Any

from ..constants import DEFAULT_BACKEND_TIMEOUT
from ..proxy import Proxy
from .base import BackendResponse, BaseBackend


def _import_curl_cffi() -> Any:
    try:
        import curl_cffi
    except ImportError as e:
        raise ImportError("Install with 'uv add omniproxy --extra curl_cffi'") from e
    return curl_cffi


def _timeout_arg(timeout: float) -> float | None:
    """Map a backend timeout to curl_cffi; ``0`` or negative means no limit (``None``)."""
    if timeout is None or timeout <= 0:
        return None
    return float(timeout)


def _response_from_curl(r: Any) -> BackendResponse:
    jd = None
    with contextlib.suppress(Exception):
        jd = r.json()
    return BackendResponse(
        status_code=r.status_code,
        headers=dict(r.headers) if hasattr(r.headers, "items") else {},
        json_data=jd,
        text=getattr(r, "text", "") or "",
    )


class CurlBackend(BaseBackend):
    """TLS-impersonating :class:`BaseBackend` using ``curl_cffi``'s requests-style API.

    Uses the top-level helpers documented upstream (``curl_cffi.get``, ``curl_cffi.request``) and
    :class:`curl_cffi.AsyncSession` for async calls. Supports HTTP/HTTPS and SOCKS URLs understood
    by curl_cffi. Browser impersonation defaults to ``impersonate='chrome'`` unless overridden in
    ``**kwargs`` (see upstream `impersonate` docs).

    Attributes
    ----------
    name: :class:`str`
        Constant ``curl_cffi``.
    """

    name = "curl_cffi"

    def get(
        self, url: str, proxy: Proxy, *, timeout: float = DEFAULT_BACKEND_TIMEOUT, **kwargs: Any
    ) -> BackendResponse:
        """Synchronous GET through *proxy* using ``curl_cffi.get``.

        Args:
            url (str): Target URL.
            proxy (Proxy): HTTP or SOCKS proxy URL.
            timeout (float): Per-request timeout in seconds (sub-second values allowed).
            **kwargs (Any): May include ``impersonate`` (default ``"chrome"``), ``http_version``, etc.

        Returns:
            BackendResponse: Parsed response.

        Raises:
            ImportError: If curl_cffi is not installed.
            ValueError: If the proxy protocol is unsupported.

        Example:
            >>> CurlBackend.get.__name__
            'get'
        """
        curl = _import_curl_cffi()

        if "http" in proxy.protocol or "socks" in proxy.protocol:
            proxy_kw: dict[str, Any] = {"proxy": proxy.url}
        else:
            raise ValueError(
                f'Unsupported proxy protocol "{proxy.protocol}" for curl_cffi backend.'
            )
        impersonate = kwargs.pop("impersonate", "chrome")

        r = curl.get(
            url,
            **proxy_kw,
            timeout=_timeout_arg(timeout),
            impersonate=impersonate,
            **kwargs,
        )
        return _response_from_curl(r)

    async def aget(
        self, url: str, proxy: Proxy, *, timeout: float = DEFAULT_BACKEND_TIMEOUT, **kwargs: Any
    ) -> BackendResponse:
        """Async GET through *proxy* using :class:`curl_cffi.AsyncSession` (native asyncio)."""
        curl = _import_curl_cffi()

        if "http" in proxy.protocol or "socks" in proxy.protocol:
            proxy_kw = {"proxy": proxy.url}
        else:
            raise ValueError(
                f'Unsupported proxy protocol "{proxy.protocol}" for curl_cffi backend.'
            )
        impersonate = kwargs.pop("impersonate", "chrome")

        async with curl.AsyncSession() as session:
            r = await session.get(
                url,
                **proxy_kw,
                timeout=_timeout_arg(timeout),
                impersonate=impersonate,
                **kwargs,
            )
        return _response_from_curl(r)

    def request_direct(
        self, method: str, url: str, *, timeout: float = DEFAULT_BACKEND_TIMEOUT, **kwargs: Any
    ) -> BackendResponse:
        curl = _import_curl_cffi()

        impersonate = kwargs.pop("impersonate", "chrome")
        r = curl.request(
            method.upper(),
            url,
            timeout=_timeout_arg(timeout),
            impersonate=impersonate,
            **kwargs,
        )
        return _response_from_curl(r)

    async def arequest_direct(
        self, method: str, url: str, *, timeout: float = DEFAULT_BACKEND_TIMEOUT, **kwargs: Any
    ) -> BackendResponse:
        """Async ``request_direct`` using :class:`curl_cffi.AsyncSession` (native asyncio).

        Args:
            method (str): HTTP verb.
            url (str): Target URL.
            timeout (float): Timeout seconds.
            **kwargs (Any): Forwarded to the session request.

        Returns:
            BackendResponse: Parsed response.

        Example:
            >>> CurlBackend.arequest_direct.__name__
            'arequest_direct'
        """
        curl = _import_curl_cffi()

        impersonate = kwargs.pop("impersonate", "chrome")
        async with curl.AsyncSession() as session:
            r = await session.request(
                method.upper(),
                url,
                timeout=_timeout_arg(timeout),
                impersonate=impersonate,
                **kwargs,
            )
        return _response_from_curl(r)
