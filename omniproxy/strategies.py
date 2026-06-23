"""Proxy selection strategies."""

from __future__ import annotations

import random
from collections import deque
from typing import Any, Protocol

from .proxy import Proxy
from .scoring import EMAState

class SelectionStrategy(Protocol):
    """Protocol implemented by every pool selection strategy.

    Implementations are expected to be stateless except for any state they
    explicitly manage (e.g. a rotating counter).

    Version:
        Added in 4.0.0.
    """

    def select(
        self,
        eligible: list[Proxy],
        scores: dict[str, EMAState],
        context: Any,
    ) -> Proxy | None:
        """Pick one proxy from ``eligible``.

        Args:
            eligible (list[Proxy]): Candidate proxies known to be currently
                usable (not in cooldown, within concurrency limits, etc.).
            scores (dict[str, EMAState]): Mapping of proxy URL to current
                EMA state. May be empty if scoring is disabled.
            context (Any): Free-form context object. Round-robin strategies
                receive the pool's full ordered proxy list; others may ignore
                it.

        Returns:
            Proxy | None: Selected proxy, or ``None`` when ``eligible`` is empty.

        Version:
            Added in 4.0.0.
        """
        ...


class RoundRobinStrategy:
    """Round-robin selection anchored to the pool's full proxy ordering.

    The rotation index advances through the complete proxy list (passed as
    ``context``), skipping ineligible entries. This keeps fairness stable
    when the eligible subset shrinks or grows between calls.

    Attributes:
        _index (int): Position in the full pool list for the next pick.

    Version:
        Added in 4.0.0.
    """

    def __init__(self) -> None:
        """Initialise the strategy with a zeroed rotation counter.

        Version:
            Added in 4.0.0.
        """
        self._index = 0

    def select(
        self,
        eligible: list[Proxy],
        scores: dict[str, EMAState],
        context: Any,
    ) -> Proxy | None:
        """Return the next proxy in round-robin order.

        Args:
            eligible (list[Proxy]): Candidate proxies.
            scores (dict[str, EMAState]): Unused.
            context (Any): Full ordered pool proxy list (``deque`` or
                ``list``). When omitted, falls back to indexing ``eligible``.

        Returns:
            Proxy | None: The next proxy, or ``None`` when ``eligible`` is empty.

        Version:
            Added in 4.0.0.
        """
        if not eligible:
            return None
        eligible_urls = {p.url for p in eligible}
        if context is not None:
            full_list = list(context)
            if full_list:
                n = len(full_list)
                for offset in range(n):
                    idx = (self._index + offset) % n
                    proxy = full_list[idx]
                    if proxy.url in eligible_urls:
                        self._index = idx + 1
                        return proxy
                return None
        idx: int = self._index % len(eligible)
        self._index += 1
        return eligible[idx]


class RandomStrategy:
    """Uniform random selection from the eligible list.

    Version:
        Added in 4.0.0.
    """

    def select(
        self,
        eligible: list[Proxy],
        scores: dict[str, EMAState],
        context: Any,
    ) -> Proxy | None:
        """Pick a proxy uniformly at random.

        Args:
            eligible (list[Proxy]): Candidate proxies.
            scores (dict[str, EMAState]): Unused.
            context (Any): Unused.

        Returns:
            Proxy | None: A random proxy, or ``None`` when ``eligible`` is empty.

        Version:
            Added in 4.0.0.
        """
        if not eligible:
            return None
        return random.choice(seq=eligible)


class WeightedStrategy:
    """Probability-of-selection proportional to the proxy's EMA score.

    Proxies without a known score are assigned a tiny floor weight so they
    still occasionally get chosen and can build up a track record.

    Version:
        Added in 4.0.0.
    """

    def select(
        self,
        eligible: list[Proxy],
        scores: dict[str, EMAState],
        context: Any,
    ) -> Proxy | None:
        """Pick a proxy with probability proportional to its score.

        Args:
            eligible (list[Proxy]): Candidate proxies.
            scores (dict[str, EMAState]): EMA states keyed by proxy URL.
            context (Any): Unused.

        Returns:
            Proxy | None: Selected proxy, or ``None`` when ``eligible`` is empty.

        Version:
            Added in 4.0.0.
        """
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
    """Always pick the proxy with the lowest EMA latency.

    Proxies without a latency sample yet are not considered "lowest" and
    naturally lose to any proxy that has produced a latency measurement.

    Version:
        Added in 4.0.0.
    """

    def select(
        self,
        eligible: list[Proxy],
        scores: dict[str, EMAState],
        context: Any,
    ) -> Proxy | None:
        """Pick the proxy with the smallest ``latency_ema``.

        Args:
            eligible (list[Proxy]): Candidate proxies.
            scores (dict[str, EMAState]): EMA states keyed by proxy URL.
            context (Any): Unused.

        Returns:
            Proxy | None: The best proxy, or ``None`` when ``eligible`` is
            empty. When no proxy has a recorded latency, the first
            candidate is returned.

        Version:
            Added in 4.0.0.
        """
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