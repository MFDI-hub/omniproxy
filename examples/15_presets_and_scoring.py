"""Every PoolConfig preset, run offline by swapping in a local health probe."""

from __future__ import annotations

import asyncio

from omniproxy import Proxy
from omniproxy.config import HealthCheckConfig, PoolConfig
from omniproxy.pool import AsyncProxyPool
from omniproxy.scoring import EMAState, compute_score, update_ema

from _support import SEEDS, title

PRESETS = (
    "scraping_preset",
    "api_gateway_preset",
    "stealth_preset",
    "rotating_residential_preset",
    "load_balancer_preset",
)


def offline_preset(name: str) -> PoolConfig:
    preset = getattr(PoolConfig, name)()
    health = preset.health_check
    if health is None and preset.warmup.enabled:
        health = HealthCheckConfig(custom_check=lambda _proxy: True)
    elif health is not None:
        health = health.model_copy(update={"custom_check": lambda _proxy: True, "url": None})
    return preset.model_copy(
        update={
            "health_check": health,
            "acquire_timeout": 0.0,
            "rotate_on_acquire": False,
            "rotate_on_failure": False,
        }
    )


async def run_presets() -> None:
    proxies = [Proxy(SEEDS[0]), Proxy(SEEDS[1])]
    for name in PRESETS:
        config = offline_preset(name)
        print(
            name,
            "strategy",
            config.strategy.value,
            "session",
            config.session.cooldown_policy.value,
            "missing",
            config.filter_missing_metadata.value,
            "warmup",
            config.warmup.failure_policy.value if config.warmup.enabled else "off",
            "breaker",
            config.circuit_breaker is not None,
            "scoring",
            config.scoring is not None,
        )
        try:
            async with AsyncProxyPool(config, proxies) as pool:
                proxy = await pool.acquire()
                await pool.mark_success(proxy, latency=0.2)
                print("  acquired", proxy.url)
        except Exception as exc:
            print("  ", type(exc).__name__, exc)


def score_grid() -> None:
    title("success_weight x latency_weight")
    state = EMAState()
    update_ema(state, success=True, latency=0.05, decay=0.8)
    update_ema(state, success=True, latency=0.05, decay=0.8)
    update_ema(state, success=False, latency=0.9, decay=0.8)
    for success in (0.0, 0.25, 0.5, 0.75, 1.0):
        latency = 1.0 - success
        print(f"success={success:.2f} latency={latency:.2f} score={compute_score(state, success, latency):.3f}")


def main() -> None:
    title("presets")
    asyncio.run(run_presets())
    score_grid()


if __name__ == "__main__":
    main()
