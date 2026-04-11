"""Resolve an installed HTTP backend by name."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..constants import SUPPORTED_BACKENDS
from .base import BaseBackend

if TYPE_CHECKING:
    pass


def supported_backends() -> tuple[str, ...]:
    """Return stable backend names accepted by :func:`get_backend`.

    Returns:
        tuple[str, ...]: Ordered names matching :data:`~omniproxy.constants.SUPPORTED_BACKENDS`.

    Example:
        >>> "httpx" in supported_backends()
        True
    """
    return SUPPORTED_BACKENDS


def get_backend(name: str | None = None) -> BaseBackend:
    """Instantiate the configured HTTP backend implementation.

    Args:
        name (str | None): Backend key; ``None`` uses ``settings.default_backend``.

    Returns:
        BaseBackend: Concrete backend instance.

    Raises:
        ImportError: If the optional dependency for that backend is missing.
        ValueError: If *name* is unknown.

    Example:
        >>> get_backend.__name__
        'get_backend'
    """
    from ..config import settings

    key = (name or settings.default_backend).lower().replace("-", "_")
    if key in ("curlcffi", "curl-cffi"):
        key = "curl_cffi"
    if key == "tlsclient":
        key = "tls_client"

    if key == "httpx":
        try:
            from .httpx_client import HttpxBackend

            return HttpxBackend()
        except ImportError as e:
            raise ImportError("Install with 'uv add omniproxy --extra httpx'") from e

    if key == "aiohttp":
        try:
            from .aiohttp_client import AiohttpBackend

            return AiohttpBackend()
        except ImportError as e:
            raise ImportError("Install with 'uv add omniproxy --extra aiohttp'") from e

    if key == "requests":
        try:
            from .requests_client import RequestsBackend

            return RequestsBackend()
        except ImportError as e:
            raise ImportError("Install with 'uv add omniproxy --extra requests'") from e

    if key == "curl_cffi":
        try:
            from .curl_client import CurlBackend

            return CurlBackend()
        except ImportError as e:
            raise ImportError("Install with 'uv add omniproxy --extra curl_cffi'") from e

    if key == "tls_client":
        try:
            from .tls_client import TlsClientBackend

            return TlsClientBackend()
        except ImportError as e:
            raise ImportError("Install with 'uv add omniproxy --extra tls_client'") from e

    raise ValueError(
        f"Unknown backend {name!r}; choose {', '.join(SUPPORTED_BACKENDS)}"
    )
