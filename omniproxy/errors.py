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
    """Base class for errors raised by :class:`~omniproxy.pool.ProxyPool` selection or accounting.

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

        :meth:`~omniproxy.pool.ProxyPool.get_next` may invoke :attr:`~omniproxy.config.PoolConfig.refresh_callback`
        once before surfacing this error when configured.

    Attributes
    ----------
    args: :class:`tuple`
        Explanation such as ``No proxies available`` or ``Refresh timed out``.
    """


class PoolSaturated(ProxyPoolError):
    """Raised when candidate proxies exist for the given filters but all are blocked by limits.

    Triggers when every matching URL is at :attr:`~omniproxy.config.LimitsConfig.max_connections_per_proxy`
    **or** fails the per-URL RPS token bucket (:class:`~omniproxy.pool.TokenBucket`).

    Attributes
    ----------
    args: :class:`tuple`
        Default message explains saturation (connections and/or rate limits).
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
        Message identifies the proxy (often :attr:`~omniproxy.proxy.Proxy.safe_url`) and missing key.
    """


class PoolClosedError(RuntimeError):
    """Raised when a closed :class:`~omniproxy.pool.ProxyPool` rejects new work (clean lifecycle).

    This is a :class:`RuntimeError` subclass so callers that broadly catch ``Exception`` still see
    shutdown as distinct from :exc:`PoolExhausted` / :exc:`PoolSaturated`, while ``BaseException``
    handlers remain unaffected.

    Attributes
    ----------
    args: :class:`tuple`
        Human-readable reason (typically ``"proxy pool is closed"``).
    """
