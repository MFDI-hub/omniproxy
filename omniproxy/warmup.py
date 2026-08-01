"""Warm-up phase for new pools."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import WarmupConfig
    from .pool import AsyncProxyPool

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
    and records successes. Each probe batch is bounded by the remaining
    warmup deadline; unfinished checks are cancelled so they cannot mutate
    pool state after the deadline.

    Never raises on failure: always returns ``(False, ready_count)`` when
    ``min_ready`` is unmet so the caller can fire ``on_warmup_completed``
    before applying :attr:`~omniproxy.config.WarmupConfig.failure_policy`.

    Args:
        pool (AsyncProxyPool): Async pool being warmed.
        config (WarmupConfig): Warmup configuration.
        health_check_fn: Awaitable callable
            ``(proxy, health_check_config) -> (proxy, CheckResult)``.

    Returns:
        tuple[bool, int]: ``(success, ready_count)`` where ``success`` is
        ``True`` only when ``ready_count >= config.min_ready``.

    Version:
        Added in 4.0.0.
    """
    if not config.enabled:
        return True, 0

    loop = asyncio.get_running_loop()
    deadline = loop.time() + config.timeout
    ready_urls: set[str] = set()
    sem = pool._health_sem

    async def check_one(proxy):
        async with sem:
            return await health_check_fn(proxy, pool._config.health_check)

    while len(ready_urls) < config.min_ready:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return False, len(ready_urls)

        candidates = await pool._unchecked_proxies()
        candidates = [p for p in candidates if p.url not in ready_urls]
        if not candidates:
            break

        tasks = [asyncio.create_task(check_one(p)) for p in candidates]
        done, pending = await asyncio.wait(tasks, timeout=remaining)
        timed_out = bool(pending)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        for task in done:
            if task.cancelled():
                continue
            exc = task.exception()
            if exc is not None:
                continue
            proxy, result = task.result()
            applied = False
            if result.success:
                applied = await pool._record_health_check_result(proxy, result)
            if applied and _proxy_counts_as_ready(config, proxy, result):
                ready_urls.add(proxy.url)
                if len(ready_urls) >= config.min_ready:
                    return True, len(ready_urls)

        if timed_out or loop.time() >= deadline:
            return False, len(ready_urls)

        sleep_for = min(_POLL_INTERVAL, deadline - loop.time())
        if sleep_for > 0:
            await asyncio.sleep(sleep_for)

    ready = len(ready_urls) >= config.min_ready
    return ready, len(ready_urls)


__all__: list[str] = ["run_warmup"]
