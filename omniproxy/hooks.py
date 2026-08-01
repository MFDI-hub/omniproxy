"""Deferred lifecycle hook execution."""

from __future__ import annotations

import asyncio
from logging import Logger
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import LifecycleHooks

async def run_deferred(deferred: list[tuple[str, tuple]], hooks: LifecycleHooks) -> None:
    """Execute deferred lifecycle hook calls outside the main state lock.

    Pool internals collect hook invocations while holding their state lock,
    then schedule this coroutine to actually run them once the lock is
    released. Both synchronous callables and coroutine functions are
    supported. Hook exceptions are logged but never re-raised, so a buggy
    hook cannot break the pool.

    Args:
        deferred (list[tuple[str, tuple]]): Ordered list of
            ``(hook_name, args)`` pairs. ``hook_name`` must match an
            attribute on ``hooks``; ``args`` is forwarded positionally.
        hooks (LifecycleHooks): Container of hook callables.

    Returns:
        None

    Version:
        Added in 4.0.0.
    """
    import logging
    logger: Logger = logging.getLogger(name=__name__)

    for name, args in deferred:
        hook = getattr(hooks, name, None)
        if hook is None:
            continue
        try:
            if asyncio.iscoroutinefunction(hook):
                await hook(*args)
            else:
                hook(*args)
        except Exception:
            logger.exception("Hook %s failed", name)


__all__: list[str] = ["run_deferred"]