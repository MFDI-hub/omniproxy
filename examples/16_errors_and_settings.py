"""Pool errors, invalid configuration, and global settings."""

from __future__ import annotations

import asyncio
import threading
import time

from omniproxy import Proxy
from omniproxy.config import CooldownConfig, GlobalConfig, LimitsConfig, PoolConfig, ScoringConfig, settings
from omniproxy.enum import FilterMissingMetadata, HttpBackend, PoolStrategy
from omniproxy.errors import (
    ConfigurationError,
    MissingProxyMetadata,
    NoMatchingProxy,
    OmniproxyError,
    PoolCircuitOpenError,
    PoolClosedError,
    PoolDrainingError,
    PoolExhausted,
    PoolSaturated,
    ProxyPoolError,
    SessionBrokenError,
    WarmupFailedError,
)
from omniproxy.pool import AsyncProxyPool, SyncProxyPool

from _support import SEEDS, meta, offline_config, title


def invalid_configs() -> None:
    title("configuration rejects")
    samples = (
        lambda: PoolConfig(strategy=PoolStrategy.WEIGHTED),
        lambda: PoolConfig(strategy=PoolStrategy.LOWEST_LATENCY),
        lambda: PoolConfig(cooldown=CooldownConfig(min=10, max=1)),
        lambda: PoolConfig(min_size=5, max_size=1),
        lambda: PoolConfig(max_latency=0),
        lambda: PoolConfig(scoring=ScoringConfig(success_weight=0.2, latency_weight=0.2)),
        lambda: GlobalConfig(default_backend="not-a-backend"),
        lambda: GlobalConfig(default_check_urls=()),
    )
    for build in samples:
        try:
            build()
            print("unexpectedly accepted", build)
        except (ValueError, TypeError) as exc:
            print(type(exc).__name__, str(exc).splitlines()[0])


def runtime_errors() -> None:
    title("runtime pool errors")
    cases: list[tuple[str, object]] = []

    empty = SyncProxyPool(offline_config(), [])
    try:
        empty.acquire()
    except PoolExhausted as exc:
        cases.append(("exhausted", exc))
    finally:
        empty.close()

    async def _closed() -> None:
        async with AsyncProxyPool(offline_config(), [Proxy(SEEDS[0])]) as pool:
            await pool.close()
            try:
                await pool.acquire()
            except PoolClosedError as exc:
                cases.append(("closed", exc))

    asyncio.run(_closed())

    saturated = SyncProxyPool(
        offline_config(limits=LimitsConfig(max_connections_per_proxy=1)),
        [Proxy(SEEDS[0])],
    )
    held = saturated.acquire()
    try:
        saturated.acquire()
    except PoolSaturated as exc:
        cases.append(("saturated", exc))
    finally:
        saturated.release(held)
        saturated.close()

    missing = SyncProxyPool(
        offline_config(filter_missing_metadata=FilterMissingMetadata.RAISE),
        [Proxy(SEEDS[0])],
    )
    try:
        missing.acquire(min_anonymity="elite")
    except MissingProxyMetadata as exc:
        cases.append(("missing", exc))
    finally:
        missing.close()

    unmatched = SyncProxyPool(
        offline_config(),
        [meta(SEEDS[0], country="US")],
    )
    try:
        unmatched.acquire(country="JP")
    except NoMatchingProxy as exc:
        cases.append(("no-match", exc))
    finally:
        unmatched.close()

    for label, exc in cases:
        print(label, type(exc).__name__, exc)

    title("drain rejects a new acquire")
    draining = SyncProxyPool(
        offline_config(drain_timeout=2.0),
        [Proxy(SEEDS[0])],
    )
    held = draining.acquire()
    errors: list[BaseException] = []

    def _close() -> None:
        draining.close()

    worker = threading.Thread(target=_close)
    worker.start()
    time.sleep(0.2)
    try:
        draining.acquire()
    except PoolDrainingError as exc:
        errors.append(exc)
    finally:
        draining.release(held)
        worker.join(timeout=3)
    print("draining", [type(item).__name__ for item in errors] or "none")


def hierarchy() -> None:
    title("exception hierarchy")
    for cls in (
        OmniproxyError,
        ProxyPoolError,
        PoolExhausted,
        PoolSaturated,
        NoMatchingProxy,
        MissingProxyMetadata,
        PoolClosedError,
        PoolDrainingError,
        SessionBrokenError,
        WarmupFailedError,
        ConfigurationError,
        PoolCircuitOpenError,
    ):
        print(cls.__name__, "->", cls.__bases__[0].__name__)


def global_settings() -> None:
    title("GlobalConfig")
    print("current backend", settings.default_backend)
    print("backends", [item.value for item in HttpBackend])
    try:
        settings.default_timeout = 1  # type: ignore[misc]
    except Exception as exc:
        print("frozen", type(exc).__name__)
    copied = settings.model_copy(update={"default_timeout": 4.0, "default_backend": "httpx"})
    print("copy timeout", copied.default_timeout, "backend", copied.default_backend)
    print("singleton unchanged", settings.default_timeout)


def main() -> None:
    invalid_configs()
    runtime_errors()
    hierarchy()
    global_settings()


if __name__ == "__main__":
    main()
