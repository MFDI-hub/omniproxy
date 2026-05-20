"""Proxy refresh helpers (callback / fetcher integration)."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from pydantic import ValidationError

from .proxy import Proxy

if TYPE_CHECKING:
    from .config import RefreshConfig
    from .fetchers.base import ProxyFetcher

logger = logging.getLogger(__name__)


def _normalize_proxies(items: list) -> list[Proxy]:
    proxies: list[Proxy] = []
    for item in items:
        try:
            proxy = Proxy.validate(item) if isinstance(item, str) else item
        except (ValueError, ValidationError):
            continue
        if not isinstance(proxy, Proxy):
            continue
        proxies.append(proxy)
    return proxies


async def _run_callback(config: RefreshConfig) -> list[Proxy]:
    """Try primary then fallback callbacks with timeout."""
    callbacks: list = []
    if config.async_callback:
        callbacks.append(("async", config.async_callback))
    if config.sync_callback:
        callbacks.append(("sync", config.sync_callback))
    for cb in config.fallback_async_callbacks:
        callbacks.append(("async", cb))
    for cb in config.fallback_sync_callbacks:
        callbacks.append(("sync", cb))

    for kind, cb in callbacks:
        try:
            if kind == "async":
                coro = cb()
                result = await asyncio.wait_for(coro, timeout=config.timeout)
            else:
                result = await asyncio.wait_for(
                    asyncio.to_thread(cb),
                    timeout=config.timeout,
                )
            proxies = _normalize_proxies(result)
            if proxies:
                return proxies
        except Exception:
            logger.warning("Refresh callback failed", exc_info=True)
            continue
    return []


async def fetch_from_refresh_config(config: RefreshConfig) -> list[Proxy]:
    """Execute configured callbacks, trying async first then fallbacks."""
    return await _run_callback(config)


async def fetch_from_fetchers(fetchers: list[ProxyFetcher]) -> list[Proxy]:
    """Iterate over fetcher objects, deduplicating results."""
    seen: set[str] = set()
    collected: list[Proxy] = []
    for fetcher in fetchers:
        try:
            raw = await fetcher.fetch()
        except Exception:
            logger.warning("Fetcher %r failed", fetcher, exc_info=True)
            continue
        for item in raw:
            try:
                proxy = Proxy.validate(item) if isinstance(item, str) else item
            except (ValueError, ValidationError):
                continue
            if not isinstance(proxy, Proxy):
                continue
            if proxy.url not in seen:
                seen.add(proxy.url)
                collected.append(proxy)
    return collected


__all__: list[str] = ["fetch_from_fetchers", "fetch_from_refresh_config"]
