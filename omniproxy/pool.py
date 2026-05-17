"""Async and synchronous proxy pool implementations."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

from .config import PoolConfig, HealthCheckConfig
from .enum import FilterMissingMetadata, PoolStrategy, SessionCooldownPolicy, WarmupFailurePolicy
from .errors import (
    MissingProxyMetadata,
    PoolCircuitOpenError,
    PoolClosedError,
    PoolDrainingError,
    PoolExhausted,
    SessionBrokenError,
)
from .proxy import Proxy
from .cooldown import compute_cooldown, is_in_cooldown
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
    """Transient filter options for :meth:`AsyncProxyPool.acquire`."""
    tags: set[str] | None = None
    country: str | None = None
    min_anonymity: str | None = None
    session_key: str | None = None
    accept_callback: Any = None
    # additional fields can be added as needed

    @classmethod
    def from_kwargs(cls, **filters: Any) -> AcquireOptions:
        if "session_key" not in filters and "session_id" in filters:
            filters = dict(filters)
            filters["session_key"] = filters.pop("session_id")
        return cls(**{k: v for k, v in filters.items() if k in cls.__slots__})


@dataclass
class PoolStatistics:
    """Simple counters for observability."""
    served: int = 0
    failed: int = 0
    released: int = 0
    exhausted_count: int = 0


class AsyncProxyPool:
    """Asynchronous, thread‑safe proxy pool with health, scoring, circuit breaker.

    Must be used as an async context manager:

        async with AsyncProxyPool(config, fetchers=[...]) as pool:
            proxy = await pool.acquire()
            ...
    """

    def __init__(
        self,
        config: PoolConfig,
        initial_proxies: list[Proxy] = (),
        fetchers: list[ProxyFetcher] | None = None,
    ) -> None:
        self._config = config
        self._fetchers = fetchers or []
        self._state_lock = asyncio.Lock()
        self._available_cond = asyncio.Condition(self._state_lock)
        self._refresh_lock = asyncio.Lock()
        self._ready = asyncio.Event()
        self._draining = asyncio.Event()
        self._closed = False
        # state containers (all updated under _state_lock)
        self._proxies: deque[Proxy] = deque()
        self._cooldown_until: dict[str, float] = {}
        self._scores: dict[str, EMAState] = {}
        self._connections: dict[str, int] = {}
        self._token_buckets: dict[str, Any] = {}          # per‑proxy rate limiter objects
        self._session_registry: dict[str, SessionEntry] = {}
        self._dead_letter_queue: list[Any] = []
        self._failure_window: deque[float] = deque()
        self._circuit_breaker = CircuitBreaker(
            config.circuit_breaker if config.circuit_breaker else None
        ) if config.circuit_breaker else None
        self._statistics = PoolStatistics()
        self._metrics_queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._background_tasks: set[asyncio.Task] = set()

        # Strategy instance
        self._strategy: SelectionStrategy = self._build_strategy(config.strategy)
        # internal state for strategies that need it (like round‑robin index)
        self._strategy_state: Any = None

        # Warm‑up semaphore for health checks
        if config.health_check:
            self._health_sem = asyncio.Semaphore(
                getattr(config.health_check, "max_concurrent_checks", 50)
            )
        else:
            self._health_sem = asyncio.Semaphore(50)

        # Seed initial proxies
        for p in initial_proxies:
            if not isinstance(p, Proxy):
                p = Proxy(p)
            self._proxies.append(p)

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------
    async def __aenter__(self) -> AsyncProxyPool:
        await self._start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self._close()

    async def _start(self) -> None:
        # Background tasks
        self._bg_health = asyncio.create_task(self._health_check_loop())
        self._bg_dead = (
            asyncio.create_task(self._dead_letter_retrier())
            if self._config.dead_letter.enabled
            else None
        )
        self._bg_refresh = asyncio.create_task(self._refresh_loop()) if self._config.refresh else None
        self._bg_metrics = asyncio.create_task(self._metrics_worker()) if self._config.metrics_exporter else None

        # Warm‑up if enabled
        if self._config.warmup.enabled:
            from .warmup import run_warmup
            from .extended_proxy import arun_health_check
            ok = await run_warmup(self, self._config.warmup, arun_health_check)
            if not ok:
                if self._config.warmup.failure_policy == WarmupFailurePolicy.RAISE:
                    raise RuntimeError("Warm‑up failed")
        self._ready.set()

    async def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Cancel background tasks
        for task in [self._bg_health, self._bg_dead, self._bg_refresh, self._bg_metrics]:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        # Drain if configured (optional)
        if self._config.drain_timeout > 0:
            await asyncio.sleep(self._config.drain_timeout)

        # Final state snapshot can be persisted here via state_store_factory

    async def close(self) -> None:
        """Shut down background workers (idempotent)."""
        await self._close()

    # ------------------------------------------------------------------
    # Strategy helper
    # ------------------------------------------------------------------
    @staticmethod
    def _build_strategy(strategy: PoolStrategy) -> SelectionStrategy:
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

    # ------------------------------------------------------------------
    # Acquisition (TOCTOU‑free, atomic)
    # ------------------------------------------------------------------
    async def acquire(self, **filters: Any) -> Proxy:
        options = AcquireOptions.from_kwargs(**filters)
        deferred: list[tuple[str, tuple]] = []
        start_time = asyncio.get_event_loop().time()
        timeout = self._config.acquire_timeout

        while True:
            async with self._state_lock:
                self._check_availability()
                proxy = self._select(options)
                if proxy is not None:
                    self._mark_acquired(proxy, options.session_key)
                    deferred.append(("on_proxy_acquired", (proxy,)))
                    break

                # No proxy available – wait for release or timeout
                if timeout > 0:
                    remaining = timeout - (asyncio.get_event_loop().time() - start_time)
                    if remaining <= 0:
                        # fall through to exception
                        pass
                    else:
                        try:
                            await asyncio.wait_for(self._available_cond.wait(), remaining)
                            continue
                        except asyncio.TimeoutError:
                            pass
                # No timeout or timed out – raise after optional refresh
                refreshed = await self._attempt_on_demand_refresh(options)
                if refreshed:
                    continue
                # No proxy and no refresh → raise
                if self._missing_metadata_raises(options):
                    raise MissingProxyMetadata(...)
                self._statistics.exhausted_count += 1
                if self._config.hooks.on_exhausted:
                    deferred.append(("on_exhausted", ()))
                await run_deferred(deferred, self._config.hooks)
                raise PoolExhausted("No proxies available")

        # Acquired a proxy – continu

        if proxy is not None:
            if self._config.rotate_on_acquire and proxy.rotation_url:
                await proxy.arotate()
            await run_deferred(deferred, self._config.hooks)
            return proxy

        # No proxy → attempt on‑demand refresh (avoids thundering herd)
        refreshed = await self._attempt_on_demand_refresh(options)
        if refreshed:
            async with self._state_lock:
                self._check_availability()
                proxy = self._select(options)
                if proxy is not None:
                    self._mark_acquired(proxy, options.session_key)
                    deferred.append(("on_proxy_acquired", (proxy,)))

        if proxy is None:
            async with self._state_lock:
                if self._missing_metadata_raises(options):
                    raise MissingProxyMetadata(
                        f"No usable proxy declares metadata for filter "
                        f"{options.country!r} (pool filter_missing_metadata=RAISE)"
                    )
            self._statistics.exhausted_count += 1
            if self._config.hooks.on_exhausted:
                deferred.append(("on_exhausted", ()))
            await run_deferred(deferred, self._config.hooks)
            raise PoolExhausted("No proxies available")

        if self._config.rotate_on_acquire and proxy.rotation_url:
            await proxy.arotate()
        await run_deferred(deferred, self._config.hooks)
        return proxy

    async def release(self, proxy: Proxy) -> None:
        async with self._state_lock:
            self._connections[proxy.url] = max(0, self._connections.get(proxy.url, 0) - 1)
            self._available_cond.notify()
            # No scoring change on release; just decrement connection
        if self._config.hooks.on_proxy_released:
            await run_deferred([("on_proxy_released", (proxy,))], self._config.hooks)

    async def mark_failed(self, proxy: Proxy, exc: type | None = None) -> None:
        """Report that *proxy* failed (client‑side)."""
        deferred = []
        async with self._state_lock:
            self._connections[proxy.url] = max(0, self._connections.get(proxy.url, 0) - 1)
            # Scoring
            state = self._scores.get(proxy.url)
            if state is None and self._config.scoring:
                state = EMAState()
                self._scores[proxy.url] = state
            if state:
                update_ema(state, success=False, latency=None, decay=self._config.scoring.decay_factor)
            # Cooldown
            self._apply_cooldown(proxy, exc)
            # Circuit breaker
            if self._circuit_breaker:
                self._circuit_breaker.record_failure()
            deferred.append(("on_proxy_failed", (proxy, exc)))

        await run_deferred(deferred, self._config.hooks)

    # ------------------------------------------------------------------
    # Internal helpers (all called under _state_lock)
    # ------------------------------------------------------------------
    def _check_availability(self) -> None:
        if self._closed:
            raise PoolClosedError("Pool is closed")
        if self._draining.is_set():
            raise PoolDrainingError("Pool is draining")
        if self._circuit_breaker and not self._circuit_breaker.allow_request():
            if self._circuit_breaker.state == CircuitBreakerState.HALF_OPEN:
                # Check if probe already in flight; allow_request already handled that.
                raise PoolCircuitOpenError("Circuit breaker open (HALF_OPEN probe in progress)")
            raise PoolCircuitOpenError("Circuit breaker open")

    def _select(self, options: AcquireOptions) -> Proxy | None:
        """Apply eligibility filters, then strategy. Must hold _state_lock."""
        eligible = self._get_eligible(options)
        if not eligible:
            return None
        return self._strategy.select(eligible, self._scores, self._strategy_state)

    def _missing_metadata_raises(self, options: AcquireOptions) -> bool:
        if self._config.filter_missing_metadata != FilterMissingMetadata.RAISE:
            return False
        if not options.country:
            return False
        if any(getattr(p, "country", None) == options.country for p in self._proxies):
            return False
        return any(getattr(p, "country", None) in (None, "") for p in self._proxies)

    def _at_connection_cap(self, proxy: Proxy) -> bool:
        lim = self._config.limits.max_connections_per_proxy
        return bool(lim is not None and lim > 0 and self._connections.get(proxy.url, 0) >= lim)

    def _sticky_filters_ok(self, proxy: Proxy, options: AcquireOptions) -> bool:
        if options.tags and not (options.tags & set(getattr(proxy, "tags", []))):
            return False
        if options.country and proxy.country != options.country:
            return False
        if options.min_anonymity and proxy.anonymity != options.min_anonymity:
            return False
        if options.accept_callback and not options.accept_callback(proxy):
            return False
        return True

    def _get_eligible(self, options: AcquireOptions) -> list[Proxy]:
        """Return proxies that pass all configured filters."""
        now = time.monotonic()

        if options.session_key:
            bound = resolve_session(
                options.session_key,
                self._session_registry,
                list(self._proxies),
                self._config.session,
                now,
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
                    self._session_registry.pop(options.session_key, None)
                else:
                    if self._at_connection_cap(bound):
                        return []
                    if not self._sticky_filters_ok(bound, options):
                        return []
                    return [bound]

        result: list[Proxy] = []
        for proxy in self._proxies:
            if is_in_cooldown(proxy.url, self._cooldown_until, now):
                continue
            if self._at_connection_cap(proxy):
                continue
            if options.tags and not (options.tags & set(getattr(proxy, "tags", []))):
                continue
            if options.country and proxy.country != options.country:
                continue
            if options.min_anonymity and proxy.anonymity != options.min_anonymity:
                continue
            if options.accept_callback and not options.accept_callback(proxy):
                continue
            result.append(proxy)
        return result

    def _mark_acquired(self, proxy: Proxy, session_key: str | None = None) -> None:
        """Increment connections and register bound session keys."""
        self._connections[proxy.url] = self._connections.get(proxy.url, 0) + 1
        self._statistics.served += 1
        if session_key:
            self._session_registry[session_key] = SessionEntry(
                proxy_id=proxy.url,
                expires_at=time.monotonic() + self._config.session.ttl,
            )

    def _apply_cooldown(self, proxy: Proxy, exc: type | None = None) -> None:
        """Place *proxy* into cooldown with computed duration."""
        cfg = self._config.cooldown
        failures = self._count_consecutive_failures(proxy)
        total_proxies = len(self._proxies)
        if cfg.strategy is not None:
            raw = cfg.strategy(float(cfg.base), int(failures), int(total_proxies))
            dur = max(cfg.min, min(cfg.max, float(raw)))
        else:
            dur = compute_cooldown(
                cfg.base,
                cfg.adaptive,
                failures,
                cfg.penalties,
                exc,
                _min=cfg.min,
                _max=cfg.max,
            )
        self._cooldown_until[proxy.url] = time.monotonic() + dur

    def _apply_check_result(self, proxy: Proxy, result: CheckResult, deferred: list) -> None:
        """Process health check outcome; called under lock."""
        if result.success:
            self._scores[proxy.url] = update_ema(
                self._scores.get(proxy.url, EMAState()),
                success=True,
                latency=result.latency,
                decay=self._config.scoring.decay_factor if self._config.scoring else 0.9,
            )
            if self._circuit_breaker:
                self._circuit_breaker.record_success()
            # Remove cooldown if successful
            self._cooldown_until.pop(proxy.url, None)
            deferred.append(("on_proxy_recovered", (proxy,)))
        else:
            self._scores[proxy.url] = update_ema(
                self._scores.get(proxy.url, EMAState()),
                success=False,
                latency=result.latency,
                decay=self._config.scoring.decay_factor if self._config.scoring else 0.9,
            )
            self._apply_cooldown(proxy, result.exc_type)
            if self._circuit_breaker:
                self._circuit_breaker.record_failure()
            deferred.append(("on_proxy_failed", (proxy, result.exc_type)))

    def _count_consecutive_failures(self, proxy: Proxy) -> int:
        # Placeholder; in a full implementation track consecutive failures per proxy
        return 1

    # ------------------------------------------------------------------
    # On‑demand refresh with dedicated lock
    # ------------------------------------------------------------------
    async def _attempt_on_demand_refresh(self, options: AcquireOptions) -> bool:
        if self._refresh_lock.locked():
            return False
        async with self._refresh_lock:
            new_proxies = await self._fetch_new_proxies()
            if new_proxies:
                async with self._state_lock:
                    self._merge_new_proxies(new_proxies)
            return bool(new_proxies)

    async def _fetch_new_proxies(self) -> list[Proxy]:
        from .refresh import fetch_from_fetchers, fetch_from_refresh_config
        if self._config.refresh and (self._config.refresh.async_callback or self._config.refresh.sync_callback):
            return await fetch_from_refresh_config(self._config.refresh)
        if self._fetchers:
            return await fetch_from_fetchers(self._fetchers)
        return []

    def _merge_new_proxies(self, proxies: list[Proxy]) -> None:
        existing_urls = {p.url for p in self._proxies}
        for proxy in proxies:
            if proxy.url not in existing_urls:
                self._proxies.append(proxy)
                existing_urls.add(proxy.url)
        # Trim if max_size set
        if self._config.max_size and len(self._proxies) > self._config.max_size:
            # remove worst by score or just oldest
            while len(self._proxies) > self._config.max_size:
                self._proxies.popleft()

    # ------------------------------------------------------------------
    # Background loops
    # ------------------------------------------------------------------
    async def _health_check_loop(self) -> None:
        from .extended_proxy import arun_health_check
        hc = self._config.health_check
        if hc is None:
            while not self._closed:
                await asyncio.sleep(60.0)
            return

        interval = hc.check_interval if hc.check_interval is not None else 60.0
        while not self._closed:
            await asyncio.sleep(interval)
            async with self._state_lock:
                candidates = [p for p in self._proxies if not is_in_cooldown(p.url, self._cooldown_until)]
            if not candidates:
                continue
            # Limit concurrency
            sem = self._health_sem
            async def bounded_check(p):
                async with sem:
                    return await arun_health_check(p, hc)
            tasks = [bounded_check(p) for p in candidates]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            deferred = []
            async with self._state_lock:
                for item in results:
                    if isinstance(item, BaseException):
                        logger.warning("Health check task failed: %s", item)
                        continue
                    proxy, check_result = item
                    self._apply_check_result(proxy, check_result, deferred)
            await run_deferred(deferred, self._config.hooks)

    async def _dead_letter_retrier(self) -> None:
        from .dead_letter import retry_cycle
        from .extended_proxy import arun_health_check
        await retry_cycle(
            self,
            self._dead_letter_queue,
            arun_health_check,
            self._state_lock,
            self._config.dead_letter,
        )

    async def _refresh_loop(self) -> None:
        interval = 300  # default; could be configurable
        while not self._closed:
            await asyncio.sleep(interval)
            new_proxies = await self._fetch_new_proxies()
            if new_proxies:
                async with self._state_lock:
                    self._merge_new_proxies(new_proxies)

    async def _metrics_worker(self) -> None:
        while not self._closed:
            name, value, tags = await self._metrics_queue.get()
            try:
                self._config.metrics_exporter.emit_gauge(name, value, tags)
            except Exception:
                logger.exception("Metrics emission failed")

    def _enqueue_metric(self, name: str, value: float, tags: dict[str, str] | None = None) -> None:
        try:
            self._metrics_queue.put_nowait((name, value, tags))
        except asyncio.QueueFull:
            logger.debug("Metrics queue full, dropping metric")

    async def mark_success(self, proxy: Proxy, latency: float | None = None) -> None:
        """Report that *proxy* succeeded (used for scoring and cooldown removal)."""
        async with self._state_lock:
            state = self._scores.get(proxy.url)
            if state is None and self._config.scoring:
                from .scoring import EMAState
                state = EMAState()
                self._scores[proxy.url] = state
            if state and self._config.scoring:
                from .scoring import update_ema
                update_ema(state, success=True, latency=latency, decay=self._config.scoring.decay_factor)
            self._cooldown_until.pop(proxy.url, None)
            if self._circuit_breaker:
                self._circuit_breaker.record_success()

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------
    @property
    def statistics(self) -> PoolStatistics:
        return self._statistics


class SyncProxyPool:
    """Blocking wrapper around :class:`AsyncProxyPool`.

    Usage::

        with SyncProxyPool(config, fetchers=[...]) as pool:
            proxy = pool.acquire()
    """

    def __init__(
        self,
        config: PoolConfig,
        initial_proxies: list[Proxy] = (),
        fetchers: list[ProxyFetcher] | None = None,
    ) -> None:
        self._async_pool = AsyncProxyPool(config, initial_proxies, fetchers)
        self._loop = asyncio.new_event_loop()
        self._shutdown = False
        self._thread = threading.Thread(
            target=self._daemon_loop_runner,
            daemon=True,
            name="omniproxy-SyncProxyPool-loop",
        )
        self._thread.start()
        asyncio.run_coroutine_threadsafe(self._async_pool.__aenter__(), self._loop).result()

    def _daemon_loop_runner(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        finally:
            if not self._loop.is_closed():
                self._loop.close()

    def __enter__(self) -> SyncProxyPool:
        return self

    def __exit__(self, *args: Any) -> None:
        # Pool stays open for further acquire() calls; use :meth:`close` for teardown.
        return None

    def close(self) -> None:
        if self._shutdown:
            return
        self._shutdown = True
        asyncio.run_coroutine_threadsafe(
            self._async_pool.__aexit__(None, None, None),
            self._loop,
        ).result()
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5.0)

    def acquire(self, **filters: Any) -> Proxy:
        return asyncio.run_coroutine_threadsafe(
            self._async_pool.acquire(**filters), self._loop
        ).result()

    def release(self, proxy: Proxy) -> None:
        return asyncio.run_coroutine_threadsafe(
            self._async_pool.release(proxy), self._loop
        ).result()

    def mark_failed(self, proxy: Proxy, exc: type | None = None) -> None:
        return asyncio.run_coroutine_threadsafe(
            self._async_pool.mark_failed(proxy, exc), self._loop
        ).result()

    def mark_success(self, proxy: Proxy, latency: float | None = None) -> None:
        return asyncio.run_coroutine_threadsafe(
            self._async_pool.mark_success(proxy, latency), self._loop
        ).result()

__all__ = ["AcquireOptions", "AsyncProxyPool", "PoolStatistics", "SyncProxyPool"]