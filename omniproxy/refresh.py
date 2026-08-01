"""Proxy refresh helpers (callback / fetcher integration)."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from pydantic import ValidationError

from .extended_proxy import Proxy
from .proxy import Proxy as BaseProxy

if TYPE_CHECKING:
    from .config import RefreshConfig
    from .fetchers.base import ProxyFetcher

logger = logging.getLogger(__name__)


def _coerce_proxy(item: object) -> Proxy | None:
    """Coerce a refresh/fetcher item into the public :class:`Proxy` type.

    Args:
        item (object): Raw callback/fetcher item (string or proxy instance).

    Returns:
        Proxy | None: Validated public proxy, or ``None`` when coercion fails.
    """
    try:
        if isinstance(item, str):
            return Proxy.validate(item)
        if isinstance(item, Proxy):
            return item
        if isinstance(item, BaseProxy):
            return Proxy.validate(str(item))
    except (ValueError, ValidationError):
        return None
    return None


def _normalize_proxies(items: list) -> list[Proxy]:
    """Normalize a heterogenous list into validated :class:`Proxy` objects.

    Strings are parsed via :meth:`Proxy.validate`; existing :class:`Proxy`
    instances are kept as-is. Base :class:`~omniproxy.proxy.Proxy` instances
    are re-validated as the public subclass. Anything that fails validation
    is silently dropped.

    Args:
        items (list): Items returned by a refresh callback or fetcher.

    Returns:
        list[Proxy]: Validated proxies preserving the original order.

    Version:
        Added in 4.0.0.
    """
    proxies: list[Proxy] = []
    for item in items:
        proxy = _coerce_proxy(item)
        if proxy is not None:
            proxies.append(proxy)
    return proxies


async def _run_callback(config: RefreshConfig) -> list[Proxy]:
    """Run the configured refresh callbacks until one produces proxies.

    Tries the primary async callback, the primary sync callback, then the
    fallback async and sync callbacks (in that order). Each invocation is
    bounded by ``config.timeout``. Failures are logged and the next
    callback is attempted.

    Args:
        config (RefreshConfig): Refresh configuration with callbacks and timeout.

    Returns:
        list[Proxy]: The first non-empty validated proxy list, or ``[]`` if
        all callbacks failed.

    Version:
        Added in 4.0.0.
    """
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
    """Public wrapper for executing a :class:`RefreshConfig`.

    Args:
        config (RefreshConfig): Refresh configuration.

    Returns:
        list[Proxy]: Validated proxies from the first successful callback,
        or an empty list when every callback failed.

    Version:
        Added in 4.0.0.
    """
    return await _run_callback(config)


async def fetch_from_fetchers(
    fetchers: list[ProxyFetcher],
    *,
    timeout: float = 10.0,
) -> list[Proxy]:
    """Aggregate proxies from a list of fetchers with deduplication.

    Each fetcher is awaited in order, bounded by ``timeout`` so a single
    hung source cannot stall refresh indefinitely. Items that cannot be
    parsed are dropped; the remainder are deduplicated by canonical proxy
    URL.

    Args:
        fetchers (list[ProxyFetcher]): Ordered list of fetchers to query.
        timeout (float): Maximum seconds allowed per ``fetcher.fetch()``
            call. Defaults to ``10.0`` (same as :class:`RefreshConfig`).

    Returns:
        list[Proxy]: Unique validated proxies in first-seen order.

    Version:
        Added in 4.0.0.
    """
    seen: set[str] = set()
    collected: list[Proxy] = []
    for fetcher in fetchers:
        try:
            raw = await asyncio.wait_for(fetcher.fetch(), timeout=timeout)
        except Exception:
            logger.warning("Fetcher %r failed", fetcher, exc_info=True)
            continue
        for item in raw:
            proxy = _coerce_proxy(item)
            if proxy is None:
                continue
            if proxy.url not in seen:
                seen.add(proxy.url)
                collected.append(proxy)
    return collected


__all__: list[str] = ["fetch_from_fetchers", "fetch_from_refresh_config"]
