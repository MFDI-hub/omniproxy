"""Protocol for pluggable proxy sources used by refresh / on-demand fetch."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..proxy import Proxy


@runtime_checkable
class ProxyFetcher(Protocol):
    """Async source of proxies; may return strings or validated :class:`~omniproxy.proxy.Proxy` objects."""

    async def fetch(self) -> list[Proxy | str]: ...
