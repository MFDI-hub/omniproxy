"""Pool-level circuit breaker state machine."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

from .config import CircuitBreakerConfig
from .enum import CircuitBreakerState


@dataclass
class CircuitBreaker:
    """Pool-wide circuit breaker state machine.

    Drives the CLOSED / OPEN / HALF_OPEN transitions used by the proxy pools to
    short-circuit acquisitions when the underlying pool is unhealthy. The
    breaker observes a sliding window of success/failure events; once the
    failure ratio over ``min_throughput`` samples crosses
    ``CircuitBreakerConfig.failure_ratio`` it trips to OPEN, blocks new
    requests until ``half_open_timeout`` elapses, then allows a single probe to
    decide whether to close again.

    All public methods are synchronous and intentionally cheap; callers (the
    pools) invoke them under their own ``_state_lock``.

    Attributes:
        config (CircuitBreakerConfig): Tuning knobs for the breaker.
        state (CircuitBreakerState): Current breaker state.
        event_window (deque[tuple[float, bool]]): Sliding window of
            ``(monotonic_timestamp, success)`` tuples used in CLOSED state.

    Version:
        Added in 4.0.0.
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
        """Epoch of the in-flight HALF_OPEN probe.

        Returns:
            int | None: Monotonic epoch identifying the currently in-flight
            probe, or ``None`` when no probe is active. Used by callers to tag
            their outcome so late completions for stale probes are ignored.

        Version:
            Added in 4.0.0.
        """
        return self._active_probe_epoch if self._probe_in_flight else None

    def drain_pending_transitions(self) -> list[str]:
        """Pop and return queued transition labels.

        Returns:
            list[str]: An ordered list containing ``"open"`` and/or ``"close"``
            entries representing transitions that occurred since the last
            drain. The internal buffer is cleared.

        Version:
            Added in 4.0.0.
        """
        out = list(self._pending_transitions)
        self._pending_transitions.clear()
        return out

    def record_failure(self, now: float | None = None, *, probe_epoch: int | None = None) -> None:
        """Record a failed proxy request against the breaker.

        Args:
            now (float | None): Monotonic timestamp of the failure. Defaults
                to ``time.monotonic()``.
            probe_epoch (int | None): Epoch returned from
                :pyattr:`active_probe_epoch` when the call was issued. Only
                meaningful while the breaker is HALF_OPEN; stale epochs are
                ignored.

        Returns:
            None

        Version:
            Added in 4.0.0.
        """
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
        """Record a successful proxy request against the breaker.

        Args:
            now (float | None): Monotonic timestamp of the success. Defaults
                to ``time.monotonic()``.
            probe_epoch (int | None): Epoch returned from
                :pyattr:`active_probe_epoch` when the call was issued. Only
                meaningful while the breaker is HALF_OPEN; stale epochs are
                ignored.

        Returns:
            None

        Version:
            Added in 4.0.0.
        """
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
        """Decide whether a new proxy acquisition is allowed.

        In CLOSED state, this both prunes the event window and re-evaluates
        the trip condition. In OPEN, it transitions to HALF_OPEN once the
        cooldown has elapsed and claims the probe slot atomically. In
        HALF_OPEN, only the first caller that finds the slot free is allowed.

        Args:
            now (float | None): Monotonic timestamp to evaluate against.
                Defaults to ``time.monotonic()``.

        Returns:
            bool: ``True`` if the caller may proceed to acquire a proxy,
            ``False`` if the breaker is shedding load.

        Version:
            Added in 4.0.0.
        """
        if now is None:
            now = time.monotonic()

        if self.state == CircuitBreakerState.CLOSED:
            self._trim_window(now)
            self._maybe_open(now)
            return self.state == CircuitBreakerState.CLOSED

        if self.state == CircuitBreakerState.OPEN:
            if (
                self.config.half_open_timeout > 0
                and self._opened_at is not None
                and now - self._opened_at >= self.config.half_open_timeout
            ):
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
        """Resolve the in-flight HALF_OPEN probe.

        Args:
            success (bool): ``True`` to transition back to CLOSED, ``False``
                to re-open the breaker.
            now (float | None): Monotonic timestamp of completion. Defaults
                to ``time.monotonic()``.

        Returns:
            None

        Version:
            Added in 4.0.0.
        """
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
        """Return ``True`` if ``probe_epoch`` belongs to the in-flight probe.

        Args:
            probe_epoch (int | None): Epoch returned from
                :pyattr:`active_probe_epoch` when the call was issued.

        Returns:
            bool: ``True`` when the breaker should consume this outcome.

        Version:
            Added in 4.0.0.
        """
        if not self._probe_in_flight:
            return False
        return probe_epoch is not None and probe_epoch == self._active_probe_epoch

    def _begin_probe(self, now: float) -> None:
        """Allocate a fresh probe slot at ``now``.

        Args:
            now (float): Monotonic timestamp of probe start.

        Returns:
            None

        Version:
            Added in 4.0.0.
        """
        self._probe_epoch += 1
        self._active_probe_epoch = self._probe_epoch
        self._probe_in_flight = True
        self._probe_started_at = now

    def _expire_stale_probe(self, now: float) -> None:
        """Release the probe slot if it has exceeded ``half_open_timeout``.

        Args:
            now (float): Monotonic timestamp used to compute the age.

        Returns:
            None

        Version:
            Added in 4.0.0.
        """
        if not self._probe_in_flight or self._probe_started_at is None:
            return
        if now - self._probe_started_at >= self.config.half_open_timeout:
            self._probe_in_flight = False
            self._probe_started_at = None
            self._active_probe_epoch = None
            self._probe_epoch += 1

    def _trim_window(self, now: float) -> None:
        """Drop events older than ``config.window_seconds`` from the window.

        Args:
            now (float): Monotonic timestamp considered "now".

        Returns:
            None

        Version:
            Added in 4.0.0.
        """
        horizon = now - self.config.window_seconds
        while self.event_window and self.event_window[0][0] < horizon:
            self.event_window.popleft()

    def _maybe_open(self, now: float) -> None:
        """Trip to OPEN if the windowed failure ratio is high enough.

        Args:
            now (float): Monotonic timestamp recorded as the open time when
                the breaker trips.

        Returns:
            None

        Version:
            Added in 4.0.0.
        """
        if self.state != CircuitBreakerState.CLOSED:
            return
        total = len(self.event_window)
        if total < self.config.min_throughput:
            return
        failures = sum(1 for _, ok in self.event_window if not ok)
        if failures / total >= self.config.failure_ratio:
            self._to_open(now)

    def _to_open(self, now: float) -> None:
        """Internal transition to ``OPEN``.

        Args:
            now (float): Monotonic timestamp stored as ``_opened_at``.

        Returns:
            None

        Version:
            Added in 4.0.0.
        """
        if self.state != CircuitBreakerState.OPEN:
            self._pending_transitions.append("open")
        self.state = CircuitBreakerState.OPEN
        self._opened_at = now
        self._probe_in_flight = False
        self._probe_started_at = None
        self._active_probe_epoch = None

    def _to_half_open(self) -> None:
        """Internal transition to ``HALF_OPEN``.

        Returns:
            None

        Version:
            Added in 4.0.0.
        """
        self.state = CircuitBreakerState.HALF_OPEN
        self._probe_in_flight = False
        self._probe_started_at = None
        self._active_probe_epoch = None

    def _to_closed(self) -> None:
        """Internal transition to ``CLOSED``.

        Resets the sliding event window so a freshly closed breaker starts
        from a clean slate.

        Returns:
            None

        Version:
            Added in 4.0.0.
        """
        if self.state != CircuitBreakerState.CLOSED:
            self._pending_transitions.append("close")
        self.state = CircuitBreakerState.CLOSED
        self.event_window.clear()
        self._probe_in_flight = False
        self._probe_started_at = None
        self._active_probe_epoch = None
        self._opened_at = None


__all__: list[str] = ["CircuitBreaker"]
