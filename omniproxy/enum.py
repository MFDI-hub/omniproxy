"""Closed sets of string values used across omniproxy (pool, config, I/O, protocols).

Use these :class:`enum.Enum` subclasses for type clarity and iteration; member *values*
match the strings stored in configs, URLs, and serialized data so they stay wire-compatible
with existing :class:`str` comparisons and :class:`typing.Literal` unions elsewhere.
"""

from __future__ import annotations

from enum import Enum


class ProxyProtocol(str, Enum):
    """Supported proxy URL schemes (see :data:`~omniproxy.constants.ALLOWED_PROTOCOLS`)."""

    HTTP = "http"
    HTTPS = "https"
    SOCKS5 = "socks5"
    SOCKS4 = "socks4"


class HttpBackend(str, Enum):
    """HTTP client implementations (see :data:`~omniproxy.constants.SUPPORTED_BACKENDS`)."""

    HTTPX = "httpx"
    AIOHTTP = "aiohttp"
    REQUESTS = "requests"
    CURL_CFFI = "curl_cffi"
    TLS_CLIENT = "tls_client"


class PoolStrategy(str, Enum):
    """:attr:`~omniproxy.config.PoolConfig.strategy` selection modes."""

    ROUND_ROBIN = "round_robin"
    RANDOM = "random"
    WEIGHTED = "weighted"
    LOWEST_LATENCY = "lowest_latency"


class PoolStructure(str, Enum):
    """Underlying container for the active proxy sequence."""

    LIST = "list"
    DEQUE = "deque"


class HealthStrategy(str, Enum):
    """Legacy health scheduling keys (kept for backwards compatibility)."""

    ON_FAILURE = "on_failure"
    ON_FAILURE_RECOVER = "on_failure_recover"
    TTL = "ttl"
    TTL_AND_FAILURE = "ttl_and_failure"


class FilterMissingMetadata(str, Enum):
    """:attr:`~omniproxy.config.PoolConfig.filter_missing_metadata` behaviour."""

    SKIP = "skip"
    RAISE = "raise"
    IGNORE = "ignore"


class WarmupFailurePolicy(str, Enum):
    """:attr:`~omniproxy.config.PoolConfig.warmup_failure_policy`."""

    RAISE = "raise"
    PARTIAL = "partial"


class SessionCooldownPolicy(str, Enum):
    """:attr:`~omniproxy.config.PoolConfig.session_cooldown_policy`."""

    BLOCK = "block"
    REBIND = "rebind"
    RAISE = "raise"


class IoInvalidLinePolicy(str, Enum):
    """``on_invalid`` for :mod:`omniproxy.io` readers."""

    RAISE = "raise"
    SKIP = "skip"


class CircuitBreakerState(str, Enum):
    """Pool-level circuit breaker phase (:class:`~omniproxy.pool.ProxyPool` internals)."""

    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class AnonymityTier(str, Enum):
    """Proxy anonymity classification (see :data:`~omniproxy.constants.ANONYMITY_RANKS`)."""

    TRANSPARENT = "transparent"
    ANONYMOUS = "anonymous"
    ELITE = "elite"


class HttpVerb(str, Enum):
    """HTTP methods used where the API restricts verbs (e.g. rotation URLs)."""

    GET = "GET"
    POST = "POST"


class AnonymityLeakHeader(str, Enum):
    """Response header names consulted when inferring anonymity tier."""

    X_FORWARDED_FOR = "x-forwarded-for"
    VIA = "via"
    FORWARDED = "forwarded"


__all__ = [
    "AnonymityLeakHeader",
    "AnonymityTier",
    "CircuitBreakerState",
    "FilterMissingMetadata",
    "HealthStrategy",
    "HttpBackend",
    "HttpVerb",
    "IoInvalidLinePolicy",
    "PoolStrategy",
    "PoolStructure",
    "ProxyProtocol",
    "SessionCooldownPolicy",
    "WarmupFailurePolicy",
]
