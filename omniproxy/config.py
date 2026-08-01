"""Process‑wide configuration for omniproxy (Pydantic v2 edition).

The module exposes:
- :class:`GlobalConfig` (process‑wide defaults, replace the old ``OmniproxyConfig``)
- :class:`PoolConfig` (orchestrates sub‑configs)
- Presets: :meth:`PoolConfig.scraping_preset`, etc.

All types are fully hinted – no ``Any`` in public signatures.
"""
from __future__ import annotations

import logging
import warnings
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Optional, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

if TYPE_CHECKING:
    from .extended_proxy import CheckResult, Proxy

from .constants import (
    DEFAULT_BACKEND,
    DEFAULT_CHECK_INFO_URL_TEMPLATES,
    DEFAULT_CHECK_URLS,
    DEFAULT_HEALTH_CHECK_URLS,
    DEFAULT_TIMEOUT,
    VALID_BACKENDS,
)
from .enum import (
    FilterMissingMetadata,
    PoolStrategy,
    PoolStructure,
    SessionCooldownPolicy,
    WarmupFailurePolicy,
    DeadLetterPersistence,
)

Strategy = PoolStrategy
Structure = PoolStructure

# ---------- Protocols (unchanged aside from StateStore) ----------
class TokenBucketProtocol(Protocol):
    """Minimal interface for per-proxy token-bucket rate limiters.

    Implementations are expected to be lightweight and re-entrant from the
    threads or tasks that the pool runs on.

    Version:
        Added in 4.0.0.
    """

    def consume(self, tokens: int = 1) -> bool:
        """Try to consume tokens from the bucket.

        Args:
            tokens (int): Number of tokens to remove. Defaults to ``1``.

        Returns:
            bool: ``True`` if the bucket had enough tokens, ``False`` if the
            caller should back off.

        Version:
            Added in 4.0.0.
        """
        ...

    def refill(self) -> None:
        """Replenish the bucket according to the implementation's schedule.

        Returns:
            None

        Version:
            Added in 4.0.0.
        """
        ...

    def tokens_available(self) -> float:
        """Return the current number of available tokens.

        Returns:
            float: Approximate token count; implementations may return a
            fractional value.

        Version:
            Added in 4.0.0.
        """
        ...

class MetricsExporter(Protocol):
    """Pluggable sink for pool metrics.

    Implementations forward gauges and counters to whatever observability
    backend the caller prefers (Prometheus, StatsD, OpenTelemetry, etc.).

    Version:
        Added in 4.0.0.
    """

    def emit_gauge(self, name: str, value: float, tags: dict[str, str] | None = None) -> None:
        """Emit a gauge sample.

        Args:
            name (str): Metric name.
            value (float): Current value.
            tags (dict[str, str] | None): Optional dimension tags.

        Returns:
            None

        Version:
            Added in 4.0.0.
        """
        ...

    def emit_counter(self, name: str, value: float, tags: dict[str, str] | None = None) -> None:
        """Emit a counter increment.

        Args:
            name (str): Metric name.
            value (float): Increment amount.
            tags (dict[str, str] | None): Optional dimension tags.

        Returns:
            None

        Version:
            Added in 4.0.0.
        """
        ...

    def close(self) -> None:
        """Flush and release exporter resources.

        Returns:
            None

        Version:
            Added in 4.0.0.
        """
        ...

class StateStore(Protocol):
    """Key/value store with optional TTL used for cross-process pool state.

    Values are strings (not necessarily floats) so callers can store JSON
    blobs or serialized counters as needed.

    Version:
        Added in 4.0.0.
    """

    def get(self, key: str) -> str | None:
        """Read the value stored at ``key``.

        Args:
            key (str): Lookup key.

        Returns:
            str | None: The stored value or ``None`` if the key is missing.

        Version:
            Added in 4.0.0.
        """
        ...

    def set(self, key: str, value: str, ttl: float | None = None) -> None:
        """Write a value at ``key`` with optional TTL.

        Args:
            key (str): Storage key.
            value (str): String payload to store.
            ttl (float | None): Optional time-to-live in seconds. When
                ``None``, the entry is persisted indefinitely.

        Returns:
            None

        Version:
            Added in 4.0.0.
        """
        ...

    def delete(self, key: str) -> None:
        """Delete the entry at ``key``.

        Args:
            key (str): Storage key to remove.

        Returns:
            None

        Version:
            Added in 4.0.0.
        """
        ...

# ---------- Helper for warmup_validator ----------
def bool_to_score(ok: bool) -> float:
    """Convert a pass/fail boolean into a numeric scoring value.

    A simple adapter for plugging boolean validators into the scoring system,
    which expects a float in ``[0.0, 1.0]``.

    Args:
        ok (bool): Whether the underlying check passed.

    Returns:
        float: ``1.0`` if ``ok`` is truthy, otherwise ``0.0``.

    Version:
        Added in 4.0.0.
    """
    return 1.0 if ok else 0.0

