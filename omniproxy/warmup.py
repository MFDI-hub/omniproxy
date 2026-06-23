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
    """Decide whether a freshly checked proxy is considered "warm".

    A proxy counts as ready when the health check succeeded and the optional
    user validator returns ``>= 1.0``. Validator exceptions are logged and
    treated as a failure.

    Args:
        config (WarmupConfig): Warmup configuration providing the validator.
        proxy: The :class:`Proxy` instance being evaluated.
        result: ``CheckResult`` from the health check.

    Returns:
        bool: ``True`` if the proxy is ready, ``False`` otherwise.

    Version:
        Added in 4.0.0.
    """
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
    """Drive the warmup phase until ``min_ready`` proxies are validated or timeout.

    Repeatedly takes unchecked candidates from the pool, runs the supplied
    ``health_check_fn`` against each (bounded by the pool's health semaphore),
    and records successes. The loop exits when enough proxies are ready, when
    the configured timeout elapses, or when there are no candidates left.

    Args:
        pool (AsyncProxyPool): Async pool being warmed.
        config (WarmupConfig): Warmup configuration.
        health_check_fn: Awaitable callable
            ``(proxy, health_check_config) -> (proxy, CheckResult)``.

    Returns:
        tuple[bool, int]: ``(success, ready_count)`` where ``success`` is
        ``True`` only when ``ready_count >= config.min_ready``.

    Raises:
        WarmupFailedError: When the timeout elapses and
            ``config.failure_policy`` is :attr:`WarmupFailurePolicy.RAISE`.

    Version:
        Added in 4.0.0.
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
            applied = False
            if result.success:
                applied = await pool._record_health_check_result(proxy, result)
            if applied and _proxy_counts_as_ready(config, proxy, result):
                ready_urls.add(proxy.url)
                if len(ready_urls) >= config.min_ready:
                    return True, len(ready_urls)

        if loop.time() <= deadline:
            await asyncio.sleep(_POLL_INTERVAL)

    ready = len(ready_urls) >= config.min_ready
    return ready, len(ready_urls)


__all__: list[str] = ["run_warmup"]
