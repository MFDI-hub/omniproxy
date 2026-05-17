"""Focused pool behaviours for the current SyncProxyPool / AsyncProxyPool APIs."""

from __future__ import annotations

import asyncio

import pytest
from omniproxy import Proxy
from omniproxy.config import PoolConfig
from omniproxy.errors import MissingProxyMetadata, PoolCircuitOpenError, PoolExhausted, SessionBrokenError
from omniproxy.pool import AsyncProxyPool, SyncProxyPool

from tests.pool_configs import pool_with_exhausted_hook_comprehensive

class TestCountryAndMetadata:
    def test_country_filter(self, s0: str, s1: str, minimal_round_robin_pool_config: PoolConfig) -> None:
        us = Proxy(s0)
        us._set_attribute("country", "US")
        fr = Proxy(s1)
        fr._set_attribute("country", "FR")
        pool = SyncProxyPool(minimal_round_robin_pool_config, [us, fr])
        got = pool.acquire(country="US")
        assert got.country == "US"
        pool.release(got)
        pool.close()

    def test_filter_missing_raise(
        self, s0: str, comprehensive_filter_missing_raise_pool_config: PoolConfig
    ) -> None:
        bare = Proxy(s0)
        cfg = comprehensive_filter_missing_raise_pool_config
        pool = SyncProxyPool(cfg, [bare])
        with pytest.raises(MissingProxyMetadata):
            pool.acquire(country="US")
        pool.close()


class TestSessionCooldown:
    def test_rebind_returns_same_proxy(
        self, s0: str, s1: str, comprehensive_session_rebind_120_pool_config: PoolConfig
    ) -> None:
        cfg = comprehensive_session_rebind_120_pool_config
        p1, p2 = Proxy(s0), Proxy(s1)
        pool = SyncProxyPool(cfg, [p1, p2])
        a = pool.acquire(session_key="k1")
        pool.release(a)
        b = pool.acquire(session_key="k1")
        assert a.url == b.url
        pool.release(b)
        pool.close()

    def test_raise_on_session_proxy_cooling(
        self, s0: str, comprehensive_session_raise_cooling_pool_config: PoolConfig
    ) -> None:
        cfg = comprehensive_session_raise_cooling_pool_config
        p = Proxy(s0)
        pool = SyncProxyPool(cfg, [p])
        pool.acquire(session_key="s")
        pool.mark_failed(p)
        with pytest.raises(SessionBrokenError):
            pool.acquire(session_key="s")
        pool.close()


class TestCircuitBreaker:
    def test_stops_acquire_when_open(
        self, s0: str, s1: str, comprehensive_circuit_acquire_block_pool_config: PoolConfig
    ) -> None:
        cfg = comprehensive_circuit_acquire_block_pool_config
        a, b = Proxy(s0), Proxy(s1)
        pool = SyncProxyPool(cfg, [a, b])
        pool.mark_failed(a)
        pool.mark_failed(b)
        with pytest.raises(PoolCircuitOpenError):
            pool.acquire()
        pool.close()


class TestHooksExhaustion:
    def test_on_exhausted_called(self, s0: str, ) -> None:
        calls: list[int] = []
        cfg = pool_with_exhausted_hook_comprehensive(lambda: calls.append(1))
        pool = SyncProxyPool(cfg, [Proxy(s0)])
        pool.mark_failed(Proxy(s0))
        with pytest.raises(PoolExhausted):
            pool.acquire()
        assert calls == [1]
        pool.close()


class TestAsyncPoolBasics:
    @pytest.mark.asyncio
    async def test_acquire_release(self, default_pool_config: PoolConfig, s0: str) -> None:
        async with AsyncProxyPool(default_pool_config, [Proxy(s0)]) as pool:
            p = await pool.acquire()
            assert isinstance(p, Proxy)
            await pool.release(p)


class TestConcurrencySmoke:
    def test_sequential_burst_sync_acquire_release(
        self, default_pool_config: PoolConfig, s0: str, s1: str
    ) -> None:
        pool = SyncProxyPool(default_pool_config, [Proxy(s0), Proxy(s1)])
        for _ in range(128):
            p = pool.acquire()
            pool.release(p)
        pool.close()

    @pytest.mark.asyncio
    async def test_parallel_async_acquire_release(
        self, default_pool_config: PoolConfig, s0: str, s1: str
    ) -> None:
        async with AsyncProxyPool(default_pool_config, [Proxy(s0), Proxy(s1)]) as pool:

            async def grab() -> None:
                for _ in range(20):
                    px = await pool.acquire()
                    await pool.release(px)

            await asyncio.gather(*(grab() for _ in range(15)))
