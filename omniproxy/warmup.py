"""Warm‑up phase for new pools."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from .enum import WarmupFailurePolicy

if TYPE_CHECKING:
    from .pool import AsyncProxyPool
    from .config import WarmupConfig

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 0.25


def _proxy_counts_as_ready(config: WarmupConfig, proxy, result) -> bool:
    if not result.success:
        return False
    if config.validator is None:
        return True
    try:
        return config.validator(proxy) >= 1.0
    except Exception:
        logger.warning("Warmup validator failed for %s", proxy.url, exc_info=True)
        return False


async def run_warmup(
    pool: AsyncProxyPool,
    config: WarmupConfig,
    health_check_fn,
) -> tuple[bool, int]:
    """Check proxies until *min_ready* are working or timeout.

    Returns ``(success, ready_count)``.
    """
    if not config.enabled:
        return True, 0

    from .errors import WarmupFailedError

    loop = asyncio.get_running_loop()
    deadline = loop.time() + config.timeout
    ready_urls: set[str] = set()
    sem = pool._health_sem

    async def check_one(proxy):
        async with sem:
            return await health_check_fn(proxy, pool._config.health_check)

    while len(ready_urls) < config.min_ready:
        if loop.time() > deadline:
            if config.failure_policy == WarmupFailurePolicy.RAISE:
                raise WarmupFailedError(
                    f"Warmup failed: {len(ready_urls)}/{config.min_ready} ready after "
                    f"{config.timeout}s"
                )
            return False, len(ready_urls)

        candidates = await pool._unchecked_proxies()
        candidates = [p for p in candidates if p.url not in ready_urls]
        if not candidates:
            break

        results = await asyncio.gather(
            *(check_one(p) for p in candidates),
            return_exceptions=True,
        )

        for item in results:
            if loop.time() > deadline:
                break
            if isinstance(item, BaseException):
                continue
            proxy, result = item
            if result.success:
                await pool._record_health_check_result(proxy, result)
            if _proxy_counts_as_ready(config, proxy, result):
                ready_urls.add(proxy.url)
                if len(ready_urls) >= config.min_ready:
                    return True, len(ready_urls)

        if loop.time() <= deadline:
            await asyncio.sleep(_POLL_INTERVAL)

    ready = len(ready_urls) >= config.min_ready
    return ready, len(ready_urls)


__all__: list[str] = ["run_warmup"]
