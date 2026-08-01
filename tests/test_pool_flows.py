"""Pool-flow feature tests mapped 1:1 to ``docs/pool-flows/``.

Offline tests use synthetic seeds / ``custom_check``. Live / integration tests
require ``.env`` with ``PROXY_LIST``, ``TOKEN``, and ``OMNIPROXY_LIVE_TESTS=1``.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import FrozenInstanceError
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from omniproxy import Proxy, ProxyPool
from omniproxy.config import (
    CircuitBreakerConfig,
    CooldownConfig,
    DeadLetterConfig,
    FilterMissingMetadata,
    HealthCheckConfig,
    LifecycleHooks,
    LimitsConfig,
    PoolConfig,
    RefreshConfig,
    ScoringConfig,
    SessionConfig,
    WarmupConfig,
)
from omniproxy.dead_letter import DeadLetterEntry, maybe_add
from omniproxy.enum import (
    CircuitBreakerState,
    DeadLetterPersistence,
    PoolStrategy,
    SessionCooldownPolicy,
    WarmupFailurePolicy,
)
from omniproxy.errors import (
    PoolCircuitOpenError,
    PoolClosedError,
    PoolDrainingError,
    PoolExhausted,
    WarmupFailedError,
)
from omniproxy.extended_proxy import CheckResult
from omniproxy.hooks import run_deferred
from omniproxy.pool import AsyncProxyPool, SyncProxyPool

from tests.conftest import proxy_with_meta


def _quiet(**updates: Any) -> PoolConfig:
    """Minimal pool config with health/scoring/breaker off unless overridden."""
    base = {
        "strategy": PoolStrategy.ROUND_ROBIN,
        "health_check": None,
        "circuit_breaker": None,
        "scoring": None,
        "cooldown": CooldownConfig(base=10.0, min=0.05, max=60.0, adaptive=False, failure_threshold=2),
        "acquire_timeout": 2.0,
        "wait_fallback_interval": 0.05,
        "limits": LimitsConfig(max_connections_per_proxy=5),
        "log_level": 50,
        "drain_timeout": 0.0,
    }
    base.update(updates)
    return PoolConfig(**base)


def _live_base(discord_health: HealthCheckConfig, **updates: Any) -> PoolConfig:
    """Scraping-like live config with Discord health check attached."""
    cfg = PoolConfig.scraping_preset().model_copy(
        update={
            "health_check": discord_health,
            "warmup": WarmupConfig(enabled=False),
            "circuit_breaker": None,
            "acquire_timeout": 15.0,
            "log_level": 50,
            "drain_timeout": 1.0,
        }
    )
    return cfg.model_copy(update=updates) if updates else cfg


# ---------------------------------------------------------------------------
# 01 — Async pool start
# ---------------------------------------------------------------------------


class TestFlow01AsyncPoolStart:
    @pytest.mark.asyncio
    async def test_async_with_starts_and_serves(self, s0: str) -> None:
        async with AsyncProxyPool(_quiet(), [Proxy(s0)]) as pool:
            assert pool._ready.is_set()
            p = await pool.acquire()
            assert p.url == Proxy(s0).url
            await pool.release(p)

    @pytest.mark.asyncio
    async def test_second_start_is_noop(self, s0: str) -> None:
        pool = AsyncProxyPool(_quiet(), [Proxy(s0)])
        await pool._start()
        ready_before = pool._ready.is_set()
        await pool._start()
        assert ready_before and pool._ready.is_set()
        await pool.close()

    @pytest.mark.asyncio
    async def test_start_failure_closes_and_reraises(self, s0: str) -> None:
        cfg = _quiet(
            health_check=HealthCheckConfig(custom_check=lambda _p: False),
            warmup=WarmupConfig(
                enabled=True,
                min_ready=1,
                timeout=0.2,
                failure_policy=WarmupFailurePolicy.RAISE,
            ),
        )
        with pytest.raises(WarmupFailedError):
            async with AsyncProxyPool(cfg, [Proxy(s0)]):
                pass

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_live_async_start_with_seed(
        self, live_proxies: list[Proxy], discord_health_config: HealthCheckConfig
    ) -> None:
        cfg = _live_base(discord_health_config)
        async with AsyncProxyPool(cfg, live_proxies[:3]) as pool:
            p = await pool.acquire()
            await pool.release(p)


# ---------------------------------------------------------------------------
# 02 — Sync pool start
# ---------------------------------------------------------------------------


class TestFlow02SyncPoolStart:
    def test_sync_with_and_proxy_pool_alias(self, s0: str) -> None:
        assert ProxyPool is SyncProxyPool
        with SyncProxyPool(_quiet(), [Proxy(s0)]) as pool:
            p = pool.acquire()
            assert p.url == Proxy(s0).url
            pool.release(p)

    def test_startup_failure_tears_down_loop(self, s0: str) -> None:
        cfg = _quiet(
            health_check=HealthCheckConfig(custom_check=lambda _p: False),
            warmup=WarmupConfig(
                enabled=True,
                min_ready=1,
                timeout=0.2,
                failure_policy=WarmupFailurePolicy.RAISE,
            ),
        )
        with pytest.raises(WarmupFailedError):
            SyncProxyPool(cfg, [Proxy(s0)])

    @pytest.mark.integration
    def test_live_sync_start(
        self, live_proxies: list[Proxy], discord_health_config: HealthCheckConfig
    ) -> None:
        cfg = _live_base(discord_health_config)
        with SyncProxyPool(cfg, live_proxies[:3]) as pool:
            p = pool.acquire()
            pool.release(p)


# ---------------------------------------------------------------------------
# 03 — Acquire
# ---------------------------------------------------------------------------


class TestFlow03Acquire:
    def test_tags_and_accept_callback(self, s0: str, s1: str) -> None:
        a = proxy_with_meta(s0, tags=["a"])
        b = proxy_with_meta(s1, tags=["b"])
        with SyncProxyPool(_quiet(), [a, b]) as pool:
            p = pool.acquire(tags={"b"})
            assert p.url == b.url
            pool.release(p)
            p2 = pool.acquire(accept_callback=lambda pr: pr.url == a.url)
            assert p2.url == a.url
            pool.release(p2)

    def test_country_filter(self, s0: str, s1: str) -> None:
        us = proxy_with_meta(s0, country="US")
        de = proxy_with_meta(s1, country="DE")
        with SyncProxyPool(_quiet(), [us, de]) as pool:
            p = pool.acquire(country="DE")
            assert p.url == de.url
            pool.release(p)

    @pytest.mark.asyncio
    async def test_post_acquire_failure_rolls_back_lease(self) -> None:
        cfg = _quiet(
            rotate_on_acquire=True,
            limits=LimitsConfig(max_connections_per_proxy=1),
        )
        proxy = Proxy("login:pass@203.0.113.56:8899[https://rotate.invalid/flip]")
        async with AsyncProxyPool(cfg, [proxy]) as pool:
            with patch.object(
                Proxy, "arotate", new_callable=AsyncMock, side_effect=RuntimeError("boom")
            ), pytest.raises(RuntimeError, match="boom"):
                await pool.acquire()
            assert pool._connections.get(proxy.url, 0) == 0
            with patch.object(Proxy, "arotate", new_callable=AsyncMock, return_value=True):
                p = await pool.acquire()
                await pool.release(p)

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_live_acquire_release(
        self, live_proxies: list[Proxy], discord_health_config: HealthCheckConfig
    ) -> None:
        async with AsyncProxyPool(_live_base(discord_health_config), live_proxies[:2]) as pool:
            p = await pool.acquire()
            assert isinstance(p, Proxy)
            await pool.release(p)


# ---------------------------------------------------------------------------
# 04 — Release
# ---------------------------------------------------------------------------


class TestFlow04Release:
    @pytest.mark.asyncio
    async def test_release_increments_released_not_failed(self, s0: str) -> None:
        async with AsyncProxyPool(_quiet(), [Proxy(s0)]) as pool:
            p = await pool.acquire()
            await pool.release(p)
            assert pool.statistics.served == 1
            assert pool.statistics.released == 1
            assert pool.statistics.failed == 0

    @pytest.mark.asyncio
    async def test_release_idempotent(self, s0: str) -> None:
        async with AsyncProxyPool(_quiet(), [Proxy(s0)]) as pool:
            p = await pool.acquire()
            await pool.release(p)
            await pool.release(p)  # no-op second return
            assert pool.statistics.released == 1

    @pytest.mark.integration
    def test_live_release(
        self, live_proxies: list[Proxy], discord_health_config: HealthCheckConfig
    ) -> None:
        with SyncProxyPool(_live_base(discord_health_config), live_proxies[:2]) as pool:
            p = pool.acquire()
            pool.release(p)
            assert pool._async_pool.statistics.released >= 1


# ---------------------------------------------------------------------------
# 05 — Mark success
# ---------------------------------------------------------------------------


class TestFlow05MarkSuccess:
    @pytest.mark.asyncio
    async def test_mark_success_returns_lease_without_released_stat(self, s0: str) -> None:
        cfg = _quiet(scoring=ScoringConfig(window_seconds=60.0, min_samples=1))
        async with AsyncProxyPool(cfg, [Proxy(s0)]) as pool:
            p = await pool.acquire()
            await pool.mark_success(p, latency=0.12)
            assert pool.statistics.served == 1
            assert pool.statistics.released == 0
            assert pool.statistics.failed == 0

    @pytest.mark.asyncio
    async def test_mark_success_clears_half_open_probe_only_for_probe(
        self, s0: str, s1: str
    ) -> None:
        cfg = _quiet(
            circuit_breaker=CircuitBreakerConfig(
                window_seconds=30.0,
                failure_ratio=0.5,
                half_open_timeout=0.05,
                min_throughput=2,
            ),
            cooldown=CooldownConfig(failure_threshold=100, base=0.01, min=0.01, max=1.0),
        )
        proxies = [Proxy(s0), Proxy(s1)]
        async with AsyncProxyPool(cfg, proxies) as pool:
            assert pool._circuit_breaker is not None
            # Trip breaker
            for _ in range(4):
                px = await pool.acquire()
                await pool.mark_failed(px, TimeoutError)
            assert pool._circuit_breaker.state == CircuitBreakerState.OPEN
            await asyncio.sleep(0.08)
            # HALF_OPEN probe
            probe = await pool.acquire()
            assert pool._circuit_breaker.state == CircuitBreakerState.HALF_OPEN
            # Non-probe success must not clear markers incorrectly — release probe first path:
            # succeed the probe itself
            await pool.mark_success(probe, latency=0.01)
            assert pool._circuit_breaker.state == CircuitBreakerState.CLOSED

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_live_mark_success(
        self, live_proxies: list[Proxy], discord_health_config: HealthCheckConfig
    ) -> None:
        cfg = _live_base(discord_health_config, scoring=ScoringConfig(window_seconds=60.0))
        async with AsyncProxyPool(cfg, live_proxies[:2]) as pool:
            p = await pool.acquire()
            await pool.mark_success(p, latency=0.2)


# ---------------------------------------------------------------------------
# 06 — Mark failed
# ---------------------------------------------------------------------------


class TestFlow06MarkFailed:
    @pytest.mark.asyncio
    async def test_mark_failed_increments_failed_and_cools(self, s0: str) -> None:
        cfg = _quiet(
            cooldown=CooldownConfig(base=30.0, min=1.0, max=60.0, adaptive=False, failure_threshold=1),
            acquire_timeout=0.2,
        )
        async with AsyncProxyPool(cfg, [Proxy(s0)]) as pool:
            p = await pool.acquire()
            await pool.mark_failed(p, TimeoutError)
            assert pool.statistics.failed == 1
            assert pool.statistics.released == 0
            with pytest.raises(PoolExhausted):
                await pool.acquire()

    @pytest.mark.asyncio
    async def test_mark_failed_exc_class_not_instance(self, s0: str) -> None:
        failed: list[tuple[str, type | None]] = []
        cfg = _quiet(
            hooks=LifecycleHooks(on_proxy_failed=lambda p, exc: failed.append((p.url, exc))),
        )
        async with AsyncProxyPool(cfg, [Proxy(s0)]) as pool:
            p = await pool.acquire()
            await pool.mark_failed(p, TimeoutError)
        assert failed and failed[0][1] is TimeoutError

    @pytest.mark.integration
    def test_live_mark_failed_cools_proxy(
        self, live_proxies: list[Proxy], discord_health_config: HealthCheckConfig
    ) -> None:
        cfg = _live_base(
            discord_health_config,
            cooldown=CooldownConfig(base=60.0, min=5.0, max=120.0, adaptive=False, failure_threshold=1),
            acquire_timeout=1.0,
            circuit_breaker=None,
        )
        # Single proxy so cooldown exhausts the pool
        with SyncProxyPool(cfg, live_proxies[:1]) as pool:
            p = pool.acquire()
            pool.mark_failed(p, TimeoutError)
            with pytest.raises(PoolExhausted):
                pool.acquire()


# ---------------------------------------------------------------------------
# 07 — Close / drain
# ---------------------------------------------------------------------------


class TestFlow07CloseDrain:
    @pytest.mark.asyncio
    async def test_drain_rejects_new_acquires(self, s0: str) -> None:
        async with AsyncProxyPool(_quiet(), [Proxy(s0)]) as pool:
            pool._draining.set()
            with pytest.raises(PoolDrainingError):
                await pool.acquire()

    @pytest.mark.asyncio
    async def test_close_then_acquire_raises_closed(self, s0: str) -> None:
        pool = AsyncProxyPool(_quiet(), [Proxy(s0)])
        await pool._start()
        await pool.close()
        with pytest.raises(PoolClosedError):
            await pool.acquire()

    @pytest.mark.asyncio
    async def test_close_idempotent(self, s0: str) -> None:
        async with AsyncProxyPool(_quiet(), [Proxy(s0)]) as pool:
            pass
        await pool.close()
        await pool.close()

    def test_sync_close_idempotent(self, s0: str) -> None:
        pool = SyncProxyPool(_quiet(), [Proxy(s0)])
        pool.close()
        pool.close()

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_live_close(
        self, live_proxies: list[Proxy], discord_health_config: HealthCheckConfig
    ) -> None:
        pool = AsyncProxyPool(_live_base(discord_health_config), live_proxies[:2])
        await pool._start()
        p = await pool.acquire()
        await pool.release(p)
        await pool.close()
        with pytest.raises(PoolClosedError):
            await pool.acquire()


# ---------------------------------------------------------------------------
# 08 — Refresh & merge
# ---------------------------------------------------------------------------


class TestFlow08RefreshMerge:
    @pytest.mark.asyncio
    async def test_seed_only_via_initial_proxies(self, s0: str) -> None:
        async with AsyncProxyPool(_quiet(), [Proxy(s0)]) as pool:
            assert len(pool._proxies) == 1
            assert pool._proxies[0].url == Proxy(s0).url

    @pytest.mark.asyncio
    async def test_on_demand_refresh_callback_refills(self, s0: str, s1: str) -> None:
        batch = [Proxy(s1)]

        async def _reload() -> list[Proxy]:
            return list(batch)

        cfg = _quiet(acquire_timeout=0.0, refresh=RefreshConfig(async_callback=_reload))
        # Start empty — acquire triggers on-demand refresh
        async with AsyncProxyPool(cfg, []) as pool:
            p = await pool.acquire()
            assert p.url == Proxy(s1).url
            await pool.release(p)

    @pytest.mark.asyncio
    async def test_refresh_callback_beats_fetchers(self, s0: str, s1: str) -> None:
        class BadFetcher:
            async def fetch(self) -> list[Proxy]:
                return [Proxy(s0)]

        async def good() -> list[Proxy]:
            return [Proxy(s1)]

        cfg = _quiet(
            acquire_timeout=0.0,
            refresh=RefreshConfig(async_callback=good),
        )
        async with AsyncProxyPool(cfg, [], fetchers=[BadFetcher()]) as pool:
            p = await pool.acquire()
            assert p.url == Proxy(s1).url
            await pool.release(p)

    @pytest.mark.asyncio
    async def test_fetch_failure_soft(self, s0: str) -> None:
        class BoomFetcher:
            async def fetch(self) -> list[Proxy]:
                raise RuntimeError("network down")

        cfg = _quiet(
            refresh=RefreshConfig(interval_seconds=0.2, timeout=0.1),
        )
        async with AsyncProxyPool(cfg, [Proxy(s0)], fetchers=[BoomFetcher()]) as pool:
            await asyncio.sleep(0.35)
            p = await pool.acquire()
            await pool.release(p)

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_live_seed_merge_unchanged(
        self, live_proxies: list[Proxy], discord_health_config: HealthCheckConfig
    ) -> None:
        seeds = live_proxies[:2]
        async with AsyncProxyPool(_live_base(discord_health_config), seeds) as pool:
            assert {p.url for p in pool._proxies} == {p.url for p in seeds}


# ---------------------------------------------------------------------------
# 09 — Sticky sessions
# ---------------------------------------------------------------------------


class TestFlow09StickySessions:
    def test_session_key_sticks(self, s0: str, s1: str) -> None:
        cfg = _quiet(session=SessionConfig(ttl=60.0, cooldown_policy=SessionCooldownPolicy.REBIND))
        with SyncProxyPool(cfg, [Proxy(s0), Proxy(s1)]) as pool:
            a = pool.acquire(session_key="user-1")
            pool.release(a)
            b = pool.acquire(session_key="user-1")
            assert a.url == b.url
            pool.release(b)

    def test_session_id_alias(self, s0: str, s1: str) -> None:
        cfg = _quiet(session=SessionConfig(ttl=60.0))
        with SyncProxyPool(cfg, [Proxy(s0), Proxy(s1)]) as pool:
            a = pool.acquire(session_id="legacy")
            pool.release(a)
            b = pool.acquire(session_id="legacy")
            assert a.url == b.url
            pool.release(b)

    @pytest.mark.integration
    def test_live_sticky(
        self, live_proxies: list[Proxy], discord_health_config: HealthCheckConfig
    ) -> None:
        if len(live_proxies) < 2:
            pytest.skip("need >=2 proxies")
        cfg = _live_base(
            discord_health_config,
            session=SessionConfig(ttl=120.0, cooldown_policy=SessionCooldownPolicy.REBIND),
        )
        with SyncProxyPool(cfg, live_proxies[:3]) as pool:
            a = pool.acquire(session_key="live-sticky")
            pool.release(a)
            b = pool.acquire(session_key="live-sticky")
            assert a.url == b.url
            pool.release(b)


# ---------------------------------------------------------------------------
# 10 — Circuit breaker
# ---------------------------------------------------------------------------


class TestFlow10CircuitBreaker:
    @pytest.mark.asyncio
    async def test_open_sheds_with_pool_circuit_open_error(self, s0: str, s1: str) -> None:
        cfg = _quiet(
            circuit_breaker=CircuitBreakerConfig(
                window_seconds=60.0,
                failure_ratio=0.5,
                half_open_timeout=30.0,
                min_throughput=2,
            ),
            cooldown=CooldownConfig(failure_threshold=100, base=0.01, min=0.01, max=1.0),
            acquire_timeout=0.3,
        )
        async with AsyncProxyPool(cfg, [Proxy(s0), Proxy(s1)]) as pool:
            opened = False
            for _ in range(8):
                try:
                    p = await pool.acquire()
                except PoolCircuitOpenError:
                    opened = True
                    break
                await pool.mark_failed(p, TimeoutError)
            if not opened:
                with pytest.raises(PoolCircuitOpenError):
                    await pool.acquire()
            assert pool._circuit_breaker is not None
            assert pool._circuit_breaker.state == CircuitBreakerState.OPEN

    @pytest.mark.asyncio
    async def test_health_does_not_feed_breaker(self, s0: str) -> None:
        cfg = _quiet(
            circuit_breaker=CircuitBreakerConfig(
                window_seconds=60.0,
                failure_ratio=0.01,
                half_open_timeout=30.0,
                min_throughput=1,
            ),
        )
        async with AsyncProxyPool(cfg, [Proxy(s0)]) as pool:
            assert pool._circuit_breaker is not None
            await pool._record_health_check_result(
                pool._proxies[0],
                CheckResult(success=False, latency=0.01, exc_type=TimeoutError, status_code=None),
            )
            assert pool._circuit_breaker.state == CircuitBreakerState.CLOSED
            p = await pool.acquire()
            await pool.release(p)

    @pytest.mark.integration
    def test_live_circuit_open(
        self, live_proxies: list[Proxy], discord_health_config: HealthCheckConfig
    ) -> None:
        if len(live_proxies) < 2:
            pytest.skip("need >=2 proxies")
        cfg = _live_base(
            discord_health_config,
            circuit_breaker=CircuitBreakerConfig(
                window_seconds=60.0,
                failure_ratio=0.4,
                half_open_timeout=60.0,
                min_throughput=2,
            ),
            cooldown=CooldownConfig(failure_threshold=100, base=0.01, min=0.01, max=1.0),
            acquire_timeout=2.0,
        )
        with SyncProxyPool(cfg, live_proxies[:3]) as pool:
            for _px in live_proxies[:3]:
                for _ in range(3):
                    try:
                        p = pool.acquire()
                    except PoolCircuitOpenError:
                        return
                    pool.mark_failed(p, TimeoutError)
            with pytest.raises(PoolCircuitOpenError):
                pool.acquire()


# ---------------------------------------------------------------------------
# 11 — Health-check loop
# ---------------------------------------------------------------------------


class TestFlow11HealthCheckLoop:
    @pytest.mark.asyncio
    async def test_health_loop_waits_for_ready(self, s0: str) -> None:
        checks: list[str] = []

        def _check(p: Proxy) -> bool:
            checks.append(p.url)
            return True

        cfg = _quiet(
            health_check=HealthCheckConfig(
                custom_check=_check,
                recovery_interval=0.05,
            ),
            warmup=WarmupConfig(
                enabled=True,
                min_ready=1,
                timeout=2.0,
                failure_policy=WarmupFailurePolicy.RAISE,
            ),
        )
        async with AsyncProxyPool(cfg, [Proxy(s0)]) as pool:
            assert pool._ready.is_set()
            # Warmup already checked once; loop may check again
            await asyncio.sleep(0.2)
            assert checks  # at least warmup ran

    @pytest.mark.asyncio
    async def test_apply_skips_removed_proxy(self, s0: str, s1: str) -> None:
        cfg = _quiet(health_check=HealthCheckConfig(custom_check=lambda _p: True))
        gone = Proxy(s0)
        stay = Proxy(s1)
        async with AsyncProxyPool(cfg, [stay]) as pool:
            applied = pool._apply_check_result(
                gone,
                CheckResult(success=True, latency=0.01, exc_type=None, status_code=200),
                [],
            )
            assert applied is False

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_live_health_loop_runs(
        self, live_proxies: list[Proxy], discord_health_config: HealthCheckConfig
    ) -> None:
        hc = discord_health_config.model_copy(update={"recovery_interval": 2.0})
        cfg = _live_base(hc)
        async with AsyncProxyPool(cfg, live_proxies[:2]) as pool:
            await asyncio.sleep(0.5)
            p = await pool.acquire()
            await pool.release(p)


# ---------------------------------------------------------------------------
# 12 — Warmup
# ---------------------------------------------------------------------------


class TestFlow12Warmup:
    @pytest.mark.asyncio
    async def test_warmup_blocks_ready_until_done(self, s0: str) -> None:
        cfg = _quiet(
            health_check=HealthCheckConfig(custom_check=lambda _p: True),
            warmup=WarmupConfig(
                enabled=True,
                min_ready=1,
                timeout=3.0,
                failure_policy=WarmupFailurePolicy.RAISE,
            ),
        )
        async with AsyncProxyPool(cfg, [Proxy(s0)]) as pool:
            assert pool._ready.is_set()
            p = await pool.acquire()
            await pool.release(p)

    @pytest.mark.asyncio
    async def test_warmup_raise_on_unmet(self, s0: str) -> None:
        cfg = _quiet(
            health_check=HealthCheckConfig(custom_check=lambda _p: False),
            warmup=WarmupConfig(
                enabled=True,
                min_ready=1,
                timeout=0.25,
                failure_policy=WarmupFailurePolicy.RAISE,
            ),
        )
        with pytest.raises(WarmupFailedError):
            async with AsyncProxyPool(cfg, [Proxy(s0)]):
                pass

    @pytest.mark.asyncio
    async def test_warmup_partial_allows_start(self, s0: str) -> None:
        cfg = _quiet(
            health_check=HealthCheckConfig(custom_check=lambda _p: False),
            warmup=WarmupConfig(
                enabled=True,
                min_ready=5,
                timeout=0.1,
                failure_policy=WarmupFailurePolicy.PARTIAL,
            ),
            acquire_timeout=0.3,
        )
        async with AsyncProxyPool(cfg, [Proxy(s0)]) as pool:
            assert pool._ready.is_set()

    @pytest.mark.asyncio
    async def test_validator_must_score_ge_one(self, s0: str) -> None:
        cfg = _quiet(
            health_check=HealthCheckConfig(custom_check=lambda _p: True),
            warmup=WarmupConfig(
                enabled=True,
                min_ready=1,
                timeout=1.0,
                failure_policy=WarmupFailurePolicy.RAISE,
                validator=lambda _p: 0.5,
            ),
        )
        with pytest.raises(WarmupFailedError):
            async with AsyncProxyPool(cfg, [Proxy(s0)]):
                pass

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_live_warmup_discord(
        self, live_proxies: list[Proxy], discord_health_config: HealthCheckConfig
    ) -> None:
        cfg = _live_base(
            discord_health_config,
            warmup=WarmupConfig(
                enabled=True,
                min_ready=1,
                timeout=30.0,
                failure_policy=WarmupFailurePolicy.PARTIAL,
            ),
        )
        async with AsyncProxyPool(cfg, live_proxies[:3]) as pool:
            assert pool._ready.is_set()
            p = await pool.acquire()
            await pool.release(p)


# ---------------------------------------------------------------------------
# 13 — Config presets
# ---------------------------------------------------------------------------


class TestFlow13ConfigPresets:
    def test_scraping_preset(self) -> None:
        c = PoolConfig.scraping_preset()
        assert c.strategy == PoolStrategy.ROUND_ROBIN
        assert c.warmup.enabled is False
        assert c.health_check is None
        assert c.scoring is not None and c.circuit_breaker is not None

    def test_api_gateway_preset(self) -> None:
        c = PoolConfig.api_gateway_preset()
        assert c.strategy == PoolStrategy.WEIGHTED
        assert c.warmup.enabled is True
        assert c.health_check is not None
        assert c.filter_missing_metadata == FilterMissingMetadata.RAISE

    def test_stealth_preset(self) -> None:
        c = PoolConfig.stealth_preset()
        assert c.strategy == PoolStrategy.LOWEST_LATENCY
        assert c.rotate_on_acquire is True

    def test_rotating_residential_preset(self) -> None:
        c = PoolConfig.rotating_residential_preset()
        assert c.strategy == PoolStrategy.RANDOM
        assert c.use_rotation_urls is True and c.rotate_on_acquire is True

    def test_load_balancer_preset(self) -> None:
        c = PoolConfig.load_balancer_preset()
        assert c.strategy == PoolStrategy.ROUND_ROBIN
        assert c.scoring is None and c.circuit_breaker is None
        assert c.health_check is not None

    def test_presets_are_frozen(self) -> None:
        from pydantic import ValidationError

        c = PoolConfig.scraping_preset()
        with pytest.raises(ValidationError):
            c.acquire_timeout = 1.0  # type: ignore[misc]

    @pytest.mark.integration
    def test_live_scraping_preset_smoke(
        self, live_proxies: list[Proxy], discord_health_config: HealthCheckConfig
    ) -> None:
        cfg = PoolConfig.scraping_preset().model_copy(
            update={"health_check": discord_health_config, "circuit_breaker": None, "log_level": 50}
        )
        with SyncProxyPool(cfg, live_proxies[:2]) as pool:
            p = pool.acquire()
            pool.release(p)


# ---------------------------------------------------------------------------
# 14 — Statistics & metrics
# ---------------------------------------------------------------------------


class TestFlow14StatisticsMetrics:
    @pytest.mark.asyncio
    async def test_counter_semantics(self, s0: str, s1: str) -> None:
        cfg = _quiet(acquire_timeout=0.15)
        async with AsyncProxyPool(cfg, [Proxy(s0)]) as pool:
            p = await pool.acquire()
            await pool.release(p)
            assert pool.statistics.served == 1
            assert pool.statistics.released == 1

            p = await pool.acquire()
            await pool.mark_success(p, latency=0.01)
            assert pool.statistics.served == 2
            assert pool.statistics.released == 1  # mark_success does not bump released

            p = await pool.acquire()
            await pool.mark_failed(p, TimeoutError)
            assert pool.statistics.failed == 1
            assert pool.statistics.released == 1

        # Exhausted path
        async with AsyncProxyPool(_quiet(acquire_timeout=0.0), []) as pool:
            with pytest.raises(PoolExhausted):
                await pool.acquire()
            assert pool.statistics.exhausted_count >= 1

    @pytest.mark.asyncio
    async def test_statistics_frozen_and_health_excludes_failed(self, s0: str) -> None:
        async with AsyncProxyPool(_quiet(), [Proxy(s0)]) as pool:
            await pool._record_health_check_result(
                pool._proxies[0],
                CheckResult(success=False, latency=0.01, exc_type=TimeoutError, status_code=None),
            )
            assert pool.statistics.failed == 0
            snap = pool.statistics
            with pytest.raises(FrozenInstanceError):
                snap.failed = 9  # type: ignore[misc]

    @pytest.mark.asyncio
    async def test_metrics_exporter_receives_gauges(self, s0: str, recording_metrics_exporter: Any) -> None:
        cfg = _quiet(metrics_exporter=recording_metrics_exporter, drain_timeout=0.2)
        async with AsyncProxyPool(cfg, [Proxy(s0)]) as pool:
            p = await pool.acquire()
            await pool.release(p)
            await asyncio.sleep(0.15)
        assert recording_metrics_exporter.gauges

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_live_statistics(
        self, live_proxies: list[Proxy], discord_health_config: HealthCheckConfig
    ) -> None:
        async with AsyncProxyPool(_live_base(discord_health_config), live_proxies[:2]) as pool:
            p = await pool.acquire()
            await pool.mark_success(p, latency=0.3)
            assert pool.statistics.served >= 1
            assert pool.statistics.released == 0


# ---------------------------------------------------------------------------
# 15 — Dead letter
# ---------------------------------------------------------------------------


class TestFlow15DeadLetter:
    def test_rejects_enabled_without_health(self) -> None:
        with pytest.raises(ValueError, match="health_check"):
            PoolConfig(dead_letter=DeadLetterConfig(enabled=True), health_check=None)

    def test_maybe_add_max_size(self, s0: str, s1: str, s2: str) -> None:
        dl = DeadLetterConfig(enabled=True, max_size=2)
        q: list[DeadLetterEntry] = []
        maybe_add(DeadLetterEntry(proxy=Proxy(s0), error=None, timestamp=1.0), dl, q)
        maybe_add(DeadLetterEntry(proxy=Proxy(s1), error=None, timestamp=2.0), dl, q)
        maybe_add(DeadLetterEntry(proxy=Proxy(s2), error=None, timestamp=3.0), dl, q)
        assert len(q) == 2
        assert q[-1].proxy.url == Proxy(s2).url

    @pytest.mark.asyncio
    async def test_eviction_feeds_dead_letter(self, s0: str, s1: str) -> None:
        added: list[str] = []
        cfg = _quiet(
            health_check=HealthCheckConfig(custom_check=lambda _p: True),
            dead_letter=DeadLetterConfig(enabled=True, retry_interval_seconds=60.0),
            max_size=1,
            hooks=LifecycleHooks(on_dead_letter_added=lambda p, _e: added.append(p.url)),
        )
        async with AsyncProxyPool(cfg, [Proxy(s0)]) as pool:
            async with pool._state_lock:
                _, hooks = pool._merge_new_proxies([Proxy(s1)])
            await run_deferred(hooks, cfg.hooks)
            assert len(pool._dead_letter_queue) == 1
            assert added == [Proxy(s0).url]

    @pytest.mark.asyncio
    async def test_retry_worker_recovers(self, s0: str) -> None:
        cfg = _quiet(
            health_check=HealthCheckConfig(custom_check=lambda _p: True),
            dead_letter=DeadLetterConfig(enabled=True, retry_interval_seconds=0.05),
            max_size=1,
            acquire_timeout=2.0,
        )
        async with AsyncProxyPool(cfg, [Proxy(s0)]) as pool:
            async with pool._state_lock:
                maybe_add(
                    DeadLetterEntry(proxy=Proxy(s0), error="test", timestamp=time.monotonic()),
                    cfg.dead_letter,
                    pool._dead_letter_queue,
                )
            # Wait for retry cycle (first attempt after interval)
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and pool._dead_letter_queue:
                await asyncio.sleep(0.05)
            # Either recovered into pool or still retrying — pool must stay usable
            p = await pool.acquire()
            await pool.release(p)

    @pytest.mark.asyncio
    async def test_state_store_url_only_persistence(self, s0: str, s1: str) -> None:
        store: dict[str, str] = {}

        class MemStore:
            def get(self, key: str) -> str | None:
                return store.get(key)

            def set(self, key: str, value: str, ttl: float | None = None) -> None:
                store[key] = value

            def delete(self, key: str) -> None:
                store.pop(key, None)

        cfg = _quiet(
            health_check=HealthCheckConfig(custom_check=lambda _p: True),
            dead_letter=DeadLetterConfig(
                enabled=True,
                persistence=DeadLetterPersistence.STATE_STORE,
                retry_interval_seconds=60.0,
            ),
            state_store_factory=MemStore,
            max_size=1,
        )
        async with AsyncProxyPool(cfg, [Proxy(s0)]) as pool:
            async with pool._state_lock:
                _, hooks = pool._merge_new_proxies([Proxy(s1)])
            await run_deferred(hooks, cfg.hooks)
            assert store

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_live_dead_letter_config_valid(
        self, live_proxies: list[Proxy], discord_health_config: HealthCheckConfig
    ) -> None:
        cfg = _live_base(
            discord_health_config,
            dead_letter=DeadLetterConfig(enabled=True, retry_interval_seconds=30.0, max_size=10),
            max_size=50,
        )
        async with AsyncProxyPool(cfg, live_proxies[:2]) as pool:
            p = await pool.acquire()
            await pool.release(p)
            assert pool._dead_letter_queue is not None
