"""Protocol for pluggable proxy sources used by refresh / on-demand fetch."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..proxy import Proxy


@runtime_checkable
class ProxyFetcher(Protocol):
    """Pluggable async source of proxies.

    Concrete implementations live alongside this module
    (:class:`FileFetcher`, :class:`URLFetcher`, :class:`ScrapeFetcher`).
    They are consumed by :class:`AsyncProxyPool` via the refresh loop and
    by :func:`fetch_from_fetchers`.

    Version:
        Added in 4.0.0.
    """

    async def fetch(self) -> list[Proxy | str]:
        """Fetch the current list of proxies from this source.

        Returns:
            list[Proxy | str]: Either parsed :class:`Proxy` objects or
            raw strings that downstream code will normalise.

        Version:
            Added in 4.0.0.
        """
        ...
