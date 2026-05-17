"""Deferred lifecycle hook execution."""

from __future__ import annotations
from logging import Logger

from typing import TYPE_CHECKING, Any
import asyncio

if TYPE_CHECKING:
    from .config import LifecycleHooks

async def run_deferred(deferred: list[tuple[str, tuple]], hooks: LifecycleHooks) -> None:
    """Execute *deferred* hook calls outside the main state lock.

    Each entry is ``(hook_name, args)``.  Errors are logged but not re‑raised.
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