"""Abstract backend interface for HTTP checks."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..constants import DEFAULT_BACKEND_TIMEOUT

if TYPE_CHECKING:
    from ..proxy import Proxy


@dataclass
class BackendResponse:
    """Normalised container returned by every :class:`BaseBackend` implementation.

    JSON is parsed best-effort; failures leave :attr:`json_data` as ``None`` while still exposing
    :attr:`status_code`, :attr:`headers`, and :attr:`text`.

    Attributes
    ----------
    status_code: :class:`int`
        Numeric HTTP status from the upstream response.
    headers: :class:`collections.abc.Mapping` [:class:`str`, :class:`str`]
        Lowercasing is **not** enforced; keys follow the underlying client library.
    json_data: :class:`Any`
        Decoded JSON object (usually :class:`dict` or :class:`list`) when parsing succeeded.
    text: :class:`str`
        Raw response body as text; may be empty for non-text payloads.
    """

    status_code: int
    headers: Mapping[str, str] = field(default_factory=dict)
    json_data: Any = None
    text: str = ""


class BaseBackend(ABC):
    """Abstract interface implemented by httpx, aiohttp, requests, curl_cffi, and tls_client adapters.

    Concrete classes are constructed by :func:`~omniproxy.backends.factory.get_backend`. All
    methods must accept a :class:`~omniproxy.proxy.Proxy` for proxied calls and return
    :class:`BackendResponse` for uniform handling in :mod:`omniproxy.extended_proxy`.

    Attributes
    ----------
    name: :class:`str`
        Short identifier (``httpx``, ``aiohttp``, ``requests``, ``curl_cffi``, ``tls_client``).
    """

    name: str = "base"

    @abstractmethod
    def get(
        self,
        url: str,
        proxy: Proxy,
        *,
        timeout: float = DEFAULT_BACKEND_TIMEOUT,
        **kwargs: Any,
    ) -> BackendResponse:
        """Perform a synchronous GET through *proxy*.

        Args:
            url (str): Target URL.
            proxy (Proxy): Proxy to route through.
            timeout (float): Per-request timeout in seconds.
            **kwargs (Any): Backend-specific options.

        Returns:
            BackendResponse: Normalised response object.

        Example:
            >>> BaseBackend.get.__name__
            'get'
        """
        ...

    @abstractmethod
    async def aget(
        self,
        url: str,
        proxy: Proxy,
        *,
        timeout: float = DEFAULT_BACKEND_TIMEOUT,
        **kwargs: Any,
    ) -> BackendResponse:
        """Async GET through *proxy* (mirror of :meth:`get`).

        Args:
            url (str): Target URL.
            proxy (Proxy): Proxy to route through.
            timeout (float): Per-request timeout in seconds.
            **kwargs (Any): Backend-specific options.

        Returns:
            BackendResponse: Normalised response object.

        Example:
            >>> BaseBackend.aget.__name__
            'aget'
        """
        ...

    @abstractmethod
    def request_direct(
        self,
        method: str,
        url: str,
        *,
        timeout: float = DEFAULT_BACKEND_TIMEOUT,
        **kwargs: Any,
    ) -> BackendResponse:
        """HTTP request without a proxy (e.g. mobile rotation URL).

        Args:
            method (str): HTTP verb such as ``"GET"`` or ``"POST"``.
            url (str): Absolute URL hit directly.
            timeout (float): Per-request timeout in seconds.
            **kwargs (Any): Backend-specific options.

        Returns:
            BackendResponse: Normalised response object.

        Example:
            >>> BaseBackend.request_direct.__name__
            'request_direct'
        """
        ...

    @abstractmethod
    async def arequest_direct(
        self,
        method: str,
        url: str,
        *,
        timeout: float = DEFAULT_BACKEND_TIMEOUT,
        **kwargs: Any,
    ) -> BackendResponse:
        """Async HTTP request without a proxy.

        Args:
            method (str): HTTP verb.
            url (str): Absolute URL.
            timeout (float): Per-request timeout in seconds.
            **kwargs (Any): Backend-specific options.

        Returns:
            BackendResponse: Normalised response object.

        Example:
            >>> BaseBackend.arequest_direct.__name__
            'arequest_direct'
        """
        ...
