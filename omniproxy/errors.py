"""Custom exceptions for omniproxy."""


class OmniproxyError(Exception):
    """Base exception type for all omniproxy-specific failures.

    Subclass this (or one of its derivatives below) if you extend the library with custom errors
    that should be distinguishable from built-in :class:`Exception` types.

    Attributes
    ----------
    args: :class:`tuple`
        Standard exception tuple (message and optional cause data).
    """


class ProxyPoolError(OmniproxyError):
    """Base class for errors raised by :class:`~omniproxy.pool.AsyncProxyPool` selection or accounting.

    Concrete subclasses describe **why** acquisition failed (empty pool, no filter match, or
    saturation under limits).

    Attributes
    ----------
    args: :class:`tuple`
        Human-readable detail string as ``args[0]`` in typical raises.
    """


class PoolExhausted(ProxyPoolError):
    """Raised when there are no active proxies, or a refresh timed out / returned nothing usable.

    .. note::

        :meth:`~omniproxy.pool.AsyncProxyPool.acquire` may invoke configured refresh callbacks
        once before surfacing this error when the pool is empty or fully blocked.

    Attributes
    ----------
    args: :class:`tuple`
        Explanation such as ``No proxies available`` or ``All matching proxies are cooling down``.
    """


class PoolSaturated(ProxyPoolError):
    """Raised when candidate proxies exist for the given filters but all are blocked by limits.

    Triggers when every matching URL is at
    :attr:`~omniproxy.config.LimitsConfig.max_connections_per_proxy`.

    Attributes
    ----------
    args: :class:`tuple`
        Default message explains saturation (connection limits).
    """


class NoMatchingProxy(ProxyPoolError):
    """Raised when the active pool is non-empty but **no** proxy satisfies the filter kwargs.

    Unlike :exc:`PoolExhausted`, this indicates a **pure filter miss** (e.g. wrong ``country``).

    Attributes
    ----------
    args: :class:`tuple`
        Message names the failed filter combination.
    """


class MissingProxyMetadata(ProxyPoolError):
    """Raised when ``PoolConfig.filter_missing_metadata='raise'`` and a required field is unset.

    Typical case: ``min_anonymity='elite'`` but :attr:`~omniproxy.proxy.Proxy.anonymity` is ``None``.

    Attributes
    ----------
    args: :class:`tuple`
        Message identifies the missing metadata field and the filter that triggered the check.
    """


class PoolClosedError(OmniproxyError, RuntimeError):
    """Raised when a closed pool rejects new work (clean lifecycle).

    Inherits from both :class:`OmniproxyError` and :class:`RuntimeError` so callers can catch
    either the library base or runtime errors.

    Attributes
    ----------
    args: :class:`tuple`
        Human-readable reason (typically ``"Pool is closed"``).
    """


class PoolDrainingError(ProxyPoolError):
    """Raised when a pool is draining and rejects new acquisitions."""


class SessionBrokenError(ProxyPoolError):
    """Raised when a sticky session binding is invalid or the bound proxy is unusable."""


class WarmupFailedError(ProxyPoolError):
    """Raised when pool warmup cannot satisfy :attr:`~omniproxy.config.WarmupConfig.min_ready`."""


class ConfigurationError(OmniproxyError):
    """Invalid pool or package configuration."""


class PoolCircuitOpenError(ProxyPoolError):
    """Raised when the pool-level circuit breaker is open and probes are disallowed."""
