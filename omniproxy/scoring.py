"""EMA‑based scoring for proxies."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass


@dataclass(slots=True)
class EMAState:
    """Exponentially weighted moving average for success rate and latency."""
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
    """Update *state* in‑place and return it.

    *decay* is the smoothing factor (e.g. 0.9).  Higher → more weight on history.
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
    """Combine success EMA and latency EMA into a single 0‑1 score."""
    if state.latency_ema is None or state.latency_ema <= 0:
        latency_score = 0.0
    else:
        # Normalise latency with a plausible ceiling (e.g. 5 s)
        latency_score: float | int = max(0.0, 1.0 - state.latency_ema / 1.0)

    return success_weight * state.success_ema + latency_weight * latency_score


__all__: list[str] = ["EMAState", "compute_score", "update_ema"]