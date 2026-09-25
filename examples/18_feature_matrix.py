"""Cross-product of the pool axes that change selection, sessions, and metadata handling."""

from __future__ import annotations

import asyncio
import itertools

from omniproxy.enum import (
    FilterMissingMetadata,
    PoolStrategy,
    PoolStructure,
    SessionCooldownPolicy,
    WarmupFailurePolicy,
)
from omniproxy.config import CooldownConfig, HealthCheckConfig, SessionConfig, WarmupConfig
from omniproxy.errors import WarmupFailedError
from omniproxy.pool import AsyncProxyPool

from _support import SEEDS, meta, offline_config, title


async def selection_matrix() -> None:
    title("strategy x structure x session policy x missing-metadata policy")
    proxy = meta(SEEDS[0], country="US", anonymity="elite", latency=0.2, tags=("fast",))
    axes = itertools.product(
        PoolStrategy,
        PoolStructure,
        SessionCooldownPolicy,
        FilterMissingMetadata,
    )
    for strategy, structure, session_policy, missing in axes:
        config = offline_config(
            strategy=strategy,
            structure=structure,
            session=SessionConfig(ttl=30.0, cooldown_policy=session_policy),
            filter_missing_metadata=missing,
        )
        async with AsyncProxyPool(config, [proxy]) as pool:
            leased = await pool.acquire(
                country="US",
                min_anonymity="elite",
                max_latency=1.0,
                tags={"fast"},
                session_key="matrix",
            )
            print(
                f"{strategy.value:16} {structure.value:5} "
                f"{session_policy.value:6} {missing.value:6} {leased.ip}"
            )
            await pool.release(leased)


async def warmup_matrix() -> None:
    title("warmup failure policy x probe result")
    proxies = [meta(SEEDS[1])]
    for policy in WarmupFailurePolicy:
        for probe_ok in (True, False):
            config = offline_config(
                cooldown=CooldownConfig(
                    base=30.0,
                    min=1.0,
                    max=60.0,
                    adaptive=False,
                    failure_threshold=99,
                ),
                health_check=HealthCheckConfig(custom_check=lambda _proxy, ok=probe_ok: ok),
                warmup=WarmupConfig(
                    enabled=True,
                    min_ready=1,
                    timeout=3.0,
                    failure_policy=policy,
                ),
            )
            label = f"{policy.value} probe={probe_ok}"
            try:
                async with AsyncProxyPool(config, proxies) as pool:
                    leased = await pool.acquire()
                    print(label, "acquired", leased.ip)
                    await pool.release(leased)
            except WarmupFailedError:
                print(label, "WarmupFailedError")
            except Exception as exc:
                print(label, type(exc).__name__, exc)


def main() -> None:
    asyncio.run(selection_matrix())
    asyncio.run(warmup_matrix())


if __name__ == "__main__":
    main()
