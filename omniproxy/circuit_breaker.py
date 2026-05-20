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

    All public methods are synchronous and meant to be called under the pool's
    ``_state_lock``.
    """

    config: CircuitBreakerConfig
    state: CircuitBreakerState = CircuitBreakerState.CLOSED
    event_window: deque[tuple[float, bool]] = field(default_factory=deque)
    _opened_at: float | None = None
    _probe_in_flight: bool = False
    _probe_started_at: float | None = None
    _probe_epoch: int = 0
    _active_probe_epoch: int | None = None
    _pending_transitions: list[str] = field(default_factory=list)

    @property
    def active_probe_epoch(self) -> int | None:
        """Epoch of the in-flight HALF_OPEN probe, if any."""
        return self._active_probe_epoch if self._probe_in_flight else None

    def drain_pending_transitions(self) -> list[str]:
        """Return and clear pending state transition labels (``open`` / ``close``)."""
        out = list(self._pending_transitions)
        self._pending_transitions.clear()
        return out

    def record_failure(self, now: float | None = None, *, probe_epoch: int | None = None) -> None:
        if now is None:
            now = time.monotonic()
        if self.state == CircuitBreakerState.HALF_OPEN:
            if not self._probe_completion_valid(probe_epoch):
                return
            self.probe_completed(success=False, now=now)
            return
        self.event_window.append((now, False))
        self._trim_window(now)
        self._maybe_open(now)

    def record_success(self, now: float | None = None, *, probe_epoch: int | None = None) -> None:
        if now is None:
            now = time.monotonic()
        if self.state == CircuitBreakerState.HALF_OPEN:
            if not self._probe_completion_valid(probe_epoch):
                return
            self.probe_completed(success=True, now=now)
            return
        self.event_window.append((now, True))
        self._trim_window(now)

    def allow_request(self, now: float | None = None) -> bool:
        """Return False if the breaker disallows a new acquisition.

        In HALF_OPEN, the first allowed call atomically claims the probe slot.
        """
        if now is None:
            now = time.monotonic()

        if self.state == CircuitBreakerState.CLOSED:
            self._trim_window(now)
            self._maybe_open(now)
            return self.state == CircuitBreakerState.CLOSED

        if self.state == CircuitBreakerState.OPEN:
            if self.config.half_open_timeout > 0 and self._opened_at is not None:
                if now - self._opened_at >= self.config.half_open_timeout:
                    self._to_half_open()
                    self._begin_probe(now)
                    return True
            return False

        self._expire_stale_probe(now)
        if self._probe_in_flight:
            return False
        self._begin_probe(now)
        return True

    def probe_completed(self, success: bool, now: float | None = None) -> None:
        """Call with the probe outcome (HALF_OPEN only)."""
        if now is None:
            now = time.monotonic()
        if not self._probe_in_flight:
            return
        self._probe_in_flight = False
        self._probe_started_at = None
        self._active_probe_epoch = None
        if success:
            self._to_closed()
        else:
            self._to_open(now)

    def _probe_completion_valid(self, probe_epoch: int | None) -> bool:
        if not self._probe_in_flight:
            return False
        if probe_epoch is None:
            return True
        return probe_epoch == self._active_probe_epoch

    def _begin_probe(self, now: float) -> None:
        self._probe_epoch += 1
        self._active_probe_epoch = self._probe_epoch
        self._probe_in_flight = True
        self._probe_started_at = now

    def _expire_stale_probe(self, now: float) -> None:
        if not self._probe_in_flight or self._probe_started_at is None:
            return
        if now - self._probe_started_at >= self.config.half_open_timeout:
            self._probe_in_flight = False
            self._probe_started_at = None
            self._active_probe_epoch = None
            self._probe_epoch += 1

    def _trim_window(self, now: float) -> None:
        horizon = now - self.config.window_seconds
        while self.event_window and self.event_window[0][0] < horizon:
            self.event_window.popleft()

    def _maybe_open(self, now: float) -> None:
        if self.state != CircuitBreakerState.CLOSED:
            return
        total = len(self.event_window)
        if total < self.config.min_throughput:
            return
        failures = sum(1 for _, ok in self.event_window if not ok)
        if failures / total >= self.config.failure_ratio:
            self._to_open(now)

    def _to_open(self, now: float) -> None:
        if self.state != CircuitBreakerState.OPEN:
            self._pending_transitions.append("open")
        self.state = CircuitBreakerState.OPEN
        self._opened_at = now
        self._probe_in_flight = False
        self._probe_started_at = None
        self._active_probe_epoch = None

    def _to_half_open(self) -> None:
        self.state = CircuitBreakerState.HALF_OPEN
        self._probe_in_flight = False
        self._probe_started_at = None
        self._active_probe_epoch = None

    def _to_closed(self) -> None:
        if self.state != CircuitBreakerState.CLOSED:
            self._pending_transitions.append("close")
        self.state = CircuitBreakerState.CLOSED
        self.event_window.clear()
        self._probe_in_flight = False
        self._probe_started_at = None
        self._active_probe_epoch = None
        self._opened_at = None


__all__: list[str] = ["CircuitBreaker"]