# ---------- Sub‑config models (Pydantic v2) ----------
class ScoringConfig(BaseModel):
    """Configuration for the latency/success scoring engine.

    Attributes:
        window_seconds (float): Length of the rolling observation window.
        decay_factor (float): Exponential decay applied to older samples;
            must be in ``(0, 1)`` exclusive.
        success_weight (float): Weight applied to success ratio
            (must sum to 1.0 with ``latency_weight``).
        latency_weight (float): Weight applied to inverse-latency
            (must sum to 1.0 with ``success_weight``).
        min_samples (int): Minimum samples before scoring becomes active.
        eviction_threshold (float): Score below which a proxy may be evicted.
        eviction_grace_period (float): Seconds a low-scoring proxy is given
            before it is evicted.

    Version:
        Added in 4.0.0.
    """

    model_config = ConfigDict(extra="forbid")
    window_seconds: float = 300.0
    decay_factor: float = 0.9
    success_weight: float = 0.6
    latency_weight: float = 0.4
    min_samples: int = 5
    eviction_threshold: float = 0.2
    eviction_grace_period: float = 60.0

    @model_validator(mode="after")
    def _validate_scoring_config(self) -> ScoringConfig:
        """Validate single-field scoring constraints.

        Returns:
            ScoringConfig: The validated instance.

        Raises:
            ValueError: If ``decay_factor`` is not in ``(0, 1)`` exclusive.

        Version:
            Added in 4.0.0.
        """
        if self.decay_factor <= 0 or self.decay_factor >= 1:
            raise ValueError("scoring.decay_factor must be between 0 and 1 exclusive")
        return self

class CircuitBreakerConfig(BaseModel):
    """Tuning knobs for the pool-wide :class:`CircuitBreaker`.

    Attributes:
        window_seconds (float): Sliding-window size for failure counting.
        failure_ratio (float): Failure ratio in ``(0, 1)`` required to trip.
        half_open_timeout (float): Seconds the breaker stays OPEN before
            allowing a single probe in HALF_OPEN.
        min_throughput (int): Minimum events in the window before the
            failure ratio is evaluated.

    Version:
        Added in 4.0.0.
    """

    model_config = ConfigDict(extra="forbid")
    window_seconds: float = 60.0
    failure_ratio: float = 0.5
    half_open_timeout: float = 30.0
    min_throughput: int = 10

class DeadLetterConfig(BaseModel):
    """Dead-letter queue configuration for chronically failing proxies.

    Attributes:
        enabled (bool): Toggle the dead-letter pipeline.
        max_size (int | None): Maximum entries to retain, or ``None`` for
            unbounded.
        retry_interval_seconds (float | None): If set, periodically attempt
            to re-introduce dead-lettered proxies after this many seconds.
        persistence (DeadLetterPersistence): Where to keep dead-letter
            state (memory, state store, etc.).

    Version:
        Added in 4.0.0.
    """

    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    max_size: int | None = 1000
    retry_interval_seconds: float | None = None
    persistence: DeadLetterPersistence = DeadLetterPersistence.MEMORY

# ---------- HealthCheckConfig (simplified) ----------
class HealthCheckConfig(BaseModel):
    """HTTP-based health check definition used during warmup and recovery.

    Attributes:
        url (str | None): Target URL. ``None`` defers to ``settings.health_check_urls``.
        method (str): HTTP method, e.g. ``"GET"`` or ``"HEAD"``.
        expected_status (int | None): Required status code, or ``None`` to skip status check.
        expected_json_fields (dict[str, Any] | None): Optional JSON body fields
            that must match for a successful response.
        timeout (float | None): Per-request timeout in seconds.
        headers (dict[str, str]): Extra headers to send with the request.
        recovery_interval (float): Seconds between recovery probes for
            cooled-down proxies.
        check_interval (float | None): Periodic check interval; ``None`` disables periodic checks.
        custom_check (Callable[[Proxy], bool] | None): Replace the HTTP check
            with a user-supplied callable.

    Version:
        Added in 4.0.0.
    """

    model_config = ConfigDict(extra="forbid")
    url: str | None = None
    method: str = "GET"
    expected_status: int | None = 200
    expected_json_fields: dict[str, Any] | None = None      # only this field now
    timeout: float | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    recovery_interval: float = 60.0
    check_interval: float | None = None
    custom_check: Callable[["Proxy"], bool] | None = None

