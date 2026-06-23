"""omniproxy public API.

This package exposes proxy management primitives for both synchronous and
asynchronous workloads: configurable proxy pools, health checkers, fetchers
that pull proxies from disk or remote sources, scoring utilities, and a small
set of error types. Most users only need to import the symbols re-exported
here.

Version:
    4.0.0
"""

from __future__ import annotations

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
from .pool import AcquireOptions, AsyncProxyPool, PoolStatistics, SyncProxyPool
from .proxy import PlaywrightProxySettings, ProxyPattern

# Older docs and callers used ``ProxyPool`` for the sync pool wrapper.
ProxyPool = SyncProxyPool

__all__ = [
    "AcquireOptions",
    "AsyncProxyPool",
    "CheckResult",
    "MissingProxyMetadata",
    "NoMatchingProxy",
    "PlaywrightProxySettings",
    "PoolClosedError",
    "PoolExhausted",
    "PoolSaturated",
    "PoolStatistics",
    "Proxy",
    "ProxyPattern",
    "ProxyPool",
    "SyncProxyPool",
    "acheck_proxies",
    "acheck_proxy",
    "apply_check_result_metadata",
    "check_proxies",
    "check_proxy",
    "fetch_proxies",
    "iter_proxies_from_file",
    "read_proxies",
    "save_proxies",
    "settings",
]

__version__ = "4.0.0"
