"""Resolve an installed HTTP backend by name."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .base import BaseBackend

if TYPE_CHECKING:
    pass


def get_backend(name: str | None = None) -> BaseBackend:
    from .. import config

    key = (name or config.default_backend).lower().replace("-", "_")
    if key in ("curlcffi", "curl-cffi"):
        key = "curl_cffi"

    if key == "httpx":
        try:
            from .httpx_client import HttpxBackend

            return HttpxBackend()
        except ImportError as e:
            raise ImportError("Install with 'uv add proxystr --extra httpx'") from e

    if key == "aiohttp":
        try:
            from .aiohttp_client import AiohttpBackend

            return AiohttpBackend()
        except ImportError as e:
            raise ImportError("Install with 'uv add proxystr --extra aiohttp'") from e

    if key == "requests":
        try:
            from .requests_client import RequestsBackend

            return RequestsBackend()
        except ImportError as e:
            raise ImportError("Install with 'uv add proxystr --extra requests'") from e

    if key == "curl_cffi":
        try:
            from .curl_client import CurlBackend

            return CurlBackend()
        except ImportError as e:
            raise ImportError("Install with 'uv add proxystr --extra curl_cffi'") from e

    if key == "tls_client":
        try:
            from .tls_client import TlsClientBackend

            return TlsClientBackend()
        except ImportError as e:
            raise ImportError("Install with 'uv add proxystr --extra tls_client'") from e

    raise ValueError(f"Unknown backend {name!r}; choose httpx, aiohttp, requests, curl_cffi, tls_client")