class LimitsConfig(BaseModel):
    """Per-proxy concurrency and rate limits.

    Attributes:
        max_connections_per_proxy (int | None): Maximum simultaneous
            acquisitions of a single proxy.
        max_rps_per_proxy (float | None): Maximum requests per second per
            proxy enforced via a token bucket.
        token_bucket_capacity (float): Capacity (in tokens) for the per-proxy
            bucket.
        token_bucket_factory (Callable[[Proxy], Any] | None): Custom factory
            returning a :class:`TokenBucketProtocol` implementation.

    Version:
        Added in 4.0.0.
    """

    model_config = ConfigDict(extra="forbid")
    max_connections_per_proxy: int | None = None
    max_rps_per_proxy: float | None = None
    token_bucket_capacity: float = 1.0
    token_bucket_factory: Callable[["Proxy"], Any] | None = None

    @model_validator(mode="after")
    def _validate_limits(self) -> LimitsConfig:
        """Ensure ``max_connections_per_proxy`` is at least 1 when set.

        Returns:
            LimitsConfig: The validated instance.

        Raises:
            ValueError: If ``max_connections_per_proxy`` is below 1.

        Version:
            Added in 4.0.0.
        """
        if (
            self.max_connections_per_proxy is not None
            and self.max_connections_per_proxy < 1
        ):
            raise ValueError("limits.max_connections_per_proxy must be >= 1 when set")
        return self

# ---------- LifecycleHooks (fully typed) ----------
class LifecycleHooks(BaseModel):
    """Optional user callbacks invoked at key points of the pool lifecycle.

    Every hook is optional and defaults to ``None``. The pool catches and
    logs hook exceptions; failing hooks do not interrupt normal operation.

    Attributes:
        on_proxy_acquired (Callable[[Proxy], None] | None): Fired when a
            proxy is checked out.
        on_proxy_released (Callable[[Proxy], None] | None): Fired when a
            proxy is returned to the pool.
        on_proxy_failed (Callable[[Proxy, type | None], None] | None):
            Fired when a proxy is marked failed; second arg is the exception type.
        on_proxy_cooled_down (Callable[[Proxy], None] | None): Fired when a
            proxy enters cooldown.
        on_proxy_recovered (Callable[[Proxy], None] | None): Fired when a
            proxy returns to ACTIVE state.
        on_exhausted (Callable[[], None] | None): Fired when the pool has
            no usable proxies.
        on_saturated (Callable[[], None] | None): Fired when per-proxy
            limits prevent any acquisition.
        on_check_complete (Callable[[Proxy, CheckResult], None] | None):
            Fired after each external check call.
        on_refresh_started (Callable[[], None] | None): Fired before a
            background refresh.
        on_refresh_completed (Callable[[int], None] | None): Fired after a
            refresh, with the number of new proxies added.
        on_warmup_started (Callable[[], None] | None): Fired before warmup.
        on_warmup_completed (Callable[[int, int], None] | None): Fired
            after warmup with ``(passed, total)``.
        on_circuit_open (Callable[[], None] | None): Fired on breaker OPEN transition.
        on_circuit_close (Callable[[], None] | None): Fired on breaker CLOSE transition.
        on_auto_evicted (Callable[[Proxy, str], None] | None): Fired when a
            proxy is auto-evicted with the reason string.
        on_session_rebind (Callable[[str, Proxy, Proxy], None] | None):
            Fired when a sticky session swaps proxies.
        on_draining (Callable[[], None] | None): Fired when the pool starts draining.
        on_config_updated (Callable[[set[str]], None] | None): Fired when
            pool config is hot-reloaded with the set of changed field names.
        on_dead_letter_added (Callable[[Proxy, str | None], None] | None):
            Fired when a proxy is added to the dead-letter queue.

    Version:
        Added in 4.0.0.
    """

    model_config = ConfigDict(extra="forbid")
    on_proxy_acquired: Callable[["Proxy"], None] | None = None
    on_proxy_released: Callable[["Proxy"], None] | None = None
    on_proxy_failed: Callable[["Proxy", type | None], None] | None = None
    on_proxy_cooled_down: Callable[["Proxy"], None] | None = None
    on_proxy_recovered: Callable[["Proxy"], None] | None = None
    on_exhausted: Callable[[], None] | None = None
    on_saturated: Callable[[], None] | None = None
    on_check_complete: Callable[["Proxy", CheckResult], None] | None = None
    on_refresh_started: Callable[[], None] | None = None
    on_refresh_completed: Callable[[int], None] | None = None
    on_warmup_started: Callable[[], None] | None = None
    on_warmup_completed: Callable[[int, int], None] | None = None
    on_circuit_open: Callable[[], None] | None = None
    on_circuit_close: Callable[[], None] | None = None
    on_auto_evicted: Callable[["Proxy", str], None] | None = None
    on_session_rebind: Callable[[str, "Proxy", "Proxy"], None] | None = None
    on_draining: Callable[[], None] | None = None
    on_config_updated: Callable[[set[str]], None] | None = None
    on_dead_letter_added: Callable[["Proxy", str | None], None] | None = None

# ---------- Inner configurators for PoolConfig ----------
class CooldownConfig(BaseModel):
    """Cooldown policy for failing proxies.

    Attributes:
        base (float): Base cooldown duration in seconds.
        adaptive (bool): When ``True``, consecutive failures grow the
            cooldown geometrically toward ``max``.
        min (float): Minimum cooldown floor.
        max (float): Maximum cooldown ceiling.
        strategy (Callable[[float, int, int], float] | None): Custom function
            ``(base, consecutive_failures, total_failures) -> seconds`` that
            overrides the default adaptive logic.
        failure_threshold (int): Consecutive failures required before
            cooldown is applied.
        penalties (dict[type, float]): Multiplier applied to the cooldown
            for specific exception types.

    Version:
        Added in 4.0.0.
    """

    model_config = ConfigDict(extra="forbid")
    base: float = 300.0
    adaptive: bool = True
    min: float = 30.0
    max: float = 600.0
    strategy: Callable[[float, int, int], float] | None = None
    failure_threshold: int = 1
    penalties: dict[type, float] = Field(default_factory=dict)

