"""Async and synchronous proxy pool implementations."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, fields, replace
from typing import TYPE_CHECKING, Any

from .config import PoolConfig
from .constants import ANONYMITY_RANKS
from .enum import (
    DeadLetterPersistence,
    FilterMissingMetadata,
    PoolStrategy,
    SessionCooldownPolicy,
    WarmupFailurePolicy,
)
from .errors import (
    MissingProxyMetadata,
    NoMatchingProxy,
    PoolCircuitOpenError,
    PoolClosedError,
    PoolDrainingError,
    PoolExhausted,
    PoolSaturated,
    SessionBrokenError,
    WarmupFailedError,
)
from .extended_proxy import Proxy
from .cooldown import coerce_exception_type, compute_cooldown, is_in_cooldown
from .circuit_breaker import CircuitBreaker, CircuitBreakerState
from .scoring import EMAState, update_ema
from .session import SessionEntry, resolve_session
from .hooks import run_deferred

if TYPE_CHECKING:
    from .fetchers.base import ProxyFetcher
    from .extended_proxy import CheckResult
    from .strategies import SelectionStrategy

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class AcquireOptions:
    """Transient filter options for :meth:`AsyncProxyPool.acquire`.

    Used to narrow which proxies are considered for one acquisition.
    Instances are built either directly or via :meth:`from_kwargs`, which
    knows how to merge in pool-level defaults.

    Attributes:
        tags (set[str] | None): Restrict acquisitions to proxies whose
            ``tags`` set intersects this collection.
        country (str | None): Require a specific country code/name on the
            proxy metadata.
        min_anonymity (str | None): Minimum anonymity tier
            (``transparent`` < ``anonymous`` < ``elite``).
        session_key (str | None): Sticky-session identifier.
        accept_callback (Any): Optional predicate
            ``Callable[[Proxy], bool]`` for custom acceptance.

    Version:
        Added in 4.0.0.
    """

    tags: set[str] | None = None
    country: str | None = None
    min_anonymity: str | None = None
    session_key: str | None = None
    accept_callback: Any = None

    @classmethod
    def from_kwargs(cls, config: PoolConfig, **filters: Any) -> AcquireOptions:
        """Build :class:`AcquireOptions` from kwargs, applying pool defaults.

        Accepts both ``session_key`` and the legacy ``session_id`` alias.
        Unknown keys are logged at WARNING level and discarded. Pool-level
        ``acquire_tags`` and ``accept_callback`` are inherited when the
        caller does not override them.

        Args:
            config (PoolConfig): Pool configuration providing defaults.
            **filters (Any): Caller-supplied filter kwargs.

        Returns:
            AcquireOptions: Merged options ready for one acquisition.

        Version:
            Added in 4.0.0.
        """
        if "session_key" not in filters and "session_id" in filters:
            filters = dict(filters)
            filters["session_key"] = filters.pop("session_id")
        known = {f.name for f in fields(cls)}
        unknown = set(filters) - known
        if unknown:
            logger.warning("Unknown acquire filter(s) ignored: %s", sorted(unknown))
        merged = {k: v for k, v in filters.items() if k in known}
        if merged.get("tags") is None and config.acquire_tags is not None:
            merged["tags"] = config.acquire_tags
        if merged.get("accept_callback") is None and config.accept_callback is not None:
            pool_cb = config.accept_callback

            def accept_callback(proxy: Proxy) -> bool:
                return pool_cb(proxy, filters)

            merged["accept_callback"] = accept_callback
        return cls(**merged)


@dataclass(frozen=True, slots=True)
class PoolStatistics:
    """Immutable monotonic counters for pool observability.

    Instances returned by :attr:`AsyncProxyPool.statistics` are frozen
    snapshots; mutating them raises :exc:`dataclasses.FrozenInstanceError`.

    Attributes:
        served (int): Number of proxies handed out by ``acquire``.
        failed (int): Number of client-reported acquisition failures
            via :meth:`AsyncProxyPool.mark_failed`. Background health-check
            failures do **not** increment this counter.
        released (int): Number of proxies returned via ``release``.
        exhausted_count (int): Number of times ``acquire`` raised
            :exc:`PoolExhausted` or a related error.

    Version:
        Added in 4.0.0.
    """

    served: int = 0
    failed: int = 0
    released: int = 0
    exhausted_count: int = 0


class AsyncProxyPool:
    """Asynchronous, thread-safe proxy pool.

    Owns the proxy collection, scoring engine, circuit breaker, dead-letter
    queue, health-check loop and background refresh tasks. Must be used as
    an async context manager so background workers can be started and
    cleanly stopped::

        async with AsyncProxyPool(config, fetchers=[...]) as pool:
            proxy = await pool.acquire()
            try:
                ...
            finally:
                await pool.release(proxy)

    The pool is safe to use from multiple coroutines within the same loop;
    use :class:`SyncProxyPool` to bridge into synchronous code.

    Attributes:
        statistics (PoolStatistics): Frozen observability counter snapshot
            (re-read the property for updated values).

    Version:
        Added in 4.0.0.
    """

    def __init__(
        self,
        config: PoolConfig,
        initial_proxies: list[Proxy] = [],
        fetchers: list[ProxyFetcher] | None = None,
    ) -> None:
        """Build the pool with the given config, seed proxies, and fetchers.

        Args:
            config (PoolConfig): Frozen pool configuration.
            initial_proxies (list[Proxy]): Optional seed proxies. Strings
                are coerced through :class:`Proxy`.
            fetchers (list[ProxyFetcher] | None): Optional list of fetchers
                used by the refresh loop.

        Version:
            Added in 4.0.0.
        """
        self._config: PoolConfig = config
        self._fetchers = fetchers or []
        self._state_lock = asyncio.Lock()
        self._available_cond = asyncio.Condition(self._state_lock)
        self._refresh_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._ready = asyncio.Event()
        self._draining = asyncio.Event()
        self._closed = False
        self._pending_session_rebind: dict[str, Proxy] = {}
        self._proxies: deque[Proxy] = deque()
        self._cooldown_until: dict[str, float] = {}
        self._scores: dict[str, EMAState] = {}
        self._connections: dict[str, int] = {}
        self._consecutive_failures: dict[str, int] = {}
        self._token_buckets: dict[str, Any] = {}
        self._session_registry: dict[str, SessionEntry] = {}
        self._dead_letter_store: Any | None = None
        self._dead_letter_queue: list[Any] = []
        if config.dead_letter.enabled:
            if (
                config.dead_letter.persistence == DeadLetterPersistence.STATE_STORE
                and config.state_store_factory is not None
            ):
                from .dead_letter import load_queue

                self._dead_letter_store = config.state_store_factory()
                self._dead_letter_queue = load_queue(self._dead_letter_store)
        self._circuit_breaker = (
            CircuitBreaker(config.circuit_breaker) if config.circuit_breaker else None
        )
        self._statistics = PoolStatistics()
        self._metrics_queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._background_tasks: set[asyncio.Task] = set()
        self._strategy: SelectionStrategy = self._build_strategy(config.strategy)
        self._strategy_state: Any = None
        self._bg_health: asyncio.Task | None = None
        self._bg_dead: asyncio.Task | None = None
        self._bg_refresh: asyncio.Task | None = None
        self._bg_metrics: asyncio.Task | None = None
        self._half_open_probe_epoch: int | None = None
        self._half_open_probe_url: str | None = None
        self._half_open_probe_proxy: Proxy | None = None
        self._refresh_needed = False
        self._refresh_generation = 0

        if config.health_check:
            self._health_sem = asyncio.Semaphore(
                getattr(config.health_check, "max_concurrent_checks", 50)
            )
        else:
            self._health_sem = asyncio.Semaphore(50)

        for p in initial_proxies:
            if not isinstance(p, Proxy):
                p = Proxy(p)
            self._proxies.append(p)

    async def __aenter__(self) -> AsyncProxyPool:
        """Start background workers and run warmup (if enabled).

        Returns:
            AsyncProxyPool: The pool ready to serve acquisitions.

        Raises:
            PoolClosedError: If the pool has already been closed.
            WarmupFailedError: When warmup fails and the configured failure
                policy is :attr:`WarmupFailurePolicy.RAISE`.

        Version:
            Added in 4.0.0.
        """
        await self._start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        """Stop background workers and drain in-flight acquisitions.

        Args:
            exc_type: Exception type if the ``with`` block raised.
            exc: Exception instance if the ``with`` block raised.
            tb: Traceback if the ``with`` block raised.

        Returns:
            None

        Version:
            Added in 4.0.0.
        """
        await self._close()

    def _track_background_task(self, task: asyncio.Task) -> asyncio.Task:
        """Register a background task for cancellation on shutdown.

        Args:
            task (asyncio.Task): Newly created worker task.

        Returns:
            asyncio.Task: The same task, tracked in ``_background_tasks``.

        Version:
            Added in 4.0.0.
        """
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    def _spawn_background_tasks(self) -> None:
        """Create configured background workers and track them for cleanup.

        Health, dead-letter, and refresh workers wait on ``_ready`` before
        mutating pool state so they do not race warmup. Metrics may drain
        immediately.

        Returns:
            None

        Version:
            Added in 4.0.0.
        """
        if self._config.health_check:
            self._bg_health = self._track_background_task(
                asyncio.create_task(self._health_check_loop())
            )
        self._bg_dead = (
            self._track_background_task(asyncio.create_task(self._dead_letter_retrier()))
            if self._config.dead_letter.enabled
            else None
        )
        has_refresh = (
            self._fetchers
            or self._config.refresh.async_callback
            or self._config.refresh.sync_callback
            or self._config.refresh.fallback_async_callbacks
            or self._config.refresh.fallback_sync_callbacks
        )
        self._bg_refresh = (
            self._track_background_task(asyncio.create_task(self._refresh_loop()))
            if has_refresh
            else None
        )
        self._bg_metrics = (
            self._track_background_task(asyncio.create_task(self._metrics_worker()))
            if self._config.metrics_exporter
            else None
        )

    async def _start(self) -> None:
        """Spawn background workers and run optional warmup.

        Called by :meth:`__aenter__`. Idempotent: a second call after the
        pool is ready is a no-op. Serialized with concurrent ``close()`` via
        ``_close_lock`` so a mid-start shutdown cannot leave orphan workers.
        Any failure during startup triggers a clean shutdown and re-raises.

        Returns:
            None

        Raises:
            PoolClosedError: If the pool has already been closed.
            WarmupFailedError: When warmup fails and the configured failure
                policy is :attr:`WarmupFailurePolicy.RAISE`.

        Version:
            Added in 4.0.0.
        """
        async with self._start_lock:
            try:
                async with self._close_lock:
                    if self._closed:
                        raise PoolClosedError("Pool is closed")
                    if self._ready.is_set():
                        return
                    await self._stop_background_tasks()
                    self._spawn_background_tasks()

                if self._config.warmup.enabled:
                    from .warmup import run_warmup
                    from .extended_proxy import arun_health_check

                    warmup_hooks: list[tuple[str, tuple]] = []
                    if self._config.hooks.on_warmup_started:
                        warmup_hooks.append(("on_warmup_started", ()))
                    await run_deferred(warmup_hooks, self._config.hooks)

                    ok, ready_count = await run_warmup(
                        self, self._config.warmup, arun_health_check
                    )
                    if self._config.hooks.on_warmup_completed:
                        await run_deferred(
                            [
                                (
                                    "on_warmup_completed",
                                    (ready_count, self._config.warmup.min_ready),
                                )
                            ],
                            self._config.hooks,
                        )
                    if (
                        not ok
                        and self._config.warmup.failure_policy == WarmupFailurePolicy.RAISE
                    ):
                        raise WarmupFailedError(
                            f"Warmup failed: fewer than {self._config.warmup.min_ready} "
                            f"proxies ready within {self._config.warmup.timeout}s"
                        )

                async with self._close_lock:
                    if self._closed:
                        raise PoolClosedError("Pool is closed")
                    self._ready.set()
            except BaseException:
                await self._close()
                raise

    async def _stop_background_tasks(self) -> None:
        """Cancel and await all background tasks (health, refresh, metrics).

        Cancels every task in ``_background_tasks`` plus the named
        ``_bg_*`` handles, then clears those references.

        Returns:
            None

        Version:
            Added in 4.0.0.
        """
        tasks: set[asyncio.Task] = {
            t for t in self._background_tasks if not t.done()
        }
        for t in (self._bg_health, self._bg_dead, self._bg_refresh, self._bg_metrics):
            if t is not None and not t.done():
                tasks.add(t)
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._background_tasks.clear()
        self._bg_health = None
        self._bg_dead = None
        self._bg_refresh = None
        self._bg_metrics = None

    async def _close(self) -> None:
        """Drain in-flight acquisitions and stop the pool.

        Sets the draining flag, waits up to ``config.drain_timeout`` for
        outstanding acquisitions to return, then cancels background tasks.
        If the pool never became ready (e.g. close during warmup), ``_ready``
        is set after ``_closed`` so waiters on startup can observe the close.
        Idempotent.

        Returns:
            None

        Version:
            Added in 4.0.0.
        """
        async with self._close_lock:
            if self._closed:
                return

            if self._ready.is_set():
                self._draining.set()
                async with self._state_lock:
                    self._available_cond.notify_all()
                drain_hooks: list[tuple[str, tuple]] = []
                if self._config.hooks.on_draining:
                    drain_hooks.append(("on_draining", ()))
                await run_deferred(drain_hooks, self._config.hooks)

                deadline = (
                    time.monotonic() + self._config.drain_timeout
                    if self._config.drain_timeout > 0
                    else None
                )
                while deadline is not None:
                    async with self._state_lock:
                        if sum(self._connections.values()) == 0:
                            break
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        try:
                            await asyncio.wait_for(self._available_cond.wait(), remaining)
                        except asyncio.TimeoutError:
                            break

            self._closed = True
            # Unblock acquirers / workers still waiting on startup so they
            # can observe ``_closed`` and exit (or raise PoolClosedError).
            if not self._ready.is_set():
                self._ready.set()
            async with self._state_lock:
                self._pending_session_rebind.clear()
                self._available_cond.notify_all()
            await self._stop_background_tasks()

    async def close(self) -> None:
        """Shut down background workers and drain in-flight acquisitions.

        Public alias for the internal :meth:`_close`. Safe to call multiple
        times; subsequent invocations are no-ops.

        Returns:
            None

        Version:
            Added in 4.0.0.
        """
        await self._close()

    @staticmethod
    def _build_strategy(strategy: PoolStrategy) -> SelectionStrategy:
        """Instantiate the :class:`SelectionStrategy` for the given enum value.

        Args:
            strategy (PoolStrategy): Strategy identifier.

        Returns:
            SelectionStrategy: Newly constructed strategy instance.

        Version:
            Added in 4.0.0.
        """
        from .strategies import (
            RoundRobinStrategy,
            RandomStrategy,
            WeightedStrategy,
            LowestLatencyStrategy,
        )
        mapping = {
            PoolStrategy.ROUND_ROBIN: RoundRobinStrategy,
            PoolStrategy.RANDOM: RandomStrategy,
            PoolStrategy.WEIGHTED: WeightedStrategy,
            PoolStrategy.LOWEST_LATENCY: LowestLatencyStrategy,
        }
        return mapping[strategy]()

    def _append_circuit_hooks(self, deferred: list[tuple[str, tuple]]) -> None:
        """Queue circuit-breaker hook invocations onto ``deferred``.

        Drains pending transitions from the breaker and appends
        ``on_circuit_open`` / ``on_circuit_close`` entries when their
        callbacks are configured.

        Args:
            deferred (list[tuple[str, tuple]]): Hook-invocation buffer that
                will be passed to :func:`run_deferred`.

        Returns:
            None

        Version:
            Added in 4.0.0.
        """
        if not self._circuit_breaker:
            return
        for transition in self._circuit_breaker.drain_pending_transitions():
            if transition == "open" and self._config.hooks.on_circuit_open:
                deferred.append(("on_circuit_open", ()))
            elif transition == "close" and self._config.hooks.on_circuit_close:
                deferred.append(("on_circuit_close", ()))

    def _bounded_wait_timeout(self, remaining: float | None) -> float:
        """Compute a polling wait that respects cooldown wake-ups and the user budget.

        Used as a fallback when ``Condition.wait`` would otherwise block
        indefinitely. Picks the smallest of the fallback interval, the
        caller's remaining budget, and the time until the next cooldown
        expires.

        Args:
            remaining (float | None): Remaining caller budget in seconds,
                or ``None`` to wait without an upper bound.

        Returns:
            float: Wait duration in seconds, at least ``0.001``.

        Version:
            Added in 4.0.0.
        """
        interval = self._config.wait_fallback_interval
        if interval <= 0:
            interval = 0.25
        wait_time = interval
        if remaining is not None:
            wait_time = min(wait_time, remaining)
        now = time.monotonic()
        if self._cooldown_until:
            next_wakeup = min(self._cooldown_until.values()) - now
            if next_wakeup > 0:
                wait_time = min(wait_time, next_wakeup)
        return max(wait_time, 0.001)

    async def _wait_for_availability(self, remaining: float | None) -> None:
        """Wait on the availability condition for a bounded duration.

        Args:
            remaining (float | None): Caller-remaining budget. ``None`` means
                rely solely on cooldown/fallback interval bounds.

        Returns:
            None

        Version:
            Added in 4.0.0.
        """
        wait_time = self._bounded_wait_timeout(remaining)
        try:
            await asyncio.wait_for(self._available_cond.wait(), wait_time)
        except asyncio.TimeoutError:
            pass

    def _return_lease(self, proxy: Proxy, *, count_release_stat: bool) -> None:
        """Release one acquire lease for ``proxy``.

        Idempotent: when no leases are outstanding the call is a no-op.
        Notifies the availability condition so waiters can pick up the
        slot.

        Args:
            proxy (Proxy): Proxy whose lease should be released.
            count_release_stat (bool): When ``True`` increments
                ``PoolStatistics.released``.

        Returns:
            None

        Version:
            Added in 4.0.0.
        """
        current = self._connections.get(proxy.url, 0)
        if current <= 0:
            return
        self._connections[proxy.url] = current - 1
        if count_release_stat:
            self._statistics = replace(
                self._statistics, released=self._statistics.released + 1
            )
        self._available_cond.notify_all()

    async def acquire(self, **filters: Any) -> Proxy:
        """Acquire one proxy from the pool, blocking up to ``acquire_timeout``.

        Honours pool-wide policies: filters, sticky sessions, cooldown,
        per-proxy connection limits, the circuit breaker, on-demand
        refresh, and lifecycle hooks. The returned proxy must be released
        with :meth:`release` (or :meth:`mark_failed` / :meth:`mark_success`).

        Args:
            **filters (Any): Acquire-time filters forwarded to
                :meth:`AcquireOptions.from_kwargs`. Common keys include
                ``tags``, ``country``, ``min_anonymity``, ``session_key``
                and ``accept_callback``.

        Returns:
            Proxy: Acquired proxy.

        Raises:
            PoolClosedError: Pool has been closed.
            PoolDrainingError: Pool is shutting down.
            PoolCircuitOpenError: Circuit breaker is shedding traffic.
            PoolExhausted: No usable proxies available within the budget.
            PoolSaturated: Matching proxies exist but all hit connection
                caps or cooldowns.
            NoMatchingProxy: Filters exclude every available proxy.
            MissingProxyMetadata: ``filter_missing_metadata='raise'`` and a
                required metadata field is missing on every candidate.
            SessionBrokenError: Sticky session policy raised on invalid
                binding.

        Version:
            Added in 4.0.0.
        """
        await self._ready.wait()
        options = AcquireOptions.from_kwargs(self._config, **filters)
        loop = asyncio.get_running_loop()
        timeout = self._config.acquire_timeout
        start_time = loop.time()
        proxy: Proxy | None = None
        did_on_demand_refresh = False

        while True:
            deferred: list[tuple[str, tuple]] = []
            should_refresh = False
            circuit_open = False
            circuit_exc: PoolCircuitOpenError | None = None
            exhausted_hooks: list[tuple[str, tuple]] = []
            missing_metadata_msg: str | None = None

            async with self._state_lock:
                try:
                    self._check_availability()
                except PoolCircuitOpenError as exc:
                    circuit_exc = exc
                self._append_circuit_hooks(deferred)

                if circuit_exc is None:
                    proxy = self._select(options)
                    if proxy is not None:
                        self._mark_acquired(proxy, options.session_key)
                        deferred.append(("on_proxy_acquired", (proxy,)))
                        if options.session_key:
                            old = self._pending_session_rebind.pop(options.session_key, None)
                            if (
                                old is not None
                                and old.url != proxy.url
                                and self._config.hooks.on_session_rebind
                            ):
                                deferred.append(
                                    ("on_session_rebind", (options.session_key, old, proxy))
                                )
                        break

                if timeout < 0:
                    await self._wait_for_availability(None)
                    continue

                if timeout > 0:
                    remaining = timeout - (loop.time() - start_time)
                    if remaining > 0:
                        await self._wait_for_availability(remaining)
                        continue

                if circuit_exc is not None:
                    circuit_open = True
                else:
                    missing_metadata_msg = self._missing_metadata_message(options)
                    if missing_metadata_msg is None and not did_on_demand_refresh:
                        should_refresh = True
                        if self._config.hooks.on_exhausted:
                            exhausted_hooks.append(("on_exhausted", ()))

            if circuit_open:
                await run_deferred(deferred, self._config.hooks)
                assert circuit_exc is not None
                raise circuit_exc

            if missing_metadata_msg is not None:
                raise MissingProxyMetadata(missing_metadata_msg)

            if should_refresh:
                refreshed = await self._attempt_on_demand_refresh(options)
                did_on_demand_refresh = True
                if refreshed:
                    continue

            async with self._state_lock:
                self._statistics = replace(
                    self._statistics,
                    exhausted_count=self._statistics.exhausted_count + 1,
                )
                self._emit_stat_metric("pool.exhausted", float(self._statistics.exhausted_count))
                exc = self._classify_acquire_failure(options)
                if isinstance(exc, PoolSaturated) and self._config.hooks.on_saturated:
                    deferred.append(("on_saturated", ()))
            deferred.extend(exhausted_hooks)
            await run_deferred(deferred, self._config.hooks)
            raise exc

        # Lease is held from here; any failure before returning to the caller
        # must release it or the connection counter will leak.
        try:
            if self._config.rotate_on_acquire and proxy.rotation_url:
                await proxy.arotate()
            await run_deferred(deferred, self._config.hooks)
            return proxy
        except Exception:
            async with self._state_lock:
                self._return_lease(proxy, count_release_stat=False)
                if self._half_open_probe_proxy is proxy:
                    self._half_open_probe_epoch = None
                    self._half_open_probe_url = None
                    self._half_open_probe_proxy = None
            raise

    async def release(self, proxy: Proxy) -> None:
        """Return a proxy to the pool after a successful use.

        Releases the lease, clears any in-flight HALF_OPEN probe marker,
        and fires ``on_proxy_released`` when configured. Do not call both
        :meth:`release` and :meth:`mark_failed` for the same acquisition.

        Args:
            proxy (Proxy): Proxy previously returned by :meth:`acquire`.

        Returns:
            None

        Version:
            Added in 4.0.0.
        """
        async with self._state_lock:
            self._return_lease(proxy, count_release_stat=True)
            if self._half_open_probe_proxy is proxy:
                self._half_open_probe_epoch = None
                self._half_open_probe_url = None
                self._half_open_probe_proxy = None
        if self._config.hooks.on_proxy_released:
            await run_deferred([("on_proxy_released", (proxy,))], self._config.hooks)

    async def mark_failed(self, proxy: Proxy, exc: type | None = None) -> None:
        """Report that ``proxy`` failed for the current acquisition.

        Updates failure counts, scoring EMA, cooldown timers, and the
        circuit breaker. Also returns the acquire lease (do not call both
        :meth:`mark_failed` and :meth:`release` for the same acquisition).
        Optionally rotates the proxy when ``rotate_on_failure`` is set.

        Args:
            proxy (Proxy): Proxy that failed.
            exc (type | None): Exception class (or instance) that caused
                the failure; used for per-type cooldown penalties. Non-exception
                values are ignored with a warning so failure hooks still fire.

        Returns:
            None

        Version:
            Added in 4.0.0.
        """
        deferred: list[tuple[str, tuple]] = []
        rotate = False
        exc_type = coerce_exception_type(exc)
        if exc is not None and exc_type is None:
            logger.warning(
                "mark_failed exc must be an exception class or instance, got %r; ignoring",
                exc,
            )
        async with self._state_lock:
            is_probe = self._half_open_probe_proxy is proxy
            probe_epoch = self._half_open_probe_epoch if is_probe else None
            self._return_lease(proxy, count_release_stat=False)
            if is_probe:
                self._half_open_probe_epoch = None
                self._half_open_probe_url = None
                self._half_open_probe_proxy = None
            self._statistics = replace(self._statistics, failed=self._statistics.failed + 1)
            self._emit_stat_metric("pool.failed", float(self._statistics.failed))
            self._consecutive_failures[proxy.url] = self._consecutive_failures.get(proxy.url, 0) + 1
            state = self._scores.get(proxy.url)
            if state is None and self._config.scoring:
                state = EMAState()
                self._scores[proxy.url] = state
            if state and self._config.scoring:
                update_ema(
                    state,
                    success=False,
                    latency=None,
                    decay=self._config.scoring.decay_factor,
                )
            self._apply_cooldown(proxy, exc_type, deferred)
            if self._circuit_breaker:
                self._circuit_breaker.record_failure(probe_epoch=probe_epoch)
                self._append_circuit_hooks(deferred)
            rotate = self._config.rotate_on_failure and bool(proxy.rotation_url)
            deferred.append(("on_proxy_failed", (proxy, exc_type)))

        # Rotation is best-effort I/O; failure lifecycle hooks must still fire.
        try:
            if rotate:
                await proxy.arotate()
        finally:
            await run_deferred(deferred, self._config.hooks)

    async def mark_success(self, proxy: Proxy, latency: float | None = None) -> None:
        """Report that ``proxy`` succeeded for the current acquisition.

        Clears any consecutive-failure count, updates scoring EMAs with the
        observed latency, removes pending cooldown, feeds the circuit
        breaker, and releases the acquire lease. Do not call both
        :meth:`mark_success` and :meth:`release` for the same acquisition.

        Args:
            proxy (Proxy): Proxy that succeeded.
            latency (float | None): Observed request latency in seconds.

        Returns:
            None

        Version:
            Added in 4.0.0.
        """
        deferred: list[tuple[str, tuple]] = []
        async with self._state_lock:
            is_probe = self._half_open_probe_proxy is proxy
            probe_epoch = self._half_open_probe_epoch if is_probe else None
            self._return_lease(proxy, count_release_stat=False)
            if is_probe:
                self._half_open_probe_epoch = None
                self._half_open_probe_url = None
                self._half_open_probe_proxy = None
            self._consecutive_failures.pop(proxy.url, None)
            state = self._scores.get(proxy.url)
            if state is None and self._config.scoring:
                state = EMAState()
                self._scores[proxy.url] = state
            if state and self._config.scoring:
                update_ema(
                    state,
                    success=True,
                    latency=latency,
                    decay=self._config.scoring.decay_factor,
                )
            self._cooldown_until.pop(proxy.url, None)
            if self._circuit_breaker:
                self._circuit_breaker.record_success(probe_epoch=probe_epoch)
                self._append_circuit_hooks(deferred)
            self._available_cond.notify_all()
        await run_deferred(deferred, self._config.hooks)

    async def _unchecked_proxies(self) -> list[Proxy]:
        """Return proxies that have not been verified as working yet.

        Helper used by warmup to find candidates worth probing.

        Returns:
            list[Proxy]: Snapshot of proxies whose ``is_working`` is falsey.

        Version:
            Added in 4.0.0.
        """
        async with self._state_lock:
            return [p for p in self._proxies if not p.is_working]

    async def _record_health_check_result(self, proxy: Proxy, result: CheckResult) -> bool:
        """Apply a health-check outcome to the pool under the state lock.

        Args:
            proxy (Proxy): The proxy that was checked.
            result (CheckResult): Outcome of the health check.

        Returns:
            bool: ``True`` when the result was applied to a proxy still in the pool.

        Version:
            Added in 4.0.0.
        """
        async with self._state_lock:
            return self._apply_check_result(proxy, result, [])

    def _check_availability(self) -> None:
        """Raise if the pool is unavailable for new acquisitions.

        Returns:
            None

        Raises:
            PoolClosedError: When the pool has already been closed.
            PoolDrainingError: When the pool is shutting down.
            PoolCircuitOpenError: When the circuit breaker is shedding load.

        Version:
            Added in 4.0.0.
        """
        if self._closed:
            raise PoolClosedError("Pool is closed")
        if self._draining.is_set():
            raise PoolDrainingError("Pool is draining")
        if not self._circuit_breaker:
            return
        if not self._circuit_breaker.allow_request():
            if self._circuit_breaker.state == CircuitBreakerState.HALF_OPEN:
                raise PoolCircuitOpenError(
                    "Circuit breaker open (HALF_OPEN probe in progress)"
                )
            raise PoolCircuitOpenError("Circuit breaker open")

    def _select(self, options: AcquireOptions) -> Proxy | None:
        """Apply the active strategy to choose a proxy from eligible candidates.

        Args:
            options (AcquireOptions): Acquire-time filters.

        Returns:
            Proxy | None: Selected proxy, or ``None`` when no candidate is
            eligible.

        Version:
            Added in 4.0.0.
        """
        eligible = self._get_eligible(options)
        if not eligible:
            return None
        return self._strategy.select(eligible, self._scores, self._proxies)

    @staticmethod
    def _anonymity_rank(label: str | None) -> int:
        """Return the numeric ranking for an anonymity label.

        Args:
            label (str | None): Anonymity label (case-insensitive) or ``None``.

        Returns:
            int: Ranking from :data:`ANONYMITY_RANKS`; ``0`` for unknown or
            missing labels.

        Version:
            Added in 4.0.0.
        """
        if not label:
            return 0
        return ANONYMITY_RANKS.get(label.lower(), 0)

    def _metadata_value_missing(self, proxy: Proxy, attr: str) -> bool:
        """Decide whether a proxy's metadata attribute counts as missing.

        Args:
            proxy (Proxy): Proxy to inspect.
            attr (str): Metadata attribute name (``country``, ``anonymity``,
                ``tags``, ...).

        Returns:
            bool: ``True`` if the attribute is unset (``None`` or empty).

        Version:
            Added in 4.0.0.
        """
        value = getattr(proxy, attr, None)
        if attr == "tags":
            return not value
        return value in (None, "")

    def _active_metadata_filters(self, options: AcquireOptions) -> list[tuple[str, Any]]:
        """Collect active metadata filters from ``options``.

        Args:
            options (AcquireOptions): Acquire-time filters.

        Returns:
            list[tuple[str, Any]]: ``(attr_name, filter_value)`` pairs that
            should be enforced for this acquisition.

        Version:
            Added in 4.0.0.
        """
        filters: list[tuple[str, Any]] = []
        if options.country:
            filters.append(("country", options.country))
        if options.min_anonymity:
            filters.append(("anonymity", options.min_anonymity))
        if options.tags:
            filters.append(("tags", options.tags))
        return filters

    def _proxy_matches_metadata_filter(
        self, proxy: Proxy, attr: str, filter_val: Any, *, ignore_missing: bool
    ) -> bool:
        """Check whether ``proxy`` satisfies one metadata filter.

        Args:
            proxy (Proxy): Proxy being inspected.
            attr (str): Metadata attribute name.
            filter_val (Any): Required value (or minimum/intersection).
            ignore_missing (bool): When ``True``, missing metadata is
                treated as a pass instead of a fail.

        Returns:
            bool: ``True`` if the proxy matches.

        Version:
            Added in 4.0.0.
        """
        if attr == "country":
            if self._metadata_value_missing(proxy, "country"):
                return ignore_missing
            return proxy.country == filter_val
        if attr == "anonymity":
            if self._metadata_value_missing(proxy, "anonymity"):
                return ignore_missing
            return self._anonymity_rank(proxy.anonymity) >= self._anonymity_rank(filter_val)
        if attr == "tags":
            if self._metadata_value_missing(proxy, "tags"):
                return ignore_missing
            return bool(filter_val & set(getattr(proxy, "tags", [])))
        return False

    def _missing_metadata_message(self, options: AcquireOptions) -> str | None:
        """Build a :exc:`MissingProxyMetadata` message if required metadata is absent.

        Args:
            options (AcquireOptions): Acquire-time filters.

        Returns:
            str | None: A human-readable error message when
            ``filter_missing_metadata=RAISE`` is active and at least one
            filter has no matching proxy because of missing metadata.
            ``None`` otherwise.

        Version:
            Added in 4.0.0.
        """
        if self._config.filter_missing_metadata != FilterMissingMetadata.RAISE:
            return None
        for attr, filter_val in self._active_metadata_filters(options):
            if any(
                self._proxy_matches_metadata_filter(p, attr, filter_val, ignore_missing=False)
                for p in self._proxies
            ):
                continue
            if any(self._metadata_value_missing(p, attr) for p in self._proxies):
                return (
                    f"No usable proxy declares metadata for filter "
                    f"{attr}={filter_val!r} (pool filter_missing_metadata=RAISE)"
                )
        return None

    def _classify_acquire_failure(self, options: AcquireOptions):
        """Pick the most accurate exception to raise from a failed acquire.

        Args:
            options (AcquireOptions): The acquire-time filters in effect.

        Returns:
            Exception: One of :exc:`PoolExhausted`, :exc:`PoolSaturated`,
            or :exc:`NoMatchingProxy`.

        Version:
            Added in 4.0.0.
        """
        if not self._proxies:
            return PoolExhausted("No proxies available")

        now = time.monotonic()
        has_filters = any(
            [options.tags, options.country, options.min_anonymity, options.accept_callback]
        )
        if has_filters and not self._any_filter_match(options):
            return NoMatchingProxy("No proxy matches the requested filters")

        matching = [p for p in self._proxies if self._sticky_filters_ok(p, options)]
        if matching and all(
            self._at_connection_cap(p) or is_in_cooldown(p.url, self._cooldown_until, now)
            for p in matching
        ):
            if any(self._at_connection_cap(p) for p in matching):
                return PoolSaturated("All matching proxies are at connection limit or cooling down")
            return PoolExhausted("All matching proxies are cooling down")

        return PoolExhausted("No proxies available")

    def _any_filter_match(self, options: AcquireOptions) -> bool:
        """Return ``True`` when at least one proxy passes all filters.

        Args:
            options (AcquireOptions): Acquire-time filters.

        Returns:
            bool: ``True`` if any proxy in the pool satisfies the filters.

        Version:
            Added in 4.0.0.
        """
        return any(self._sticky_filters_ok(p, options) for p in self._proxies)

    def _at_connection_cap(self, proxy: Proxy) -> bool:
        """Return ``True`` if ``proxy`` has reached its connection limit.

        Args:
            proxy (Proxy): Proxy to test.

        Returns:
            bool: ``True`` when ``max_connections_per_proxy`` is set and the
            current connection count equals or exceeds it.

        Version:
            Added in 4.0.0.
        """
        lim = self._config.limits.max_connections_per_proxy
        return bool(lim is not None and self._connections.get(proxy.url, 0) >= lim)

    def _sticky_filters_ok(self, proxy: Proxy, options: AcquireOptions) -> bool:
        """Run every acquire-time filter against ``proxy``.

        Args:
            proxy (Proxy): Proxy to evaluate.
            options (AcquireOptions): Acquire-time filters.

        Returns:
            bool: ``True`` if every active filter passes.

        Version:
            Added in 4.0.0.
        """
        ignore_missing = (
            self._config.filter_missing_metadata == FilterMissingMetadata.IGNORE
        )
        for attr, filter_val in self._active_metadata_filters(options):
            if not self._proxy_matches_metadata_filter(
                proxy, attr, filter_val, ignore_missing=ignore_missing
            ):
                return False
        if options.accept_callback and not options.accept_callback(proxy):
            return False
        return True

    def _get_eligible(self, options: AcquireOptions) -> list[Proxy]:
        """Return the list of proxies eligible for selection right now.

        Handles sticky-session resolution, cooldown timers, per-proxy
        connection limits, and metadata filters.

        Args:
            options (AcquireOptions): Acquire-time filters.

        Returns:
            list[Proxy]: Candidate proxies in pool order.

        Raises:
            SessionBrokenError: When a sticky session is invalid and the
                policy is :attr:`SessionCooldownPolicy.RAISE`.

        Version:
            Added in 4.0.0.
        """
        now = time.monotonic()
        # Sticky REBIND: defer registry drop / pending_rebind until a
        # replacement is actually eligible. Setting them when no alternative
        # exists leaves a stale pending entry and an unbound session that can
        # block subsequent acquires from re-evaluating the original binding.
        rebind_from: Proxy | None = None

        if options.session_key:
            bound = resolve_session(
                options.session_key,
                self._session_registry,
                list(self._proxies),
                self._config.session,
                now,
                pending_rebind=self._pending_session_rebind,
            )
            if bound is not None:
                if is_in_cooldown(bound.url, self._cooldown_until, now):
                    pol = self._config.session.cooldown_policy
                    if pol == SessionCooldownPolicy.BLOCK:
                        return []
                    if pol == SessionCooldownPolicy.RAISE:
                        raise SessionBrokenError(
                            f"Session '{options.session_key}' proxy is unavailable (cooldown)"
                        )
                    rebind_from = bound
                else:
                    if self._at_connection_cap(bound):
                        return []
                    if not self._sticky_filters_ok(bound, options):
                        return []
                    return [bound]

        result: list[Proxy] = []
        for proxy in self._proxies:
            if rebind_from is not None and proxy.url == rebind_from.url:
                continue
            if is_in_cooldown(proxy.url, self._cooldown_until, now):
                continue
            if self._at_connection_cap(proxy):
                continue
            if not self._sticky_filters_ok(proxy, options):
                continue
            result.append(proxy)

        if options.session_key:
            if rebind_from is not None and result:
                self._pending_session_rebind[options.session_key] = rebind_from
                self._session_registry.pop(options.session_key, None)
            elif not result:
                # Selection failed — drop stale pending so the next attempt
                # re-evaluates the binding from scratch.
                self._pending_session_rebind.pop(options.session_key, None)

        return result

    def _mark_acquired(self, proxy: Proxy, session_key: str | None = None) -> None:
        """Record an acquired lease and maybe bind a sticky session.

        Args:
            proxy (Proxy): Proxy being handed out.
            session_key (str | None): Sticky session identifier to bind.

        Returns:
            None

        Version:
            Added in 4.0.0.
        """
        self._connections[proxy.url] = self._connections.get(proxy.url, 0) + 1
        self._statistics = replace(self._statistics, served=self._statistics.served + 1)
        self._emit_stat_metric("pool.served", float(self._statistics.served))
        if (
            self._circuit_breaker
            and self._circuit_breaker.state == CircuitBreakerState.HALF_OPEN
        ):
            self._half_open_probe_epoch = self._circuit_breaker.active_probe_epoch
            self._half_open_probe_url = proxy.url
            self._half_open_probe_proxy = proxy
        else:
            self._half_open_probe_epoch = None
            self._half_open_probe_url = None
            self._half_open_probe_proxy = None
        if session_key:
            self._session_registry[session_key] = SessionEntry(
                proxy_id=proxy.url,
                expires_at=time.monotonic() + self._config.session.ttl,
            )

    def _apply_cooldown(
        self,
        proxy: Proxy,
        exc: type | None = None,
        deferred: list[tuple[str, tuple]] | None = None,
    ) -> None:
        """Apply a cooldown timer to ``proxy`` after a failure.

        No-op when the consecutive failure count is below the configured
        ``failure_threshold``. Uses a user-supplied
        ``cooldown.strategy`` callable when provided, otherwise
        :func:`compute_cooldown`.

        Args:
            proxy (Proxy): Proxy to cool down.
            exc (type | None): Exception class that caused the failure
                (looked up in cooldown penalties). Non-exception values are
                ignored.
            deferred (list[tuple[str, tuple]] | None): Optional hook
                buffer; receives an ``on_proxy_cooled_down`` entry when the
                hook is configured.

        Returns:
            None

        Version:
            Added in 4.0.0.
        """
        cfg = self._config.cooldown
        failures = self._consecutive_failures.get(proxy.url, 0)
        if failures < cfg.failure_threshold:
            return
        total_proxies = len(self._proxies)
        exc_type = coerce_exception_type(exc)
        if cfg.strategy is not None:
            raw = cfg.strategy(float(cfg.base), int(failures), int(total_proxies))
            dur = max(cfg.min, min(cfg.max, float(raw)))
        else:
            dur = compute_cooldown(
                cfg.base,
                cfg.adaptive,
                failures,
                cfg.penalties,
                exc_type,
                _min=cfg.min,
                _max=cfg.max,
            )
        self._cooldown_until[proxy.url] = time.monotonic() + dur
        if deferred is not None and self._config.hooks.on_proxy_cooled_down:
            deferred.append(("on_proxy_cooled_down", (proxy,)))

    def _apply_check_result(self, proxy: Proxy, result: CheckResult, deferred: list) -> bool:
        """Integrate a health-check outcome into pool accounting.

        Updates failure counts, scoring EMA, and cooldown timers. Does not
        feed the pool-wide circuit breaker (only client-reported outcomes
        via :meth:`mark_success` / :meth:`mark_failed` do). Queues lifecycle
        hooks for later execution via :func:`run_deferred`.

        Args:
            proxy (Proxy): Checked proxy.
            result (CheckResult): Health-check outcome.
            deferred (list): Hook-invocation buffer.

        Returns:
            bool: ``True`` when the result was applied to a proxy still in the pool.

        Version:
            Added in 4.0.0.
        """
        if proxy.url not in {p.url for p in self._proxies}:
            return False
        if result.success:
            self._consecutive_failures.pop(proxy.url, None)
            if self._config.scoring:
                state = self._scores.get(proxy.url)
                if state is None:
                    state = EMAState()
                    self._scores[proxy.url] = state
                update_ema(
                    state,
                    success=True,
                    latency=result.latency,
                    decay=self._config.scoring.decay_factor,
                )
            self._cooldown_until.pop(proxy.url, None)
            if self._config.hooks.on_check_complete:
                deferred.append(("on_check_complete", (proxy, result)))
            deferred.append(("on_proxy_recovered", (proxy,)))
        else:
            # Health-check failures must not pollute PoolStatistics.failed /
            # pool.failed (those track client mark_failed only).
            self._consecutive_failures[proxy.url] = self._consecutive_failures.get(proxy.url, 0) + 1
            if self._config.scoring:
                state = self._scores.get(proxy.url)
                if state is None:
                    state = EMAState()
                    self._scores[proxy.url] = state
                update_ema(
                    state,
                    success=False,
                    latency=result.latency,
                    decay=self._config.scoring.decay_factor,
                )
            self._apply_cooldown(proxy, result.exc_type, deferred)
            if self._config.hooks.on_check_complete:
                deferred.append(("on_check_complete", (proxy, result)))
            deferred.append(("on_proxy_failed", (proxy, result.exc_type)))
        return True

    def _count_consecutive_failures(self, proxy: Proxy) -> int:
        """Return the consecutive failure count, never less than ``1``.

        Args:
            proxy (Proxy): Proxy to inspect.

        Returns:
            int: At least ``1`` so callers can use it as a multiplier.

        Version:
            Added in 4.0.0.
        """
        return max(1, self._consecutive_failures.get(proxy.url, 0))

    def _evict_proxy(
        self,
        proxy: Proxy,
        deferred: list[tuple[str, tuple]] | None = None,
        *,
        reason: str = "max_size",
    ) -> None:
        """Remove all bookkeeping for a proxy from the pool.

        Cleans up scoring, cooldown, connection counts, token buckets, and
        sticky-session entries pointing at the proxy. When dead-letter is
        enabled, enqueues the proxy for later retry (caller must hold
        ``_state_lock``).

        Args:
            proxy (Proxy): Proxy that was removed from ``_proxies``.
            deferred (list[tuple[str, tuple]] | None): Optional hook buffer
                for ``on_auto_evicted`` / ``on_dead_letter_added``.
            reason (str): Eviction reason string passed to hooks and stored
                on the dead-letter entry.

        Returns:
            None

        Version:
            Added in 4.0.0.
        """
        url = proxy.url
        self._scores.pop(url, None)
        self._cooldown_until.pop(url, None)
        self._connections.pop(url, None)
        self._token_buckets.pop(url, None)
        self._consecutive_failures.pop(url, None)
        stale = [k for k, entry in self._session_registry.items() if entry.proxy_id == url]
        for key in stale:
            self._session_registry.pop(key, None)
        stale_rebind = [k for k, bound in self._pending_session_rebind.items() if bound.url == url]
        for key in stale_rebind:
            self._pending_session_rebind.pop(key, None)
        if self._config.dead_letter.enabled:
            from .dead_letter import DeadLetterEntry, maybe_add, persist_queue

            maybe_add(
                DeadLetterEntry(proxy=proxy, error=reason, timestamp=time.monotonic()),
                self._config.dead_letter,
                self._dead_letter_queue,
            )
            persist_queue(
                self._dead_letter_queue,
                self._config.dead_letter,
                self._dead_letter_store,
            )
            if deferred is not None and self._config.hooks.on_dead_letter_added:
                deferred.append(("on_dead_letter_added", (proxy, reason)))
        if deferred is not None and self._config.hooks.on_auto_evicted:
            deferred.append(("on_auto_evicted", (proxy, reason)))

    async def _attempt_on_demand_refresh(self, options: AcquireOptions) -> bool:
        """Run a single refresh cycle under the refresh lock.

        Args:
            options (AcquireOptions): Acquire-time filters (currently unused
                but kept for forward compatibility).

        Returns:
            bool: ``True`` if at least one new proxy was added.

        Version:
            Added in 4.0.0.
        """
        gen = self._refresh_generation
        async with self._refresh_lock:
            if self._refresh_generation != gen:
                return True
            added = await self._refresh_and_merge()
            return added > 0

    async def _fetch_new_proxies(self) -> list[Proxy]:
        """Fetch a fresh proxy list from callbacks or fetchers.

        Refresh callbacks take precedence over fetchers; when neither is
        configured, an empty list is returned.

        Returns:
            list[Proxy]: Newly fetched (but not yet merged) proxies.

        Version:
            Added in 4.0.0.
        """
        from .refresh import fetch_from_fetchers, fetch_from_refresh_config

        refresh = self._config.refresh
        if (
            refresh.async_callback
            or refresh.sync_callback
            or refresh.fallback_async_callbacks
            or refresh.fallback_sync_callbacks
        ):
            return await fetch_from_refresh_config(refresh)
        if self._fetchers:
            return await fetch_from_fetchers(
                self._fetchers,
                timeout=refresh.timeout,
            )
        return []

    async def _refresh_and_merge(self) -> int:
        """Fetch and merge new proxies, firing refresh hooks around the cycle.

        Failures during fetch are logged but never re-raised; the function
        treats them as "no proxies returned".

        Returns:
            int: Number of new proxies actually added to the pool.

        Version:
            Added in 4.0.0.
        """
        refresh_hooks: list[tuple[str, tuple]] = []
        if self._config.hooks.on_refresh_started:
            refresh_hooks.append(("on_refresh_started", ()))
        await run_deferred(refresh_hooks, self._config.hooks)

        try:
            new_proxies = await self._fetch_new_proxies()
        except Exception:
            logger.exception("Refresh failed")
            new_proxies = []

        added = 0
        evict_hooks: list[tuple[str, tuple]] = []
        if new_proxies:
            async with self._state_lock:
                added, evict_hooks = self._merge_new_proxies(new_proxies)
                if added:
                    self._available_cond.notify_all()

        if evict_hooks:
            await run_deferred(evict_hooks, self._config.hooks)

        if self._config.hooks.on_refresh_completed:
            await run_deferred(
                [("on_refresh_completed", (added,))],
                self._config.hooks,
            )
        self._refresh_generation += 1
        return added

    def _merge_new_proxies(self, proxies: list[Proxy]) -> tuple[int, list[tuple[str, tuple]]]:
        """Append unseen proxies and evict from the front when ``max_size`` is exceeded.

        Args:
            proxies (list[Proxy]): Freshly fetched proxies.

        Returns:
            tuple[int, list[tuple[str, tuple]]]: ``(added_count, eviction_hooks)``;
            ``added_count`` is the number of brand-new URLs and
            ``eviction_hooks`` is a hook-invocation buffer for any proxies
            that were evicted to honour ``max_size``.

        Version:
            Added in 4.0.0.
        """
        existing_urls = {p.url for p in self._proxies}
        added = 0
        evict_deferred: list[tuple[str, tuple]] = []
        for proxy in proxies:
            if proxy.url not in existing_urls:
                self._proxies.append(proxy)
                existing_urls.add(proxy.url)
                added += 1
        if self._config.max_size and len(self._proxies) > self._config.max_size:
            while len(self._proxies) > self._config.max_size:
                evicted = self._proxies.popleft()
                self._evict_proxy(evicted, evict_deferred, reason="max_size")
        self._check_min_size()
        return added, evict_deferred

    def _has_refresh_source(self) -> bool:
        """Return ``True`` when fetchers or refresh callbacks can refill the pool.

        Returns:
            bool: Whether an on-demand or periodic refresh can fetch proxies.

        Version:
            Added in 4.0.0.
        """
        refresh = self._config.refresh
        if (
            refresh.async_callback
            or refresh.sync_callback
            or refresh.fallback_async_callbacks
            or refresh.fallback_sync_callbacks
        ):
            return True
        return bool(self._fetchers)

    def _check_min_size(self) -> None:
        """Log and flag refresh when the pool drops below ``min_size``.

        Must be called under ``_state_lock``.

        Returns:
            None

        Version:
            Added in 4.0.0.
        """
        min_size = self._config.min_size
        if min_size is None or len(self._proxies) >= min_size:
            return
        logger.warning(
            "Pool size %d below min_size %d",
            len(self._proxies),
            min_size,
        )
        if self._has_refresh_source():
            self._refresh_needed = True

    async def _health_check_loop(self) -> None:
        """Background coroutine that periodically health-checks live proxies.

        Sleeps for ``health_check.check_interval`` (default 60s) between
        cycles. Each cycle gathers the in-flight proxies, runs them through
        the configured health check bounded by ``self._health_sem``, and
        applies results under the state lock. Per-cycle and per-result
        failures are logged and swallowed so one bad proxy or apply error
        cannot terminate the loop.

        Returns:
            None

        Version:
            Added in 4.0.0.
        """
        from .extended_proxy import arun_health_check

        hc = self._config.health_check
        assert hc is not None
        interval = hc.check_interval if hc.check_interval is not None else 60.0
        await self._ready.wait()
        while not self._closed:
            await asyncio.sleep(interval)
            if self._closed:
                break
            try:
                now = time.monotonic()
                async with self._state_lock:
                    candidates = [
                        p
                        for p in self._proxies
                        if not is_in_cooldown(p.url, self._cooldown_until, now)
                    ]
                if not candidates:
                    continue

                sem = self._health_sem

                async def bounded_check(p: Proxy):
                    async with sem:
                        # arun_health_check returns (Proxy, CheckResult)
                        return await arun_health_check(p, hc)

                results = await asyncio.gather(
                    *(bounded_check(p) for p in candidates),
                    return_exceptions=True,
                )
                deferred: list[tuple[str, tuple]] = []
                async with self._state_lock:
                    for item in results:
                        if isinstance(item, BaseException):
                            logger.warning("Health check task failed: %s", item)
                            continue
                        proxy, check_result = item
                        try:
                            self._apply_check_result(proxy, check_result, deferred)
                        except Exception:
                            logger.exception(
                                "Failed to apply health-check result for %s",
                                getattr(proxy, "url", proxy),
                            )
                await run_deferred(deferred, self._config.hooks)
            except Exception:
                logger.exception("Health check cycle failed")

    async def _dead_letter_retrier(self) -> None:
        """Background coroutine: periodically retry dead-letter entries.

        Returns:
            None

        Version:
            Added in 4.0.0.
        """
        from .dead_letter import retry_cycle
        from .extended_proxy import arun_health_check

        await self._ready.wait()
        if self._closed:
            return
        await retry_cycle(
            self,
            self._dead_letter_queue,
            arun_health_check,
            self._state_lock,
            self._config.dead_letter,
        )

    async def _refresh_loop(self) -> None:
        """Background coroutine: refresh the pool on a fixed interval.

        Returns:
            None

        Version:
            Added in 4.0.0.
        """
        interval = self._config.refresh.interval_seconds
        await self._ready.wait()
        while not self._closed:
            async with self._state_lock:
                urgent = self._refresh_needed
                if urgent:
                    self._refresh_needed = False
            if not urgent:
                await asyncio.sleep(interval)
            if self._closed:
                break
            async with self._state_lock:
                pool_size = len(self._proxies)
                below_min = (
                    self._config.min_size is not None
                    and pool_size < self._config.min_size
                )
            if below_min and not self._has_refresh_source():
                logger.warning(
                    "Pool size %d below min_size %d but no refresh source configured",
                    pool_size,
                    self._config.min_size,
                )
            async with self._refresh_lock:
                try:
                    await self._refresh_and_merge()
                except Exception:
                    logger.exception("Background refresh cycle failed")

    async def _metrics_worker(self) -> None:
        """Background coroutine: drain queued metrics into the exporter.

        Exceptions raised by the exporter are logged and swallowed so a
        misbehaving exporter cannot affect the rest of the pool.

        Returns:
            None

        Version:
            Added in 4.0.0.
        """
        while not self._closed:
            name, value, tags = await self._metrics_queue.get()
            try:
                self._config.metrics_exporter.emit_gauge(name, value, tags)
            except Exception:
                logger.exception("Metrics emission failed")

    def _emit_stat_metric(
        self,
        name: str,
        value: float,
        tags: dict[str, str] | None = None,
    ) -> None:
        """Push a stat metric to the queue when an exporter is configured.

        Args:
            name (str): Metric name.
            value (float): Sample value.
            tags (dict[str, str] | None): Optional label mapping.

        Returns:
            None

        Version:
            Added in 4.0.0.
        """
        if self._config.metrics_exporter:
            self._enqueue_metric(name, value, tags)

    def _enqueue_metric(self, name: str, value: float, tags: dict[str, str] | None = None) -> None:
        """Non-blocking enqueue helper for the metrics worker.

        Drops the sample when the queue is full (logged at DEBUG).

        Args:
            name (str): Metric name.
            value (float): Sample value.
            tags (dict[str, str] | None): Optional label mapping.

        Returns:
            None

        Version:
            Added in 4.0.0.
        """
        try:
            self._metrics_queue.put_nowait((name, value, tags))
        except asyncio.QueueFull:
            logger.debug("Metrics queue full, dropping metric")

    @property
    def statistics(self) -> PoolStatistics:
        """Frozen snapshot of pool observability counters.

        Returns a :class:`PoolStatistics` instance that cannot be mutated.
        Callers that need a later reading should re-read this property.

        Returns:
            PoolStatistics: Immutable counter snapshot.

        Version:
            Added in 4.0.0.
        """
        return self._statistics


class SyncProxyPool:
    """Blocking wrapper around :class:`AsyncProxyPool`.

    Runs an internal event loop on a daemon thread so synchronous code can
    interact with the async pool using the same API surface. Created
    instances must be used as a context manager (or closed explicitly with
    :meth:`close`) so the loop thread is shut down cleanly::

        with SyncProxyPool(config, fetchers=[...]) as pool:
            proxy = pool.acquire()

    Version:
        Added in 4.0.0.
    """

    def __init__(
        self,
        config: PoolConfig,
        initial_proxies: list[Proxy] = (),
        fetchers: list[ProxyFetcher] | None = None,
    ) -> None:
        """Construct the underlying async pool and spin up the loop thread.

        Args:
            config (PoolConfig): Pool configuration.
            initial_proxies (list[Proxy]): Seed proxies.
            fetchers (list[ProxyFetcher] | None): Optional fetchers.

        Raises:
            Exception: Any exception raised while entering the async pool
                is propagated after tearing down the loop thread.

        Version:
            Added in 4.0.0.
        """
        self._async_pool = AsyncProxyPool(config, initial_proxies, fetchers)
        self._loop = asyncio.new_event_loop()
        self._shutdown = False
        self._thread = threading.Thread(
            target=self._daemon_loop_runner,
            daemon=True,
            name="omniproxy-SyncProxyPool-loop",
        )
        self._thread.start()
        try:
            asyncio.run_coroutine_threadsafe(self._async_pool.__aenter__(), self._loop).result()
        except Exception:
            self._shutdown = True
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5.0)
            raise

    def _daemon_loop_runner(self) -> None:
        """Run the dedicated asyncio loop until ``loop.stop`` is requested.

        Returns:
            None

        Version:
            Added in 4.0.0.
        """
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        finally:
            if not self._loop.is_closed():
                self._loop.close()

    def __enter__(self) -> SyncProxyPool:
        """Enter the context manager.

        Returns:
            SyncProxyPool: ``self``; the underlying async pool is already
            started by :meth:`__init__`.

        Version:
            Added in 4.0.0.
        """
        return self

    def __exit__(self, *args: Any) -> None:
        """Tear down the underlying loop thread.

        Args:
            *args (Any): Exception triple from the ``with`` block (unused).

        Returns:
            None

        Version:
            Added in 4.0.0.
        """
        self.close()

    def __del__(self) -> None:
        """Best-effort cleanup when the pool is garbage-collected without closing."""
        try:
            if not getattr(self, "_shutdown", True):
                self.close()
        except Exception:
            pass

    def close(self) -> None:
        """Drain the async pool and stop the loop thread.

        Idempotent. The loop thread is given five seconds to terminate;
        failure to do so emits a WARNING log.

        Returns:
            None

        Version:
            Added in 4.0.0.
        """
        if self._shutdown:
            return
        self._shutdown = True
        try:
            asyncio.run_coroutine_threadsafe(
                self._async_pool.__aexit__(None, None, None),
                self._loop,
            ).result()
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5.0)
            if self._thread.is_alive():
                logger.warning("SyncProxyPool loop thread did not stop within 5s")

    def _run_on_loop(self, coro):
        """Schedule ``coro`` on the daemon loop and block for the result.

        Args:
            coro: Awaitable to run on the loop thread.

        Returns:
            Any: Whatever ``coro`` resolves to.

        Version:
            Added in 4.0.0.
        """
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    def acquire(self, **filters: Any) -> Proxy:
        """Synchronous wrapper for :meth:`AsyncProxyPool.acquire`.

        Args:
            **filters (Any): Acquire-time filters; same semantics as the
                async variant.

        Returns:
            Proxy: Acquired proxy.

        Raises:
            Same exceptions as :meth:`AsyncProxyPool.acquire`.

        Version:
            Added in 4.0.0.
        """
        return self._run_on_loop(self._async_pool.acquire(**filters))

    def release(self, proxy: Proxy) -> None:
        """Synchronous wrapper for :meth:`AsyncProxyPool.release`.

        Args:
            proxy (Proxy): Proxy returned by :meth:`acquire`.

        Returns:
            None

        Version:
            Added in 4.0.0.
        """
        self._run_on_loop(self._async_pool.release(proxy))

    def mark_failed(self, proxy: Proxy, exc: type | None = None) -> None:
        """Synchronous wrapper for :meth:`AsyncProxyPool.mark_failed`.

        Args:
            proxy (Proxy): Proxy that failed.
            exc (type | None): Optional exception class.

        Returns:
            None

        Version:
            Added in 4.0.0.
        """
        self._run_on_loop(self._async_pool.mark_failed(proxy, exc))

    def mark_success(self, proxy: Proxy, latency: float | None = None) -> None:
        """Synchronous wrapper for :meth:`AsyncProxyPool.mark_success`.

        Args:
            proxy (Proxy): Proxy that succeeded.
            latency (float | None): Observed latency in seconds.

        Returns:
            None

        Version:
            Added in 4.0.0.
        """
        self._run_on_loop(self._async_pool.mark_success(proxy, latency))


__all__ = ["AcquireOptions", "AsyncProxyPool", "PoolStatistics", "SyncProxyPool"]
