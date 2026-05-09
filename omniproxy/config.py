"""Global defaults for omniproxy v2.1 (backends, timeouts, check URLs, scoring, circuit breaker)."""

from __future__ import annotations

import logging
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from .extended_proxy import CheckResult

from .constants import (
    DEFAULT_BACKEND,
    DEFAULT_CHECK_INFO_URL_TEMPLATES,
    DEFAULT_CHECK_URLS,
    DEFAULT_HEALTH_CHECK_URLS,
    DEFAULT_TIMEOUT,
    OMNIPROXY_CONFIG_PUBLIC_KEYS,
    VALID_BACKENDS,
)
from .enum import (
    FilterMissingMetadata,
    HealthStrategy,
    PoolStrategy,
    PoolStructure,
    SessionCooldownPolicy,
    WarmupFailurePolicy,
)

Strategy = PoolStrategy
Structure = PoolStructure


# v2.1: New protocols
class TokenBucketProtocol(Protocol):
    def consume(self, tokens: int = 1) -> bool: ...
    def refill(self) -> None: ...
    def tokens_available(self) -> float: ...


class MetricsExporter(Protocol):
    def emit_gauge(self, name: str, value: float, tags: dict[str, str] | None = None) -> None: ...
    def emit_counter(self, name: str, value: float, tags: dict[str, str] | None = None) -> None: ...
    def close(self) -> None: ...


class StateStore(Protocol):
    def get(self, key: str) -> float | None: ...
    def set(self, key: str, value: float, ttl: float | None = None) -> None: ...
    def delete(self, key: str) -> None: ...


# ---------------------------------------------------------------------------
# New configuration dataclasses for v2.1
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ScoringConfig:
    window_seconds: float = 300.0
    decay_factor: float = 0.9  # EMA decay for latency (0<decay≤1)
    success_weight: float = 0.6
    latency_weight: float = 0.4
    min_samples: int = 5
    eviction_threshold: float = 0.2
    eviction_grace_period: float = 60.0  # seconds score must stay below threshold


@dataclass(slots=True)
class CircuitBreakerConfig:
    window_seconds: float = 60.0
    failure_ratio: float = 0.5
    half_open_timeout: float = 30.0
    min_throughput: int = 10


@dataclass(slots=True)
class HealthCheckConfig:
    """Now supports custom_check, method, expected_json_fields per v2.1 spec."""

    url: str | None = None
    method: str = "GET"
    expected_status: int | None = 200
    expected_fields: set[str] | None = None  # backwards compat
    expected_json_fields: dict[str, Any] | None = None  # new
    timeout: float | None = None
    headers: dict[str, str] = field(default_factory=dict)
    recovery_interval: float = 60.0
    custom_check: Callable[[Any], bool] | None = None  # Any = Proxy


@dataclass(slots=True)
class LimitsConfig:
    max_connections_per_proxy: int | None = None
    max_rps_per_proxy: float | None = None
    token_bucket_capacity: float = 1.0
    token_bucket_factory: Callable[[Any], TokenBucketProtocol] | None = None  # Any = Proxy


# Existing LifecycleHooks, extended with v2.1 callbacks
@dataclass(slots=True)
class LifecycleHooks:
    on_proxy_acquired: Callable[[Any], None] | None = None  # Proxy
    on_proxy_released: Callable[[Any], None] | None = None  # Proxy
    on_proxy_failed: Callable[[Any, type | None], None] | None = None  # Proxy, exc type
    on_proxy_cooled_down: Callable[[Any], None] | None = None
    on_proxy_recovered: Callable[[Any], None] | None = None
    on_exhausted: Callable[[], None] | None = None
    on_saturated: Callable[[], None] | None = None
    on_check_complete: Callable[[Any, CheckResult], None] | None = None
    # v2.1 additions
    on_refresh_started: Callable[[], None] | None = None
    on_refresh_completed: Callable[[int], None] | None = None  # proxies_added
    on_warmup_started: Callable[[], None] | None = None
    on_warmup_completed: Callable[[int, int], None] | None = None  # ready_count, total
    on_circuit_open: Callable[[], None] | None = None
    on_circuit_close: Callable[[], None] | None = None
    on_auto_evicted: Callable[[Any, str], None] | None = None  # proxy, reason
    on_session_rebind: Callable[[str, Any, Any], None] | None = None  # session_id, old, new
    on_draining: Callable[[], None] | None = None
    on_config_updated: Callable[[set[str]], None] | None = None


