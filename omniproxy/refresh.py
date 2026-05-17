"""Proxy refresh helpers (callback / fetcher integration)."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from .proxy import Proxy

if TYPE_CHECKING:
    from .config import RefreshConfig
    from .fetchers.base import ProxyFetcher


async def fetch_from_refresh_config(config: RefreshConfig) -> list[Proxy]:
    """Execute configured callbacks, trying async first."""
    proxies: list[Proxy] = []
    if config.async_callback:
        result = await config.async_callback()
        proxies.extend(Proxy.validate(p) if isinstance(p, str) else p for p in result)
    elif config.sync_callback:
        # Run synchronous callback in thread
        result = await asyncio.to_thread(config.sync_callback)
        proxies.extend(Proxy.validate(p) if isinstance(p, str) else p for p in result)
    return proxies


async def fetch_from_fetchers(fetchers: list[ProxyFetcher]) -> list[Proxy]:
    """Iterate over fetcher objects, deduplicating results."""
    seen: set[str] = set()
    collected: list[Proxy] = []
    for fetcher in fetchers:
        try:
            raw = await fetcher.fetch()
        except Exception:
            continue
        for item in raw:
            if isinstance(item, str):
                try:
                    proxy = Proxy(item)
                except ValueError:
                    continue
            else:
                proxy = item
            if proxy.url not in seen:
                seen.add(proxy.url)
                collected.append(proxy)
    return collected


__all__: list[str] = ["fetch_from_fetchers", "fetch_from_refresh_config"]