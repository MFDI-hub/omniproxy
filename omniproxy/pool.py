"""Managed proxy groups with rotation, cooldown blacklisting, scoring, circuit breaker, and sessions (v2.1)."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import random
import threading
import time
import warnings
import weakref
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, cast, runtime_checkable

from .config import (
    CircuitBreakerConfig,
    LifecycleHooks,
    LimitsConfig,
    PoolConfig,
    ScoringConfig,
    Strategy,
)
from .constants import ANONYMITY_RANKS
from .enum import (
    CircuitBreakerState,
    FilterMissingMetadata,
    PoolStrategy,
    PoolStructure,
    SessionCooldownPolicy,
)
from .errors import (
    MissingProxyMetadata,
    NoMatchingProxy,
    PoolCircuitOpenError,
    PoolClosedError,
    PoolExhausted,
    PoolSaturated,
    SessionBrokenError,
)
from .extended_proxy import Proxy, arun_health_check

_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Scoring, circuit breaker, sticky sessions (v2.1)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ProxyScore:
    """Rolling success/latency aggregates for one proxy URL key."""

    proxy: Proxy
    successes: int = 0
    failures: int = 0
    _latency_samples: list[tuple[float, float]] = field(default_factory=list)  # (monotonic_ts, latency_s)
    last_update: float = 0.0
    _score: float = 0.5
    _score_dirty: bool = True
    _avg_dirty: bool = True
    _cached_avg_latency: float | None = None
    _cached_score_valid: bool = False  # avoids treating 0.0 score as falsy vs cache miss

    def update(self, config: ScoringConfig, success: bool, latency: float | None) -> None:
        now = time.monotonic()
        if success:
            self.successes += 1
        else:
            self.failures += 1
        if latency is not None:
            self._latency_samples.append((now, float(latency)))
        cutoff = now - config.window_seconds
        self._latency_samples = [x for x in self._latency_samples if x[0] >= cutoff]
        self.last_update = now
        self._score_dirty = True
        self._avg_dirty = True
        self._cached_score_valid = False

    def _flush_avg_latency_cache_if_dirty(self) -> None:
        if not self._avg_dirty:
            return
        if not self._latency_samples:
            self._cached_avg_latency = None
        else:
            self._cached_avg_latency = sum(x[1] for x in self._latency_samples) / len(
                self._latency_samples
            )
        self._avg_dirty = False

    def compute_score(self, config: ScoringConfig, pool_avg_latency: float) -> float:
        if not self._score_dirty and self._cached_score_valid:
            return self._score
        total = self.successes + self.failures
        if total < config.min_samples:
            self._flush_avg_latency_cache_if_dirty()
            self._cached_score_valid = True
            self._score_dirty = False
            return self._score
        suc_rate = self.successes / total if total > 0 else 0.0
        avg = self.avg_latency()
        if avg is not None:
            if pool_avg_latency > 0:
                latency_score = max(0.0, min(1.0, 1.0 - (avg / (pool_avg_latency * 2))))
            else:
                latency_score = 1.0
        else:
            latency_score = 0.5
        score = config.success_weight * suc_rate + config.latency_weight * latency_score
        self._score = score
        self._score_dirty = False
        self._cached_score_valid = True
        return score

    def avg_latency(self) -> float | None:
        self._flush_avg_latency_cache_if_dirty()
        return self._cached_avg_latency


class CircuitBreaker:
    """Pool-level breaker: CLOSED counts outcomes; OPEN blocks; HALF_OPEN admits one trial."""

    def __init__(self, config: CircuitBreakerConfig, hooks: LifecycleHooks | None) -> None:
        self.config = config
        self._hooks = hooks
        self.state: CircuitBreakerState = CircuitBreakerState.CLOSED
        self.failures = 0
        self.successes = 0
        self.last_state_change = time.monotonic()
        self._lock = threading.Lock()

    def reset(self) -> None:
        with self._lock:
            self.state = CircuitBreakerState.CLOSED
            self.failures = 0
            self.successes = 0
            self.last_state_change = time.monotonic()

    def allow_request(self) -> bool:
        with self._lock:
            now = time.monotonic()
            if self.state == CircuitBreakerState.OPEN and (now - self.last_state_change) >= self.config.half_open_timeout:
                self.state = CircuitBreakerState.HALF_OPEN
                self.last_state_change = now
                self.successes = 0
                self.failures = 0
            return self.state != CircuitBreakerState.OPEN

    def record(self, success: bool) -> None:
        with self._lock:
            if self.state == CircuitBreakerState.HALF_OPEN:
                if success:
                    self.state = CircuitBreakerState.CLOSED
                    self.last_state_change = time.monotonic()
                    self.successes = 0
                    self.failures = 0
                    if self._hooks and self._hooks.on_circuit_close:
                        self._hooks.on_circuit_close()
                else:
                    self.state = CircuitBreakerState.OPEN
                    self.last_state_change = time.monotonic()
                    self.successes = 0
                    self.failures = 0
                    if self._hooks and self._hooks.on_circuit_open:
                        self._hooks.on_circuit_open()
                return
            if self.state == CircuitBreakerState.OPEN:
                return
            # CLOSED
            if success:
                self.successes += 1
            else:
                self.failures += 1
            total = self.successes + self.failures
            if total < self.config.min_throughput:
                return
            ratio = self.failures / total if total > 0 else 0.0
            if ratio >= self.config.failure_ratio:
                self.state = CircuitBreakerState.OPEN
                self.last_state_change = time.monotonic()
                self.successes = 0
                self.failures = 0
                if self._hooks and self._hooks.on_circuit_open:
                    self._hooks.on_circuit_open()


class SessionStore:
    """Sticky session_id → proxy with sliding TTL (monotonic expiry)."""

    def __init__(self, ttl: float) -> None:
        self._sessions: dict[str, tuple[Proxy, float]] = {}
        self._lock = threading.Lock()
        self._ttl = ttl

    def reset(self) -> None:
        with self._lock:
            self._sessions.clear()

    def get(self, session_id: str) -> Proxy | None:
        with self._lock:
            entry = self._sessions.get(session_id)
            if not entry:
                return None
            proxy, expire = entry
            if time.monotonic() < expire:
                self._sessions[session_id] = (proxy, time.monotonic() + self._ttl)
                return proxy
            del self._sessions[session_id]
            return None

    def bind(self, session_id: str, proxy: Proxy) -> None:
        with self._lock:
            self._sessions[session_id] = (proxy, time.monotonic() + self._ttl)

    def peek_binding(self, session_id: str) -> Proxy | None:
        """Return the bound proxy if *session_id* exists and TTL has not expired.

        Unlike :meth:`get`, this does not extend the TTL. Expired entries are removed.
        """
        with self._lock:
            entry = self._sessions.get(session_id)
            if not entry:
                return None
            proxy, expire = entry
            now = time.monotonic()
            if now < expire:
                return proxy
            del self._sessions[session_id]
            return None

    def unbind(self, session_id: str) -> Proxy | None:
        with self._lock:
            entry = self._sessions.pop(session_id, None)
            return entry[0] if entry else None


_RESERVED_POOL_KWARGS = frozenset({"session_id"})


def _acquire_filter_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in kwargs.items() if k not in _RESERVED_POOL_KWARGS}


@runtime_checkable
class BasePoolProtocol(Protocol):
    """Structural type for shared pool accounting and configuration."""

    @property
    def proxies(self) -> Sequence[Proxy]: ...

    @property
    def config(self) -> PoolConfig: ...

    def mark_success(self, proxy: Proxy | str, *, latency: float | None = None) -> None: ...
    def mark_failed(self, proxy: Proxy | str, exc_type: type | None = None) -> None: ...


@runtime_checkable
class MonitorablePoolProtocol(BasePoolProtocol, Protocol):
    """Pool types that expose closure state and cooling snapshots."""

    @property
    def is_closed(self) -> bool: ...

    @property
    def cooling_proxies(self) -> Sequence[Proxy]: ...


@runtime_checkable
class SyncPoolProtocol(BasePoolProtocol, Protocol):
    """Synchronous acquisition surface."""

    def get_next(self, **kwargs: Any) -> Proxy: ...

    def __enter__(self) -> Proxy: ...
    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> bool | None: ...

    def close(self) -> None: ...


@runtime_checkable
class AsyncPoolProtocol(BasePoolProtocol, Protocol):
    """Asynchronous acquisition surface."""

    async def aget_next(self, **kwargs: Any) -> Proxy: ...

    async def __aenter__(self) -> Proxy: ...
    async def __aexit__(self, exc_type: object, exc_val: object, exc_tb: object) -> bool | None: ...

    async def aclose(self) -> None: ...


@dataclass(slots=True)
class TokenBucket:
    """Per-URL token bucket used internally when :attr:`PoolConfig.limits.max_rps_per_proxy` is set.

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
        "_ada_cooldown_fn",
        "_circuit_breaker",
        "_cooldown_until",
        "_dead_letter",
        "_failure_counts",
        "_index",
        "_index_cache",
        "_index_dirty",
        "_pool_avg_latency_count",
        "_pool_avg_latency_dirty",
        "_pool_avg_latency_sum",
        "_prototypes",
        "_scores",
        "_scoring_config",
        "_session_store",
        "_success_counts",
        "_token_buckets",
        "config",
        "proxies",
    )

    def __init__(self, config: PoolConfig, prototypes: list[Proxy]) -> None:
        self.config = config
        self._prototypes = prototypes
        self.proxies: list[Proxy] | deque[Proxy] = (
            deque(prototypes) if config.structure == PoolStructure.DEQUE else list(prototypes)
        )
        self._active_keys: set[str] = {self._nolock_key(p) for p in self.proxies}
        self._index = 0
        self._cooldown_until: dict[str, float] = {}
        self._failure_counts: dict[str, int] = {}
        self._success_counts: dict[str, int] = {}
        self._index_cache: dict[tuple[tuple[str, Any], ...], list[Proxy]] = {}
        self._index_dirty = False
        self._token_buckets: dict[str, TokenBucket] | None = None

        self._scores: dict[str, ProxyScore] = {}
        self._dead_letter: list[Proxy] = []
        self._scoring_config = config.scoring
        self._circuit_breaker = (
            CircuitBreaker(config.circuit_breaker, config.hooks)
            if config.circuit_breaker is not None
            else None
        )
        self._session_store = (
            SessionStore(config.session_ttl) if config.session_ttl > 0 else None
        )

        if config.cooldown_strategy is not None:
            self._ada_cooldown_fn = config.cooldown_strategy
        elif config.adaptive_cooldown:
            self._ada_cooldown_fn = self._default_adaptive_cooldown
        else:
            self._ada_cooldown_fn = None

        self._pool_avg_latency_sum: float = 0.0
        self._pool_avg_latency_count: int = 0
        self._pool_avg_latency_dirty: bool = True

    def _nolock_key(self, p: Proxy, *, precomputed: str | None = None) -> str:
        if precomputed is not None:
            return precomputed
        fn = self.config.dedup_key
        return fn(p) if fn is not None else p._url

    @staticmethod
    def _default_adaptive_cooldown(base: float, active: int, total: int) -> float:
        ratio = active / max(total, 1)
        if ratio >= 0.8:
            mult = 1.5
        elif ratio >= 0.5:
            mult = 1.0
        elif ratio >= 0.2:
            mult = 0.7
        else:
            mult = 0.4
        return base * mult

    def _nolock_calc_cooldown(self, exc_type: type | None = None) -> float:
        base = float(self.config.cooldown)
        if self._ada_cooldown_fn is not None:
            active = len(self._active_keys)
            total = len(self._prototypes)
            base = float(self._ada_cooldown_fn(base, active, total))
        penalty = (
            self.config.failure_penalties.get(exc_type, 1.0) if exc_type is not None else 1.0
        )
        final = base * float(penalty)
        return max(self.config.min_cooldown, min(self.config.max_cooldown, final))

    def _nolock_pool_avg_latency(self) -> float:
        if self._pool_avg_latency_dirty:
            total = 0.0
            count = 0
            for s in self._scores.values():
                a = s.avg_latency()
                if a is not None:
                    total += a
                    count += 1
            self._pool_avg_latency_sum = total
            self._pool_avg_latency_count = count
            self._pool_avg_latency_dirty = False
        if self._pool_avg_latency_count == 0:
            return 0.0
        return self._pool_avg_latency_sum / self._pool_avg_latency_count

    def _nolock_invalidate_pool_avg_latency_cache(self) -> None:
        self._pool_avg_latency_dirty = True

    def _nolock_evict_if_needed(self) -> None:
        cfg = self._scoring_config
        if cfg is None:
            return
        now = time.monotonic()
        for k, score_obj in list(self._scores.items()):
            if k not in self._active_keys:
                continue
            if score_obj.last_update <= 0:
                continue
            grace = cfg.eviction_grace_period
            if grace > 0 and (now - score_obj.last_update) < grace:
                continue
            avg_lat = self._nolock_pool_avg_latency()
            s = score_obj.compute_score(cfg, avg_lat)
            if s >= cfg.eviction_threshold:
                continue
            proxy = next((p for p in self._prototypes if self._nolock_key(p) == k), None)
            if proxy is None:
                continue
            self._active_keys.discard(k)
            if isinstance(self.proxies, list):
                self.proxies[:] = [p for p in self.proxies if self._nolock_key(p) != k]
                self._nolock_normalize_index()
            else:
                kept = [x for x in self.proxies if self._nolock_key(x) != k]
                self.proxies.clear()
                self.proxies.extend(kept)
            self._dead_letter.append(proxy)
            if len(self._dead_letter) > self.config.dead_letter_max_size:
                self._dead_letter[:] = self._dead_letter[-self.config.dead_letter_max_size :]
            self._index_dirty = True
            if hook := self.config.hooks.on_auto_evicted:
                hook(proxy, "Score below threshold")
            del self._scores[k]
            self._nolock_invalidate_pool_avg_latency_cache()

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
        dead_keys = {self._nolock_key(p) for p in self._dead_letter}
        for p in self._prototypes:
            k = self._nolock_key(p)
            if k in dead_keys:
                continue
            if k not in self._cooldown_until and k not in self._active_keys:
                self.proxies.append(p)
                self._active_keys.add(k)
                self._failure_counts.pop(k, None)
                restored_proxies.append(p)
        if restored_proxies:
            self._index_dirty = True
        if restored_proxies and (cb := self.config.hooks.on_proxy_recovered):
            for rp in restored_proxies:
                cb(rp)

    @staticmethod
    def _nolock_filter_cache_key(kwargs: dict[str, Any]) -> tuple[tuple[str, Any], ...]:
        filtered = _acquire_filter_kwargs(kwargs)
        return tuple(sorted(filtered.items()))

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
                    if fm == FilterMissingMetadata.SKIP:
                        return False
                    if fm == FilterMissingMetadata.RAISE:
                        raise MissingProxyMetadata(
                            f"Proxy {proxy.safe_url!r} has no anonymity metadata for min_anonymity filter"
                        )
                    continue
                pr = ANONYMITY_RANKS.get(str(raw).lower())
                if pr is None:
                    if fm == FilterMissingMetadata.SKIP:
                        return False
                    if fm == FilterMissingMetadata.RAISE:
                        raise MissingProxyMetadata(
                            f"Proxy {proxy.safe_url!r} has unknown anonymity {raw!r}"
                        )
                    continue
                if pr < need:
                    return False
                continue
            actual = getattr(proxy, key, None)
            if actual is None:
                if fm == FilterMissingMetadata.SKIP:
                    return False
                if fm == FilterMissingMetadata.RAISE:
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
        candidates: list[tuple[str, Proxy]] = []
        for p in pool_ordered:
            k = self._nolock_key(p)
            if k in subset_keys:
                candidates.append((k, p))

        if not candidates:
            return []

        strategy = self.config.strategy
        if strategy == PoolStrategy.RANDOM:
            random.shuffle(candidates)
            return [p for _, p in candidates]

        if strategy == PoolStrategy.WEIGHTED:
            if self._scoring_config is None:
                return [p for _, p in candidates]
            avg_lat = self._nolock_pool_avg_latency()
            scored: list[tuple[float, str, Proxy]] = []
            for k, p in candidates:
                score_obj = self._scores.get(k)
                s = (
                    score_obj.compute_score(self._scoring_config, avg_lat)
                    if score_obj is not None
                    else 0.5
                )
                scored.append((s, k, p))
            scored.sort(key=lambda x: (-x[0], x[1]))
            ordered = [p for _, _, p in scored]
            nc = len(ordered)
            start = self._index % nc
            return ordered[start:] + ordered[:start]

        if strategy == PoolStrategy.LOWEST_LATENCY:
            lat_ranked: list[tuple[float, str, Proxy]] = []
            for k, p in candidates:
                score_obj = self._scores.get(k)
                if score_obj is None:
                    lat_v = float("inf")
                else:
                    av = score_obj.avg_latency()
                    lat_v = float("inf") if av is None else av
                lat_ranked.append((lat_v, k, p))
            lat_ranked.sort(key=lambda x: (x[0], x[1]))
            ordered = [p for _, _, p in lat_ranked]
            nc = len(ordered)
            start = self._index % nc
            return ordered[start:] + ordered[:start]

        ordered_c = [p for _, p in candidates]
        nc = len(ordered_c)
        start = self._index % nc
        return ordered_c[start:] + ordered_c[:start]

    def _nolock_try_session_bind(
        self,
        session_id: str,
        filter_kw: dict[str, Any],
        active_connections: dict[str, int],
    ) -> Proxy | None:
        if self._session_store is None:
            return None
        bound = self._session_store.get(session_id)
        if bound is None:
            return None
        k = self._nolock_key(bound)
        if k not in self._active_keys or k in self._cooldown_until:
            return None
        if filter_kw and not self._nolock_proxy_passes_filters(bound, filter_kw):
            return None
        limit = self.config.limits.max_connections_per_proxy
        if limit is not None and active_connections.get(k, 0) >= limit:
            return None
        if self.config.limits.max_rps_per_proxy is not None and not self._nolock_consume_token(k):
            return None
        active_connections[k] = active_connections.get(k, 0) + 1
        return bound

    def _nolock_consume_token(self, k: str) -> bool:
        rps = self.config.limits.max_rps_per_proxy
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
        self._nolock_evict_if_needed()
        if self._index_dirty:
            self._index_cache.clear()
            self._index_dirty = False

        if self._circuit_breaker is not None and not self._circuit_breaker.allow_request():
            raise PoolCircuitOpenError("Proxy pool circuit breaker is open")

        filter_kw = _acquire_filter_kwargs(kwargs)
        cache_key = self._nolock_filter_cache_key(kwargs)
        if cache_key not in self._index_cache:
            self._index_cache[cache_key] = self._nolock_filter_proxies_for_kwargs(filter_kw)
        subset = self._index_cache[cache_key]

        session_raw = kwargs.get("session_id")
        session_id = session_raw if isinstance(session_raw, str) and session_raw else None

        active = self._nolock_snapshot_active_order()
        if not active:
            if session_id and self._session_store is not None:
                had_binding = self._session_store.peek_binding(session_id) is not None
                if had_binding:
                    pol = self.config.session_cooldown_policy
                    if pol == SessionCooldownPolicy.RAISE:
                        raise SessionBrokenError(f"No usable bound proxy for session {session_id!r}")
                    if pol == SessionCooldownPolicy.BLOCK:
                        raise PoolSaturated(
                            "Sticky session proxy unavailable (session_cooldown_policy='block')"
                        )
            raise PoolExhausted("No proxies available")

        if not subset:
            raise NoMatchingProxy("No proxy matches the requested filters")

        if session_id and self._session_store is not None:
            had_binding = self._session_store.peek_binding(session_id) is not None
            p = self._nolock_try_session_bind(session_id, filter_kw, active_connections)
            if p is not None:
                return p
            pol = self.config.session_cooldown_policy
            if had_binding:
                if pol == SessionCooldownPolicy.RAISE:
                    raise SessionBrokenError(f"No usable bound proxy for session {session_id!r}")
                if pol == SessionCooldownPolicy.BLOCK:
                    raise PoolSaturated(
                        "Sticky session proxy unavailable (session_cooldown_policy='block')"
                    )

        ordered = self._nolock_ordered_candidates(subset, active)
        limit = self.config.limits.max_connections_per_proxy
        saturated = False
        nc = len(ordered)

        for i, p in enumerate(ordered):
            k = self._nolock_key(p)
            if limit is not None:
                cur = active_connections.get(k, 0)
                if cur >= limit:
                    saturated = True
                    continue

            if self.config.limits.max_rps_per_proxy is not None and not self._nolock_consume_token(
                k
            ):
                saturated = True
                continue

            active_connections[k] = active_connections.get(k, 0) + 1
            if self.config.strategy in (
                PoolStrategy.ROUND_ROBIN,
                PoolStrategy.WEIGHTED,
                PoolStrategy.LOWEST_LATENCY,
            ) and nc:
                self._index = (self._index + i + 1) % nc
            if session_id and self._session_store is not None:
                self._session_store.bind(session_id, p)
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
            cd = self._nolock_calc_cooldown(exc_type)
            self._cooldown_until[k] = time.monotonic() + cd
            if isinstance(self.proxies, list):
                self.proxies[:] = [x for x in self.proxies if self._nolock_key(x) != k]
                self._nolock_normalize_index()
            else:
                kept = [x for x in self.proxies if self._nolock_key(x) != k]
                self.proxies.clear()
                self.proxies.extend(kept)
            self._active_keys.discard(k)
        self._index_dirty = True
        if self._scoring_config is not None:
            score_obj = self._scores.get(k)
            if score_obj is None:
                score_obj = ProxyScore(p)
            score_obj.update(self._scoring_config, success=False, latency=None)
            self._scores[k] = score_obj
            self._nolock_invalidate_pool_avg_latency_cache()
        return cooled, p

    def _nolock_mark_success(self, p: Proxy, latency: float | None = None) -> int:
        k = self._nolock_key(p)
        prev_failures = self._failure_counts.get(k, 0)
        self._success_counts[k] = self._success_counts.get(k, 0) + 1
        self._failure_counts.pop(k, None)
        self._index_dirty = True
        if self._scoring_config is not None:
            score_obj = self._scores.get(k)
            if score_obj is None:
                score_obj = ProxyScore(p)
            score_obj.update(self._scoring_config, success=True, latency=latency)
            self._scores[k] = score_obj
            self._nolock_invalidate_pool_avg_latency_cache()
        return prev_failures

    def _nolock_reset(self) -> None:
        self.proxies = (
            deque(self._prototypes) if self.config.structure == PoolStructure.DEQUE else list(self._prototypes)
        )
        self._active_keys = {self._nolock_key(p) for p in self.proxies}
        self._cooldown_until.clear()
        self._failure_counts.clear()
        self._success_counts.clear()
        self._index = 0
        self._index_dirty = True
        self._scores.clear()
        self._pool_avg_latency_sum = 0.0
        self._pool_avg_latency_count = 0
        self._pool_avg_latency_dirty = True
        self._dead_letter.clear()
        if self._session_store is not None:
            self._session_store.reset()
        if self._circuit_breaker is not None:
            self._circuit_breaker.reset()
        self._token_buckets = None


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
        # ``start_monitoring`` / ``start_monitoring_thread`` require ``health_check``, but
        # ``HealthMonitor`` may be constructed directly; bail out quietly when unset.
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
                if pool.is_closed:
                    raise PoolClosedError("proxy pool is closed")
                try:
                    # Split reads: list(pool) and cooling_proxies are each consistent snapshots
                    # under the coordinator lock; merging by URL tolerates a small race between them.
                    active = list(pool)
                    cooling = list(pool.cooling_proxies)
                    ordered: dict[str, Proxy] = {}
                    st = pool._state
                    for p in active + cooling:
                        ordered[st._nolock_key(p)] = p
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
                            if cb := pool.config.hooks.on_check_complete:
                                cb(p, result)
                        for item in results:
                            if isinstance(item, BaseException):
                                continue
                            p, result = item
                            if result.success:
                                pool.mark_success(p, latency=result.latency)
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


