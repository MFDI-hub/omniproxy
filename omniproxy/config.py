"""Global defaults for omniproxy (backends, timeouts, check URLs)."""

from __future__ import annotations

import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from .extended_proxy import Proxy

from .constants import (
    DEFAULT_BACKEND,
    DEFAULT_CHECK_INFO_URL_TEMPLATE,
    DEFAULT_CHECK_URL,
    DEFAULT_TIMEOUT,
    OMNIPROXY_CONFIG_PUBLIC_KEYS,
    VALID_BACKENDS,
)

Strategy = Literal["round_robin", "random"]
Structure = Literal["list", "deque"]
HealthStrategy = Literal["on_failure", "on_failure_recover", "ttl", "ttl_and_failure"]


@dataclass(slots=True)
class HealthCheckConfig:
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    expected_status: int | None = 200
    expected_fields: set[str] | None = None
    timeout: float | None = None
    strategy: HealthStrategy = "on_failure"
    recovery_interval: float = 60.0
    ttl: float | None = None


@dataclass(slots=True)
class PoolConfig:
    """Centralised configuration for :class:`ProxyPool` behaviour."""

    strategy: Strategy = "round_robin"
    """Proxy selection strategy."""

    structure: Structure = "deque"
    """Underlying data structure used to hold active proxies.
    'deque' is optimal for round-robin; 'list' is required for random strategy."""

    cooldown: float = 60.0
    """Seconds a proxy is blacklisted after being marked failed."""

    failure_penalties: dict[type, float] = field(default_factory=dict)
    """Multiply cooldown duration by ``failure_penalties.get(exc_type, 1.0)`` when marking failed."""

    max_connections_per_proxy: int | None = None
    """If set, cap concurrent in-flight uses per proxy URL (see :meth:`ProxyPool.get_next`)."""

    on_saturated: Callable[[], None] | None = None
    """Called when matching proxies exist but all are at the connection limit."""

    filter_missing_metadata: Literal["skip", "raise", "include"] = "skip"
    """How to treat proxies missing metadata required by a filter (e.g. ``min_anonymity``)."""

    on_exhausted: Callable[[], None] | None = None
    """Called when :meth:`ProxyPool.get_next` / :meth:`ProxyPool.aget_next` exhausts the active set."""

    refresh_callback: Callable[[], list[Proxy | str]] | None = None
    """Sync callback returning new proxies to merge when the pool is exhausted."""

    arefresh_callback: Callable[[], Awaitable[list[Proxy | str]]] | None = None
    """Async callback returning new proxies to merge when the pool is exhausted."""

    refresh_timeout: float = 10.0
    """Max seconds to wait for an in-flight refresh before failing with :exc:`PoolExhausted`."""

    # --- context-manager behaviour ---
    auto_mark_failed_on_exception: bool = True
    """If True, context managers call ``mark_failed`` when the block raises an exception."""

    auto_mark_success_on_exit: bool = False
    """If True, context managers call ``mark_success`` on clean exit."""

    reraise: bool = True
    """If True, exceptions from inside the context block are re-raised after accounting."""

    # --- failure threshold ---
    failure_threshold: int = 1
    """Number of ``mark_failed`` calls before a proxy is cooled down."""

    # --- extra fields reserved for future use ---
    extra: dict[str, Any] = field(default_factory=dict)

    health_check: HealthCheckConfig | None = None
    """Health check configuration."""


class OmniproxyConfig:
    """Thread-safe validated configuration (read/write attributes on ``config``)."""

    __slots__ = ("_data", "_lock")

    def __init__(self) -> None:
        object.__setattr__(self, "_lock", threading.RLock())
        object.__setattr__(
            self,
            "_data",
            {
                "default_backend": DEFAULT_BACKEND,
                "default_timeout": DEFAULT_TIMEOUT,
                "default_connect_timeout": None,
                "default_check_url": DEFAULT_CHECK_URL,
                "default_check_info_url_template": DEFAULT_CHECK_INFO_URL_TEMPLATE,
            },
        )

    def _validate_backend(self, v: str) -> str:
        key = v.lower().replace("-", "_")
        if key == "curlcffi":
            key = "curl_cffi"
        if key not in VALID_BACKENDS:
            raise ValueError(f"Unknown backend {v!r}; choose {', '.join(sorted(VALID_BACKENDS))}")
        return key

    def _validate_positive_timeout(self, v: float | None) -> float | None:
        if v is None:
            return None
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            raise TypeError("timeout must be a number or None")
        if v <= 0:
            raise ValueError("timeout must be positive")
        return float(v)

    def _validate_url_string(self, v: str, name: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError(f"{name} must be a non-empty string")
        return v

    def __getattr__(self, name: str) -> Any:
        if name in OMNIPROXY_CONFIG_PUBLIC_KEYS:
            # with self._lock:
            return self._data[name]
        raise AttributeError(f"{type(self).__name__!r} object has no attribute {name!r}")

    def __setattr__(self, name: str, value: Any) -> None:
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
        if name == "default_check_url":
            with self._lock:
                self._data["default_check_url"] = self._validate_url_string(
                    str(value), "default_check_url"
                )
            return None
        if name == "default_check_info_url_template":
            with self._lock:
                self._data["default_check_info_url_template"] = self._validate_url_string(
                    str(value), "default_check_info_url_template"
                )
            return None
        raise AttributeError(f"Unknown config attribute {name!r}")


# Singleton used as ``from omniproxy.config import settings`` (avoids shadowing this module).
settings = OmniproxyConfig()


__all__ = ["OmniproxyConfig", "settings"]
