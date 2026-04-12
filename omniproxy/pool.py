"""Managed proxy groups with rotation and cooldown blacklisting."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import random
import threading
import time
import weakref
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from .config import PoolConfig, Strategy
from .constants import ANONYMITY_RANKS
from .errors import (
    MissingProxyMetadata,
    NoMatchingProxy,
    PoolClosedError,
    PoolExhausted,
    PoolSaturated,
)
from .extended_proxy import Proxy, arun_health_check

_LOG = logging.getLogger(__name__)


@runtime_checkable
class BasePoolProtocol(Protocol):
    """Bulk and per-proxy accounting shared by sync and async coordinators."""

    def mark_success(self, proxy: Proxy | str) -> None: ...
    def mark_failed(self, proxy: Proxy | str, exc_type: type | None = None) -> None: ...
    def reset_pool(self) -> None: ...


@runtime_checkable
class SyncPoolProtocol(BasePoolProtocol, Protocol):
    """Synchronous acquisition surface."""

    def get_next(self, **kwargs: Any) -> Proxy: ...

    def __enter__(self) -> Proxy: ...
    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> bool | None: ...


@runtime_checkable
class AsyncPoolProtocol(BasePoolProtocol, Protocol):
    """Asynchronous acquisition surface."""

    async def aget_next(self, **kwargs: Any) -> Proxy: ...

    async def __aenter__(self) -> Proxy: ...
    async def __aexit__(self, exc_type: object, exc_val: object, exc_tb: object) -> bool | None: ...


@dataclass(slots=True)
class TokenBucket:
    """Per-URL token bucket used internally when :attr:`PoolConfig.max_rps_per_proxy` is set.

    Instances are created lazily inside :meth:`_PoolState._nolock_consume_token` and mutated on each
    successful token acquisition.

    Attributes
    ----------
    rate: :class:`float`
        Tokens added per elapsed second (requests-per-second cap).
    capacity: :class:`float`
        Maximum stored tokens (burst ceiling), at least ``1.0``.
    tokens: :class:`float`
        Current spendable tokens after the last refill calculation.
    last_refill: :class:`float`
        Last ``time.monotonic()`` sample used to integrate refill.
    """

    rate: float
    capacity: float
    tokens: float
    last_refill: float


@dataclass(slots=True)
class _AsyncExitHold:
    """Tokens to restore :class:`contextvars.ContextVar` state after ``async with pool``."""

    task_proxy_token: contextvars.Token
    carry_reset_token: contextvars.Token | None = None


class _PoolState:
    """Lock-free container for pool data; mutators are ``_nolock_*`` and require the coordinator lock."""

    __slots__ = (
        "_active_keys",
        "_cooldown_until",
        "_failure_counts",
        "_index",
        "_index_cache",
        "_index_dirty",
        "_prototypes",
        "_success_counts",
        "_token_buckets",
        "config",
        "proxies",
    )

    def __init__(self, config: PoolConfig, prototypes: list[Proxy]) -> None:
        self.config = config
        self._prototypes = prototypes
        self.proxies: list[Proxy] | deque[Proxy] = (
            deque(prototypes) if config.structure == "deque" else list(prototypes)
        )
        self._active_keys: set[str] = {self._nolock_key(p) for p in self.proxies}
        self._index = 0
        self._cooldown_until: dict[str, float] = {}
        self._failure_counts: dict[str, int] = {}
        self._success_counts: dict[str, int] = {}
        self._index_cache: dict[tuple[tuple[str, Any], ...], list[Proxy]] = {}
        self._index_dirty = False
        self._token_buckets: dict[str, TokenBucket] | None = None

    @staticmethod
    def _nolock_key(p: Proxy) -> str:
        return p.url

    def _nolock_shortest_active_cooldown(self, now: float) -> float | None:
        """Seconds until the next cooldown expiry; *now* is :func:`time.monotonic`."""
        deltas = [until - now for until in self._cooldown_until.values() if until > now]
        return min(deltas) if deltas else None

    def _nolock_normalize_index(self) -> None:
        if self.proxies and isinstance(self.proxies, list):
            self._index = self._index % len(self.proxies)
        else:
            self._index = 0

    def _nolock_snapshot_active_order(self) -> list[Proxy]:
        return list(self.proxies)

    def _nolock_purge_cooldown(self) -> None:
        now = time.monotonic()
        restored_proxies: list[Proxy] = []
        for k, until in list(self._cooldown_until.items()):
            if until <= now:
                del self._cooldown_until[k]
        for p in self._prototypes:
            k = self._nolock_key(p)
            if k not in self._cooldown_until and k not in self._active_keys:
                self.proxies.append(p)
                self._active_keys.add(k)
                self._failure_counts.pop(k, None)
                restored_proxies.append(p)
        if restored_proxies:
            self._index_dirty = True
        if restored_proxies and (cb := self.config.on_proxy_recovered):
            for rp in restored_proxies:
                cb(rp)

    @staticmethod
    def _nolock_filter_cache_key(kwargs: dict[str, Any]) -> tuple[tuple[str, Any], ...]:
        return tuple(sorted(kwargs.items()))

    @staticmethod
    def _nolock_min_anonymity_rank(label: str) -> int:
        key = label.lower()
        if key not in ANONYMITY_RANKS:
            raise ValueError(
                f"Unknown anonymity level {label!r}; expected one of {set(ANONYMITY_RANKS)!r}"
            )
        return ANONYMITY_RANKS[key]

    def _nolock_proxy_passes_filters(self, proxy: Proxy, kwargs: dict[str, Any]) -> bool:
        fm = self.config.filter_missing_metadata
        for key, value in kwargs.items():
            if key == "min_anonymity":
                need = self._nolock_min_anonymity_rank(str(value))
                raw = proxy.anonymity
                if raw is None:
                    if fm == "skip":
                        return False
                    if fm == "raise":
                        raise MissingProxyMetadata(
                            f"Proxy {proxy.safe_url!r} has no anonymity metadata for min_anonymity filter"
                        )
                    continue
                pr = ANONYMITY_RANKS.get(str(raw).lower())
                if pr is None:
                    if fm == "skip":
                        return False
                    if fm == "raise":
                        raise MissingProxyMetadata(
                            f"Proxy {proxy.safe_url!r} has unknown anonymity {raw!r}"
                        )
                    continue
                if pr < need:
                    return False
                continue
            actual = getattr(proxy, key, None)
            if actual is None:
                if fm == "skip":
                    return False
                if fm == "raise":
                    raise MissingProxyMetadata(
                        f"Proxy {proxy.safe_url!r} has no value for attribute {key!r}"
                    )
                continue
            if actual != value:
                return False
        return True

    def _nolock_filter_proxies_for_kwargs(self, kwargs: dict[str, Any]) -> list[Proxy]:
        active = self._nolock_snapshot_active_order()
        if not kwargs:
            return list(active)
        return [p for p in active if self._nolock_proxy_passes_filters(p, kwargs)]

    def _nolock_ordered_candidates(
        self, subset: list[Proxy], pool_ordered: list[Proxy]
    ) -> list[Proxy]:
        subset_keys = {self._nolock_key(p) for p in subset}
        candidates = [p for p in pool_ordered if self._nolock_key(p) in subset_keys]
        if self.config.strategy == "random":
            return random.sample(candidates, len(candidates))
        nc = len(candidates)
        if nc == 0:
            return []
        start = self._index % nc
        return candidates[start:] + candidates[:start]

    def _nolock_consume_token(self, k: str) -> bool:
        rps = self.config.max_rps_per_proxy
        if rps is None or rps <= 0:
            return True
        if self._token_buckets is None:
            self._token_buckets = {}
        now = time.monotonic()
        b = self._token_buckets.get(k)
        if b is None:
            cap = max(1.0, float(rps))
            self._token_buckets[k] = TokenBucket(
                rate=float(rps), capacity=cap, tokens=cap, last_refill=now
            )
            b = self._token_buckets[k]
        elapsed = now - b.last_refill
        b.tokens = min(b.capacity, b.tokens + b.rate * elapsed)
        b.last_refill = now
        if b.tokens >= 1.0:
            b.tokens -= 1.0
            return True
        return False

    def _nolock_select_candidate(
        self, kwargs: dict[str, Any], active_connections: dict[str, int]
    ) -> Proxy:
        self._nolock_purge_cooldown()
        if self._index_dirty:
            self._index_cache.clear()
            self._index_dirty = False

        cache_key = self._nolock_filter_cache_key(kwargs)
        if cache_key not in self._index_cache:
            self._index_cache[cache_key] = self._nolock_filter_proxies_for_kwargs(kwargs)
        subset = self._index_cache[cache_key]

        active = self._nolock_snapshot_active_order()
        if not active:
            raise PoolExhausted("No proxies available")

        if not subset:
            raise NoMatchingProxy("No proxy matches the requested filters")

        ordered = self._nolock_ordered_candidates(subset, active)
        limit = self.config.max_connections_per_proxy
        saturated = False
        nc = len(ordered)

        for i, p in enumerate(ordered):
            k = self._nolock_key(p)
            if limit is not None:
                cur = active_connections.get(k, 0)
                if cur >= limit:
                    saturated = True
                    continue

            if self.config.max_rps_per_proxy is not None and not self._nolock_consume_token(k):
                saturated = True
                continue

            active_connections[k] = active_connections.get(k, 0) + 1
            if self.config.strategy == "round_robin" and nc:
                self._index = (self._index + i + 1) % nc
            return p

        if saturated:
            raise PoolSaturated(
                "All matching proxies are saturated (connections and/or rate limits)"
            )
        raise PoolExhausted("No proxies available")

    def _nolock_merge_refreshed_proxies(self, raw: list[Proxy | str]) -> None:
        for item in raw:
            p = Proxy(item) if not isinstance(item, Proxy) else item
            k = self._nolock_key(p)
            if not any(self._nolock_key(x) == k for x in self._prototypes):
                self._prototypes.append(p)
            if k not in self._cooldown_until and k not in self._active_keys:
                self.proxies.append(p)
                self._active_keys.add(k)
        if isinstance(self.proxies, list):
            self._nolock_normalize_index()
        self._index_dirty = True

    def _nolock_mark_failed(self, p: Proxy, exc_type: type | None) -> tuple[bool, Proxy]:
        k = self._nolock_key(p)
        count = self._failure_counts.get(k, 0) + 1
        self._failure_counts[k] = count
        cooled = count >= self.config.failure_threshold
        if cooled:
            penalty = self.config.failure_penalties.get(exc_type, 1.0)
            self._cooldown_until[k] = time.monotonic() + (self.config.cooldown * penalty)
            if isinstance(self.proxies, list):
                self.proxies[:] = [x for x in self.proxies if self._nolock_key(x) != k]
                self._nolock_normalize_index()
            else:
                kept = [x for x in self.proxies if self._nolock_key(x) != k]
                self.proxies.clear()
                self.proxies.extend(kept)
            self._active_keys.discard(k)
        self._index_dirty = True
        return cooled, p

    def _nolock_mark_success(self, p: Proxy) -> int:
        k = self._nolock_key(p)
        prev_failures = self._failure_counts.get(k, 0)
        self._success_counts[k] = self._success_counts.get(k, 0) + 1
        self._failure_counts.pop(k, None)
        self._index_dirty = True
        return prev_failures

    def _nolock_reset(self) -> None:
        self.proxies = (
            deque(self._prototypes) if self.config.structure == "deque" else list(self._prototypes)
        )
        self._active_keys = {self._nolock_key(p) for p in self.proxies}
        self._cooldown_until.clear()
        self._failure_counts.clear()
        self._success_counts.clear()
        self._index = 0
        self._index_dirty = True


class HealthMonitor:
    """Background health pass; holds a weak reference to the pool."""

    __slots__ = ("_pool_ref",)

    def __init__(self, pool: Any) -> None:
        self._pool_ref = weakref.ref(pool)

    async def run(self) -> None:
        pool = self._pool_ref()
        if pool is None:
            _LOG.warning(
                "Health monitor stopping immediately: proxy pool was garbage-collected without close()"
            )
            return
        hc = pool.config.health_check
        if hc is None:
            return
        try:
            while True:
                pool = self._pool_ref()
                if pool is None:
                    _LOG.warning(
                        "Health monitor stopping: proxy pool was garbage-collected without close()"
                    )
                    return
                if pool._closed:
                    raise PoolClosedError("proxy pool is closed")
                try:
                    with pool._lock:
                        active = list(pool._state.proxies)
                        cooling = [
                            p
                            for p in pool._state._prototypes
                            if pool._state._nolock_key(p) in pool._state._cooldown_until
                        ]
                    ordered: dict[str, Proxy] = {}
                    for p in active + cooling:
                        ordered[pool._state._nolock_key(p)] = p
                    proxies_to_check = list(ordered.values())

                    if proxies_to_check:
                        results = await asyncio.gather(
                            *[arun_health_check(p, hc) for p in proxies_to_check],
                            return_exceptions=True,
                        )
                        for item in results:
                            if isinstance(item, BaseException):
                                continue
                            p, result = item
                            if cb := pool.config.on_check_complete:
                                cb(p, result)
                        for item in results:
                            if isinstance(item, BaseException):
                                continue
                            p, result = item
                            if result.success:
                                pool.mark_success(p)
                            else:
                                pool.mark_failed(p, result.exc_type)
                except PoolClosedError:
                    raise
                except Exception:
                    _LOG.exception("Health monitor iteration failed; retrying after interval")

                await asyncio.sleep(hc.recovery_interval)
        except PoolClosedError:
            _LOG.info("Health monitor stopped (pool closed)")
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOG.exception("Health monitor exited after unexpected error")
            return


class ProxyPool:
    """Thread-safe collection of :class:`~omniproxy.extended_proxy.Proxy` objects with rotation and cooldowns.

    Selection is driven by :attr:`config` (round-robin over a :class:`collections.deque` or random
    over a :class:`list`). Failed proxies can be blacklisted for :attr:`~omniproxy.config.PoolConfig.cooldown`
    seconds; optional per-URL concurrency and RPS caps raise :exc:`~omniproxy.errors.PoolSaturated`
    when saturated. Sync and async ``with`` statements acquire a proxy, track in-flight use, and
    optionally record success/failure (see :attr:`~omniproxy.config.PoolConfig.auto_mark_failed_on_exception`).

    .. note::

        Filter kwargs passed to :meth:`get_next` / :meth:`aget_next` compare **equality** against
        metadata attributes on each proxy, plus special handling for ``min_anonymity`` against
        :data:`~omniproxy.constants.ANONYMITY_RANKS`.

    Attributes
    ----------
    config: :class:`~omniproxy.config.PoolConfig`
        Live configuration object; may be replaced only by constructing a new pool.
    proxies
        Active proxy sequence: a :class:`collections.deque` for ``round_robin`` or a :class:`list`
        for ``random``. Do not mutate directly; use :meth:`mark_failed`, :meth:`mark_success`,
        :meth:`reset`, or refresh callbacks.
    """

    __slots__ = (
        "__weakref__",
        "_active_connections",
        "_async_condition_obj",
        "_async_consumer_loop",
        "_async_exit_carry",
        "_async_lock_obj",
        "_async_notify_tasks",
        "_async_notify_tasks_lock",
        "_closed",
        "_condition",
        "_finalize_ref",
        "_health_loop",
        "_health_monitor",
        "_health_task",
        "_health_thread",
        "_local",
        "_lock",
        "_refresh_event_async_obj",
        "_refresh_event_sync",
        "_state",
        "_task_proxy",
        "config",
    )

    def __init__(
        self,
        proxies: list[Proxy | str],
        config: PoolConfig | None = None,
        *,
        # backwards-compat aliases
        strategy: Strategy | None = None,
        cooldown: float | None = None,
    ) -> None:
        """Create a pool from seed proxies and optional :class:`~omniproxy.config.PoolConfig`.

        Args:
            proxies (list[Proxy | str]): Seed proxies.
            config (PoolConfig | None): Full pool behaviour configuration.
            strategy (Strategy | None): Shorthand to override ``config.strategy``.
            cooldown (float | None): Shorthand to override ``config.cooldown``.

        Returns:
            None

        Example:
            >>> ProxyPool([], PoolConfig(strategy="random")).config.strategy
            'random'
        """
        if config is None:
            config = PoolConfig()
        if strategy is not None:
            config.strategy = strategy
        if cooldown is not None:
            config.cooldown = cooldown

        # Enforce structure constraint: random strategy requires O(1) index access
        if config.strategy == "random" and config.structure == "deque":
            config.structure = "list"

        self.config: PoolConfig = config

        prototypes = [Proxy(p) if not isinstance(p, Proxy) else p for p in proxies]
        self._state = _PoolState(config, prototypes)

        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._closed = False

        self._async_lock_obj: asyncio.Lock | None = None
        self._async_condition_obj: asyncio.Condition | None = None
        self._async_consumer_loop: asyncio.AbstractEventLoop | None = None
        self._async_exit_carry: contextvars.ContextVar[_AsyncExitHold | None] = (
            contextvars.ContextVar("omniproxy_pool_async_exit_carry", default=None)
        )
        self._async_notify_tasks: set[asyncio.Task[Any]] = set()
        self._async_notify_tasks_lock = threading.Lock()

        self._active_connections: dict[str, int] = {}

        self._refresh_event_sync = threading.Event()
        self._refresh_event_sync.set()
        self._refresh_event_async_obj: asyncio.Event | None = None

        # Sync path: thread-local storage — each OS thread gets its own proxy slot
        self._local: threading.local = threading.local()

        # Async path: ContextVar — each asyncio.Task gets its own proxy slot
        self._task_proxy: contextvars.ContextVar[Proxy | None] = contextvars.ContextVar(
            "task_proxy", default=None
        )

        self._finalize_ref = weakref.finalize(
            self, _finalize_pool_connections, self._active_connections, self._lock
        )

        self._health_task: asyncio.Task | None = None
        self._health_thread: threading.Thread | None = None
        self._health_loop: asyncio.AbstractEventLoop | None = None
        self._health_monitor: HealthMonitor | None = None

    @property
    def proxies(self) -> list[Proxy] | deque[Proxy]:
        """Active proxy sequence (same container as :class:`_PoolState`)."""
        return self._state.proxies

    @property
    def _async_lock(self) -> asyncio.Lock:
        """Lazily create the :class:`asyncio.Lock` guarding async pool accounting.

        Returns:
            asyncio.Lock: Shared lock instance for this pool.

        Example:
            >>> type(ProxyPool(["127.0.0.1:1"])._async_lock).__name__
            'Lock'
        """
        lock = self._async_lock_obj
        if lock is None:
            lock = asyncio.Lock()
            self._async_lock_obj = lock
        return lock

    @property
    def _async_condition(self) -> asyncio.Condition:
        """Async coordinator condition, explicitly bound to :attr:`_async_lock`."""
        lock = self._async_lock
        cond = self._async_condition_obj
        if cond is None:
            cond = asyncio.Condition(lock)
            self._async_condition_obj = cond
        return cond

    def _bind_async_consumer_loop(self) -> None:
        """Remember the running loop for cross-thread :class:`asyncio.Condition` wakeups."""
        with contextlib.suppress(RuntimeError):
            self._async_consumer_loop = asyncio.get_running_loop()

    def _async_notify_discard(self, task: asyncio.Task[Any]) -> None:
        with self._async_notify_tasks_lock:
            self._async_notify_tasks.discard(task)

    def _cancel_pending_async_notify_tasks(self) -> None:
        """Cancel in-flight async notification tasks (e.g. during :meth:`close`)."""
        with self._async_notify_tasks_lock:
            pending = list(self._async_notify_tasks)
        for t in pending:
            if not t.done():
                t.cancel()

    def _notify_async_condition(self, *, notify_all: bool) -> None:
        """Schedule a :meth:`asyncio.Condition.notify` / :meth:`~asyncio.Condition.notify_all` on the consumer loop."""
        loop = self._async_consumer_loop
        if loop is None or not loop.is_running():
            return
        cond = self._async_condition

        def _wake() -> None:
            async def _do() -> None:
                async with cond:
                    if notify_all:
                        cond.notify_all()
                    else:
                        cond.notify(1)

            task = asyncio.create_task(_do())
            with self._async_notify_tasks_lock:
                self._async_notify_tasks.add(task)
            task.add_done_callback(self._async_notify_discard)

        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            loop.call_soon(_wake)
        else:
            loop.call_soon_threadsafe(_wake)

    @property
    def _refresh_event_async(self) -> asyncio.Event:
        """Event coordinating async refresh callbacks with :meth:`aget_next`.

        Returns:
            asyncio.Event: Initially set; cleared while ``arefresh_callback`` runs.

        Example:
            >>> ProxyPool(["127.0.0.1:1"])._refresh_event_async.is_set()
            True
        """
        ev = self._refresh_event_async_obj
        if ev is None:
            ev = asyncio.Event()
            ev.set()
            self._refresh_event_async_obj = ev
        return ev

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _key(p: Proxy) -> str:
        """Stable dedupe key for a proxy (canonical URL string).

        Args:
            p (Proxy): Proxy instance.

        Returns:
            str: ``p.url``.

        Example:
            >>> ProxyPool._key(Proxy("127.0.0.1:9")).startswith("http")
            True
        """
        return _PoolState._nolock_key(p)

    @staticmethod
    def _filter_cache_key(kwargs: dict[str, Any]) -> tuple[tuple[str, Any], ...]:
        """Normalise filter kwargs for the selection index cache.

        Args:
            kwargs (dict[str, Any]): Attribute filters passed to :meth:`get_next`.

        Returns:
            tuple[tuple[str, Any], ...]: Sorted key-value pairs.

        Example:
            >>> ProxyPool._filter_cache_key({"country": "US"})
            (('country', 'US'),)
        """
        return _PoolState._nolock_filter_cache_key(kwargs)

    def _purge_cooldown(self) -> None:
        """Expire cooldown entries and re-admit proxies whose timers elapsed.

        Returns:
            None

        Example:
            >>> ProxyPool(["127.0.0.1:1"])._purge_cooldown() is None
            True
        """
        with self._lock:
            self._state._nolock_purge_cooldown()

    def _release_active_slot(self, proxy: Proxy) -> None:
        """Decrement in-flight connection count after a context-managed use.

        Args:
            proxy (Proxy): Proxy that was acquired.

        Returns:
            None

        Example:
            >>> pool = ProxyPool(["127.0.0.1:1"])
            >>> pool._release_active_slot(Proxy("127.0.0.1:1")) is None
            True
        """
        k = self._key(proxy)
        with self._lock:
            if self._closed:
                pass
            else:
                n = self._active_connections.get(k, 0)
                if n <= 1:
                    self._active_connections.pop(k, None)
                else:
                    self._active_connections[k] = n - 1
                self._condition.notify(1)
        self._notify_async_condition(notify_all=False)
        if cb := self.config.on_proxy_released:
            cb(proxy)

    def _wait_sync_coordinator(self, deadline: float | None) -> None:
        """Block the current thread until retry or deadline; requires :attr:`acquire_timeout` > 0."""
        with self._lock:
            if self._closed:
                raise PoolClosedError("proxy pool is closed")
            nowt = time.monotonic()
            cd = self._state._nolock_shortest_active_cooldown(nowt)
            wt = self.config.wait_fallback_interval
            if cd is not None:
                wt = min(wt, cd)
            if deadline is not None:
                wt = min(wt, max(0.0, deadline - time.monotonic()))
            wt = max(wt, 0.0)
            self._condition.wait(timeout=wt if wt > 0 else 0.001)
            if self._closed:
                raise PoolClosedError("proxy pool is closed")

    async def _wait_async_coordinator(self, deadline: float | None) -> None:
        """Wait between saturated retries on :attr:`_async_condition`, mirroring the sync coordinator."""
        self._bind_async_consumer_loop()
        with self._lock:
            if self._closed:
                raise PoolClosedError("proxy pool is closed")
            nowt = time.monotonic()
            cd = self._state._nolock_shortest_active_cooldown(nowt)
            wt = self.config.wait_fallback_interval
            if cd is not None:
                wt = min(wt, cd)
            if deadline is not None:
                wt = min(wt, max(0.0, deadline - time.monotonic()))
            wt = max(wt, 0.0)
        cond = self._async_condition
        async with cond:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(cond.wait_for(lambda: True), timeout=wt)
        with self._lock:
            if self._closed:
                raise PoolClosedError("proxy pool is closed")

    def _select_candidate(self, **kwargs: Any) -> Proxy:
        """Pick the next proxy matching filters, respecting limits (must hold sync rules).

        Args:
            **kwargs (Any): Forwarded as selection filters.

        Returns:
            Proxy: Chosen proxy with its in-flight counter incremented.

        Raises:
            PoolExhausted: When no proxies are active.
            NoMatchingProxy: When filters exclude every active proxy.
            PoolSaturated: When limits block every matching proxy.

        Example:
            >>> ProxyPool(["127.0.0.1:1"])._select_candidate().port
            1
        """
        deadline = (
            time.monotonic() + self.config.acquire_timeout
            if self.config.acquire_timeout > 0
            else None
        )
        while True:
            try:
                with self._lock:
                    if self._closed:
                        raise PoolClosedError("proxy pool is closed")
                    proxy = self._state._nolock_select_candidate(kwargs, self._active_connections)
                    self._condition.notify(1)
                return proxy
            except PoolSaturated:
                if self.config.acquire_timeout <= 0 or (
                    deadline is not None and time.monotonic() >= deadline
                ):
                    raise
                self._wait_sync_coordinator(deadline)
            except (PoolExhausted, NoMatchingProxy, PoolClosedError):
                raise

    async def _aselect_candidate(self, **kwargs: Any) -> Proxy:
        """Async variant of :meth:`_select_candidate` using :meth:`_wait_async_coordinator` for spins."""
        self._bind_async_consumer_loop()
        deadline = (
            time.monotonic() + self.config.acquire_timeout
            if self.config.acquire_timeout > 0
            else None
        )
        while True:
            try:
                with self._lock:
                    if self._closed:
                        raise PoolClosedError("proxy pool is closed")
                    proxy = self._state._nolock_select_candidate(kwargs, self._active_connections)
                    self._condition.notify(1)
                return proxy
            except PoolSaturated:
                if self.config.acquire_timeout <= 0 or (
                    deadline is not None and time.monotonic() >= deadline
                ):
                    raise
                await self._wait_async_coordinator(deadline)
            except (PoolExhausted, NoMatchingProxy, PoolClosedError):
                raise

    def _merge_refreshed_proxies(self, raw: list[Proxy | str]) -> None:
        """Append refreshed proxies to prototypes and active set when not cooling down.

        Args:
            raw (list[Proxy | str]): New proxies from a refresh callback.

        Returns:
            None

        Example:
            >>> pool = ProxyPool(["127.0.0.1:1"])
            >>> pool._merge_refreshed_proxies(["127.0.0.1:2"]) is None
            True
        """
        with self._lock:
            self._state._nolock_merge_refreshed_proxies(raw)
            self._condition.notify_all()
        self._notify_async_condition(notify_all=True)

    def _run_refresh_sync(self) -> None:
        """Invoke ``refresh_callback`` and merge results, signalling waiters via sync event.

        Returns:
            None

        Example:
            >>> ProxyPool(["127.0.0.1:1"])._run_refresh_sync() is None
            True
        """
        cb = self.config.refresh_callback
        if cb is None:
            self._refresh_event_sync.set()
            return
        self._refresh_event_sync.clear()
        try:
            raw = cb()
            self._merge_refreshed_proxies(raw)
        finally:
            self._refresh_event_sync.set()

    async def _run_arefresh_async(self) -> None:
        """Await ``arefresh_callback`` and merge proxies, signalling async waiters.

        Returns:
            None

        Example:
            >>> import inspect
            >>> inspect.iscoroutinefunction(ProxyPool._run_arefresh_async)
            True
        """
        cb = self.config.arefresh_callback
        if cb is None:
            self._refresh_event_async.set()
            return
        self._refresh_event_async.clear()
        try:
            raw = await cb()
            self._merge_refreshed_proxies(raw)
        finally:
            self._refresh_event_async.set()

    # ------------------------------------------------------------------
    # Background health monitoring
    # ------------------------------------------------------------------

    def start_monitoring(self) -> None:
        """Start :class:`HealthMonitor` as a task on the current running asyncio loop.

        Returns:
            None

        Raises:
            ValueError: If ``config.health_check`` is unset.
            RuntimeError: If called without a running loop or conflicting monitoring modes.

        Example:
            >>> ProxyPool.start_monitoring.__name__
            'start_monitoring'
        """
        if self.config.health_check is None:
            raise ValueError("PoolConfig.health_check is required for background monitoring")
        if self._health_thread is not None and self._health_thread.is_alive():
            raise RuntimeError(
                "Thread-based monitoring is active; call stop_monitoring() before start_monitoring()"
            )
        try:
            current = asyncio.get_running_loop()
        except RuntimeError as err:
            raise RuntimeError("start_monitoring() requires a running asyncio event loop") from err
        if self._health_task is not None and not self._health_task.done():
            if self._health_task.get_loop() is current:
                return
            raise RuntimeError(
                "A health monitoring task is already running on a different event loop"
            )
        self._health_monitor = HealthMonitor(self)
        self._health_task = current.create_task(self._health_monitor.run())

    def start_monitoring_thread(self) -> None:
        """Run the health loop in a dedicated background thread with its own event loop.

        Returns:
            None

        Raises:
            ValueError: If ``config.health_check`` is unset.
            RuntimeError: If incompatible in-loop monitoring is already active.

        Example:
            >>> ProxyPool.start_monitoring_thread.__name__
            'start_monitoring_thread'
        """
        if self.config.health_check is None:
            raise ValueError("PoolConfig.health_check is required for background monitoring")
        if self._health_thread is not None and self._health_thread.is_alive():
            return
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        if (
            current is not None
            and self._health_task is not None
            and not self._health_task.done()
            and self._health_task.get_loop() is current
        ):
            raise RuntimeError(
                "In-loop monitoring is active; call stop_monitoring() before start_monitoring_thread()"
            )

        ready_event = threading.Event()

        def _thread_target() -> None:
            """Thread entrypoint that owns the health-check asyncio loop.

            Returns:
                None

            Example:
                >>> True
                True
            """
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._health_loop = loop
            self._health_monitor = HealthMonitor(self)
            self._health_task = loop.create_task(self._health_monitor.run())
            ready_event.set()
            try:
                loop.run_forever()
            finally:
                self._health_task = None
                self._health_loop = None
                self._health_monitor = None
                if not loop.is_closed():
                    loop.close()

        self._health_thread = threading.Thread(
            target=_thread_target,
            name="omniproxy-pool-health",
            daemon=True,
        )
        self._health_thread.start()
        ready_event.wait(timeout=5.0)

    def stop_monitoring(self) -> None:
        """Cancel thread-based or in-loop health monitoring tasks.

        Returns:
            None

        Example:
            >>> ProxyPool.stop_monitoring.__name__
            'stop_monitoring'
        """
        th = self._health_thread
        if th is not None and th.is_alive():
            loop = self._health_loop
            task = self._health_task

            def _stop() -> None:
                """Cancel health task and stop the dedicated health loop (thread-safe).

                Returns:
                    None

                Example:
                    >>> True
                    True
                """
                if task is not None and not task.done():
                    task.cancel()
                if loop is not None and loop.is_running():
                    loop.stop()

            if loop is not None:
                loop.call_soon_threadsafe(_stop)
            th.join(timeout=30.0)
            self._health_thread = None
            self._health_loop = None
            self._health_task = None
            self._health_monitor = None
            return

        t = self._health_task
        if t is not None and not t.done():
            t.cancel()
        self._health_task = None
        self._health_monitor = None

    @contextlib.asynccontextmanager
    async def monitoring(self):
        """Async context manager that starts then stops in-loop health monitoring.

        Yields:
            ProxyPool: ``self`` for the duration of the ``async with`` block.

        Example:
            >>> ProxyPool.monitoring.__name__
            'monitoring'
        """
        self.start_monitoring()
        try:
            yield self
        finally:
            self.stop_monitoring()

    # ------------------------------------------------------------------
    # Core selection
    # ------------------------------------------------------------------

    def get_next(self, **kwargs: Any) -> Proxy:
        """Acquire the next proxy synchronously, optionally filtered by *kwargs*.

        Args:
            **kwargs (Any): Attribute filters (e.g. ``country="US"``, ``min_anonymity="elite"``).

        Returns:
            Proxy: Selected proxy (in-flight counter incremented).

        Raises:
            PoolExhausted: If empty and refresh cannot help.
            PoolSaturated: If all matches are at connection/RPS limits.
            PoolClosedError: If :meth:`close` has completed.

        Example:
            >>> ProxyPool(["127.0.0.1:1"]).get_next().port
            1
        """
        with self._lock:
            if self._closed:
                raise PoolClosedError("proxy pool is closed")
        if not self._refresh_event_sync.wait(timeout=self.config.refresh_timeout):
            raise PoolExhausted("Refresh timed out")
        with self._lock:
            if self._closed:
                raise PoolClosedError("proxy pool is closed")
        try:
            proxy = self._select_candidate(**kwargs)
        except PoolExhausted:
            if self.config.on_exhausted is not None:
                self.config.on_exhausted()
            if self.config.refresh_callback is not None:
                self._run_refresh_sync()
                proxy = self._select_candidate(**kwargs)
            else:
                raise
        except PoolSaturated:
            if self.config.on_saturated is not None:
                self.config.on_saturated()
            raise
        if self.config.on_proxy_acquired:
            self.config.on_proxy_acquired(proxy)
        return proxy

    async def aget_next(self, **kwargs: Any) -> Proxy:
        """Async variant of :meth:`get_next` respecting async refresh events.

        Args:
            **kwargs (Any): Same filters as :meth:`get_next`.

        Returns:
            Proxy: Selected proxy.

        Raises:
            PoolExhausted: On exhaustion or refresh timeout.
            PoolSaturated: When limits block all matches.
            PoolClosedError: If :meth:`close` has completed.

        Example:
            >>> ProxyPool.aget_next.__name__
            'aget_next'
        """
        with self._lock:
            if self._closed:
                raise PoolClosedError("proxy pool is closed")
        try:
            await asyncio.wait_for(
                self._refresh_event_async.wait(),
                timeout=self.config.refresh_timeout,
            )
        except TimeoutError:
            raise PoolExhausted("Refresh timed out") from None
        with self._lock:
            if self._closed:
                raise PoolClosedError("proxy pool is closed")
        try:
            proxy = await self._aselect_candidate(**kwargs)
        except PoolExhausted:
            if self.config.on_exhausted is not None:
                self.config.on_exhausted()
            if self.config.arefresh_callback is not None:
                await self._run_arefresh_async()
                proxy = await self._aselect_candidate(**kwargs)
            else:
                raise
        except PoolSaturated:
            if self.config.on_saturated is not None:
                self.config.on_saturated()
            raise
        if self.config.on_proxy_acquired:
            self.config.on_proxy_acquired(proxy)
        return proxy

    # ------------------------------------------------------------------
    # Accounting
    # ------------------------------------------------------------------

    def mark_failed(self, proxy: Proxy | str, exc_type: type | None = None) -> None:
        """Increment failure counts and optionally move *proxy* to cooldown.

        Args:
            proxy (Proxy | str): Proxy that failed.
            exc_type (type | None): Optional exception type for penalty lookup.

        Returns:
            None

        Example:
            >>> pool = ProxyPool(["127.0.0.1:1"], PoolConfig(failure_threshold=99))
            >>> pool.mark_failed("127.0.0.1:1") is None
            True
        """
        with self._lock:
            if self._closed:
                raise PoolClosedError("proxy pool is closed")
            p = Proxy(proxy) if not isinstance(proxy, Proxy) else proxy
            cooled, p = self._state._nolock_mark_failed(p, exc_type)
            if cooled:
                self._condition.notify_all()
            else:
                self._condition.notify(1)
        self._notify_async_condition(notify_all=cooled)
        if cb := self.config.on_proxy_failed:
            cb(p, exc_type)
        if cooled and (cb_cd := self.config.on_proxy_cooled_down):
            cb_cd(p)

    def mark_success(self, proxy: Proxy | str) -> None:
        """Clear failure streak and bump success counters for *proxy*.

        Args:
            proxy (Proxy | str): Proxy that succeeded.

        Returns:
            None

        Example:
            >>> pool = ProxyPool(["127.0.0.1:1"])
            >>> pool.mark_success("127.0.0.1:1") is None
            True
        """
        with self._lock:
            if self._closed:
                raise PoolClosedError("proxy pool is closed")
            p = Proxy(proxy) if not isinstance(proxy, Proxy) else proxy
            prev_failures = self._state._nolock_mark_success(p)
            self._condition.notify(1)
        self._notify_async_condition(notify_all=False)
        if prev_failures > 0 and (cb := self.config.on_proxy_recovered):
            cb(p)

    # ------------------------------------------------------------------
    # Context managers
    # ------------------------------------------------------------------

    def __enter__(self) -> Proxy:
        """Enter sync context: select a proxy and stash it on thread-local storage.

        Returns:
            Proxy: Acquired proxy for the ``with`` block.

        Example:
            >>> with ProxyPool(["127.0.0.1:1"]) as p:
            ...     p.port
            1
        """
        proxy = self.get_next()
        self._local.proxy = proxy
        return proxy

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        """Exit sync context: release slot and optionally mark success/failure.

        Args:
            exc_type: Exception type if the block raised.
            exc_val: Exception value.
            exc_tb: Traceback.

        Returns:
            bool: ``False`` when ``config.reraise`` is ``True`` so exceptions propagate.

        Example:
            >>> pool = ProxyPool(["127.0.0.1:1"])
            >>> with pool:
            ...     pass
            >>> True
            True
        """
        proxy: Proxy | None = getattr(self._local, "proxy", None)
        try:
            if proxy is not None:
                self._release_active_slot(proxy)
                if exc_type is not None and self.config.auto_mark_failed_on_exception:
                    self.mark_failed(proxy, exc_type)
                elif exc_type is None and self.config.auto_mark_success_on_exit:
                    self.mark_success(proxy)
        finally:
            if proxy is not None:
                self._local.proxy = None
        return not self.config.reraise

    async def __aenter__(self) -> Proxy:
        """Enter async context: await :meth:`aget_next` and bind proxy to the current task.

        Returns:
            Proxy: Acquired proxy.

        Example:
            >>> ProxyPool.__aenter__.__name__
            '__aenter__'
        """
        proxy = await self.aget_next()
        pt = self._task_proxy.set(proxy)
        hold = _AsyncExitHold(task_proxy_token=pt)
        hold.carry_reset_token = self._async_exit_carry.set(hold)
        return proxy

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> bool:
        """Exit async context: release accounting under the async lock.

        Args:
            exc_type: Exception type from the block, if any.
            exc_val: Exception instance.
            exc_tb: Traceback.

        Returns:
            bool: Suppression flag mirroring sync :meth:`__exit__`.

        Example:
            >>> ProxyPool.__aexit__.__name__
            '__aexit__'
        """
        carry = self._async_exit_carry.get()
        try:
            proxy = self._task_proxy.get()
            if proxy is not None:
                self._release_active_slot(proxy)
                async with self._async_lock:
                    if exc_type is not None and self.config.auto_mark_failed_on_exception:
                        self.mark_failed(proxy, exc_type)
                    elif exc_type is None and self.config.auto_mark_success_on_exit:
                        self.mark_success(proxy)
        finally:
            if carry is not None:
                if carry.carry_reset_token is not None:
                    self._async_exit_carry.reset(carry.carry_reset_token)
                self._task_proxy.reset(carry.task_proxy_token)
        return not self.config.reraise

    # ------------------------------------------------------------------
    # Backwards Compatibility
    # ------------------------------------------------------------------

    def acquire(self) -> ProxyPool:
        """Back-compat no-op returning ``self`` for ``async with pool.acquire()`` patterns.

        Returns:
            ProxyPool: This instance.

        Example:
            >>> ProxyPool(["127.0.0.1:1"]).acquire() is not None
            True
        """
        return self

    def aacquire(self) -> ProxyPool:
        """Async back-compat alias of :meth:`acquire`.

        Returns:
            ProxyPool: This instance.

        Example:
            >>> ProxyPool(["127.0.0.1:1"]).aacquire() is not None
            True
        """
        return self

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def reset_pool(self) -> None:
        """Restore the active set from prototypes and clear cooldown counters.

        Bulk state change: wakes all waiters on the coordinator :class:`threading.Condition`.

        Returns:
            None

        Example:
            >>> pool = ProxyPool(["127.0.0.1:1"])
            >>> pool.reset_pool() is None
            True
        """
        with self._lock:
            if self._closed:
                raise PoolClosedError("proxy pool is closed")
            self._state._nolock_reset()
            self._condition.notify_all()
        self._notify_async_condition(notify_all=True)

    def reset(self) -> None:
        """Alias of :meth:`reset_pool` for backwards compatibility."""
        self.reset_pool()

    def close(self) -> None:
        """Idempotent shutdown: rejects new acquisitions and stops background monitoring.

        Returns:
            None

        Example:
            >>> ProxyPool(["127.0.0.1:1"]).close() is None
            True
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._condition.notify_all()
        self._cancel_pending_async_notify_tasks()
        self._notify_async_condition(notify_all=True)
        self.stop_monitoring()

    # ------------------------------------------------------------------
    # Dunder helpers
    # ------------------------------------------------------------------

    def __len__(self) -> int:
        """Return the number of currently active (non-cooldown) proxies.

        Returns:
            int: Active pool size after purging expired cooldowns.

        Example:
            >>> len(ProxyPool(["127.0.0.1:1", "127.0.0.1:2"]))
            2
        """
        with self._lock:
            self._state._nolock_purge_cooldown()
            return len(self._state.proxies)

    def __iter__(self) -> Iterator[Proxy]:
        """Iterate a snapshot of active proxies (not live-mutating).

        Returns:
            Iterator[Proxy]: Iterator over a point-in-time copy.

        Example:
            >>> next(iter(ProxyPool(["127.0.0.1:1"]))).port
            1
        """
        with self._lock:
            self._state._nolock_purge_cooldown()
            snapshot = list(self._state.proxies)
        return iter(snapshot)

    def __contains__(self, item: object) -> bool:
        """Return whether *item* matches an active proxy URL in the pool.

        Args:
            item (object): ``Proxy``, string, or other proxy-like value.

        Returns:
            bool: Membership in the active key set.

        Example:
            >>> p = Proxy("127.0.0.1:9")
            >>> p in ProxyPool([p])
            True
        """
        try:
            key = self._key(Proxy(item) if not isinstance(item, Proxy) else item)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False
        with self._lock:
            self._state._nolock_purge_cooldown()
            return key in self._state._active_keys

    def __repr__(self) -> str:
        """Summarise active, cooling, strategy, structure, and cooldown seconds.

        Returns:
            str: Debug representation.

        Example:
            >>> "ProxyPool" in repr(ProxyPool(["127.0.0.1:1"]))
            True
        """
        with self._lock:
            self._state._nolock_purge_cooldown()
            active = len(self._state.proxies)
            cooling = len(self._state._cooldown_until)
        return (
            f"ProxyPool(active={active}, cooling={cooling}, "
            f"strategy={self.config.strategy!r}, structure={self.config.structure!r}, "
            f"cooldown={self.config.cooldown}s)"
        )


def _finalize_pool_connections(
    active: dict[str, int],
    lock: threading.Lock,
) -> None:
    """Clear in-flight connection counters when a :class:`ProxyPool` is garbage-collected.

    Args:
        active (dict[str, int]): Shared per-URL in-flight map.
        lock (threading.Lock): Pool lock guarding *active*.

    Returns:
        None

    Example:
        >>> _finalize_pool_connections.__name__
        '_finalize_pool_connections'
    """
    with lock:
        active.clear()


__all__ = [
    "ANONYMITY_RANKS",
    "AsyncPoolProtocol",
    "BasePoolProtocol",
    "HealthMonitor",
    "PoolConfig",
    "ProxyPool",
    "SyncPoolProtocol",
    "TokenBucket",
]
