"""HTTP backend adapters used by checks and the rotation API.

Each backend implements :class:`BaseBackend` for a different underlying
library (``httpx``, ``aiohttp``, ``requests``, ``curl_cffi``, ``tls_client``)
and is resolved by name via :func:`get_backend`.

Version:
    4.0.0
"""

from __future__ import annotations

from .base import BackendResponse, BaseBackend
from .factory import get_backend, supported_backends

__all__ = ["BackendResponse", "BaseBackend", "get_backend", "supported_backends"]
