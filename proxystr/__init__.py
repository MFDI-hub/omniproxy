from .backends import get_backend
from .extended_proxy import Proxy, acheck_proxies, acheck_proxy, check_proxies, check_proxy
from .io import fetch_proxies, read_proxies, save_proxies
from .pool import ProxyPool
from .proxy import PlaywrightProxySettings, ProxyPattern

__all__ = [
    "AsyncClient",
    "Client",
    "Proxy",
    "ProxyPattern",
    "ProxyPool",
    "PlaywrightProxySettings",
    "acheck_proxies",
    "acheck_proxy",
    "check_proxies",
    "check_proxy",
    "fetch_proxies",
    "get_backend",
    "read_proxies",
    "save_proxies",
]


def __getattr__(name: str):
    if name == "Client":
        from .backends.httpx_client import Client as _Client

        return _Client
    if name == "AsyncClient":
        from .backends.httpx_client import AsyncClient as _AsyncClient

        return _AsyncClient
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
