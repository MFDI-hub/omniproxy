"""Advanced pool tests: scoring, filtering, sessions, circuit breaker, cooldown."""

from __future__ import annotations

import time

import pytest
from omniproxy import Proxy
from omniproxy.config import CircuitBreakerConfig
from omniproxy.errors import PoolCircuitOpenError, PoolExhausted, SessionBrokenError
from omniproxy.pool import AsyncProxyPool, SyncProxyPool

from tests.conftest import proxy_with_meta

class TestWeightedLowestLatency:
    def test_weighted_selection(self, s0, s1, weighted_scoring_pool_config):
        cfg = weighted_scoring_pool_config
        p1 = Proxy(s0)
        p2 = Proxy(s1)
        pool = SyncProxyPool(cfg, [p1, p2])

        for _ in range(10):
            pool.mark_success(p1, latency=0.1)
            pool.mark_success(p2, latency=1.0)

        time.sleep(0.05)
        wins = 0
        for _ in range(100):
            chosen = pool.acquire()
            pool.release(chosen)
            if chosen.url == p1.url:
                wins += 1
        assert wins >= 55   # 55% is a safer threshold after 100 trials
        pool.close()

    def test_lowest_latency_selection(self, s0, s1, lowest_latency_strategy_pool_config):
        cfg = lowest_latency_strategy_pool_config
        p1 = Proxy(s0)
        p2 = Proxy(s1)
        pool = SyncProxyPool(cfg, [p1, p2])
        pool.mark_success(p1, latency=0.2)
        pool.mark_success(p2, latency=0.1)
        chosen = pool.acquire()
        assert chosen.url == p2.url
        pool.release(chosen)
        pool.close()


class TestSessionStickiness:
    def test_session_rebind_policy(self, s0, s1, session_rebind_ttl60_pool_config):
        cfg = session_rebind_ttl60_pool_config
        p1 = Proxy(s0)
        p2 = Proxy(s1)
        pool = SyncProxyPool(cfg, [p1, p2])
        first = pool.acquire(session_key="sess1")
        pool.release(first)
        second = pool.acquire(session_key="sess1")
        assert first.url == second.url
        pool.release(second)
        pool.close()

    def test_block_policy_raises_on_cooldown(self, s0, session_block_strict_cooldown_pool_config):
        cfg = session_block_strict_cooldown_pool_config
        p = Proxy(s0)
        pool = SyncProxyPool(cfg, [p])
        pool.acquire(session_key="s1")
        pool.mark_failed(p)
        with pytest.raises(PoolExhausted):
            pool.acquire(session_key="s1")
        pool.close()

    def test_raise_policy_raises_broken(self, s0, session_raise_strict_cooldown_pool_config):
        cfg = session_raise_strict_cooldown_pool_config
        p = Proxy(s0)
        pool = SyncProxyPool(cfg, [p])
        pool.acquire(session_key="s1")
        pool.mark_failed(p)
        with pytest.raises(SessionBrokenError):
            pool.acquire(session_key="s1")
        pool.close()


class TestFiltering:
    def test_min_anonymity(self, s0, s1, minimal_round_robin_pool_config):
        low = proxy_with_meta(s0, anonymity="transparent")
        high = proxy_with_meta(s1, anonymity="elite")
        cfg = minimal_round_robin_pool_config
        pool = SyncProxyPool(cfg, [low, high])
        p = pool.acquire(min_anonymity="elite")
        assert p.anonymity == "elite"
        pool.release(p)
        pool.close()

    def test_country_filter(self, s0, s1, minimal_round_robin_pool_config):
        us = proxy_with_meta(s0, country="US")
        fr = proxy_with_meta(s1, country="FR")
        cfg = minimal_round_robin_pool_config
        pool = SyncProxyPool(cfg, [us, fr])
        p = pool.acquire(country="US")
        assert p.country == "US"
        pool.release(p)
        pool.close()

    def test_missing_metadata_skip(self, s0, s1, round_robin_skip_missing_metadata_pool_config):
        bare = Proxy(s0)
        tagged = proxy_with_meta(s1, country="US")
        cfg = round_robin_skip_missing_metadata_pool_config
        pool = SyncProxyPool(cfg, [bare, tagged])
        p = pool.acquire(country="US")
        assert p.url == tagged.url
        pool.release(p)
        pool.close()


class TestCircuitBreaker:
    def test_opens_and_half_open(self, s0, s1, default_pool_config):
        cfg = default_pool_config.model_copy(
            update={
                "circuit_breaker": CircuitBreakerConfig(
                    failure_ratio=0.5,
                    half_open_timeout=0.5,
                    window_seconds=60.0,
                    min_throughput=2,
                )
            }
        )
        p1 = Proxy(s0)
        p2 = Proxy(s1)
        pool = SyncProxyPool(cfg, [p1, p2])
        pool.mark_failed(p1)
        pool.mark_failed(p2)
        with pytest.raises(PoolCircuitOpenError):
            pool.acquire()
        time.sleep(0.6)
        pool.mark_success(p1)
        got = pool.acquire()
        assert got.url in {p1.url, p2.url}
        pool.release(got)
        pool.close()


class TestCooldown:
    def test_custom_cooldown_strategy(self, s0, custom_half_base_cooldown_pool_config):
        cfg = custom_half_base_cooldown_pool_config
        p = Proxy(s0)
        pool = SyncProxyPool(cfg, [p])
        pool.mark_failed(p)
        with pytest.raises(PoolExhausted):
            pool.acquire()
        pool.close()


class TestAsyncPool:
    @pytest.mark.asyncio
    async def test_async_acquire_release(self, proxy_list, default_pool_config):
        async with AsyncProxyPool(default_pool_config, proxy_list) as pool:
            p = await pool.acquire()
            assert isinstance(p, Proxy)
            await pool.release(p)

    @pytest.mark.asyncio
    async def test_async_context_manager(self, proxy_list, default_pool_config):
        async with AsyncProxyPool(default_pool_config, proxy_list) as pool:
            p = await pool.acquire()
            assert p is not None
            await pool.release(p)

    @pytest.mark.asyncio
    async def test_async_mark_failed(self, s0, async_strict_cooldown_pool_config):
        cfg = async_strict_cooldown_pool_config
        p = Proxy(s0)
        async with AsyncProxyPool(cfg, [p]) as pool:
            await pool.mark_failed(p)
            with pytest.raises(PoolExhausted):
                await pool.acquire()