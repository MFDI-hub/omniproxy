"""Cooldown shapes and the per-proxy connection cap."""

from __future__ import annotations

from omniproxy import Proxy
from omniproxy.config import CooldownConfig, LimitsConfig
from omniproxy.cooldown import compute_cooldown, is_in_cooldown
from omniproxy.errors import PoolSaturated
from omniproxy.pool import SyncProxyPool

from _support import SEEDS, offline_config, title


def cooldown_math() -> None:
    title("compute_cooldown permutations")
    specs = (
        CooldownConfig(base=10, min=1, max=100, adaptive=False, failure_threshold=1),
        CooldownConfig(base=10, min=1, max=40, adaptive=True, failure_threshold=1),
        CooldownConfig(
            base=10,
            min=1,
            max=100,
            adaptive=True,
            penalties={TimeoutError: 5.0, ConnectionError: 2.0},
        ),
        CooldownConfig(
            base=10,
            min=1,
            max=100,
            strategy=lambda base, consecutive, total: base + consecutive + total,
        ),
    )
    for index, config in enumerate(specs):
        for failures in (1, 2, 4):
            if config.strategy is not None:
                seconds = config.strategy(config.base, failures, 1)
                seconds = max(config.min, min(config.max, seconds))
            else:
                seconds = compute_cooldown(
                    config.base,
                    config.adaptive,
                    failures,
                    config.penalties,
                    TimeoutError,
                    _min=config.min,
                    _max=config.max,
                )
            print(f"spec {index} failures={failures} cooldown={seconds}")

    until = {"http://10.0.0.1:8000": 10_000.0}
    print("in cooldown", is_in_cooldown("http://10.0.0.1:8000", until, now=1.0))
    print("expired   ", is_in_cooldown("http://10.0.0.1:8000", until, now=10_001.0))


def pool_cooldown() -> None:
    title("failure_threshold before the only proxy is cooled down")
    for threshold in (1, 3):
        pool = SyncProxyPool(
            offline_config(
                cooldown=CooldownConfig(
                    base=60.0,
                    min=60.0,
                    max=60.0,
                    adaptive=False,
                    failure_threshold=threshold,
                )
            ),
            [Proxy(SEEDS[0])],
        )
        try:
            first = pool.acquire()
            pool.mark_failed(first, TimeoutError)
            try:
                second = pool.acquire()
                print(f"threshold={threshold} still available", second.url)
                pool.release(second)
            except Exception as exc:
                print(f"threshold={threshold}", type(exc).__name__)
        finally:
            pool.close()


def connection_cap() -> None:
    title("max_connections_per_proxy")
    for cap in (1, 2):
        pool = SyncProxyPool(
            offline_config(limits=LimitsConfig(max_connections_per_proxy=cap, max_rps_per_proxy=5.0)),
            [Proxy(SEEDS[0])],
        )
        held = []
        try:
            for _ in range(cap + 1):
                try:
                    held.append(pool.acquire())
                except PoolSaturated as exc:
                    print(f"cap={cap} saturated after {len(held)}", exc)
                    break
            else:
                print(f"cap={cap} acquired {len(held)} without saturation")
        finally:
            for proxy in held:
                pool.release(proxy)
            pool.close()

    title("custom token bucket factory is accepted on LimitsConfig")
    class Bucket:
        def consume(self, tokens: int = 1) -> bool:
            return True

        def refill(self) -> None:
            return None

        def tokens_available(self) -> float:
            return 1.0

    limits = LimitsConfig(
        max_connections_per_proxy=2,
        max_rps_per_proxy=1.0,
        token_bucket_capacity=3.0,
        token_bucket_factory=lambda _proxy: Bucket(),
    )
    print("limits", limits.max_connections_per_proxy, limits.max_rps_per_proxy, limits.token_bucket_capacity)


def main() -> None:
    cooldown_math()
    pool_cooldown()
    connection_cap()


if __name__ == "__main__":
    main()