def _finalize_pool_connections(
    active: dict[str, int],
    lock: threading.Lock,
) -> None:
    """Clear in-flight connection counters when a pool is garbage-collected without :meth:`close` / :meth:`aclose`.

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


class BaseProxyPool(ABC):
    """Thread-safe proxy rotation core shared by :class:`SyncProxyPool` and :class:`AsyncProxyPool`."""

    __slots__ = (
        "__weakref__",
        "_active_connections",
        "_closed",
        "_finalize_ref",
        "_lock",
        "_state",
        "config",
    )

    def __init__(
        self,
        proxies: list[Proxy | str],
        config: PoolConfig | None = None,
        *,
        strategy: Strategy | None = None,
        cooldown: float | None = None,
    ) -> None:
        if config is None:
            config = PoolConfig()
        if strategy is not None:
            config.strategy = (
                strategy if isinstance(strategy, PoolStrategy) else PoolStrategy(strategy)
            )
        if cooldown is not None:
            config.cooldown = cooldown

        if config.strategy == PoolStrategy.RANDOM and config.structure == PoolStructure.DEQUE:
            config.structure = PoolStructure.LIST

        self.config: PoolConfig = config

        prototypes = [Proxy(p) if not isinstance(p, Proxy) else p for p in proxies]
        self._state = _PoolState(config, prototypes)

        self._lock = threading.Lock()
        self._closed = False

        self._active_connections: dict[str, int] = {}

        self._finalize_ref = weakref.finalize(
            self, _finalize_pool_connections, self._active_connections, self._lock
        )

    @property
    def is_closed(self) -> bool:
        # Reads are lock-free: `_closed` is only written while holding `_lock` (see `close` / `aclose`).
        return self._closed

    @property
    def proxies(self) -> list[Proxy]:
        """Point-in-time copy of active proxies after purging expired cooldowns.

        This is a **snapshot** (always a new :class:`list`), not the live ``deque``/``list`` stored
        on :class:`_PoolState`.
        """
        with self._lock:
            self._state._nolock_purge_cooldown()
            return list(self._state.proxies)

    @property
    def cooling_proxies(self) -> list[Proxy]:
        with self._lock:
            self._state._nolock_purge_cooldown()
            return [
                p
                for p in self._state._prototypes
                if self._state._nolock_key(p) in self._state._cooldown_until
            ]

    def _notify_sync_condition(self, *, notify_all: bool = False) -> None:  # noqa: ARG002
        # Must be called with _lock held.
        return None

    def _notify_async_condition(self, *, notify_all: bool = False) -> None:  # noqa: ARG002
        return None

    def _key(self, p: Proxy) -> str:
        return self._state._nolock_key(p)

    @staticmethod
    def _filter_cache_key(kwargs: dict[str, Any]) -> tuple[tuple[str, Any], ...]:
        return _PoolState._nolock_filter_cache_key(kwargs)

    def _purge_cooldown(self) -> None:
        with self._lock:
            self._state._nolock_purge_cooldown()

    def _release_active_slot(self, proxy: Proxy) -> None:
        k = self._key(proxy)
        with self._lock:
            if not self._closed:
                n = self._active_connections.get(k, 0)
                if n <= 1:
                    self._active_connections.pop(k, None)
                else:
                    self._active_connections[k] = n - 1
                self._notify_sync_condition(notify_all=False)
        self._notify_async_condition(notify_all=False)
        if cb := self.config.hooks.on_proxy_released:
            cb(proxy)

    def _merge_refreshed_proxies(self, raw: list[Proxy | str]) -> None:
        with self._lock:
            self._state._nolock_merge_refreshed_proxies(raw)
            self._notify_sync_condition(notify_all=True)
        self._notify_async_condition(notify_all=True)

    def __len__(self) -> int:
        with self._lock:
            self._state._nolock_purge_cooldown()
            return len(self._state.proxies)

    def __iter__(self) -> Iterator[Proxy]:
        with self._lock:
            self._state._nolock_purge_cooldown()
            snapshot = list(self._state.proxies)
        return iter(snapshot)

    def __contains__(self, item: object) -> bool:
        try:
            key = self._key(
                Proxy(cast(Proxy | str, item)) if not isinstance(item, Proxy) else item
            )
        except (TypeError, ValueError):
            return False
        with self._lock:
            self._state._nolock_purge_cooldown()
            return key in self._state._active_keys

    def __repr__(self) -> str:
        with self._lock:
            self._state._nolock_purge_cooldown()
            active = len(self._state.proxies)
            cooling = len(self._state._cooldown_until)
        cls = type(self).__name__
        return (
            f"{cls}(active={active}, cooling={cooling}, "
            f"strategy={self.config.strategy!r}, structure={self.config.structure!r}, "
            f"cooldown={self.config.cooldown}s)"
        )

    def reset(self) -> None:
        self.reset_pool()

    @abstractmethod
    def mark_failed(self, proxy: Proxy | str, exc_type: type | None = None) -> None: ...

    @abstractmethod
    def mark_success(self, proxy: Proxy | str, *, latency: float | None = None) -> None: ...

    @abstractmethod
    def reset_pool(self) -> None: ...


class SyncProxyPool(BaseProxyPool):
    """Synchronous :class:`Proxy` pool with threading coordination."""

    __slots__ = (
        "_condition",
        "_health_loop",
        "_health_monitor",
        "_health_task",
        "_health_thread",
        "_local",
        "_refresh_event_sync",
    )

    def __init__(
        self,
        proxies: list[Proxy | str],
        config: PoolConfig | None = None,
        *,
        strategy: Strategy | None = None,
        cooldown: float | None = None,
    ) -> None:
        super().__init__(proxies, config, strategy=strategy, cooldown=cooldown)
        self._condition = threading.Condition(self._lock)
        self._refresh_event_sync = threading.Event()
        self._refresh_event_sync.set()
        self._local: threading.local = threading.local()
        self._health_task: asyncio.Task[Any] | None = None
        self._health_thread: threading.Thread | None = None
        self._health_loop: asyncio.AbstractEventLoop | None = None
        self._health_monitor: HealthMonitor | None = None

    def _notify_sync_condition(self, *, notify_all: bool = False) -> None:
        # Must be called with _lock held.
        if notify_all:
            self._condition.notify_all()
        else:
            self._condition.notify(1)

    def _wait_sync_coordinator(self, deadline: float | None) -> None:
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

    def _select_candidate(self, **kwargs: Any) -> Proxy:
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
                    self._notify_sync_condition(notify_all=False)
                return proxy
            except PoolSaturated:
                if self.config.acquire_timeout <= 0 or (
                    deadline is not None and time.monotonic() >= deadline
                ):
                    raise
                self._wait_sync_coordinator(deadline)
            except (PoolExhausted, NoMatchingProxy, PoolClosedError):
                raise

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

    def get_next(self, **kwargs: Any) -> Proxy:
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
            if self.config.hooks.on_exhausted is not None:
                self.config.hooks.on_exhausted()
            if self.config.refresh_callback is not None:
                self._run_refresh_sync()
                proxy = self._select_candidate(**kwargs)
            else:
                raise
        except PoolSaturated:
            if self.config.hooks.on_saturated is not None:
                self.config.hooks.on_saturated()
            raise
        if self.config.hooks.on_proxy_acquired:
            self.config.hooks.on_proxy_acquired(proxy)
        return proxy

    def __enter__(self) -> Proxy:
        proxy = self.get_next()
        self._local.proxy = proxy
        return proxy

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
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

    def mark_failed(self, proxy: Proxy | str, exc_type: type | None = None) -> None:
        with self._lock:
            if self._closed:
                raise PoolClosedError("proxy pool is closed")
            p = Proxy(proxy) if not isinstance(proxy, Proxy) else proxy
            cooled, p = self._state._nolock_mark_failed(p, exc_type)
            if self._state._circuit_breaker is not None:
                self._state._circuit_breaker.record(False)
            self._notify_sync_condition(notify_all=cooled)
        if cb := self.config.hooks.on_proxy_failed:
            cb(p, exc_type)
        if cooled and (cb_cd := self.config.hooks.on_proxy_cooled_down):
            cb_cd(p)

    def mark_success(self, proxy: Proxy | str, *, latency: float | None = None) -> None:
        with self._lock:
            if self._closed:
                raise PoolClosedError("proxy pool is closed")
            p = Proxy(proxy) if not isinstance(proxy, Proxy) else proxy
            prev_failures = self._state._nolock_mark_success(p, latency=latency)
            if self._state._circuit_breaker is not None:
                self._state._circuit_breaker.record(True)
            self._notify_sync_condition(notify_all=False)
        if prev_failures > 0 and (cb := self.config.hooks.on_proxy_recovered):
            cb(p)

    def reset_pool(self) -> None:
        """Reset active state; sync-only wake (no :meth:`_notify_async_condition`).

        Mixed sync/async pools are unsupported: unlike :meth:`BaseProxyPool._merge_refreshed_proxies`,
        a sync ``reset_pool`` does not signal async waiters by design.
        """
        with self._lock:
            if self._closed:
                raise PoolClosedError("proxy pool is closed")
            self._state._nolock_reset()
            self._notify_sync_condition(notify_all=True)

    def start_monitoring_thread(self) -> None:
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
        th = self._health_thread
        if th is not None and th.is_alive():
            loop = self._health_loop
            task = self._health_task

            def _stop() -> None:
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

        # No live health thread (never started, already joined, or crashed): clear any stale refs
        # normally dropped in ``_thread_target``'s ``finally`` block.
        self._health_thread = None
        self._health_loop = None
        self._health_task = None
        self._health_monitor = None

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._notify_sync_condition(notify_all=True)
        self.stop_monitoring()

    def acquire(self) -> SyncProxyPool:
        return self


class AsyncProxyPool(BaseProxyPool):
    """Asyncio-driven :class:`Proxy` pool."""

    __slots__ = (
        "_async_condition_obj",
        "_async_consumer_loop",
        "_async_exit_carry",
        "_async_lock_obj",
        "_async_notify_tasks",
        "_async_notify_tasks_lock",
        "_health_monitor",
        "_health_task",
        "_refresh_event_async_obj",
        "_task_proxy",
    )

    def __init__(
        self,
        proxies: list[Proxy | str],
        config: PoolConfig | None = None,
        *,
        strategy: Strategy | None = None,
        cooldown: float | None = None,
    ) -> None:
        super().__init__(proxies, config, strategy=strategy, cooldown=cooldown)
        self._async_lock_obj: asyncio.Lock | None = None
        self._async_condition_obj: asyncio.Condition | None = None
        self._async_consumer_loop: asyncio.AbstractEventLoop | None = None
        self._async_exit_carry: contextvars.ContextVar[_AsyncExitHold | None] = (
            contextvars.ContextVar("omniproxy_pool_async_exit_carry", default=None)
        )
        self._async_notify_tasks: set[asyncio.Task[Any]] = set()
        self._async_notify_tasks_lock = threading.Lock()
        self._refresh_event_async_obj: asyncio.Event | None = None
        self._task_proxy: contextvars.ContextVar[Proxy | None] = contextvars.ContextVar(
            "task_proxy", default=None
        )
        self._health_task: asyncio.Task[Any] | None = None
        self._health_monitor: HealthMonitor | None = None

    @property
    def _async_lock(self) -> asyncio.Lock:
        lock = self._async_lock_obj
        if lock is None:
            lock = asyncio.Lock()
            self._async_lock_obj = lock
        return lock

    @property
    def _async_condition(self) -> asyncio.Condition:
        lock = self._async_lock
        cond = self._async_condition_obj
        if cond is None:
            cond = asyncio.Condition(lock)
            self._async_condition_obj = cond
        return cond

    @property
    def _refresh_event_async(self) -> asyncio.Event:
        ev = self._refresh_event_async_obj
        if ev is None:
            ev = asyncio.Event()
            ev.set()
            self._refresh_event_async_obj = ev
        return ev

    def _bind_async_consumer_loop(self) -> None:
        with contextlib.suppress(RuntimeError):
            self._async_consumer_loop = asyncio.get_running_loop()

    def _async_notify_discard(self, task: asyncio.Task[Any]) -> None:
        with self._async_notify_tasks_lock:
            self._async_notify_tasks.discard(task)

    def _cancel_pending_async_notify_tasks(self) -> None:
        with self._async_notify_tasks_lock:
            pending = list(self._async_notify_tasks)
        for t in pending:
            if not t.done():
                t.cancel()

    def _notify_async_condition(self, *, notify_all: bool = False) -> None:
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

    async def _wait_async_coordinator(self, deadline: float | None) -> None:
        """Sleep between saturated retries on the asyncio :class:`~asyncio.Condition`.

        *wt* is computed under :attr:`_lock`, then the lock is released before awaiting (another task
        may run :meth:`aclose` in between; the ``cond`` object captured here remains valid). The
        final ``_closed`` check repeats under ``_lock``.

        On Python 3.11+, wraps the whole ``async with cond`` body in :class:`asyncio.timeout` (not
        the reverse) so timeout cancellation runs ``cond``'s ``__aexit__`` and releases the lock
        cleanly. Notifies from :meth:`_notify_async_condition` still shorten the wait. On 3.10 and
        below, ``asyncio.sleep`` runs **without** holding ``cond`` (no early wake on notify; avoids
        deadlock from sleeping while holding the condition lock).
        """
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
        wait_timeout = wt if wt > 0 else 0.001
        cond = self._async_condition
        timeout_cm = getattr(asyncio, "timeout", None)
        if timeout_cm is not None:
            async with cond:
                try:
                    async with timeout_cm(wait_timeout):
                        await cond.wait()
                except TimeoutError:
                    pass
        else:
            await asyncio.sleep(wait_timeout)
        with self._lock:
            if self._closed:
                raise PoolClosedError("proxy pool is closed")

    async def _aselect_candidate(self, **kwargs: Any) -> Proxy:
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
                    return self._state._nolock_select_candidate(kwargs, self._active_connections)
            except PoolSaturated:
                if self.config.acquire_timeout <= 0 or (
                    deadline is not None and time.monotonic() >= deadline
                ):
                    raise
                await self._wait_async_coordinator(deadline)
            except (PoolExhausted, NoMatchingProxy, PoolClosedError):
                raise

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

    def start_monitoring(self) -> None:
        if self.config.health_check is None:
            raise ValueError("PoolConfig.health_check is required for background monitoring")
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

    def stop_monitoring(self) -> None:
        t = self._health_task
        if t is not None and not t.done():
            t.cancel()
        self._health_task = None
        self._health_monitor = None

    @contextlib.asynccontextmanager
    async def monitoring(self):
        self.start_monitoring()
        try:
            yield self
        finally:
            self.stop_monitoring()

    async def aget_next(self, **kwargs: Any) -> Proxy:
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
            if self.config.hooks.on_exhausted is not None:
                self.config.hooks.on_exhausted()
            if self.config.arefresh_callback is not None:
                await self._run_arefresh_async()
                proxy = await self._aselect_candidate(**kwargs)
            else:
                raise
        except PoolSaturated:
            if self.config.hooks.on_saturated is not None:
                self.config.hooks.on_saturated()
            raise
        if self.config.hooks.on_proxy_acquired:
            self.config.hooks.on_proxy_acquired(proxy)
        return proxy

    async def __aenter__(self) -> Proxy:
        proxy = await self.aget_next()
        pt = self._task_proxy.set(proxy)
        hold = _AsyncExitHold(task_proxy_token=pt)
        hold.carry_reset_token = self._async_exit_carry.set(hold)
        return proxy

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> bool:
        carry = self._async_exit_carry.get()
        try:
            proxy = self._task_proxy.get()
            if proxy is not None:
                self._release_active_slot(proxy)
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

    def mark_failed(self, proxy: Proxy | str, exc_type: type | None = None) -> None:
        with self._lock:
            if self._closed:
                raise PoolClosedError("proxy pool is closed")
            p = Proxy(proxy) if not isinstance(proxy, Proxy) else proxy
            cooled, p = self._state._nolock_mark_failed(p, exc_type)
            if self._state._circuit_breaker is not None:
                self._state._circuit_breaker.record(False)
        self._notify_async_condition(notify_all=cooled)
        if cb := self.config.hooks.on_proxy_failed:
            cb(p, exc_type)
        if cooled and (cb_cd := self.config.hooks.on_proxy_cooled_down):
            cb_cd(p)

    def mark_success(self, proxy: Proxy | str, *, latency: float | None = None) -> None:
        with self._lock:
            if self._closed:
                raise PoolClosedError("proxy pool is closed")
            p = Proxy(proxy) if not isinstance(proxy, Proxy) else proxy
            prev_failures = self._state._nolock_mark_success(p, latency=latency)
            if self._state._circuit_breaker is not None:
                self._state._circuit_breaker.record(True)
        self._notify_async_condition(notify_all=False)
        if prev_failures > 0 and (cb := self.config.hooks.on_proxy_recovered):
            cb(p)

    def reset_pool(self) -> None:
        with self._lock:
            if self._closed:
                raise PoolClosedError("proxy pool is closed")
            self._state._nolock_reset()
        self._notify_async_condition(notify_all=True)

    async def aclose(self) -> None:
        """Close the pool and tear down async coordination.

        The final ``notify_all`` for blocked async waiters is scheduled on the consumer loop and
        may run *after* this coroutine returns; ``cond`` is captured by closure before lazy
        condition fields are cleared. Callers should not assume waiters have already been woken
        when ``await aclose()`` completes.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._cancel_pending_async_notify_tasks()
        loop = self._async_consumer_loop
        cond = self._async_condition_obj
        if loop is not None and loop.is_running() and cond is not None:

            async def _do_wake() -> None:
                # Untracked one-shot task: swallow failures; CancelledError is intentional here.
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    async with cond:
                        cond.notify_all()

            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None

            def _wake_done(t: asyncio.Task[None]) -> None:
                if t.cancelled():
                    return
                with contextlib.suppress(BaseException):
                    _ = t.exception()

            def _schedule_wake() -> None:
                # Untracked wake; see :meth:`aclose` docstring.
                wake_task = asyncio.create_task(_do_wake())
                wake_task.add_done_callback(_wake_done)

            if running is loop:
                _schedule_wake()
            else:
                loop.call_soon_threadsafe(_schedule_wake)
        self._async_condition_obj = None
        self._async_lock_obj = None
        self._refresh_event_async_obj = None
        self.stop_monitoring()

    def aacquire(self) -> AsyncProxyPool:
        return self


class ProxyPool(SyncProxyPool):
    """Deprecated alias for :class:`SyncProxyPool`."""

    __slots__ = ()

    def __init__(
        self,
        proxies: list[Proxy | str],
        config: PoolConfig | None = None,
        *,
        strategy: Strategy | None = None,
        cooldown: float | None = None,
    ) -> None:
        super().__init__(proxies, config, strategy=strategy, cooldown=cooldown)
        warnings.warn(
            "ProxyPool is deprecated; use SyncProxyPool for synchronous usage or AsyncProxyPool "
            "for asyncio-based usage. Note that `async with` on ProxyPool will fail.",
            DeprecationWarning,
            stacklevel=2,
        )


__all__ = [
    "AsyncPoolProtocol",
    "AsyncProxyPool",
    "BasePoolProtocol",
    "BaseProxyPool",
    "CircuitBreaker",
    "HealthMonitor",
    "LifecycleHooks",
    "LimitsConfig",
    "MonitorablePoolProtocol",
    "PoolConfig",
    "ProxyPool",
    "ProxyScore",
    "SessionStore",
    "SyncPoolProtocol",
    "SyncProxyPool",
    "TokenBucket",
]
