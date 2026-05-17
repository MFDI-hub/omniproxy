"""Cooldown computation and helpers."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import CooldownConfig


def compute_cooldown(
    base: float,
    adaptive: bool,
    failure_count: int,
    penalties: dict[type[BaseException], float],
    exception_type: type[BaseException] | None = None,
    _min: float = 30.0,
    _max: float = 600.0,
) -> float:
    """Return the number of seconds a proxy should stay in cooldown.

    Parameters
    ----------
    base: float
        Base cooldown seconds.
    adaptive: bool
        If True, cooldown grows exponentially with failure_count.
    failure_count: int
        Number of consecutive failures.
    penalties: dict
        Additional seconds added per exception type.
    exception_type: type | None
        Exception type that caused the failure (used to look up penalty).
    _min, _max: float
        Clamp values.
    """
    if adaptive:
        duration: float | int = base * (2 ** (failure_count - 1))
    else:
        duration: float | int = base

    if exception_type is not None:
        for exc, penalty in penalties.items():
            if issubclass(exception_type, exc):
                duration += penalty
                break

    return max(_min, min(_max, duration))


def is_in_cooldown(proxy_id: str, cooldown_until: dict[str, float], now: float | None = None) -> bool:
    """Check whether *proxy_id* is still cooling down."""
    if now is None:
        now: int | float = time.monotonic()
    until: int | float | None = cooldown_until.get(proxy_id)
    if until is None:
        return False
    if now >= until:
        cooldown_until.pop(proxy_id, None)
        return False
    return True


__all__: list[str] = ["compute_cooldown", "is_in_cooldown"]