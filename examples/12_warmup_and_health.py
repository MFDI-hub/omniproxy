"""Warmup failure policies, custom validators, and health-check specifications."""

from __future__ import annotations

import asyncio

from omniproxy import CheckResult, Proxy
from omniproxy.config import CooldownConfig, HealthCheckConfig, WarmupConfig, bool_to_score
from omniproxy.enum import HttpVerb, WarmupFailurePolicy
from omniproxy.errors import WarmupFailedError
from omniproxy.extended_proxy import arun_health_check, run_health_check
from omniproxy.pool import AsyncProxyPool

from _support import SEEDS, offline_config, title


def health_specs() -> None:
    title("run_health_check with a custom probe")
    proxy = Proxy(SEEDS[0])

    def allow(candidate: Proxy) -> bool:
        return candidate.port == 8000

    def deny(_candidate: Proxy) -> bool:
        return False

    print("HttpVerb", [verb.value for verb in HttpVerb])
    for name, check in (("allow", allow), ("deny", deny)):
        for method in ("GET", "HEAD"):
            spec = HealthCheckConfig(
                custom_check=check,
                method=method,
                expected_status=200,
                expected_json_fields={"status": "ok"},
                headers={"X-Example": "1"},
                timeout=2.0,
                max_concurrent_checks=4,
                recovery_interval=5.0,
                check_interval=30.0,
                max_latency=1.0,
            )
            _checked, result = run_health_check(proxy, spec)
            print(name, method, result.success)

    async def async_probe() -> None:
        spec = HealthCheckConfig(custom_check=allow)
        _checked, result = await arun_health_check(proxy, spec)
        print("arun_health_check", result.success)

    asyncio.run(async_probe())
    print("bool_to_score", bool_to_score(True), bool_to_score(False))
    _ = CheckResult


async def warmup_policies() -> None:
    title("WarmupFailurePolicy")
    proxies = [Proxy(SEEDS[0]), Proxy(SEEDS[1])]

    def passing(_proxy: Proxy) -> bool:
        return True

    def failing(_proxy: Proxy) -> bool:
        return False

    def score_of(proxy: Proxy) -> float:
        return 1.0 if proxy.port == 8000 else 0.0

    cases = (
        ("pass-raise", passing, WarmupFailurePolicy.RAISE, None),
        ("pass-partial", passing, WarmupFailurePolicy.PARTIAL, None),
        ("fail-raise", failing, WarmupFailurePolicy.RAISE, None),
        ("fail-partial", failing, WarmupFailurePolicy.PARTIAL, None),
        ("validator", passing, WarmupFailurePolicy.PARTIAL, score_of),
    )
    for label, probe, policy, validator in cases:
        config = offline_config(
            cooldown=CooldownConfig(
                base=30.0,
                min=1.0,
                max=60.0,
                adaptive=False,
                failure_threshold=99,
            ),
            health_check=HealthCheckConfig(custom_check=probe),
            warmup=WarmupConfig(
                enabled=True,
                min_ready=1,
                timeout=5.0,
                failure_policy=policy,
                validator=validator,
            ),
        )
        try:
            async with AsyncProxyPool(config, proxies) as pool:
                leased = await pool.acquire()
                print(label, policy.value, "ready", leased.url)
                await pool.release(leased)
        except WarmupFailedError as exc:
            print(label, policy.value, "WarmupFailedError", exc)


def main() -> None:
    health_specs()
    asyncio.run(warmup_policies())


if __name__ == "__main__":
    main()
