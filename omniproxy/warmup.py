"""Warm‑up phase for new pools."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .pool import AsyncProxyPool
    from .config import WarmupConfig
    from .extended_proxy import CheckResult


async def run_warmup(
    pool: AsyncProxyPool,
    config: WarmupConfig,
    health_check_fn,
) -> bool:
    """Check proxies until *min_ready* are working or timeout."""
    if not config.enabled:
        return True

    from .errors import WarmupFailedError

    deadline = asyncio.get_event_loop().time() + config.timeout
    ready = 0
    total = len(pool._proxies)

    while ready < config.min_ready:
        if asyncio.get_event_loop().time() > deadline:
            if config.failure_policy == "raise":
                raise WarmupFailedError(
                    f"Warmup failed: {ready}/{config.min_ready} ready after {config.timeout}s"
                )
            return False

        # Find an unchecked proxy
        async with pool._state_lock:
            candidates = [p for p in pool._proxies if not p.is_working]
        if not candidates:
            break

        # Check one at a time for simplicity; could be parallelized
        for proxy in candidates:
            if asyncio.get_event_loop().time() > deadline:
                break
            _, result = await health_check_fn(proxy, pool._config.health_check)
            async with pool._state_lock:
                pool._apply_check_result(proxy, result, [])
            if result.success:
                ready += 1
                if ready >= config.min_ready:
                    return True

    return ready >= config.min_ready


__all__: list[str] = ["run_warmup"]