"""Process‑wide configuration for omniproxy (Pydantic v2 edition).

The module exposes:
- :class:`GlobalConfig` (process‑wide defaults, replace the old ``OmniproxyConfig``)
- :class:`PoolConfig` (orchestrates sub‑configs)
- Presets: :meth:`PoolConfig.scraping_preset`, etc.

All types are fully hinted – no ``Any`` in public signatures.
"""
from __future__ import annotations

import logging
import threading
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
    def consume(self, tokens: int = 1) -> bool: ...
    def refill(self) -> None: ...
    def tokens_available(self) -> float: ...

class MetricsExporter(Protocol):
    def emit_gauge(self, name: str, value: float, tags: dict[str, str] | None = None) -> None: ...
    def emit_counter(self, name: str, value: float, tags: dict[str, str] | None = None) -> None: ...
    def close(self) -> None: ...

class StateStore(Protocol):
    """Key/value store with optional TTL.  Values are strings (*not* only floats)."""
    def get(self, key: str) -> str | None: ...
    def set(self, key: str, value: str, ttl: float | None = None) -> None: ...
    def delete(self, key: str) -> None: ...

# ---------- Helper for warmup_validator ----------
def bool_to_score(ok: bool) -> float:
    """Convert a pass/fail boolean to a 0.0/1.0 score."""
    return 1.0 if ok else 0.0

# ---------- Sub‑config models (Pydantic v2) ----------
class ScoringConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    window_seconds: float = 300.0
    decay_factor: float = 0.9
    success_weight: float = 0.6
    latency_weight: float = 0.4
    min_samples: int = 5
    eviction_threshold: float = 0.2
    eviction_grace_period: float = 60.0

class CircuitBreakerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    window_seconds: float = 60.0
    failure_ratio: float = 0.5
    half_open_timeout: float = 30.0
    min_throughput: int = 10

class DeadLetterConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    max_size: int | None = 1000
    retry_interval_seconds: float | None = None
    persistence: DeadLetterPersistence = DeadLetterPersistence.MEMORY

# ---------- HealthCheckConfig (simplified) ----------
class HealthCheckConfig(BaseModel):
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
    model_config = ConfigDict(extra="forbid")
    max_connections_per_proxy: int | None = None
    max_rps_per_proxy: float | None = None
    token_bucket_capacity: float = 1.0
    token_bucket_factory: Callable[["Proxy"], Any] | None = None

# ---------- LifecycleHooks (fully typed) ----------
class LifecycleHooks(BaseModel):
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
    model_config = ConfigDict(extra="forbid")
    base: float = 300.0
    adaptive: bool = True
    min: float = 30.0
    max: float = 600.0
    strategy: Callable[[float, int, int], float] | None = None
    failure_threshold: int = 1
    penalties: dict[type, float] = Field(default_factory=dict)

class WarmupConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    min_ready: int = 0
    timeout: float = 30.0
    failure_policy: WarmupFailurePolicy = WarmupFailurePolicy.RAISE
    validator: Callable[["Proxy"], float] | None = None   # float

class RefreshConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sync_callback: Callable[[], list["Proxy"]] | None = None
    async_callback: Callable[[], Awaitable[list["Proxy"]]] | None = None
    fallback_sync_callbacks: list[Callable[[], list["Proxy"]]] = Field(default_factory=list)
    fallback_async_callbacks: list[Callable[[], Awaitable[list["Proxy"]]]] = Field(default_factory=list)
    timeout: float = 10.0

class SessionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ttl: float = 300.0
    cooldown_policy: SessionCooldownPolicy = SessionCooldownPolicy.REBIND

# ---------- Global config (Pydantic v2, frozen) ----------
class GlobalConfig(BaseModel):
    """Immutable, thread‑safe process‑wide defaults.  Use the module‑level ``settings`` singleton."""
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
        key = v.lower().replace("-", "_")
        if key == "curlcffi":
            key = "curl_cffi"
        if key not in VALID_BACKENDS:
            raise ValueError(f"Unknown backend {v!r}")
        return key

    @field_validator("default_timeout", "default_connect_timeout")
    @classmethod
    def _validate_timeout(cls, v: float | None, info) -> float | None:
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
        if not isinstance(v, (list, tuple)):
            raise TypeError(f"{info.field_name} must be a list/tuple")
        for i, item in enumerate(v):
            if not isinstance(item, str) or not item.strip():
                raise ValueError(f"{info.field_name}[{i}] must be a non‑empty string")
        return tuple(v)

    @model_validator(mode="after")
    def _validate_config(self) -> GlobalConfig:
        # default_check_urls must not be empty
        if not self.default_check_urls:
            raise ValueError("default_check_urls must be non‑empty")
        return self

# Singleton (immutable, so no lock needed after creation)
settings = GlobalConfig()

# ---------- PoolConfig (simplified orchestrator) ----------
class PoolConfig(BaseModel):
    """Top‑level proxy‑pool configuration.

    All behavioural details are delegated to sub‑config models;
    only fields that cannot be cleanly grouped elsewhere remain here.
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
        if self.min_size is not None and self.min_size < 0:
            raise ValueError("min_size must be >= 0")
        if self.max_size is not None and self.max_size < 0:
            raise ValueError("max_size must be >= 0")
        if self.min_size is not None and self.max_size is not None and self.min_size > self.max_size:
            raise ValueError("min_size cannot exceed max_size")

        # dead letter
        dl = self.dead_letter
        if dl.max_size is not None and dl.max_size < 0:
            raise ValueError("dead_letter.max_size must be >= 0")
        if dl.retry_interval_seconds is not None and dl.retry_interval_seconds <= 0:
            raise ValueError("dead_letter.retry_interval_seconds must be > 0")
        if dl.persistence == DeadLetterPersistence.STATE_STORE and self.state_store_factory is None:
            import warnings
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

        # weighted strategy
        if self.strategy == PoolStrategy.WEIGHTED and self.scoring is None:
            raise ValueError(
                "strategy='weighted' requires a ScoringConfig; pass scoring=ScoringConfig() "
                "or switch to a different strategy."
            )

        # rotation consistency
        if self.use_rotation_urls and not self.rotate_on_acquire:
            import warnings
            warnings.warn(
                "use_rotation_urls=True but rotate_on_acquire=False; rotation URLs will not be called on acquire.",
                stacklevel=2,
            )

        return self

    # ---------- Presets (now exhaustive) ----------
    @classmethod
    def scraping_preset(cls) -> PoolConfig:
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