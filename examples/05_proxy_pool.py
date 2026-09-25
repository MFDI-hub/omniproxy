"""Sync and async pools: every selection strategy, lease lifecycle, and statistics."""

from __future__ import annotations

import asyncio

from omniproxy import Proxy, ProxyPool
from omniproxy.config import ScoringConfig
from omniproxy.enum import PoolStrategy, PoolStructure
from omniproxy.pool import AsyncProxyPool, SyncProxyPool
from omniproxy.scoring import EMAState, compute_score, update_ema

from _support import SEEDS, offline_config, title


def sync_lifecycle() -> None:
    title("SyncProxyPool lease: acquire, release, mark_success, mark_failed")
    proxies = [Proxy(raw) for raw in SEEDS[:3]]
    pool = SyncProxyPool(offline_config(), proxies)
    try:
        first = pool.acquire()
        pool.release(first)
        second = pool.acquire()
        pool.mark_success(second, latency=0.15)
        third = pool.acquire()
        pool.mark_failed(third, ConnectionError)
        print("released", first.url)
        print("success ", second.url)
        print("failed  ", third.url)
        print("ProxyPool is SyncProxyPool", ProxyPool is SyncProxyPool)
    finally:
        pool.close()


def strategies() -> None:
    title("PoolStrategy")
    seeds = [Proxy(raw) for raw in SEEDS[:3]]

    robin = SyncProxyPool(
        offline_config(strategy=PoolStrategy.ROUND_ROBIN, structure=PoolStructure.DEQUE),
        seeds,
    )
    try:
        order = []
        for _ in range(len(seeds)):
            proxy = robin.acquire()
            order.append(proxy.url)
            robin.release(proxy)
        print("round_robin", order)
    finally:
        robin.close()

    random_pool = SyncProxyPool(offline_config(strategy=PoolStrategy.RANDOM), seeds)
    try:
        seen: dict[str, int] = {}
        for _ in range(12):
            proxy = random_pool.acquire()
            seen[proxy.url] = seen.get(proxy.url, 0) + 1
            random_pool.release(proxy)
        print("random", seen)
    finally:
        random_pool.close()

    lowest_config = offline_config(
        strategy=PoolStrategy.LOWEST_LATENCY,
        scoring=ScoringConfig(min_samples=1),
        structure=PoolStructure.LIST,
    )
    scored = SyncProxyPool(lowest_config, seeds)
    try:
        for proxy, latency in zip(seeds, (0.9, 0.1, 0.4), strict=True):
            scored.mark_success(proxy, latency=latency)
        chosen = scored.acquire()
        print(
            "lowest_latency",
            chosen.url,
            "expected",
            seeds[1].url,
            "structure",
            lowest_config.structure.value,
        )
        scored.release(chosen)
    finally:
        scored.close()

    weighted = SyncProxyPool(
        offline_config(
            strategy=PoolStrategy.WEIGHTED,
            scoring=ScoringConfig(min_samples=1, success_weight=0.5, latency_weight=0.5),
        ),
        seeds[:2],
    )
    try:
        slow = weighted.acquire()
        weighted.mark_success(slow, latency=1.5)
        fast = weighted.acquire()
        weighted.mark_success(fast, latency=0.05)
        counts: dict[str, int] = {}
        for _ in range(20):
            proxy = weighted.acquire()
            counts[proxy.url] = counts.get(proxy.url, 0) + 1
            weighted.release(proxy)
        print("weighted", counts)
    finally:
        weighted.close()


def scoring_math() -> None:
    title("EMA scoring weights")
    state = EMAState()
    update_ema(state, success=True, latency=0.2, decay=0.5)
    update_ema(state, success=False, latency=1.0, decay=0.5)
    for success_weight, latency_weight in ((0.9, 0.1), (0.5, 0.5), (0.1, 0.9)):
        score = compute_score(state, success_weight=success_weight, latency_weight=latency_weight)
        print(f"weights {success_weight}/{latency_weight} score={score:.3f}")


async def async_pool() -> None:
    title("AsyncProxyPool statistics")
    config = offline_config()
    async with AsyncProxyPool(config, [Proxy(SEEDS[0]), Proxy(SEEDS[1])]) as pool:
        proxy = await pool.acquire()
        await pool.mark_success(proxy, latency=0.2)
        failed = await pool.acquire()
        await pool.mark_failed(failed, TimeoutError)
        stats = pool.statistics
        print("served", stats.served, "failed", stats.failed, "released", stats.released)


def main() -> None:
    sync_lifecycle()
    strategies()
    scoring_math()
    asyncio.run(async_pool())


if __name__ == "__main__":
    main()