@dataclass(slots=True)
class PoolConfig:
    strategy: PoolStrategy = PoolStrategy.ROUND_ROBIN
    structure: PoolStructure = PoolStructure.DEQUE  # automatically adjusted if needed
    acquire_timeout: float = 0.0
    wait_fallback_interval: float = 0.25

    cooldown: float = 300.0  # base_cooldown in spec
    adaptive_cooldown: bool = True
    min_cooldown: float = 30.0
    max_cooldown: float = 600.0
    cooldown_strategy: Callable[[float, int, int], float] | None = (
        None  # base, active, total -> secs
    )
    failure_threshold: int = 1  # number of mark_failed before cooldown
    failure_penalties: dict[type, float] = field(default_factory=dict)

    limits: LimitsConfig = field(default_factory=LimitsConfig)
    hooks: LifecycleHooks = field(default_factory=LifecycleHooks)

    filter_missing_metadata: FilterMissingMetadata = FilterMissingMetadata.SKIP

    refresh_callback: Callable[[], list[Any]] | None = None  # list[Proxy|str]
    arefresh_callback: Callable[[], Awaitable[list[Any]]] | None = None
    fallback_refresh_callbacks: list[Callable[[], list[Any]]] = field(default_factory=list)
    afallback_refresh_callbacks: list[Callable[[], Awaitable[list[Any]]]] = field(
        default_factory=list
    )
    refresh_timeout: float = 10.0

    auto_mark_failed_on_exception: bool = True
    auto_mark_success_on_exit: bool = False
    reraise: bool = True

    # v2.1 additions
    warmup: bool = False
    min_ready: int = 0
    warmup_timeout: float = 30.0
    warmup_failure_policy: WarmupFailurePolicy = WarmupFailurePolicy.RAISE

    scoring: ScoringConfig | None = None  # None disables scoring & eviction
    circuit_breaker: CircuitBreakerConfig | None = None
    dedup_key: Callable[[Any], str] | None = None  # default canonical URL

    session_ttl: float = 300.0
    session_cooldown_policy: SessionCooldownPolicy = SessionCooldownPolicy.REBIND

    dead_letter_max_size: int = 1000
    ignore_exceptions: tuple[type, ...] = ()
    proxy_failure_classifier: Callable[[BaseException, Any | None], bool] | None = None

    rotate_on_acquire: bool = False
    rotate_on_failure: bool = False

    metrics_exporter: MetricsExporter | None = None
    log_level: int = logging.INFO

    state_store_factory: Callable[[], StateStore] | None = None

    extra: dict[str, Any] = field(default_factory=dict)  # forward compat
    health_check: HealthCheckConfig | None = None

    # v2.1: Presets
    @classmethod
    def scraping_preset(cls) -> PoolConfig:
        return cls(
            strategy=PoolStrategy.ROUND_ROBIN,
            cooldown=120.0,
            adaptive_cooldown=True,
            min_cooldown=15.0,
            max_cooldown=300.0,
            failure_threshold=2,
            failure_penalties={ConnectionError: 2.0, TimeoutError: 1.5},
            acquire_timeout=10.0,
            wait_fallback_interval=0.5,
            limits=LimitsConfig(
                max_connections_per_proxy=50, max_rps_per_proxy=5.0, token_bucket_capacity=2.0
            ),
            scoring=ScoringConfig(
                window_seconds=120.0, eviction_threshold=0.15, eviction_grace_period=30.0
            ),
            circuit_breaker=CircuitBreakerConfig(
                failure_ratio=0.6, half_open_timeout=15.0, min_throughput=20
            ),
            session_cooldown_policy=SessionCooldownPolicy.REBIND,
            warmup=False,
            auto_mark_failed_on_exception=True,
            auto_mark_success_on_exit=True,
            filter_missing_metadata=FilterMissingMetadata.SKIP,
            log_level=logging.WARNING,
        )

    @classmethod
    def api_gateway_preset(cls) -> PoolConfig:
        return cls(
            strategy=PoolStrategy.WEIGHTED,
            cooldown=600.0,
            adaptive_cooldown=True,
            min_cooldown=120.0,
            max_cooldown=1800.0,
            failure_threshold=5,
            acquire_timeout=30.0,
            limits=LimitsConfig(
                max_connections_per_proxy=5, max_rps_per_proxy=1.0, token_bucket_capacity=1.0
            ),
            scoring=ScoringConfig(
                window_seconds=600.0, eviction_threshold=0.1, eviction_grace_period=300.0
            ),
            circuit_breaker=CircuitBreakerConfig(
                failure_ratio=0.4, half_open_timeout=60.0, min_throughput=5
            ),
            session_cooldown_policy=SessionCooldownPolicy.BLOCK,
            warmup=True,
            min_ready=1,
            warmup_timeout=15.0,
            warmup_failure_policy=WarmupFailurePolicy.PARTIAL,
            auto_mark_failed_on_exception=True,
            auto_mark_success_on_exit=True,
            filter_missing_metadata=FilterMissingMetadata.RAISE,
            log_level=logging.INFO,
            health_check=HealthCheckConfig(),
        )

    def __post_init__(self) -> None:
        if not isinstance(self.strategy, PoolStrategy):
            self.strategy = PoolStrategy(self.strategy)
        if not isinstance(self.structure, PoolStructure):
            self.structure = PoolStructure(self.structure)
        if not isinstance(self.filter_missing_metadata, FilterMissingMetadata):
            self.filter_missing_metadata = FilterMissingMetadata(self.filter_missing_metadata)
        if not isinstance(self.warmup_failure_policy, WarmupFailurePolicy):
            self.warmup_failure_policy = WarmupFailurePolicy(self.warmup_failure_policy)
        if not isinstance(self.session_cooldown_policy, SessionCooldownPolicy):
            self.session_cooldown_policy = SessionCooldownPolicy(self.session_cooldown_policy)

        # Validate that scoring weights sum to 1 if scoring enabled
        if (
            self.scoring is not None
            and abs(self.scoring.success_weight + self.scoring.latency_weight - 1.0) > 1e-9
        ):
            raise ValueError("scoring.success_weight + scoring.latency_weight must equal 1.0")
        if self.warmup and self.health_check is None:
            raise ValueError("health_check must be provided when warmup=True")
        if self.min_cooldown > self.max_cooldown:
            raise ValueError("min_cooldown cannot exceed max_cooldown")
        if self.circuit_breaker is not None:
            cb = self.circuit_breaker
            if cb.failure_ratio <= 0 or cb.failure_ratio >= 1:
                raise ValueError("circuit_breaker.failure_ratio must be between 0 and 1 exclusive")
            if cb.half_open_timeout <= 0:
                raise ValueError("circuit_breaker.half_open_timeout must be >0")
        # strategy "weighted" requires scoring config
        if self.strategy == PoolStrategy.WEIGHTED and self.scoring is None:
            import warnings

            warnings.warn(
                "strategy='weighted' is selected but scoring config is None; scoring will be enabled with defaults.",
                stacklevel=2,
            )
            self.scoring = ScoringConfig()


