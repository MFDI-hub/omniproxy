"""Dead‑letter queue and retry logic."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .extended_proxy import Proxy
    from .config import DeadLetterConfig
    from .pool import AsyncProxyPool   # avoid circular import at runtime

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
        # Drop oldest
        queue.pop(0)
    queue.append(entry)


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
    ``60``), snapshots the queue under ``state_lock``, and re-checks each
    proxy via ``health_check_fn``. Successful proxies are re-added to the
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
        if not queue:
            continue

        # Work on a copy so we can safely remove from the original after success
        async with state_lock:
            entries = list(queue)
        for entry in entries[:]:
            try:
                hc = pool._config.health_check
                if hc is None:
                    continue
                result = await health_check_fn(entry.proxy, hc)
            except Exception:
                continue
            proxy, check_result = result
            if check_result.success:
                evict_hooks: list[tuple[str, tuple]] = []
                async with state_lock:
                    _, evict_hooks = pool._merge_new_proxies([proxy])
                    if entry in queue:
                        queue.remove(entry)
                if evict_hooks:
                    from .hooks import run_deferred

                    await run_deferred(evict_hooks, pool._config.hooks)


__all__: list[str] = ["DeadLetterEntry", "maybe_add", "retry_cycle"]