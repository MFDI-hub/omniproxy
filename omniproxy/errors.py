"""Custom exceptions for omniproxy."""


class OmniproxyError(Exception):
    """Base exception for all omniproxy errors."""


class ProxyPoolError(OmniproxyError):
    """Base exception for proxy pool related errors."""


class PoolExhausted(ProxyPoolError):
    """
    Raised when the pool has no active proxies available,
    and no refresh mechanism could replenish it within the timeout.
    """


class PoolSaturated(ProxyPoolError):
    """
    Raised when matching proxies exist in the pool,
    but all are currently at their maximum concurrent connection limit.
    """


class NoMatchingProxy(ProxyPoolError):
    """
    Raised when the pool contains proxies, but none match
    the requested attribute filters (e.g., country="US", min_anonymity="elite").
    """


class MissingProxyMetadata(ProxyPoolError):
    """
    Raised when ``filter_missing_metadata`` is ``"raise"`` and a proxy lacks
    metadata required to apply a filter (e.g. ``min_anonymity`` with unset anonymity).
    """
