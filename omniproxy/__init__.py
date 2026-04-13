from .backends import get_backend, supported_backends
from .config import settings
from .errors import (
    MissingProxyMetadata,
    NoMatchingProxy,
    PoolClosedError,
    PoolExhausted,
    PoolSaturated,
)
from .extended_proxy import (
    CheckResult,
    Proxy,
    acheck_proxies,
    acheck_proxy,
    apply_check_result_metadata,
    check_proxies,
    check_proxy,
)
from .io import fetch_proxies, iter_proxies_from_file, read_proxies, save_proxies
from .pool import ProxyPool
from .proxy import PlaywrightProxySettings, ProxyPattern

__all__ = [
    "AsyncClient",
    "CheckResult",
    "Client",
    "MissingProxyMetadata",
    "NoMatchingProxy",
    "PlaywrightProxySettings",
    "PoolClosedError",
    "PoolExhausted",
    "PoolSaturated",
    "Proxy",
    "ProxyPattern",
    "ProxyPool",
    "acheck_proxies",
    "acheck_proxy",
    "apply_check_result_metadata",
    "check_proxies",
    "check_proxy",
    "fetch_proxies",
    "get_backend",
    "iter_proxies_from_file",
    "read_proxies",
    "save_proxies",
    "settings",
    "supported_backends",
]


def __getattr__(name: str):
    """Lazy-import ``Client`` / ``AsyncClient`` from the httpx extra on attribute access.

    Args:
        name (str): Attribute name being resolved (``"Client"`` or ``"AsyncClient"``).

    Returns:
        type: The requested client class.

    Raises:
        AttributeError: If *name* is not a supported lazy attribute.

    Example:
        >>> from omniproxy import Client  # doctest: +SKIP
    """
    if name == "Client":
        from .backends.httpx_client import Client as _Client

        return _Client
    if name == "AsyncClient":
        from .backends.httpx_client import AsyncClient as _AsyncClient

        return _AsyncClient
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__version__ = "4.0.0"
