"""Global defaults for omniproxy (backends, timeouts, check URLs)."""

from __future__ import annotations

import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from .extended_proxy import CheckResult, Proxy

from .constants import (
    DEFAULT_BACKEND,
    DEFAULT_CHECK_INFO_URL_TEMPLATES,
    DEFAULT_CHECK_URLS,
    DEFAULT_HEALTH_CHECK_URLS,
    DEFAULT_TIMEOUT,
    OMNIPROXY_CONFIG_PUBLIC_KEYS,
    VALID_BACKENDS,
)

Strategy = Literal["round_robin", "random"]
Structure = Literal["list", "deque"]
HealthStrategy = Literal["on_failure", "on_failure_recover", "ttl", "ttl_and_failure"]


@dataclass(slots=True)
class HealthCheckConfig:
    """HTTP probe configuration used by :func:`~omniproxy.extended_proxy.run_health_check` / :func:`~omniproxy.extended_proxy.arun_health_check`.

    When stored on :attr:`PoolConfig.health_check`, :class:`~omniproxy.pool.ProxyPool` can run
    :meth:`~omniproxy.pool.ProxyPool.start_monitoring` (or the threaded variant) to re-check
    proxies on a timer and call :attr:`PoolConfig.on_check_complete`, then :meth:`~omniproxy.pool.ProxyPool.mark_success`
    or :meth:`~omniproxy.pool.ProxyPool.mark_failed` depending on the :class:`CheckResult`.

    Attributes
    ----------
    url: Optional[:class:`str`]
        Absolute URL for the health request. If ``None``, a URL is chosen from
        ``settings.health_check_urls`` or ``settings.default_check_urls`` (see
        :class:`OmniproxyConfig`).
    headers: :class:`dict` [:class:`str`, :class:`str`]
        Extra headers sent with every probe (e.g. API keys). Defaults to empty.
    expected_status: Optional[:class:`int`]
        HTTP status code treated as success. ``None`` means any **2xx** response is accepted.
        Default ``200``.
    expected_fields: Optional[:class:`set` [:class:`str`]]
        If set, the JSON body must be a :class:`dict` containing **all** of these keys.
    timeout: Optional[:class:`float`]
        Per-request timeout in seconds for the backend. ``None`` falls back to backend defaults.
    strategy
        Literal ``HealthStrategy``: ``on_failure``, ``on_failure_recover``, ``ttl``, or ``ttl_and_failure``.
        Reserved for future policy tuning around how failures interact with cooldowns.
    recovery_interval: :class:`float`
        Seconds to sleep between full passes in the pool health loop.
    ttl: Optional[:class:`float`]
        Optional soft TTL for proxy freshness; reserved for callers that interpret it.
    """

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
    """Centralised knobs for :class:`~omniproxy.pool.ProxyPool` selection, limits, and lifecycle hooks.

    Most fields are optional callbacks or limits; defaults favour round-robin over a :class:`collections.deque`
    with a fixed cooldown after repeated failures. Pair with :class:`HealthCheckConfig` for background probes.

    Attributes
    ----------
    strategy
        ``round_robin`` (fair rotation) or ``random`` (uniform random among eligible proxies).
        ``random`` forces ``structure='list'`` inside :meth:`ProxyPool.__init__`.
    structure
        ``deque`` (efficient popleft/append for round-robin) or ``list`` (required for ``random``).
    cooldown: :class:`float`
        Base seconds a proxy spends off the active list after ``failure_threshold`` failures.
        Actual duration may be scaled by :attr:`failure_penalties` per exception type.
    failure_penalties: :class:`dict` [:class:`type`, :class:`float`]
        Maps exception **types** to multipliers applied to :attr:`cooldown` when that type caused
        :meth:`~omniproxy.pool.ProxyPool.mark_failed`.
    max_connections_per_proxy: Optional[:class:`int`]
        If set, caps concurrent in-flight uses per canonical proxy URL (context managers count).
    max_rps_per_proxy: Optional[:class:`float`]
        If set, token-bucket limit on **selections** per URL (requests per second), not HTTP RPS.
    on_saturated: Optional[Callable[[], None]]
        Invoked when proxies match filters but every match is at connection or RPS limits (:exc:`~omniproxy.errors.PoolSaturated`).
    filter_missing_metadata
        ``skip`` (exclude proxy), ``raise`` (:exc:`~omniproxy.errors.MissingProxyMetadata`), or ``include`` when metadata needed by filters is absent.
    on_exhausted: Optional[Callable[[], None]]
        Called when the active set is empty before attempting :attr:`refresh_callback` / :attr:`arefresh_callback`.
    refresh_callback
        Optional callable returning ``list`` of :class:`~omniproxy.extended_proxy.Proxy` or ``str``
        to merge when the pool is exhausted synchronously.
    arefresh_callback: Optional[Callable[[], Awaitable[list]]]
        Async variant of :attr:`refresh_callback`.
    refresh_timeout: :class:`float`
        Seconds :meth:`~omniproxy.pool.ProxyPool.get_next` / :meth:`~omniproxy.pool.ProxyPool.aget_next` wait for an in-flight refresh event.
    auto_mark_failed_on_exception: :class:`bool`
        If ``True``, sync/async ``with`` blocks call :meth:`~omniproxy.pool.ProxyPool.mark_failed` when the body raises.
    auto_mark_success_on_exit: :class:`bool`
        If ``True``, clean exit from ``with`` calls :meth:`~omniproxy.pool.ProxyPool.mark_success`.
    reraise: :class:`bool`
        If ``True`` (default), context managers swallow internally but the original exception still propagates.
    failure_threshold: :class:`int`
        Number of :meth:`~omniproxy.pool.ProxyPool.mark_failed` calls before a proxy is cooled down.
    extra: :class:`dict` [:class:`str`, :class:`Any`]
        Reserved extension bag for forward-compatible options.
    health_check: Optional[:class:`HealthCheckConfig`]
        When set, enables :meth:`~omniproxy.pool.ProxyPool.start_monitoring` / threaded monitoring.
    on_proxy_failed: Optional[Callable]
        ``(proxy, exc_type | None)`` after accounting for a failure.
    on_proxy_cooled_down: Optional[Callable]
        ``(proxy)`` when a proxy moves to cooldown.
    on_proxy_recovered: Optional[Callable]
        ``(proxy)`` when a proxy returns from cooldown or failure streak clears.
    on_check_complete: Optional[Callable]
        ``(proxy, check_result)`` after each health probe in the background loop.
    on_proxy_acquired: Optional[Callable]
        ``(proxy)`` after a successful :meth:`~omniproxy.pool.ProxyPool.get_next` / :meth:`~omniproxy.pool.ProxyPool.aget_next`.
    on_proxy_released: Optional[Callable]
        ``(proxy)`` when a context manager releases the in-flight slot.
    """

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

    max_rps_per_proxy: float | None = None
    """If set, token-bucket cap on selections per proxy URL (requests per second)."""

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

    on_proxy_failed: Callable[[Proxy, type | None], None] | None = None
    on_proxy_cooled_down: Callable[[Proxy], None] | None = None
    on_proxy_recovered: Callable[[Proxy], None] | None = None
    on_check_complete: Callable[[Proxy, CheckResult], None] | None = None
    on_proxy_acquired: Callable[[Proxy], None] | None = None
    on_proxy_released: Callable[[Proxy], None] | None = None


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
            # with self._lock:
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
            v = self._validate_url_string_list(value, "default_check_urls")
            with self._lock:
                self._data["default_check_urls"] = v
            return None
        if name == "default_check_info_url_templates":
            v = self._validate_url_string_list(value, "default_check_info_url_templates")
            with self._lock:
                self._data["default_check_info_url_templates"] = v
            return None
        if name == "health_check_urls":
            v = self._validate_optional_url_string_list(value, "health_check_urls")
            with self._lock:
                self._data["health_check_urls"] = v
            return None
        raise AttributeError(f"Unknown config attribute {name!r}")


# Singleton used as ``from omniproxy.config import settings`` (avoids shadowing this module).
settings = OmniproxyConfig()


__all__ = ["OmniproxyConfig", "settings"]