class WarmupConfig(BaseModel):
    """Pool warmup behaviour run at start-up.

    Attributes:
        enabled (bool): Toggle the warmup phase.
        min_ready (int): Minimum number of validated proxies required to
            consider warmup successful.
        timeout (float): Hard deadline for the whole warmup phase, including
            in-flight health checks (unfinished probes are cancelled).
        failure_policy (WarmupFailurePolicy): Action when warmup fails (raise, partial, ignore).
        validator (Callable[[Proxy], float] | None): Optional custom validator
            returning a score in ``[0.0, 1.0]``.

    Version:
        Added in 4.0.0.
    """

    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    min_ready: int = 0
    timeout: float = 30.0
    failure_policy: WarmupFailurePolicy = WarmupFailurePolicy.RAISE
    validator: Callable[["Proxy"], float] | None = None   # float

class RefreshConfig(BaseModel):
    """Periodic background refresh configuration.

    Attributes:
        sync_callback (Callable[[], list[Proxy]] | None): Synchronous
            refresh source.
        async_callback (Callable[[], Awaitable[list[Proxy]]] | None):
            Asynchronous refresh source.
        fallback_sync_callbacks (list[Callable[[], list[Proxy]]]): Sync
            fallbacks tried in order when the primary fails.
        fallback_async_callbacks (list[Callable[[], Awaitable[list[Proxy]]]]):
            Async fallbacks tried in order when the primary fails.
        timeout (float): Maximum seconds for each refresh callback or
            fetcher ``fetch()`` call.
        interval_seconds (float): Period between refresh attempts.

    Version:
        Added in 4.0.0.
    """

    model_config = ConfigDict(extra="forbid")
    sync_callback: Callable[[], list["Proxy"]] | None = None
    async_callback: Callable[[], Awaitable[list["Proxy"]]] | None = None
    fallback_sync_callbacks: list[Callable[[], list["Proxy"]]] = Field(default_factory=list)
    fallback_async_callbacks: list[Callable[[], Awaitable[list["Proxy"]]]] = Field(default_factory=list)
    timeout: float = 10.0
    interval_seconds: float = 300.0

class SessionConfig(BaseModel):
    """Sticky session configuration.

    Attributes:
        ttl (float): Session lifetime in seconds before the binding expires.
        cooldown_policy (SessionCooldownPolicy): What to do when a sticky
            proxy enters cooldown (rebind, block, etc.).

    Version:
        Added in 4.0.0.
    """

    model_config = ConfigDict(extra="forbid")
    ttl: float = 300.0
    cooldown_policy: SessionCooldownPolicy = SessionCooldownPolicy.REBIND

