"""Built-in proxy-list fetchers: every protocol filter, then a live pool acquire."""

from __future__ import annotations

import asyncio
import itertools

from omniproxy.config import HealthCheckConfig, WarmupConfig
from omniproxy.enum import PoolStrategy, ProxyProtocol, WarmupFailurePolicy
from omniproxy.fetchers import default_fetchers
from omniproxy.pool import AsyncProxyPool
from omniproxy.refresh import fetch_from_fetchers

from _support import offline_config, title

FILTERS = ("http", "socks4", "socks5")
MAX_LATENCY_SECONDS = 3.0
CHECK_SAMPLE_SIZE = 500
CHECK_CONCURRENCY = 32


def protocol_matrix() -> None:
    title("default_fetchers protocol subsets")
    print("none", len(default_fetchers()))
    for size in range(1, len(FILTERS) + 1):
        for combo in itertools.combinations(FILTERS, size):
            fetchers = default_fetchers(protocols=combo)
            print(combo, len(fetchers), fetchers[0].__class__.__name__ if fetchers else "-")
    for protocol in ProxyProtocol:
        try:
            count = len(default_fetchers(protocols=(protocol,)))
            print("enum", protocol.value, count)
        except ValueError as exc:
            print("enum", protocol.value, "rejected", exc)
    try:
        default_fetchers(protocols=())
    except ValueError as exc:
        print("empty filter", exc)


async def live_acquire() -> None:
    title("live fetch and lowest-latency acquire")
    fetchers = default_fetchers()
    print("default sources", len(fetchers))
    print(f"checking {CHECK_SAMPLE_SIZE} proxies with {CHECK_CONCURRENCY} concurrent checks")
    try:
        proxies = await fetch_from_fetchers(fetchers, timeout=15.0)
        print("fetched proxies", len(proxies))
    except Exception as exc:
        print("fetch", type(exc).__name__, exc)
        return
    if not proxies:
        print("no proxies fetched")
        return

    config = offline_config(
        strategy=PoolStrategy.LOWEST_LATENCY,
        max_latency=MAX_LATENCY_SECONDS,
        acquire_timeout=30.0,
        health_check=HealthCheckConfig(
            timeout=8.0,
            max_concurrent_checks=CHECK_CONCURRENCY,
            max_latency=MAX_LATENCY_SECONDS,
        ),
        warmup=WarmupConfig(
            enabled=True,
            min_ready=1,
            timeout=60.0,
            failure_policy=WarmupFailurePolicy.PARTIAL,
        ),
    )
    async with AsyncProxyPool(
        config,
        initial_proxies=proxies[:CHECK_SAMPLE_SIZE],
        fetchers=fetchers,
    ) as pool:
        proxy = await pool.acquire()
        latency = proxy.latency
        extra = f" latency={latency:.3f}s" if latency is not None else ""
        print("acquired", proxy, extra)
        await pool.release(proxy)


def main() -> None:
    protocol_matrix()
    asyncio.run(live_acquire())


if __name__ == "__main__":
    main()
