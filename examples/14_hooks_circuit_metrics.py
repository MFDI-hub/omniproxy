"""Lifecycle hooks, circuit-breaker states, and a metrics exporter."""

from __future__ import annotations

import asyncio

from omniproxy import Proxy
from omniproxy.circuit_breaker import CircuitBreaker
from omniproxy.config import (
    CircuitBreakerConfig,
    CooldownConfig,
    HealthCheckConfig,
    LifecycleHooks,
    LimitsConfig,
    RefreshConfig,
    SessionConfig,
)
from omniproxy.enum import CircuitBreakerState, SessionCooldownPolicy
from omniproxy.errors import PoolCircuitOpenError, PoolExhausted, PoolSaturated
from omniproxy.metrics import PrometheusExporter
from omniproxy.pool import AsyncProxyPool

from _support import SEEDS, offline_config, title


class MemoryExporter:
    def __init__(self) -> None:
        self.samples: list[tuple[str, float]] = []

    def emit_gauge(self, name: str, value: float, tags: dict[str, str] | None = None) -> None:
        self.samples.append((name, value))

    def emit_counter(self, name: str, value: float, tags: dict[str, str] | None = None) -> None:
        self.samples.append((name, value))

    def close(self) -> None:
        return None


def breaker_states() -> None:
    title("CircuitBreaker CLOSED -> OPEN -> HALF_OPEN -> CLOSED, and a failed probe")
    config = CircuitBreakerConfig(
        window_seconds=60.0,
        failure_ratio=0.5,
        half_open_timeout=10.0,
        min_throughput=2,
    )
    breaker = CircuitBreaker(config)
    now = 1_000.0
    print("start", breaker.state.value)
    breaker.record_failure(now)
    breaker.record_failure(now)
    print("after failures", breaker.state.value, "allow", breaker.allow_request(now))
    print("half-open slot", breaker.allow_request(now + config.half_open_timeout))
    print("state", breaker.state.value)
    epoch = breaker.active_probe_epoch
    breaker.record_success(now + config.half_open_timeout, probe_epoch=epoch)
    print("after probe success", breaker.state.value)

    breaker.record_failure(now)
    breaker.record_failure(now)
    breaker.allow_request(now + config.half_open_timeout)
    epoch = breaker.active_probe_epoch
    breaker.record_failure(now + config.half_open_timeout, probe_epoch=epoch)
    print("after probe failure", breaker.state.value)
    print("members", [item.value for item in CircuitBreakerState])


async def pool_breaker() -> None:
    title("pool acquire while the breaker is open, then a half-open probe")
    exporter = MemoryExporter()
    fired: list[str] = []
    config = offline_config(
        cooldown=CooldownConfig(
            base=60.0,
            min=60.0,
            max=60.0,
            adaptive=False,
            failure_threshold=99,
        ),
        circuit_breaker=CircuitBreakerConfig(
            window_seconds=60.0,
            failure_ratio=0.5,
            half_open_timeout=0.05,
            min_throughput=2,
        ),
        metrics_exporter=exporter,
        hooks=LifecycleHooks(
            on_circuit_open=lambda: fired.append("open"),
            on_circuit_close=lambda: fired.append("close"),
            on_proxy_failed=lambda _proxy, _exc: fired.append("failed"),
            on_proxy_acquired=lambda _proxy: fired.append("acquired"),
        ),
    )
    async with AsyncProxyPool(config, [Proxy(SEEDS[0])]) as pool:
        for _ in range(2):
            proxy = await pool.acquire()
            await pool.mark_failed(proxy, TimeoutError)
        try:
            await pool.acquire()
        except PoolCircuitOpenError as exc:
            print("open", type(exc).__name__)
        await asyncio.sleep(0.1)
        probe = await pool.acquire()
        await pool.mark_success(probe, latency=0.1)
        again = await pool.acquire()
        await pool.release(again)
        await asyncio.sleep(0.2)
        print("hooks", fired)
        print("metrics", exporter.samples)
        print("stats", pool.statistics)


async def remaining_hooks() -> None:
    title("acquire, release, cooldown, saturation, exhaustion, refresh, session, drain")
    fired: list[str] = []

    def note(name: str):
        def _hook(*_args: object) -> None:
            fired.append(name)

        return _hook

    async def source() -> list[str]:
        return [SEEDS[2]]

    hooks = LifecycleHooks(
        on_proxy_acquired=note("acquired"),
        on_proxy_released=note("released"),
        on_proxy_failed=note("failed"),
        on_proxy_cooled_down=note("cooled"),
        on_proxy_recovered=note("recovered"),
        on_exhausted=note("exhausted"),
        on_saturated=note("saturated"),
        on_check_complete=note("check"),
        on_refresh_started=note("refresh-start"),
        on_refresh_completed=note("refresh-done"),
        on_session_rebind=note("rebind"),
        on_draining=note("draining"),
        on_auto_evicted=note("evicted"),
        on_dead_letter_added=note("dead"),
        on_config_updated=note("config"),
    )
    session_config = offline_config(
        session=SessionConfig(ttl=60.0, cooldown_policy=SessionCooldownPolicy.REBIND),
        hooks=hooks,
    )
    async with AsyncProxyPool(session_config, [Proxy(SEEDS[0]), Proxy(SEEDS[1])]) as pool:
        first = await pool.acquire(session_key="s")
        await pool.mark_failed(first, ConnectionError)
        second = await pool.acquire(session_key="s")
        await pool.release(second)

    cap_config = offline_config(
        limits=LimitsConfig(max_connections_per_proxy=1),
        hooks=hooks,
    )
    async with AsyncProxyPool(cap_config, [Proxy(SEEDS[0])]) as pool:
        held = await pool.acquire()
        try:
            await pool.acquire()
        except PoolSaturated:
            fired.append("saturated-exc")
        await pool.release(held)

    health_config = offline_config(
        health_check=HealthCheckConfig(custom_check=lambda _proxy: True, check_interval=0.2),
        hooks=hooks,
    )
    async with AsyncProxyPool(health_config, [Proxy(SEEDS[0])]) as pool:
        await asyncio.sleep(0.5)

    refresh_config = offline_config(
        refresh=RefreshConfig(async_callback=source, timeout=2.0, interval_seconds=30.0),
        hooks=hooks,
    )
    async with AsyncProxyPool(refresh_config, []) as pool:
        proxy = await pool.acquire()
        await pool.release(proxy)

    async with AsyncProxyPool(offline_config(hooks=hooks), []) as empty:
        try:
            await empty.acquire()
        except PoolExhausted:
            fired.append("exhausted-exc")
    print("fired", sorted(set(fired)))


def prometheus() -> None:
    title("PrometheusExporter")
    try:
        exporter = PrometheusExporter()
    except ImportError as exc:
        print("prometheus_client not installed", exc)
        return
    exporter.emit_gauge("omniproxy_example_up", 1.0, {"source": "examples"})
    exporter.emit_counter("omniproxy_example_total", 1.0, {"source": "examples"})
    exporter.close()
    print("emitted")


def main() -> None:
    breaker_states()
    asyncio.run(pool_breaker())
    asyncio.run(remaining_hooks())
    prometheus()


if __name__ == "__main__":
    main()