# ---------- Global config (Pydantic v2, frozen) ----------
class GlobalConfig(BaseModel):
    """Immutable, thread-safe process-wide defaults.

    Use the module-level :data:`settings` singleton instead of instantiating
    this directly; the singleton is frozen so mutation requires constructing
    a new instance.

    Attributes:
        default_backend (str): Default HTTP backend identifier (e.g.
            ``"httpx"``, ``"requests"``, ``"curl_cffi"``).
        default_timeout (float | None): Default request timeout in seconds.
        default_connect_timeout (float | None): Default connection timeout.
        default_check_urls (tuple[str, ...]): URLs used by the standard checkers.
        default_check_info_url_templates (tuple[str, ...]): Templates used
            for IP-info checks (e.g. anonymity classification).
        health_check_urls (tuple[str, ...]): URLs used by pool health checks.

    Version:
        Added in 4.0.0.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    default_backend: str = DEFAULT_BACKEND
    default_timeout: float | None = DEFAULT_TIMEOUT
    default_connect_timeout: float | None = None
    default_check_urls: tuple[str, ...] = DEFAULT_CHECK_URLS
    default_check_info_url_templates: tuple[str, ...] = DEFAULT_CHECK_INFO_URL_TEMPLATES
    health_check_urls: tuple[str, ...] = DEFAULT_HEALTH_CHECK_URLS

    @field_validator("default_backend")
    @classmethod
    def _validate_backend(cls, v: str) -> str:
        """Normalize and validate the backend identifier.

        Args:
            v (str): Raw backend name (e.g. ``"curlcffi"``).

        Returns:
            str: Normalized backend key as registered in :data:`VALID_BACKENDS`.

        Raises:
            ValueError: If the backend is not recognised.

        Version:
            Added in 4.0.0.
        """
        key = v.lower().replace("-", "_")
        if key == "curlcffi":
            key = "curl_cffi"
        if key not in VALID_BACKENDS:
            raise ValueError(f"Unknown backend {v!r}")
        return key

    @field_validator("default_timeout", "default_connect_timeout")
    @classmethod
    def _validate_timeout(cls, v: float | None, info) -> float | None:
        """Validate that timeouts are positive numbers or ``None``.

        Args:
            v (float | None): Candidate timeout in seconds.
            info: Pydantic field info; used to produce field-specific errors.

        Returns:
            float | None: The coerced timeout or ``None``.

        Raises:
            TypeError: If ``v`` is not numeric.
            ValueError: If ``v`` is not strictly positive.

        Version:
            Added in 4.0.0.
        """
        if v is not None:
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                raise TypeError(f"{info.field_name} must be a number or None")
            if v <= 0:
                raise ValueError(f"{info.field_name} must be positive")
            return float(v)
        return v

    @field_validator("default_check_urls", "default_check_info_url_templates", "health_check_urls")
    @classmethod
    def _validate_url_tuples(cls, v: tuple[str, ...], info) -> tuple[str, ...]:
        """Validate that URL tuples contain non-empty strings only.

        Args:
            v (tuple[str, ...]): Raw URL collection.
            info: Pydantic field info used in error messages.

        Returns:
            tuple[str, ...]: Frozen tuple of validated URLs.

        Raises:
            TypeError: If the value is not a list or tuple.
            ValueError: If any element is not a non-empty string.

        Version:
            Added in 4.0.0.
        """
        if not isinstance(v, (list, tuple)):
            raise TypeError(f"{info.field_name} must be a list/tuple")
        for i, item in enumerate(v):
            if not isinstance(item, str) or not item.strip():
                raise ValueError(f"{info.field_name}[{i}] must be a non‑empty string")
        return tuple(v)

    @model_validator(mode="after")
    def _validate_config(self) -> GlobalConfig:
        """Cross-field validation for :class:`GlobalConfig`.

        Returns:
            GlobalConfig: The validated instance.

        Raises:
            ValueError: If ``default_check_urls`` is empty.

        Version:
            Added in 4.0.0.
        """
        if not self.default_check_urls:
            raise ValueError("default_check_urls must be non‑empty")
        return self

# Singleton (immutable, so no lock needed after creation)
settings = GlobalConfig()

# ---------- PoolConfig (simplified orchestrator) ----------
class PoolConfig(BaseModel):
    """Top-level proxy-pool configuration.

    All behavioural details are delegated to sub-config models; only fields
    that cannot be cleanly grouped elsewhere remain here. Instances are
    frozen, so apply changes by building a new ``PoolConfig`` via
    ``model_copy(update=...)``.

    Attributes:
        strategy (PoolStrategy): How the pool picks the next proxy.
        structure (PoolStructure): Underlying container type.
        cooldown (CooldownConfig): Cooldown policy.
        warmup (WarmupConfig): Warmup behaviour.
        refresh (RefreshConfig): Background refresh sources.
        session (SessionConfig): Sticky session behaviour.
        limits (LimitsConfig): Per-proxy rate and connection limits.
        hooks (LifecycleHooks): User callbacks for lifecycle events.
        health_check (HealthCheckConfig | None): Optional health-check spec.
        scoring (ScoringConfig | None): Optional scoring engine config.
        circuit_breaker (CircuitBreakerConfig | None): Optional breaker config.
        dead_letter (DeadLetterConfig): Dead-letter pipeline.
        acquire_timeout (float): Acquire wait policy. ``0`` returns immediately
            after on-demand refresh, ``>0`` waits up to that many seconds,
            ``<0`` waits forever.
        wait_fallback_interval (float): Polling interval used as a fallback
            when condition variables cannot be used.
        filter_missing_metadata (FilterMissingMetadata): How to handle
            proxies that lack required metadata at acquisition time.
        accept_callback (Callable[[Proxy, dict], bool] | None): Custom
            acceptance predicate invoked at acquisition.
        auto_mark_failed_on_exception (bool): Mark proxy failed when an
            unhandled exception leaves the ``with`` block.
        auto_mark_success_on_exit (bool): Mark proxy success on clean exit.
        reraise (bool): Whether to re-raise exceptions from ``with`` blocks.
        dedup_key (Callable[[Proxy], str] | None): Custom dedup key function.
        acquire_tags (set[str] | None): Restrict acquisitions to proxies
            carrying any of these tags.
        use_rotation_urls (bool): Call the proxy's rotation URL on acquire.
        rotate_on_acquire (bool): Rotate the proxy on acquisition.
        rotate_on_failure (bool): Rotate the proxy after a failure.
        backend_override (Callable[[Proxy], str | None] | None): Choose a
            backend per-proxy.
        drain_timeout (float): Seconds to wait for in-flight acquisitions
            during shutdown.
        min_size (int | None): Lower bound on proxy count.
        max_size (int | None): Upper bound on proxy count.
        ignore_exceptions (tuple[type, ...]): Exceptions ignored for
            cooldown accounting.
        proxy_failure_classifier (Callable[[BaseException, Optional[Proxy]], bool] | None):
            Custom predicate marking exceptions as "proxy failures".
        metrics_exporter (Any | None): Optional :class:`MetricsExporter`.
        log_level (int): Standard ``logging`` level for the pool's logger.
        state_store_factory (Callable[[], Any] | None): Factory returning a
            :class:`StateStore` instance.
        extra (dict[str, Any]): Free-form extension dictionary.

    Version:
        Added in 4.0.0.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy: PoolStrategy = PoolStrategy.ROUND_ROBIN
    structure: PoolStructure = PoolStructure.DEQUE
    cooldown: CooldownConfig = Field(default_factory=CooldownConfig)
    warmup: WarmupConfig = Field(default_factory=WarmupConfig)
    refresh: RefreshConfig = Field(default_factory=RefreshConfig)
    session: SessionConfig = Field(default_factory=SessionConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    hooks: LifecycleHooks = Field(default_factory=LifecycleHooks)
    health_check: HealthCheckConfig | None = None
    scoring: ScoringConfig | None = None
    circuit_breaker: CircuitBreakerConfig | None = None
    dead_letter: DeadLetterConfig = Field(default_factory=DeadLetterConfig)

    # ----- Pool‑wide meta fields -----
    # acquire_timeout: 0 = no condition wait (still tries on-demand refresh), >0 = timed wait, <0 = wait forever
    acquire_timeout: float = 0.0
    wait_fallback_interval: float = 0.25
    filter_missing_metadata: FilterMissingMetadata = FilterMissingMetadata.SKIP
    accept_callback: Callable[["Proxy", dict], bool] | None = None
    auto_mark_failed_on_exception: bool = True
    auto_mark_success_on_exit: bool = False
    reraise: bool = True
    dedup_key: Callable[["Proxy"], str] | None = None
    acquire_tags: set[str] | None = None
    use_rotation_urls: bool = False
    rotate_on_acquire: bool = False
    rotate_on_failure: bool = False
    backend_override: Callable[["Proxy"], str | None] | None = None
    drain_timeout: float = 30.0
    min_size: int | None = None
    max_size: int | None = None
    ignore_exceptions: tuple[type, ...] = ()
    proxy_failure_classifier: Callable[[BaseException, Optional["Proxy"]], bool] | None = None
    metrics_exporter: Any | None = None
    log_level: int = logging.INFO
    state_store_factory: Callable[[], Any] | None = None
    extra: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_pool_config(self) -> PoolConfig:
        """Cross-field validation for :class:`PoolConfig`.

        Verifies that scoring weights sum to ``1.0``, warmup and dead-letter
        have a health check when enabled, cooldown bounds are sane, refresh
        timing is sensible, sizes are non-negative, dead-letter values are
        valid, the circuit breaker configuration is in range, and that
        score-dependent strategies have a scoring config. Emits warnings for
        soft inconsistencies (e.g. rotation URLs without ``rotate_on_acquire``).

        Returns:
            PoolConfig: The validated instance.

        Raises:
            ValueError: If any constraint is violated.

        Version:
            Added in 4.0.0.
        """
        # scoring weights
        s = self.scoring
        if s is not None and abs(s.success_weight + s.latency_weight - 1.0) > 1e-9:
            raise ValueError("scoring.success_weight + scoring.latency_weight must equal 1.0")

        # warmup + health_check
        if self.warmup.enabled and self.health_check is None:
            raise ValueError("health_check must be provided when warmup.enabled=True")

        # cooldown bounds
        c = self.cooldown
        if c.min > c.max:
            raise ValueError("cooldown.min cannot exceed cooldown.max")

        # sizes
        if self.drain_timeout < 0:
            raise ValueError("drain_timeout must be >= 0")

        refresh = self.refresh
        if refresh.interval_seconds <= 0:
            raise ValueError("refresh.interval_seconds must be > 0")
        if refresh.timeout <= 0:
            raise ValueError("refresh.timeout must be > 0")
        if refresh.async_callback and refresh.sync_callback:
            warnings.warn(
                "Both refresh.async_callback and refresh.sync_callback are set; "
                "async is tried first, then sync.",
                stacklevel=2,
            )
        if refresh.timeout >= refresh.interval_seconds:
            warnings.warn(
                "refresh.timeout >= refresh.interval_seconds; "
                "background refresh cycles may overlap.",
                stacklevel=2,
            )
        if self.min_size is not None and self.min_size < 0:
            raise ValueError("min_size must be >= 0")
        if self.max_size is not None and self.max_size < 0:
            raise ValueError("max_size must be >= 0")
        if self.min_size is not None and self.max_size is not None and self.min_size > self.max_size:
            raise ValueError("min_size cannot exceed max_size")

        # dead letter
        dl = self.dead_letter
        if dl.enabled and self.health_check is None:
            raise ValueError("health_check must be provided when dead_letter.enabled=True")
        if dl.max_size is not None and dl.max_size < 0:
            raise ValueError("dead_letter.max_size must be >= 0")
        if dl.retry_interval_seconds is not None and dl.retry_interval_seconds <= 0:
            raise ValueError("dead_letter.retry_interval_seconds must be > 0")
        if dl.persistence == DeadLetterPersistence.STATE_STORE and self.state_store_factory is None:
            warnings.warn(
                "dead_letter.persistence='state_store' but state_store_factory is None; "
                "persisted dead-letter behaviour requires a factory.",
                stacklevel=2,
            )

        # circuit breaker
        cb = self.circuit_breaker
        if cb is not None:
            if cb.failure_ratio <= 0 or cb.failure_ratio >= 1:
                raise ValueError("circuit_breaker.failure_ratio must be between 0 and 1 exclusive")
            if cb.half_open_timeout <= 0:
                raise ValueError("circuit_breaker.half_open_timeout must be > 0")

        # weighted / lowest-latency strategies need scoring data
        if self.strategy == PoolStrategy.WEIGHTED and self.scoring is None:
            raise ValueError(
                "strategy='weighted' requires a ScoringConfig; pass scoring=ScoringConfig() "
                "or switch to a different strategy."
            )
        if self.strategy == PoolStrategy.LOWEST_LATENCY and self.scoring is None:
            raise ValueError(
                "strategy='lowest_latency' requires a ScoringConfig; pass scoring=ScoringConfig() "
                "or switch to a different strategy."
            )

        # rotation consistency
        if self.use_rotation_urls and not self.rotate_on_acquire:
            warnings.warn(
                "use_rotation_urls=True but rotate_on_acquire=False; rotation URLs will not be called on acquire.",
                stacklevel=2,
            )

        return self

    # ---------- Presets (now exhaustive) ----------
    @classmethod
    def scraping_preset(cls) -> PoolConfig:
        """Preset tuned for high-volume web scraping.

        Uses round-robin selection, moderate cooldown, generous concurrency,
        adaptive scoring with relatively quick eviction, and an
        opinionated circuit breaker. Warmup is disabled because scraping
        traffic itself is the validation signal.

        Returns:
            PoolConfig: Frozen configuration ready to pass to a pool.

        Version:
            Added in 4.0.0.
        """
        return cls(
            strategy=PoolStrategy.ROUND_ROBIN,
            cooldown=CooldownConfig(base=120.0, adaptive=True, min=15.0, max=300.0,
                                    failure_threshold=2,
                                    penalties={ConnectionError: 2.0, TimeoutError: 1.5}),
            acquire_timeout=10.0,
            wait_fallback_interval=0.5,
            limits=LimitsConfig(max_connections_per_proxy=50, max_rps_per_proxy=5.0,
                                token_bucket_capacity=2.0),
            scoring=ScoringConfig(window_seconds=120.0, eviction_threshold=0.15,
                                  eviction_grace_period=30.0),
            circuit_breaker=CircuitBreakerConfig(failure_ratio=0.6, half_open_timeout=15.0,
                                                 min_throughput=20),
            session=SessionConfig(cooldown_policy=SessionCooldownPolicy.REBIND),
            warmup=WarmupConfig(enabled=False),                         # no warm‑up needed for scraping
            health_check=None,                                          # rely on runtime failure detection
            auto_mark_failed_on_exception=True,
            auto_mark_success_on_exit=True,
            filter_missing_metadata=FilterMissingMetadata.SKIP,
            log_level=logging.WARNING,
        )

    @classmethod
    def api_gateway_preset(cls) -> PoolConfig:
        """Preset tuned for API gateway / structured-API workloads.

        Uses weighted selection driven by scoring, conservative concurrency,
        long cooldowns, mandatory warmup against a health check, and strict
        metadata validation so misconfigured proxies fail fast.

        Returns:
            PoolConfig: Frozen configuration ready to pass to a pool.

        Version:
            Added in 4.0.0.
        """
        return cls(
            strategy=PoolStrategy.WEIGHTED,
            cooldown=CooldownConfig(base=600.0, adaptive=True, min=120.0, max=1800.0,
                                    failure_threshold=5),
            acquire_timeout=30.0,
            limits=LimitsConfig(max_connections_per_proxy=5, max_rps_per_proxy=1.0),
            scoring=ScoringConfig(window_seconds=600.0, eviction_threshold=0.1,
                                  eviction_grace_period=300.0),
            circuit_breaker=CircuitBreakerConfig(failure_ratio=0.4, half_open_timeout=60.0,
                                                 min_throughput=5),
            session=SessionConfig(cooldown_policy=SessionCooldownPolicy.BLOCK),
            warmup=WarmupConfig(enabled=True, min_ready=1, timeout=15.0,
                                failure_policy=WarmupFailurePolicy.PARTIAL),
            health_check=HealthCheckConfig(),
            auto_mark_failed_on_exception=True,
            auto_mark_success_on_exit=True,
            filter_missing_metadata=FilterMissingMetadata.RAISE,
            log_level=logging.INFO,
        )

    @classmethod
    def stealth_preset(cls) -> PoolConfig:
        """Preset tuned for stealthy, low-rate browsing.

        Picks the lowest-latency proxy, applies aggressive cooldown
        penalties, rotates on acquire, and forces strict metadata to mimic
        a small number of careful clients.

        Returns:
            PoolConfig: Frozen configuration ready to pass to a pool.

        Version:
            Added in 4.0.0.
        """
        return cls(
            strategy=PoolStrategy.LOWEST_LATENCY,
            cooldown=CooldownConfig(base=900.0, adaptive=False, min=300.0, max=3600.0,
                                    failure_threshold=1,
                                    penalties={ConnectionError: 5.0, TimeoutError: 4.0}),
            acquire_timeout=30.0,
            limits=LimitsConfig(max_connections_per_proxy=2, max_rps_per_proxy=0.5),
            scoring=ScoringConfig(window_seconds=600.0, eviction_threshold=0.05,
                                  eviction_grace_period=600.0),
            circuit_breaker=CircuitBreakerConfig(failure_ratio=0.3, half_open_timeout=120.0,
                                                 min_throughput=3),
            session=SessionConfig(cooldown_policy=SessionCooldownPolicy.BLOCK),
            warmup=WarmupConfig(enabled=True, min_ready=1, timeout=60.0,
                                failure_policy=WarmupFailurePolicy.RAISE),
            health_check=HealthCheckConfig(),
            auto_mark_failed_on_exception=True,
            auto_mark_success_on_exit=True,
            filter_missing_metadata=FilterMissingMetadata.RAISE,
            log_level=logging.WARNING,
            rotate_on_acquire=True,            # frequent IP rotation
        )

    @classmethod
    def rotating_residential_preset(cls) -> PoolConfig:
        """Preset tuned for rotating residential proxies.

        Uses random selection (each acquire yields a new IP via the
        rotation URL), moderate cooldown, and skips warmup since rotation
        provides a fresh endpoint every call.

        Returns:
            PoolConfig: Frozen configuration ready to pass to a pool.

        Version:
            Added in 4.0.0.
        """
        return cls(
            strategy=PoolStrategy.RANDOM,
            cooldown=CooldownConfig(base=180.0, adaptive=True, min=30.0, max=600.0,
                                    failure_threshold=3),
            acquire_timeout=5.0,
            limits=LimitsConfig(max_connections_per_proxy=10, max_rps_per_proxy=2.0),
            scoring=ScoringConfig(window_seconds=300.0, eviction_threshold=0.2,
                                  eviction_grace_period=120.0),
            circuit_breaker=CircuitBreakerConfig(failure_ratio=0.5, half_open_timeout=30.0,
                                                 min_throughput=10),
            session=SessionConfig(cooldown_policy=SessionCooldownPolicy.REBIND),
            warmup=WarmupConfig(enabled=False),  # rotation URLs give a new IP each call
            use_rotation_urls=True,
            rotate_on_acquire=True,
            auto_mark_failed_on_exception=True,
            auto_mark_success_on_exit=True,
            filter_missing_metadata=FilterMissingMetadata.SKIP,
            log_level=logging.INFO,
            # No refresh callback provided; user supplies their own
        )

    @classmethod
    def load_balancer_preset(cls) -> PoolConfig:
        """Preset tuned for round-robin load balancing behind a fleet of proxies.

        Disables scoring and the circuit breaker, relies on quick recovery
        via health checks, and ignores proxies with missing metadata so it
        is forgiving toward heterogeneous fleets.

        Returns:
            PoolConfig: Frozen configuration ready to pass to a pool.

        Version:
            Added in 4.0.0.
        """
        return cls(
            strategy=PoolStrategy.ROUND_ROBIN,
            cooldown=CooldownConfig(base=30.0, adaptive=False, failure_threshold=1),
            acquire_timeout=0.0,
            limits=LimitsConfig(),
            scoring=None,                          # no scoring – aggressive health checks only
            circuit_breaker=None,
            warmup=WarmupConfig(enabled=False),    # warm‑up off; health checks run on failure
            health_check=HealthCheckConfig(recovery_interval=10.0),
            auto_mark_failed_on_exception=True,
            auto_mark_success_on_exit=False,
            filter_missing_metadata=FilterMissingMetadata.IGNORE,
            log_level=logging.WARNING,
        )


__all__ = [
    "CircuitBreakerConfig",
    "CooldownConfig",
    "DeadLetterConfig",
    "DeadLetterPersistence",
    "FilterMissingMetadata",
    "GlobalConfig",
    "HealthCheckConfig",
    "LifecycleHooks",
    "LimitsConfig",
    "MetricsExporter",
    "PoolConfig",
    "RefreshConfig",
    "ScoringConfig",
    "SessionConfig",
    "StateStore",
    "Strategy",
    "Structure",
    "TokenBucketProtocol",
    "WarmupConfig",
    "WarmupFailurePolicy",
    "bool_to_score",
    "settings",
]