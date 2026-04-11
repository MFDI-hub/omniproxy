"""Managed proxy groups with rotation and cooldown blacklisting."""

from __future__ import annotations

import asyncio
import contextvars
import random
import threading
import time
import weakref
from collections import deque
from collections.abc import Iterator
from typing import Any

from .config import PoolConfig, Strategy
from .constants import ANONYMITY_RANKS
from .errors import MissingProxyMetadata, NoMatchingProxy, PoolExhausted, PoolSaturated
from .extended_proxy import Proxy


class ProxyPool:
    """Thread-safe pool with round-robin or random selection and cooldown blacklist.

    Parameters
    ----------
    proxies:
        Initial list of proxies (``Proxy`` instances or raw strings).
    config:
        :class:`PoolConfig` instance.
    """

    __slots__ = (
        "__weakref__",
        "_active_connections",
        "_active_keys",
        "_async_lock_obj",
        "_cooldown_until",
        "_failure_counts",
        "_finalize_ref",
        "_index",
        "_index_cache",
        "_index_dirty",
        "_local",
        "_lock",
        "_prototypes",
        "_refresh_event_async_obj",
        "_refresh_event_sync",
        "_success_counts",
        "_task_proxy",
        "config",
        "proxies",
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

        self._prototypes: list[Proxy] = [
            Proxy(p) if not isinstance(p, Proxy) else p for p in proxies
        ]

        self.proxies: list[Proxy] | deque[Proxy] = (
            deque(self._prototypes) if self.config.structure == "deque" else list(self._prototypes)
        )
        self._active_keys: set[str] = {self._key(p) for p in self.proxies}

        self._index: int = 0
        self._cooldown_until: dict[str, float] = {}
        self._failure_counts: dict[str, int] = {}
        self._success_counts: dict[str, int] = {}

        self._lock = threading.RLock()
        self._async_lock_obj: asyncio.Lock | None = None

        self._active_connections: dict[str, int] = {}
        self._index_cache: dict[tuple[tuple[str, Any], ...], list[Proxy]] = {}
        self._index_dirty: bool = False

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

    @property
    def _async_lock(self) -> asyncio.Lock:
        lock = self._async_lock_obj
        if lock is None:
            lock = asyncio.Lock()
            self._async_lock_obj = lock
        return lock

    @property
    def _refresh_event_async(self) -> asyncio.Event:
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
        return p.url

    @staticmethod
    def _filter_cache_key(kwargs: dict[str, Any]) -> tuple[tuple[str, Any], ...]:
        return tuple(sorted(kwargs.items()))

    def _normalize_index(self) -> None:
        if self.proxies and isinstance(self.proxies, list):
            self._index = self._index % len(self.proxies)
        else:
            self._index = 0

    def _snapshot_active_order(self) -> list[Proxy]:
        return list(self.proxies)

    def _purge_cooldown(self) -> None:
        now = time.time()
        restored = False
        for k, until in list(self._cooldown_until.items()):
            if until <= now:
                del self._cooldown_until[k]
        for p in self._prototypes:
            k = self._key(p)
            if k not in self._cooldown_until and k not in self._active_keys:
                self.proxies.append(p)
                self._active_keys.add(k)
                self._failure_counts.pop(k, None)
                restored = True
        if restored:
            self._index_dirty = True

    def _release_active_slot(self, proxy: Proxy) -> None:
        k = self._key(proxy)
        with self._lock:
            n = self._active_connections.get(k, 0)
            if n <= 1:
                self._active_connections.pop(k, None)
            else:
                self._active_connections[k] = n - 1

    def _min_anonymity_rank(self, label: str) -> int:
        key = label.lower()
        if key not in ANONYMITY_RANKS:
            raise ValueError(
                f"Unknown anonymity level {label!r}; expected one of {set(ANONYMITY_RANKS)!r}"
            )
        return ANONYMITY_RANKS[key]

    def _proxy_passes_filters(self, proxy: Proxy, kwargs: dict[str, Any]) -> bool:
        fm = self.config.filter_missing_metadata
        for key, value in kwargs.items():
            if key == "min_anonymity":
                need = self._min_anonymity_rank(str(value))
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

    def _filter_proxies_for_kwargs(self, kwargs: dict[str, Any]) -> list[Proxy]:
        active = self._snapshot_active_order()
        if not kwargs:
            return list(active)
        return [p for p in active if self._proxy_passes_filters(p, kwargs)]

    def _ordered_candidates(self, subset: list[Proxy], pool_ordered: list[Proxy]) -> list[Proxy]:
        subset_keys = {self._key(p) for p in subset}
        candidates = [p for p in pool_ordered if self._key(p) in subset_keys]
        if self.config.strategy == "random":
            return random.sample(candidates, len(candidates))
        nc = len(candidates)
        if nc == 0:
            return []
        start = self._index % nc
        return candidates[start:] + candidates[:start]

    def _select_candidate(self, **kwargs: Any) -> Proxy:
        with self._lock:
            self._purge_cooldown()
            if self._index_dirty:
                self._index_cache.clear()
                self._index_dirty = False

            cache_key = self._filter_cache_key(kwargs)
            if cache_key not in self._index_cache:
                self._index_cache[cache_key] = self._filter_proxies_for_kwargs(kwargs)
            subset = self._index_cache[cache_key]

            active = self._snapshot_active_order()
            if not active:
                raise PoolExhausted("No proxies available")

            if not subset:
                raise NoMatchingProxy("No proxy matches the requested filters")

            ordered = self._ordered_candidates(subset, active)
            limit = self.config.max_connections_per_proxy
            saturated = False
            nc = len(ordered)

            for i, p in enumerate(ordered):
                k = self._key(p)
                if limit is not None:
                    cur = self._active_connections.get(k, 0)
                    if cur >= limit:
                        saturated = True
                        continue

                self._active_connections[k] = self._active_connections.get(k, 0) + 1
                if self.config.strategy == "round_robin" and nc:
                    self._index = (self._index + i + 1) % nc
                return p

            if saturated:
                raise PoolSaturated("All matching proxies are at max concurrent connections")
            raise PoolExhausted("No proxies available")

    def _merge_refreshed_proxies(self, raw: list[Proxy | str]) -> None:
        with self._lock:
            for item in raw:
                p = Proxy(item) if not isinstance(item, Proxy) else item
                k = self._key(p)
                if not any(self._key(x) == k for x in self._prototypes):
                    self._prototypes.append(p)
                if k not in self._cooldown_until and k not in self._active_keys:
                    self.proxies.append(p)
                    self._active_keys.add(k)
            if isinstance(self.proxies, list):
                self._normalize_index()
            self._index_dirty = True

    def _run_refresh_sync(self) -> None:
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
    # Core selection
    # ------------------------------------------------------------------

    def get_next(self, **kwargs: Any) -> Proxy:
        if not self._refresh_event_sync.wait(timeout=self.config.refresh_timeout):
            raise PoolExhausted("Refresh timed out")
        try:
            return self._select_candidate(**kwargs)
        except PoolExhausted:
            if self.config.on_exhausted is not None:
                self.config.on_exhausted()
            if self.config.refresh_callback is not None:
                self._run_refresh_sync()
                return self._select_candidate(**kwargs)
            raise
        except PoolSaturated:
            if self.config.on_saturated is not None:
                self.config.on_saturated()
            raise

    async def aget_next(self, **kwargs: Any) -> Proxy:
        try:
            await asyncio.wait_for(
                self._refresh_event_async.wait(),
                timeout=self.config.refresh_timeout,
            )
        except TimeoutError:
            raise PoolExhausted("Refresh timed out") from None
        try:
            return self._select_candidate(**kwargs)
        except PoolExhausted:
            if self.config.on_exhausted is not None:
                self.config.on_exhausted()
            if self.config.arefresh_callback is not None:
                await self._run_arefresh_async()
                return self._select_candidate(**kwargs)
            raise
        except PoolSaturated:
            if self.config.on_saturated is not None:
                self.config.on_saturated()
            raise

    # ------------------------------------------------------------------
    # Accounting
    # ------------------------------------------------------------------

    def mark_failed(self, proxy: Proxy | str, exc_type: type | None = None) -> None:
        with self._lock:
            p = Proxy(proxy) if not isinstance(proxy, Proxy) else proxy
            k = self._key(p)
            count = self._failure_counts.get(k, 0) + 1
            self._failure_counts[k] = count
            if count >= self.config.failure_threshold:
                penalty = self.config.failure_penalties.get(exc_type, 1.0)
                self._cooldown_until[k] = time.time() + (self.config.cooldown * penalty)
                if isinstance(self.proxies, list):
                    self.proxies[:] = [x for x in self.proxies if self._key(x) != k]
                    self._normalize_index()
                else:
                    kept = [x for x in self.proxies if self._key(x) != k]
                    self.proxies.clear()
                    self.proxies.extend(kept)
                self._active_keys.discard(k)
            self._index_dirty = True

    def mark_success(self, proxy: Proxy | str) -> None:
        with self._lock:
            p = Proxy(proxy) if not isinstance(proxy, Proxy) else proxy
            k = self._key(p)
            self._success_counts[k] = self._success_counts.get(k, 0) + 1
            self._failure_counts.pop(k, None)
            self._index_dirty = True

    # ------------------------------------------------------------------
    # Context managers
    # ------------------------------------------------------------------

    def __enter__(self) -> Proxy:
        proxy = self.get_next()
        self._local.proxy = proxy
        return proxy

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        proxy: Proxy | None = getattr(self._local, "proxy", None)
        if proxy is not None:
            self._release_active_slot(proxy)
            if exc_type is not None and self.config.auto_mark_failed_on_exception:
                self.mark_failed(proxy, exc_type)
            elif exc_type is None and self.config.auto_mark_success_on_exit:
                self.mark_success(proxy)
            self._local.proxy = None
        return not self.config.reraise

    async def __aenter__(self) -> Proxy:
        async with self._async_lock:
            proxy = await self.aget_next()
        self._task_proxy.set(proxy)
        return proxy

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> bool:
        proxy = self._task_proxy.get()
        if proxy is not None:
            self._release_active_slot(proxy)
            async with self._async_lock:
                if exc_type is not None and self.config.auto_mark_failed_on_exception:
                    self.mark_failed(proxy, exc_type)
                elif exc_type is None and self.config.auto_mark_success_on_exit:
                    self.mark_success(proxy)
            self._task_proxy.set(None)
        return not self.config.reraise

    # ------------------------------------------------------------------
    # Backwards Compatibility
    # ------------------------------------------------------------------

    def acquire(self) -> ProxyPool:
        return self

    def aacquire(self) -> ProxyPool:
        return self

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def reset(self) -> None:
        with self._lock:
            self.proxies = (
                deque(self._prototypes)
                if self.config.structure == "deque"
                else list(self._prototypes)
            )
            self._active_keys = {self._key(p) for p in self.proxies}
            self._cooldown_until.clear()
            self._failure_counts.clear()
            self._success_counts.clear()
            self._index = 0
            self._index_dirty = True

    # ------------------------------------------------------------------
    # Dunder helpers
    # ------------------------------------------------------------------

    def __len__(self) -> int:
        with self._lock:
            self._purge_cooldown()
            return len(self.proxies)

    def __iter__(self) -> Iterator[Proxy]:
        with self._lock:
            self._purge_cooldown()
            snapshot = list(self.proxies)
        return iter(snapshot)

    def __contains__(self, item: object) -> bool:
        try:
            key = self._key(Proxy(item) if not isinstance(item, Proxy) else item)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False
        with self._lock:
            self._purge_cooldown()
            return key in self._active_keys

    def __repr__(self) -> str:
        with self._lock:
            self._purge_cooldown()
            active = len(self.proxies)
            cooling = len(self._cooldown_until)
        return (
            f"ProxyPool(active={active}, cooling={cooling}, "
            f"strategy={self.config.strategy!r}, structure={self.config.structure!r}, "
            f"cooldown={self.config.cooldown}s)"
        )


def _finalize_pool_connections(
    active: dict[str, int],
    lock: threading.RLock,
) -> None:
    with lock:
        active.clear()


__all__ = [
    "ANONYMITY_RANKS",
    "PoolConfig",
    "ProxyPool",
]
