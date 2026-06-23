"""EMA‑based scoring for proxies."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass


@dataclass(slots=True)
class EMAState:
    """Exponentially weighted moving averages for success rate and latency.

    Attributes:
        success_ema (float): Exponential moving average of success outcomes
            in ``[0.0, 1.0]``. Initialised to ``1.0`` (optimistic).
        latency_ema (float | None): Exponential moving average of request
            latencies in seconds. ``None`` until the first sample arrives.
        last_update (float): Monotonic timestamp of the last update.

    Version:
        Added in 4.0.0.
    """

    success_ema: float = 1.0          # initial assumption of health
    latency_ema: float | None = None
    last_update: float = 0.0          # monotonic timestamp


def update_ema(
    state: EMAState,
    success: bool,
    latency: float | None,
    decay: float,
    now: float | None = None,
) -> EMAState:
    """Update an :class:`EMAState` with a fresh observation.

    The state object is mutated in place; the same reference is returned for
    convenience.

    Args:
        state (EMAState): State to update.
        success (bool): Whether the observation was a success.
        latency (float | None): Latency in seconds. ``None``, ``NaN``, and
            ``inf`` are ignored for the latency EMA.
        decay (float): Smoothing factor in ``(0, 1)``. Higher values give
            more weight to history (e.g. ``0.9`` keeps 90% of the previous
            average).
        now (float | None): Monotonic timestamp recorded as
            ``state.last_update``. Defaults to ``time.monotonic()``.

    Returns:
        EMAState: The mutated ``state`` object.

    Version:
        Added in 4.0.0.
    """
    if now is None:
        now: int | float = time.monotonic()

    state.success_ema: int | float = decay * state.success_ema + (1.0 - decay) * float(success)

    if latency is not None and not (math.isnan(latency) or math.isinf(latency)):
        if state.latency_ema is None:
            state.latency_ema: int | float = latency
        else:
            state.latency_ema: int | float = decay * state.latency_ema + (1.0 - decay) * latency

    state.last_update: int | float = now
    return state


def compute_score(
    state: EMAState,
    success_weight: float = 0.6,
    latency_weight: float = 0.4,
) -> float:
    """Combine the success and latency EMAs into a single ``[0.0, 1.0]`` score.

    The latency contribution is normalised against a 1-second reference; a
    latency EMA at or above that ceiling contributes ``0`` to the score.

    Args:
        state (EMAState): EMA state to score.
        success_weight (float): Weight applied to ``state.success_ema``.
        latency_weight (float): Weight applied to the normalised latency
            score. ``success_weight + latency_weight`` should equal ``1.0``.

    Returns:
        float: Combined score in ``[0.0, 1.0]``.

    Version:
        Added in 4.0.0.
    """
    if state.latency_ema is None or state.latency_ema <= 0:
        latency_score = 0.0
    else:
        # Normalise latency with a plausible ceiling (e.g. 5 s)
        latency_score: float | int = max(0.0, 1.0 - state.latency_ema / 1.0)

    return success_weight * state.success_ema + latency_weight * latency_score


__all__: list[str] = ["EMAState", "compute_score", "update_ema"]