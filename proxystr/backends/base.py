"""Abstract backend interface for HTTP checks."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Mapping

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
        timeout: float = 10.0,
        **kwargs: Any,
    ) -> BackendResponse: ...

    @abstractmethod
    async def aget(
        self,
        url: str,
        proxy: Proxy,
        *,
        timeout: float = 10.0,
        **kwargs: Any,
    ) -> BackendResponse: ...
