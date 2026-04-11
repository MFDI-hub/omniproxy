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
    """Normalized response from any backend."""

    status_code: int
    headers: Mapping[str, str] = field(default_factory=dict)
    json_data: Any = None
    text: str = ""


class BaseBackend(ABC):
    """Sync and async GET for proxy checks."""

    name: str = "base"

    @abstractmethod
    def get(
        self,
        url: str,
        proxy: Proxy,
        *,
        timeout: float = DEFAULT_BACKEND_TIMEOUT,
        **kwargs: Any,
    ) -> BackendResponse: ...

    @abstractmethod
    async def aget(
        self,
        url: str,
        proxy: Proxy,
        *,
        timeout: float = DEFAULT_BACKEND_TIMEOUT,
        **kwargs: Any,
    ) -> BackendResponse: ...

    @abstractmethod
    def request_direct(
        self,
        method: str,
        url: str,
        *,
        timeout: float = DEFAULT_BACKEND_TIMEOUT,
        **kwargs: Any,
    ) -> BackendResponse:
        """HTTP request without a proxy (e.g. mobile rotation URL)."""

    @abstractmethod
    async def arequest_direct(
        self,
        method: str,
        url: str,
        *,
        timeout: float = DEFAULT_BACKEND_TIMEOUT,
        **kwargs: Any,
    ) -> BackendResponse:
        """Async HTTP request without a proxy."""
