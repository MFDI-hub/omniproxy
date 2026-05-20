"""Async and synchronous proxy pool implementations."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, fields
from typing import TYPE_CHECKING, Any

from .config import PoolConfig
from .constants import ANONYMITY_RANKS
from .enum import FilterMissingMetadata, PoolStrategy, SessionCooldownPolicy, WarmupFailurePolicy
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

    @classmethod
    def from_kwargs(cls, config: PoolConfig, **filters: Any) -> AcquireOptions:
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


@dataclass
class PoolStatistics:
    """Simple counters for observability."""
    served: int = 0
    failed: int = 0
    released: int = 0
    exhausted_count: int = 0


class AsyncProxyPool:
    """Asynchronous, thread‑safe proxy pool with health, scoring, circuit breaker.

    Must be used as an async context manager::

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
        self._config: PoolConfig = config
        self._fetchers = fetchers or []
        self._state_lock = asyncio.Lock()
        self._available_cond = asyncio.Condition(self._state_lock)
        self._refresh_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
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
        self._dead_letter_queue: list[Any] = []
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
        await self._start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self._close()

    async def _start(self) -> None:
        if self._closed:
            raise PoolClosedError("Pool is closed")
        if self._ready.is_set():
            return
        await self._stop_background_tasks()
        try:
            if self._config.health_check:
                self._bg_health = asyncio.create_task(self._health_check_loop())
            self._bg_dead = (
                asyncio.create_task(self._dead_letter_retrier())
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
                asyncio.create_task(self._refresh_loop()) if has_refresh else None
            )
            self._bg_metrics = (
                asyncio.create_task(self._metrics_worker())
                if self._config.metrics_exporter
                else None
            )

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
                if not ok and self._config.warmup.failure_policy == WarmupFailurePolicy.RAISE:
                    raise WarmupFailedError(
                        f"Warmup failed: fewer than {self._config.warmup.min_ready} "
                        f"proxies ready within {self._config.warmup.timeout}s"
                    )
            self._ready.set()
        except Exception:
            await self._close()
            raise

    async def _stop_background_tasks(self) -> None:
        tasks = [
            t
            for t in [self._bg_health, self._bg_dead, self._bg_refresh, self._bg_metrics]
            if t is not None and not t.done()
        ]
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._bg_health = None
        self._bg_dead = None
        self._bg_refresh = None
        self._bg_metrics = None

    async def _close(self) -> None:
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
            async with self._state_lock:
                self._available_cond.notify_all()
            await self._stop_background_tasks()

    async def close(self) -> None:
        """Shut down background workers (idempotent)."""
        await self._close()

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

    def _append_circuit_hooks(self, deferred: list[tuple[str, tuple]]) -> None:
        if not self._circuit_breaker:
            return
        for transition in self._circuit_breaker.drain_pending_transitions():
            if transition == "open" and self._config.hooks.on_circuit_open:
                deferred.append(("on_circuit_open", ()))
            elif transition == "close" and self._config.hooks.on_circuit_close:
                deferred.append(("on_circuit_close", ()))

    def _bounded_wait_timeout(self, remaining: float | None) -> float:
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
        wait_time = self._bounded_wait_timeout(remaining)
        try:
            await asyncio.wait_for(self._available_cond.wait(), wait_time)
        except asyncio.TimeoutError:
            pass

    def _return_lease(self, proxy: Proxy, *, count_release_stat: bool) -> None:
        """Idempotently release one acquire lease for *proxy*."""
        current = self._connections.get(proxy.url, 0)
        if current <= 0:
            return
        self._connections[proxy.url] = current - 1
        if count_release_stat:
            self._statistics.released += 1
        self._available_cond.notify_all()

    async def acquire(self, **filters: Any) -> Proxy:
        await self._ready.wait()
        options = AcquireOptions.from_kwargs(self._config, **filters)
        deferred: list[tuple[str, tuple]] = []
        loop = asyncio.get_running_loop()
        timeout = self._config.acquire_timeout
        start_time = loop.time()
        proxy: Proxy | None = None

        while True:
            should_refresh = False
            exhausted_hooks: list[tuple[str, tuple]] = []
            missing_metadata_msg: str | None = None

            async with self._state_lock:
                self._check_availability()
                self._append_circuit_hooks(deferred)
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

                missing_metadata_msg = self._missing_metadata_message(options)
                if missing_metadata_msg is None:
                    should_refresh = True
                    if self._config.hooks.on_exhausted:
                        exhausted_hooks.append(("on_exhausted", ()))

            if missing_metadata_msg is not None:
                raise MissingProxyMetadata(missing_metadata_msg)

            if should_refresh:
                refreshed = await self._attempt_on_demand_refresh(options)
                if refreshed:
                    continue

            async with self._state_lock:
                self._statistics.exhausted_count += 1
                self._emit_stat_metric("pool.exhausted", float(self._statistics.exhausted_count))
                exc = self._classify_acquire_failure(options)
                if isinstance(exc, PoolSaturated) and self._config.hooks.on_saturated:
                    deferred.append(("on_saturated", ()))
            deferred.extend(exhausted_hooks)
            await run_deferred(deferred, self._config.hooks)
            raise exc

        if self._config.rotate_on_acquire and proxy.rotation_url:
            await proxy.arotate()
        await run_deferred(deferred, self._config.hooks)
        return proxy

    async def release(self, proxy: Proxy) -> None:
        async with self._state_lock:
            self._return_lease(proxy, count_release_stat=True)
            if self._half_open_probe_epoch is not None:
                self._half_open_probe_epoch = None
        if self._config.hooks.on_proxy_released:
            await run_deferred([("on_proxy_released", (proxy,))], self._config.hooks)

    async def mark_failed(self, proxy: Proxy, exc: type | None = None) -> None:
        """Report that *proxy* failed (client‑side).

        Also returns the acquire lease (same as :meth:`release`). Do not call both
        ``mark_failed`` and ``release`` for the same acquisition.
        """
        deferred: list[tuple[str, tuple]] = []
        rotate = False
        probe_epoch = self._half_open_probe_epoch
        async with self._state_lock:
            self._return_lease(proxy, count_release_stat=False)
            self._half_open_probe_epoch = None
            self._statistics.failed += 1
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
            self._apply_cooldown(proxy, exc, deferred)
            if self._circuit_breaker:
                self._circuit_breaker.record_failure(probe_epoch=probe_epoch)
                self._append_circuit_hooks(deferred)
            rotate = self._config.rotate_on_failure and bool(proxy.rotation_url)
            deferred.append(("on_proxy_failed", (proxy, exc)))

        if rotate:
            await proxy.arotate()
        await run_deferred(deferred, self._config.hooks)

    async def mark_success(self, proxy: Proxy, latency: float | None = None) -> None:
        """Report that *proxy* succeeded (used for scoring and cooldown removal)."""
        deferred: list[tuple[str, tuple]] = []
        probe_epoch = self._half_open_probe_epoch
        async with self._state_lock:
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
        """Return proxies not yet marked working (warmup helper)."""
        async with self._state_lock:
            return [p for p in self._proxies if not p.is_working]

    async def _record_health_check_result(self, proxy: Proxy, result: CheckResult) -> None:
        """Apply a health-check outcome under the pool lock (warmup helper)."""
        async with self._state_lock:
            self._apply_check_result(proxy, result, [])

    def _check_availability(self) -> None:
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
        eligible = self._get_eligible(options)
        if not eligible:
            return None
        return self._strategy.select(eligible, self._scores, self._strategy_state)

    @staticmethod
    def _anonymity_rank(label: str | None) -> int:
        if not label:
            return 0
        return ANONYMITY_RANKS.get(label.lower(), 0)

    def _metadata_value_missing(self, proxy: Proxy, attr: str) -> bool:
        value = getattr(proxy, attr, None)
        if attr == "tags":
            return not value
        return value in (None, "")

    def _active_metadata_filters(self, options: AcquireOptions) -> list[tuple[str, Any]]:
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
        return any(self._sticky_filters_ok(p, options) for p in self._proxies)

    def _at_connection_cap(self, proxy: Proxy) -> bool:
        lim = self._config.limits.max_connections_per_proxy
        return bool(lim is not None and self._connections.get(proxy.url, 0) >= lim)

    def _sticky_filters_ok(self, proxy: Proxy, options: AcquireOptions) -> bool:
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
                    self._pending_session_rebind[options.session_key] = bound
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
            if not self._sticky_filters_ok(proxy, options):
                continue
            result.append(proxy)
        return result

    def _mark_acquired(self, proxy: Proxy, session_key: str | None = None) -> None:
        self._connections[proxy.url] = self._connections.get(proxy.url, 0) + 1
        self._statistics.served += 1
        self._emit_stat_metric("pool.served", float(self._statistics.served))
        if (
            self._circuit_breaker
            and self._circuit_breaker.state == CircuitBreakerState.HALF_OPEN
        ):
            self._half_open_probe_epoch = self._circuit_breaker.active_probe_epoch
        else:
            self._half_open_probe_epoch = None
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
        cfg = self._config.cooldown
        failures = self._consecutive_failures.get(proxy.url, 0)
        if failures < cfg.failure_threshold:
            return
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
        if deferred is not None and self._config.hooks.on_proxy_cooled_down:
            deferred.append(("on_proxy_cooled_down", (proxy,)))

    def _apply_check_result(self, proxy: Proxy, result: CheckResult, deferred: list) -> None:
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
            if self._circuit_breaker:
                self._circuit_breaker.record_success()
                self._append_circuit_hooks(deferred)
            self._cooldown_until.pop(proxy.url, None)
            if self._config.hooks.on_check_complete:
                deferred.append(("on_check_complete", (proxy, result)))
            deferred.append(("on_proxy_recovered", (proxy,)))
        else:
            self._statistics.failed += 1
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
            if self._circuit_breaker:
                self._circuit_breaker.record_failure()
                self._append_circuit_hooks(deferred)
            if self._config.hooks.on_check_complete:
                deferred.append(("on_check_complete", (proxy, result)))
            deferred.append(("on_proxy_failed", (proxy, result.exc_type)))

    def _count_consecutive_failures(self, proxy: Proxy) -> int:
        return max(1, self._consecutive_failures.get(proxy.url, 0))

    def _evict_proxy(self, url: str, deferred: list[tuple[str, tuple]] | None = None) -> None:
        evicted_proxy = next((p for p in self._proxies if p.url == url), None)
        self._scores.pop(url, None)
        self._cooldown_until.pop(url, None)
        self._connections.pop(url, None)
        self._token_buckets.pop(url, None)
        self._consecutive_failures.pop(url, None)
        stale = [k for k, entry in self._session_registry.items() if entry.proxy_id == url]
        for key in stale:
            self._session_registry.pop(key, None)
        if (
            deferred is not None
            and evicted_proxy is not None
            and self._config.hooks.on_auto_evicted
        ):
            deferred.append(("on_auto_evicted", (evicted_proxy, "max_size")))

    async def _attempt_on_demand_refresh(self, options: AcquireOptions) -> bool:
        async with self._refresh_lock:
            added = await self._refresh_and_merge()
            return added > 0

    async def _fetch_new_proxies(self) -> list[Proxy]:
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
            return await fetch_from_fetchers(self._fetchers)
        return []

    async def _refresh_and_merge(self) -> int:
        """Fetch proxies and merge; return count of newly added URLs."""
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
        return added

    def _merge_new_proxies(self, proxies: list[Proxy]) -> tuple[int, list[tuple[str, tuple]]]:
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
                self._evict_proxy(evicted.url, evict_deferred)
        return added, evict_deferred

    async def _health_check_loop(self) -> None:
        from .extended_proxy import arun_health_check

        hc = self._config.health_check
        assert hc is not None
        interval = hc.check_interval if hc.check_interval is not None else 60.0
        while not self._closed:
            await asyncio.sleep(interval)
            now = time.monotonic()
            async with self._state_lock:
                candidates = [
                    p for p in self._proxies if not is_in_cooldown(p.url, self._cooldown_until, now)
                ]
            if not candidates:
                continue

            sem = self._health_sem

            async def bounded_check(p: Proxy):
                async with sem:
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
        interval = self._config.refresh.interval_seconds
        while not self._closed:
            await asyncio.sleep(interval)
            await self._refresh_and_merge()

    async def _metrics_worker(self) -> None:
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
        if self._config.metrics_exporter:
            self._enqueue_metric(name, value, tags)

    def _enqueue_metric(self, name: str, value: float, tags: dict[str, str] | None = None) -> None:
        try:
            self._metrics_queue.put_nowait((name, value, tags))
        except asyncio.QueueFull:
            logger.debug("Metrics queue full, dropping metric")

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
        try:
            asyncio.run_coroutine_threadsafe(self._async_pool.__aenter__(), self._loop).result()
        except Exception:
            self._shutdown = True
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5.0)
            raise

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
        self.close()

    def close(self) -> None:
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
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    def acquire(self, **filters: Any) -> Proxy:
        return self._run_on_loop(self._async_pool.acquire(**filters))

    def release(self, proxy: Proxy) -> None:
        self._run_on_loop(self._async_pool.release(proxy))

    def mark_failed(self, proxy: Proxy, exc: type | None = None) -> None:
        self._run_on_loop(self._async_pool.mark_failed(proxy, exc))

    def mark_success(self, proxy: Proxy, latency: float | None = None) -> None:
        self._run_on_loop(self._async_pool.mark_success(proxy, latency))


__all__ = ["AcquireOptions", "AsyncProxyPool", "PoolStatistics", "SyncProxyPool"]
