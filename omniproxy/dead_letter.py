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
    proxy: Proxy
    error: str | None
    timestamp: float


def maybe_add(
    entry: DeadLetterEntry,
    config: DeadLetterConfig,
    queue: list[DeadLetterEntry],
) -> None:
    """Add to queue, respecting max size."""
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
    """Background coroutine that periodically retries dead‑letter entries."""
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
                async with state_lock:
                    pool._proxies.append(proxy)
                    if entry in queue:
                        queue.remove(entry)


__all__: list[str] = ["DeadLetterEntry", "maybe_add", "retry_cycle"]