class OmniproxyConfig:
    """Thread-safe validated store for package-wide HTTP and check defaults.

    The module exposes a single instance as ``settings`` so imports like
    ``from omniproxy.config import settings`` always share one process-wide configuration.
    Reads and writes of public keys go through :meth:`__getattr__` / :meth:`__setattr__` with
    validation; internal slots ``_data`` and ``_lock`` hold the backing dict and :class:`threading.RLock`.

    Attributes
    ----------
    default_backend
        Canonical backend key for :func:`~omniproxy.backends.factory.get_backend` when ``name`` is omitted.
        Must be one of :data:`~omniproxy.constants.VALID_BACKENDS` (aliases normalised on write).
    default_timeout: Optional[:class:`float`]
        Default per-request timeout in **seconds** for backends and checks. Must be positive or ``None``.
    default_connect_timeout: Optional[:class:`float`]
        Optional connect-phase timeout override used by callers that respect it; positive or ``None``.
    default_check_urls: :class:`list` [:class:`str`]
        Non-empty list of URL templates used by :func:`~omniproxy.extended_proxy.check_proxy` when no explicit ``url`` is passed.
    default_check_info_url_templates: :class:`list` [:class:`str`]
        Templates containing ``{fields}`` for geo / JSON info checks (``with_info=True``).
    health_check_urls: :class:`list` [:class:`str`]
        Optional override list for health probes; may be empty to clear. If empty at resolve time,
        :func:`~omniproxy.extended_proxy._resolve_health_check_url` falls back to ``default_check_urls``.
    """

    __slots__ = ("_data", "_lock")

    def __init__(self) -> None:
        """Create a new config store with package defaults.

        Returns:
            None

        Example:
            >>> OmniproxyConfig()  # doctest: +ELLIPSIS
            <omniproxy.config.OmniproxyConfig object at ...>
        """
        object.__setattr__(self, "_lock", threading.RLock())
        object.__setattr__(
            self,
            "_data",
            {
                "default_backend": DEFAULT_BACKEND,
                "default_timeout": DEFAULT_TIMEOUT,
                "default_connect_timeout": None,
                "default_check_urls": list(DEFAULT_CHECK_URLS),
                "default_check_info_url_templates": list(DEFAULT_CHECK_INFO_URL_TEMPLATES),
                "health_check_urls": list(DEFAULT_HEALTH_CHECK_URLS),
            },
        )

    def _validate_backend(self, v: str) -> str:
        """Normalise and validate a backend name.

        Args:
            v (str): Raw backend identifier (aliases like ``curlcffi`` accepted).

        Returns:
            str: Canonical backend key present in :data:`~omniproxy.constants.VALID_BACKENDS`.

        Raises:
            ValueError: If the backend is unknown.

        Example:
            >>> OmniproxyConfig()._validate_backend("HttPx")
            'httpx'
        """
        key = v.lower().replace("-", "_")
        if key == "curlcffi":
            key = "curl_cffi"
        if key not in VALID_BACKENDS:
            raise ValueError(f"Unknown backend {v!r}; choose {', '.join(sorted(VALID_BACKENDS))}")
        return key

    def _validate_positive_timeout(self, v: float | None) -> float | None:
        """Ensure a timeout is a positive float or ``None``.

        Args:
            v (float | None): Candidate timeout in seconds.

        Returns:
            float | None: ``None`` or a positive ``float``.

        Raises:
            TypeError: If *v* is not a number (or is a bool).
            ValueError: If *v* is not positive.

        Example:
            >>> OmniproxyConfig()._validate_positive_timeout(5)
            5.0
        """
        if v is None:
            return None
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            raise TypeError("timeout must be a number or None")
        if v <= 0:
            raise ValueError("timeout must be positive")
        return float(v)

    def _validate_url_string(self, v: str, name: str) -> str:
        """Validate a single non-empty URL-like string.

        Args:
            v (str): Value to validate.
            name (str): Field name used in error messages.

        Returns:
            str: Stripped, non-empty string.

        Raises:
            ValueError: If *v* is empty or not a string.

        Example:
            >>> OmniproxyConfig()._validate_url_string("https://a", "u")
            'https://a'
        """
        if not isinstance(v, str) or not v.strip():
            raise ValueError(f"{name} must be a non-empty string")
        return v

    def _validate_url_string_list(self, v: object, name: str) -> list[str]:
        """Validate a non-empty sequence of non-empty URL strings.

        Args:
            v (object): List or tuple of strings.
            name (str): Field name for errors.

        Returns:
            list[str]: Normalised list of strings.

        Raises:
            TypeError: If *v* is not a list or tuple.
            ValueError: If any entry is empty or the sequence is empty.

        Example:
            >>> OmniproxyConfig()._validate_url_string_list(["https://a"], "x")
            ['https://a']
        """
        if not isinstance(v, (list, tuple)):
            raise TypeError(f"{name} must be a list or tuple of strings")
        out: list[str] = []
        for i, item in enumerate(v):
            if not isinstance(item, str) or not item.strip():
                raise ValueError(f"{name}[{i}] must be a non-empty string")
            out.append(item)
        if not out:
            raise ValueError(f"{name} must be a non-empty sequence")
        return out

    def _validate_optional_url_string_list(self, v: object, name: str) -> list[str]:
        """Like :meth:`_validate_url_string_list` but allows an empty sequence (clears overrides).

        Args:
            v (object): List or tuple of strings (may be empty).
            name (str): Field name for errors.

        Returns:
            list[str]: Validated list (possibly empty).

        Raises:
            TypeError: If *v* is not a list or tuple.
            ValueError: If any entry is an empty string.

        Example:
            >>> OmniproxyConfig()._validate_optional_url_string_list([], "x")
            []
        """
        if not isinstance(v, (list, tuple)):
            raise TypeError(f"{name} must be a list or tuple of strings")
        out: list[str] = []
        for i, item in enumerate(v):
            if not isinstance(item, str) or not item.strip():
                raise ValueError(f"{name}[{i}] must be a non-empty string")
            out.append(item)
        return out

    def __getattr__(self, name: str) -> Any:
        """Read a public configuration attribute from the internal store.

        Args:
            name (str): Public key name.

        Returns:
            Any: Stored value.

        Raises:
            AttributeError: If *name* is not a recognised public key.

        Example:
            >>> isinstance(OmniproxyConfig().default_backend, str)
            True
        """
        if name in OMNIPROXY_CONFIG_PUBLIC_KEYS:
            with self._lock:
                return self._data[name]
        raise AttributeError(f"{type(self).__name__!r} object has no attribute {name!r}")

    def __setattr__(self, name: str, value: Any) -> None:
        """Set a validated public configuration attribute (or internal slots).

        Args:
            name (str): Attribute name.
            value (Any): New value (validated per attribute).

        Returns:
            None

        Raises:
            AttributeError: If *name* is not assignable on this object.

        Example:
            >>> c = OmniproxyConfig()
            >>> c.default_timeout = 15.0
            >>> c.default_timeout
            15.0
        """
        if name in ("_lock", "_data"):
            return object.__setattr__(self, name, value)
        if name == "default_backend":
            v = self._validate_backend(str(value))
            with self._lock:
                self._data["default_backend"] = v
            return None
        if name == "default_timeout":
            with self._lock:
                self._data["default_timeout"] = self._validate_positive_timeout(value)
            return None
        if name == "default_connect_timeout":
            with self._lock:
                self._data["default_connect_timeout"] = self._validate_positive_timeout(value)
            return None
        if name == "default_check_urls":
            url_list = self._validate_url_string_list(value, "default_check_urls")
            with self._lock:
                self._data["default_check_urls"] = url_list
            return None
        if name == "default_check_info_url_templates":
            url_list = self._validate_url_string_list(value, "default_check_info_url_templates")
            with self._lock:
                self._data["default_check_info_url_templates"] = url_list
            return None
        if name == "health_check_urls":
            opt_url_list = self._validate_optional_url_string_list(value, "health_check_urls")
            with self._lock:
                self._data["health_check_urls"] = opt_url_list
            return None
        raise AttributeError(f"Unknown config attribute {name!r}")


# Singleton used as ``from omniproxy.config import settings`` (avoids shadowing this module).
settings = OmniproxyConfig()


__all__ = [
    "FilterMissingMetadata",
    "HealthCheckConfig",
    "HealthStrategy",
    "LifecycleHooks",
    "LimitsConfig",
    "OmniproxyConfig",
    "PoolConfig",
    "PoolStrategy",
    "PoolStructure",
    "SessionCooldownPolicy",
    "Strategy",
    "Structure",
    "WarmupFailurePolicy",
    "settings",
]
