"""Proxy selection strategies."""

from __future__ import annotations

import random
from collections import deque
from typing import Any, Protocol

from .proxy import Proxy
from .scoring import EMAState

class SelectionStrategy(Protocol):
    """Callable protocol for selecting one proxy from *eligible* list."""

    def select(
        self,
        eligible: list[Proxy],
        scores: dict[str, EMAState],
        context: Any,
    ) -> Proxy | None:
        ...


class RoundRobinStrategy:
    """Simple round‑robin over the eligible list, driven by an external counter."""

    def __init__(self) -> None:
        self._index = 0

    def select(
        self,
        eligible: list[Proxy],
        scores: dict[str, EMAState],
        context: Any,
    ) -> Proxy | None:
        if not eligible:
            return None
        idx: int = self._index % len(eligible)
        self._index += 1
        return eligible[idx]


class RandomStrategy:
    """Uniform random selection."""

    def select(
        self,
        eligible: list[Proxy],
        scores: dict[str, EMAState],
        context: Any,
    ) -> Proxy | None:
        if not eligible:
            return None
        return random.choice(seq=eligible)


class WeightedStrategy:
    """Probability proportional to EMA score."""

    def select(
        self,
        eligible: list[Proxy],
        scores: dict[str, EMAState],
        context: Any,
    ) -> Proxy | None:
        if not eligible:
            return None

        weights: list[float] = []
        for p in eligible:
            state: EMAState | None = scores.get(p.url)
            if state:
                from .scoring import compute_score
                weights.append(max(0.01, compute_score(state)))  # ensure non‑zero
            else:
                weights.append(0.01)
        total: int | float = sum(weights)
        r: int | float = random.uniform(a=0, b=total)
        upto = 0.0
        for proxy, w in zip(eligible, weights):
            upto += w
            if upto >= r:
                return proxy
        return eligible[-1]


class LowestLatencyStrategy:
    """Pick the proxy with the smallest EMA latency (fallback to round-robin)."""

    def select(
        self,
        eligible: list[Proxy],
        scores: dict[str, EMAState],
        context: Any,
    ) -> Proxy | None:
        if not eligible:
            return None
        best: Proxy = eligible[0]
        best_latency = float("inf")
        for p in eligible:
            state: EMAState | None = scores.get(p.url)
            if state and state.latency_ema is not None and state.latency_ema < best_latency:
                best_latency: int | float = state.latency_ema
                best: Proxy = p
        return best


__all__: list[str] = [
    "LowestLatencyStrategy",
    "RandomStrategy",
    "RoundRobinStrategy",
    "SelectionStrategy",
    "WeightedStrategy",
]