"""Cooldown computation and helpers."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import CooldownConfig


def coerce_exception_type(exception_type: object | None) -> type[BaseException] | None:
    """Normalize a failure ``exc`` argument to an exception class.

    Accepts ``None``, an exception class, or an exception instance. Any other
    value returns ``None`` so callers can skip penalty matching without
    raising :class:`TypeError` from :func:`issubclass`.

    Args:
        exception_type (object | None): Value passed as ``exc`` to
            :meth:`~omniproxy.pool.AsyncProxyPool.mark_failed` or as
            ``exception_type`` to :func:`compute_cooldown`.

    Returns:
        type[BaseException] | None: A usable exception class, or ``None``.

    Version:
        Added in 4.0.0.
    """
    if exception_type is None:
        return None
    if isinstance(exception_type, type) and issubclass(exception_type, BaseException):
        return exception_type
    if isinstance(exception_type, BaseException):
        return type(exception_type)
    return None


def compute_cooldown(
    base: float,
    adaptive: bool,
    failure_count: int,
    penalties: dict[type[BaseException], float],
    exception_type: type[BaseException] | None = None,
    _min: float = 30.0,
    _max: float = 600.0,
) -> float:
    """Compute how long a proxy should stay in cooldown.

    Combines a base duration, optional exponential growth based on the
    consecutive failure count, and an additive per-exception-type penalty
    before clamping into ``[_min, _max]``.

    Args:
        base (float): Base cooldown duration in seconds.
        adaptive (bool): If ``True``, the duration grows as
            ``base * 2 ** (failure_count - 1)``.
        failure_count (int): Number of consecutive failures observed for
            the proxy (must be ``>= 1``).
        penalties (dict[type[BaseException], float]): Mapping of exception
            type to extra seconds added when ``exception_type`` matches.
            The first matching key in iteration order wins.
        exception_type (type[BaseException] | None): Exception class that
            caused the failure. Used as the key in ``penalties``. Invalid
            values are ignored (no penalty applied).
        _min (float): Lower clamp; defaults to ``30.0``.
        _max (float): Upper clamp; defaults to ``600.0``.

    Returns:
        float: Cooldown duration in seconds, guaranteed to be in
        ``[_min, _max]``.

    Version:
        Added in 4.0.0.
    """
    if adaptive:
        duration: float | int = base * (2 ** (failure_count - 1))
    else:
        duration: float | int = base

    matched = coerce_exception_type(exception_type)
    if matched is not None:
        for exc, penalty in penalties.items():
            if not (isinstance(exc, type) and issubclass(exc, BaseException)):
                continue
            if issubclass(matched, exc):
                duration += penalty
                break

    return max(_min, min(_max, duration))


def is_in_cooldown(proxy_id: str, cooldown_until: dict[str, float], now: float | None = None) -> bool:
    """Check whether ``proxy_id`` is still cooling down.

    The mapping ``cooldown_until`` is mutated as a side effect: expired
    entries are removed before the function returns.

    Args:
        proxy_id (str): Proxy identifier.
        cooldown_until (dict[str, float]): Mutable mapping of proxy id to
            the monotonic timestamp at which cooldown ends.
        now (float | None): Monotonic timestamp considered "now". Defaults
            to ``time.monotonic()``.

    Returns:
        bool: ``True`` if the proxy is still cooling down, ``False`` if
        it has cooled off or was never registered.

    Version:
        Added in 4.0.0.
    """
    if now is None:
        now: int | float = time.monotonic()
    until: int | float | None = cooldown_until.get(proxy_id)
    if until is None:
        return False
    if now >= until:
        cooldown_until.pop(proxy_id, None)
        return False
    return True


__all__: list[str] = ["coerce_exception_type", "compute_cooldown", "is_in_cooldown"]