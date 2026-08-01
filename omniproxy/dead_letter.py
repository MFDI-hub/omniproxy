"""Dead‑letter queue and retry logic."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .enum import DeadLetterPersistence

if TYPE_CHECKING:
    from .extended_proxy import Proxy
    from .config import DeadLetterConfig, StateStore
    from .pool import AsyncProxyPool   # avoid circular import at runtime

logger = logging.getLogger(__name__)

DEAD_LETTER_STORE_KEY = "omniproxy:dead_letter"


@dataclass(slots=True)
class DeadLetterEntry:
    """One entry in the dead-letter queue.

    Attributes:
        proxy (Proxy): Proxy that was permanently failed.
        error (str | None): Optional human-readable error description.
        timestamp (float): Monotonic timestamp of insertion.

    Version:
        Added in 4.0.0.
    """

    proxy: Proxy
    error: str | None
    timestamp: float


def maybe_add(
    entry: DeadLetterEntry,
    config: DeadLetterConfig,
    queue: list[DeadLetterEntry],
) -> None:
    """Append ``entry`` to ``queue`` while honouring the configured size cap.

    When ``config.max_size`` is set and the queue is full, the oldest entry
    is dropped before insertion (FIFO eviction).

    Callers must hold the pool's ``_state_lock`` (the same lock used by
    :func:`retry_cycle` for snapshots and removals).

    Args:
        entry (DeadLetterEntry): The entry to append.
        config (DeadLetterConfig): Dead-letter configuration providing the size cap.
        queue (list[DeadLetterEntry]): Mutable list of entries to append to.

    Returns:
        None

    Version:
        Added in 4.0.0.
    """
    if config.max_size is not None and len(queue) >= config.max_size:
        dropped = queue.pop(0)
        logger.debug(
            "Dead-letter queue full; dropping oldest entry %s",
            getattr(dropped.proxy, "url", dropped.proxy),
        )
    queue.append(entry)
    logger.debug(
        "Dead-letter enqueued %s (error=%s, size=%d)",
        getattr(entry.proxy, "url", entry.proxy),
        entry.error,
        len(queue),
    )


def persist_queue(
    queue: list[DeadLetterEntry],
    config: DeadLetterConfig,
    store: StateStore | None,
) -> None:
    """Write ``queue`` to ``store`` when persistence is ``STATE_STORE``.

    Args:
        queue (list[DeadLetterEntry]): Current in-memory queue.
        config (DeadLetterConfig): Dead-letter configuration.
        store (StateStore | None): Optional backing store.

    Returns:
        None

    Version:
        Added in 4.0.0.
    """
    if store is None or config.persistence != DeadLetterPersistence.STATE_STORE:
        return
    payload = json.dumps(
        [
            {
                "proxy": getattr(entry.proxy, "url", str(entry.proxy)),
                "error": entry.error,
                "timestamp": entry.timestamp,
            }
            for entry in queue
        ]
    )
    try:
        store.set(DEAD_LETTER_STORE_KEY, payload)
    except Exception:
        logger.exception("Failed to persist dead-letter queue")


def load_queue(store: StateStore | None) -> list[DeadLetterEntry]:
    """Restore dead-letter entries from ``store``, or return an empty list.

    Args:
        store (StateStore | None): Optional backing store.

    Returns:
        list[DeadLetterEntry]: Restored entries (empty on miss or error).

    Version:
        Added in 4.0.0.
    """
    if store is None:
        return []
    try:
        raw = store.get(DEAD_LETTER_STORE_KEY)
    except Exception:
        logger.exception("Failed to load dead-letter queue")
        return []
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Corrupt dead-letter store payload; starting empty")
        return []
    if not isinstance(data, list):
        logger.warning("Unexpected dead-letter store shape; starting empty")
        return []

    from .extended_proxy import Proxy

    entries: list[DeadLetterEntry] = []
    for item in data:
        if not isinstance(item, dict) or "proxy" not in item:
            continue
        entries.append(
            DeadLetterEntry(
                proxy=Proxy(item["proxy"]),
                error=item.get("error"),
                timestamp=float(item.get("timestamp") or 0.0),
            )
        )
    return entries


async def retry_cycle(
    pool: AsyncProxyPool,
    queue: list[DeadLetterEntry],
    health_check_fn,
    state_lock: asyncio.Lock,
    config: DeadLetterConfig,
) -> None:
    """Background coroutine that periodically retries dead-letter entries.

    Runs until ``pool._closed`` becomes truthy. On each iteration the
    coroutine sleeps for ``config.retry_interval_seconds`` (defaulting to
    ``60``) **before** the first scan, snapshots the queue under
    ``state_lock``, and re-checks each proxy via ``health_check_fn``
    bounded by ``pool._health_sem``. Successful proxies are re-added to the
    pool and removed from the queue.

    Args:
        pool (AsyncProxyPool): Owning async pool; used for shutdown signalling
            and access to the proxy collection.
        queue (list[DeadLetterEntry]): Mutable dead-letter queue.
        health_check_fn: Awaitable callable
            ``(proxy, health_check_config) -> (proxy, CheckResult)``.
        state_lock (asyncio.Lock): Lock guarding the pool's mutable state.
        config (DeadLetterConfig): Dead-letter configuration.

    Returns:
        None

    Version:
        Added in 4.0.0.
    """
    interval = config.retry_interval_seconds or 60.0
    while not pool._closed:
        await asyncio.sleep(interval)
        if pool._closed:
            break
        if not queue:
            continue

        hc = pool._config.health_check
        if hc is None:
            logger.warning("Dead-letter retry skipped: health_check is not configured")
            continue

        async with state_lock:
            entries = list(queue)
        if not entries:
            continue

        sem = pool._health_sem

        async def bounded_check(entry: DeadLetterEntry) -> tuple[DeadLetterEntry, Any]:
            async with sem:
                return entry, await health_check_fn(entry.proxy, hc)

        results = await asyncio.gather(
            *(bounded_check(e) for e in entries),
            return_exceptions=True,
        )

        for item in results:
            if pool._closed:
                return
            if isinstance(item, BaseException):
                logger.debug("Dead-letter health check raised: %s", item)
                continue
            entry, result = item
            try:
                proxy, check_result = result
            except Exception:
                logger.debug(
                    "Dead-letter health check returned unexpected result for %s",
                    getattr(entry.proxy, "url", entry.proxy),
                    exc_info=True,
                )
                continue
            if not check_result.success:
                logger.debug(
                    "Dead-letter proxy %s still unhealthy",
                    getattr(proxy, "url", proxy),
                )
                continue

            evict_hooks: list[tuple[str, tuple]] = []
            async with state_lock:
                _, evict_hooks = pool._merge_new_proxies([proxy])
                if entry in queue:
                    queue.remove(entry)
                persist_queue(queue, config, getattr(pool, "_dead_letter_store", None))
            if evict_hooks:
                from .hooks import run_deferred

                await run_deferred(evict_hooks, pool._config.hooks)
            logger.debug(
                "Dead-letter recovered %s back into pool",
                getattr(proxy, "url", proxy),
            )


__all__: list[str] = [
    "DEAD_LETTER_STORE_KEY",
    "DeadLetterEntry",
    "load_queue",
    "maybe_add",
    "persist_queue",
    "retry_cycle",
]
