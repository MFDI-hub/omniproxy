"""Pool‑level circuit breaker state machine."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

from .config import CircuitBreakerConfig
from .enum import CircuitBreakerState


@dataclass
class CircuitBreaker:
    """Drives the pool‑wide OPEN / CLOSED / HALF_OPEN transitions.

    All public methods are synchronous and meant to be called under the pool’s
    ``_state_lock``.
    """

    config: CircuitBreakerConfig
    state: CircuitBreakerState = CircuitBreakerState.CLOSED
    failure_window: deque[float] = field(default_factory=deque)
    _opened_at: float | None = None           # when OPEN was entered
    _probe_in_flight: bool = False            # only meaningful in HALF_OPEN

    def record_failure(self, now: float | None = None) -> None:
        if now is None:
            now: int | float = time.monotonic()
        self.failure_window.append(now)
        self._trim_window(now)
        self._maybe_open(now)

    def record_success(self) -> None:
        # In CLOSED, successes do nothing (window is failure‑only).
        # In HALF_OPEN, a success transitions back to CLOSED.
        if self.state == CircuitBreakerState.HALF_OPEN:
            self._to_closed()

    def allow_request(self, now: float | None = None) -> bool:
        """Return False if the breaker disallows a new acquisition."""
        if now is None:
            now: int | float = time.monotonic()

        if self.state == CircuitBreakerState.CLOSED:
            # still CLOSED? Check if we should transition to OPEN
            self._trim_window(now)
            self._maybe_open(now)
            return self.state == CircuitBreakerState.CLOSED

        if self.state == CircuitBreakerState.OPEN:
            if self.config.half_open_timeout > 0 and self._opened_at is not None:
                if now - self._opened_at >= self.config.half_open_timeout:
                    self._to_half_open()
                    return True   # the first request in HALF_OPEN is allowed to probe
            return False

        # HALF_OPEN: only allow if no probe is already in flight
        return not self._probe_in_flight

    def probe_started(self) -> None:
        """Call after allowing the probe request."""
        self._probe_in_flight = True

    def probe_completed(self, success: bool) -> None:
        """Call with the probe outcome."""
        self._probe_in_flight = False
        if success:
            self._to_closed()
        else:
            self._to_open(now=time.monotonic())

    # -- internal helpers -------------------------------------------------

    def _trim_window(self, now: float) -> None:
        horizon: int | float = now - self.config.window_seconds
        while self.failure_window and self.failure_window[0] < horizon:
            self.failure_window.popleft()

    def _maybe_open(self, now: float) -> None:
        if self.state != CircuitBreakerState.CLOSED:
            return
        total: int = len(self.failure_window)   # failures within window
        if total < self.config.min_throughput:
            return
        if total / (total + 1) >= self.config.failure_ratio:  # rough ratio
            self._to_open(now)

    def _to_open(self, now: float) -> None:
        self.state: CircuitBreakerState = CircuitBreakerState.OPEN
        self._opened_at: float = now
        self._probe_in_flight = False

    def _to_half_open(self) -> None:
        self.state: CircuitBreakerState = CircuitBreakerState.HALF_OPEN
        self._probe_in_flight = False

    def _to_closed(self) -> None:
        self.state: CircuitBreakerState = CircuitBreakerState.CLOSED
        self.failure_window.clear()
        self._probe_in_flight = False
        self._opened_at = None


__all__: list[str] = ["CircuitBreaker"]