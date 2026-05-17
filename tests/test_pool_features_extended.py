"""Focused tests for pool behaviours previously under-covered."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest

from omniproxy import Proxy
from omniproxy.config import DeadLetterConfig, LifecycleHooks, PoolConfig, WarmupConfig
from omniproxy.dead_letter import DeadLetterEntry, maybe_add
from omniproxy.enum import PoolStrategy, SessionCooldownPolicy
from omniproxy.errors import PoolClosedError, PoolDrainingError, WarmupFailedError
from omniproxy.extended_proxy import CheckResult
from omniproxy.hooks import run_deferred
from omniproxy.pool import AsyncProxyPool, SyncProxyPool

from tests import pool_configs
from tests.conftest import proxy_with_meta


def test_random_strategy_respects_rng_choice(
    s0: str,
    s1: str,
    extended_random_strategy_pool_config: PoolConfig,
) -> None:
    cfg = extended_random_strategy_pool_config
    p_lo = Proxy(s0)
    p_hi = Proxy(s1)

    def pick_second(seq):  # type: ignore[no-untyped-def]
        return seq[1]

    with patch("omniproxy.strategies.random.choice", side_effect=pick_second):
        pool = SyncProxyPool(cfg, [p_lo, p_hi])
        try:
            for _ in range(4):
                p = pool.acquire()
                assert p.url == p_hi.url
                pool.release(p)
        finally:
            pool.close()


def test_pool_config_rejects_warmup_without_health_check() -> None:
    with pytest.raises(ValueError, match="health_check must be provided when warmup\\.enabled"):
        PoolConfig(warmup=WarmupConfig(enabled=True, min_ready=1), health_check=None)


@pytest.mark.asyncio
async def test_warmup_succeeds_when_min_ready_met(
    s0: str,
    extended_warmup_min_ready_ok_pool_config: PoolConfig,
) -> None:
    cfg = extended_warmup_min_ready_ok_pool_config
    p = Proxy(s0)
    async with AsyncProxyPool(cfg, [p]) as pool:
        gotten = await pool.acquire()
        assert gotten.url == p.url
        await pool.release(gotten)


@pytest.mark.asyncio
async def test_warmup_raises_when_deadline_unreachable(
    s0: str,
    s1: str,
    extended_warmup_timeout_raise_pool_config: PoolConfig,
) -> None:
    with pytest.raises(WarmupFailedError):
        async with AsyncProxyPool(extended_warmup_timeout_raise_pool_config, [Proxy(s0), Proxy(s1)]):
            pass


@pytest.mark.asyncio
async def test_warmup_partial_allows_startup(
    s0: str,
    s1: str,
    extended_warmup_partial_unmet_pool_config: PoolConfig,
) -> None:
    async with AsyncProxyPool(extended_warmup_partial_unmet_pool_config, [Proxy(s0), Proxy(s1)]):
        await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_on_demand_async_refresh_when_exhausted(
    s0: str,
    s2: str,
    extended_refresh_async_from_seed_pool_config: PoolConfig,
) -> None:
    newcomer = Proxy(s2)
    starter = Proxy(s0)
    async with AsyncProxyPool(extended_refresh_async_from_seed_pool_config, [starter]) as pool:
        first = await pool.acquire()
        await pool.mark_failed(first)
        acquired = await pool.acquire()
        assert acquired.url == newcomer.url
        await pool.release(acquired)


@pytest.mark.asyncio
async def test_on_demand_sync_refresh_callback_via_thread(
    s1: str,
    extended_refresh_sync_from_seed_pool_config: PoolConfig,
) -> None:
    async with AsyncProxyPool(extended_refresh_sync_from_seed_pool_config, []) as pool:
        p = await pool.acquire()
        assert p.url == Proxy(s1).url
        await pool.release(p)


class _FifoFetcher:
    """Returns queued URL batches similar to fetchers.ProxyFetcher implementations."""

    def __init__(self, payloads: list[list[str]]) -> None:
        self._chunks = payloads[:]

    async def fetch(self) -> list[str]:
        return list(self._chunks.pop(0))


@pytest.mark.asyncio
async def test_on_demand_fetchers_refill_when_empty_and_no_refresh_callbacks(s0: str) -> None:
    cfg = pool_configs.extended_quick_close_acquire_zero_config()
    fetch = _FifoFetcher([[s0]])
    async with AsyncProxyPool(cfg, [], fetchers=[fetch]) as pool:
        p = await pool.acquire()
        assert p.url == Proxy(s0).url
        await pool.release(p)


@pytest.mark.asyncio
async def test_background_refresh_merges_new_once(monkeypatch: pytest.MonkeyPatch, s0: str, s3: str) -> None:
    cfg = pool_configs.extended_quick_close_only_config(drain_timeout=0.0)
    starter = Proxy(s0)
    newcomer = Proxy(s3)

    async def patched_fetch(self):  # type: ignore[no-untyped-def]
        return [newcomer]

    async def burst_refresh(self):  # type: ignore[no-untyped-def]
        await asyncio.sleep(0.06)
        if self._closed:
            return
        batch = await AsyncProxyPool._fetch_new_proxies(self)
        if batch:
            async with self._state_lock:
                self._merge_new_proxies(batch)

    monkeypatch.setattr(AsyncProxyPool, "_fetch_new_proxies", patched_fetch, raising=True)
    monkeypatch.setattr(AsyncProxyPool, "_refresh_loop", burst_refresh, raising=True)

    async with AsyncProxyPool(cfg, [starter]) as pool:
        await asyncio.sleep(0.12)
        urls = [p.url for p in pool._proxies]
        assert newcomer.url in urls


def test_maybe_add_respects_dead_letter_max_size(s0: str, s1: str, s2: str) -> None:
    dl = DeadLetterConfig(enabled=True, max_size=2)
    q: list[DeadLetterEntry] = []
    e0 = DeadLetterEntry(proxy=Proxy(s0), error=None, timestamp=1.0)
    e1 = DeadLetterEntry(proxy=Proxy(s1), error=None, timestamp=2.0)
    e2 = DeadLetterEntry(proxy=Proxy(s2), error=None, timestamp=3.0)
    maybe_add(e0, dl, q)
    maybe_add(e1, dl, q)
    maybe_add(e2, dl, q)
    assert len(q) == 2
    assert q[0].proxy.url == Proxy(s1).url
    assert q[1].proxy.url == Proxy(s2).url


@pytest.mark.asyncio
async def test_dead_letter_retry_worker_puts_proxy_back_in_pool(
    extended_dead_letter_retry_health_ok_pool_config: PoolConfig,
    s0: str,
) -> None:
    cfg = extended_dead_letter_retry_health_ok_pool_config

    corpse = Proxy(s0)
    async with AsyncProxyPool(cfg, []) as pool:
        maybe_add(
            DeadLetterEntry(proxy=corpse, error="unit", timestamp=time.time()),
            cfg.dead_letter,
            pool._dead_letter_queue,
        )
        await asyncio.sleep(0.2)
        got = await pool.acquire()
        assert got.url == corpse.url
        await pool.release(got)


@pytest.mark.asyncio
async def test_connection_cap_waits_then_second_acquire_succeeds(
    s0: str,
    extended_single_proxy_connection_wait_pool_config: PoolConfig,
) -> None:
    cfg = extended_single_proxy_connection_wait_pool_config
    p = Proxy(s0)

    async def seq(pool: AsyncProxyPool) -> None:
        px = await pool.acquire()
        await asyncio.sleep(0.12)
        await pool.release(px)

    async with AsyncProxyPool(cfg, [p]) as pool_inner:
        t1 = asyncio.create_task(seq(pool_inner))
        await asyncio.sleep(0.04)
        p2 = await pool_inner.acquire()
        assert p2.url == p.url
        await pool_inner.release(p2)
        await t1


@pytest.mark.asyncio
async def test_acquire_after_explicit_close_raises(s0: str) -> None:
    cfg = pool_configs.extended_quick_close_acquire_zero_config()
    pool = AsyncProxyPool(cfg, [Proxy(s0)])
    await pool.__aenter__()
    await pool.close()
    with pytest.raises(PoolClosedError):
        await pool.acquire()


@pytest.mark.asyncio
async def test_acquire_under_draining_event_raises(s0: str) -> None:
    cfg = pool_configs.extended_quick_close_only_config(drain_timeout=0.0)
    pool = AsyncProxyPool(cfg, [Proxy(s0)])
    await pool.__aenter__()
    pool._draining.set()
    with pytest.raises(PoolDrainingError):
        await pool.acquire()
    pool._draining.clear()
    await pool.close()


def test_sync_proxy_pool_close_is_idempotent(extended_quick_close_sync_pool_config: PoolConfig, s0: str) -> None:
    pool = SyncProxyPool(extended_quick_close_sync_pool_config, [Proxy(s0)])
    pool.close()
    pool.close()


@pytest.mark.asyncio
async def test_rotate_on_acquire_triggers_rotation_request(
    extended_rotate_on_acquire_pool_config: PoolConfig,
    mock_backend: object,
) -> None:
    p = Proxy(
        "login:pass@203.0.113.56:8899[https://rotate.invalid/flip]",
    )
    assert p.rotation_url is not None
    mock_backend.arequest_direct = AsyncMock(return_value=True)

    async with AsyncProxyPool(extended_rotate_on_acquire_pool_config, [p]) as pool:
        px = await pool.acquire()
        assert mock_backend.arequest_direct.await_count >= 1
        await pool.release(px)


@pytest.mark.asyncio
async def test_metrics_enqueue_reaches_exporter(
    recording_metrics_exporter: object,
    extended_metrics_exporter_pool_config: PoolConfig,
    s0: str,
) -> None:
    async with AsyncProxyPool(extended_metrics_exporter_pool_config, [Proxy(s0)]) as pool:
        pool._enqueue_metric("tests.pool.suite", 1.0, {"case": "unit"})
        await asyncio.sleep(0.12)

    gx = getattr(recording_metrics_exporter, "gauges", [])
    assert any(name == "tests.pool.suite" for name, _v, _t in gx)


def test_stealth_preset_shape_matches_docs() -> None:
    stealth = PoolConfig.stealth_preset()
    assert stealth.strategy == PoolStrategy.LOWEST_LATENCY
    assert stealth.rotate_on_acquire is True
    assert stealth.health_check is not None
    assert stealth.session.cooldown_policy == SessionCooldownPolicy.BLOCK


def test_rotating_residential_preset_enables_rotation_flags() -> None:
    rr = PoolConfig.rotating_residential_preset()
    assert rr.strategy == PoolStrategy.RANDOM
    assert rr.use_rotation_urls is True and rr.rotate_on_acquire is True


def test_load_balancer_preset_has_no_scoring_or_breaker() -> None:
    lb = PoolConfig.load_balancer_preset()
    assert lb.scoring is None and lb.circuit_breaker is None and lb.health_check is not None


def test_acquire_tags_skip_non_matching(
    s0: str,
    s1: str,
    minimal_round_robin_pool_config: PoolConfig,
) -> None:
    a = proxy_with_meta(s0, tags=["tier-a"])
    b = proxy_with_meta(s1, tags=["tier-b"])
    pool = SyncProxyPool(minimal_round_robin_pool_config, [a, b])
    try:
        p = pool.acquire(tags={"tier-b"})
        assert p.url == b.url
        pool.release(p)
    finally:
        pool.close()


def test_acquire_accept_callback_can_veto(
    s0: str,
    s1: str,
    minimal_round_robin_pool_config: PoolConfig,
) -> None:
    banned = Proxy(s0)
    allowed = Proxy(s1)
    pool = SyncProxyPool(minimal_round_robin_pool_config, [banned, allowed])
    try:
        cb = lambda pr: pr.url != banned.url  # noqa: E731
        p = pool.acquire(accept_callback=cb)
        assert p.url == allowed.url
        pool.release(p)
    finally:
        pool.close()


@pytest.mark.asyncio
async def test_lifecycle_hooks_acquire_release_and_fail(
    extended_hooks_tracking: dict[str, list[object]],
    extended_lifecycle_hooks_pool_config: PoolConfig,
    s0: str,
    s1: str,
) -> None:
    t = extended_hooks_tracking

    async with AsyncProxyPool(extended_lifecycle_hooks_pool_config, [Proxy(s0), Proxy(s1)]) as pool:
        px = await pool.acquire()
        assert t["acquired"] == [px.url]
        await pool.release(px)
        assert px.url in t["released"]

        py = await pool.acquire()
        await pool.mark_failed(py)
        assert len(t["failed"]) == 1
        pair = t["failed"][0]
        assert pair[0] == py.url


@pytest.mark.asyncio
async def test_on_proxy_recovered_hook_via_apply_check(s0: str) -> None:
    recovered: list[str] = []
    hooks = LifecycleHooks(on_proxy_recovered=lambda p: recovered.append(p.url))
    cfg = pool_configs.extended_quick_close_only_config(drain_timeout=0.0).model_copy(
        update={"hooks": hooks},
    )

    async with AsyncProxyPool(cfg, [Proxy(s0)]) as pool:
        probe = pool._proxies[0]
        want_url = probe.url
        deferred: list[tuple[str, tuple]] = []
        async with pool._state_lock:
            pool._apply_check_result(
                probe,
                CheckResult(success=True, latency=0.01, exc_type=None, status_code=200),
                deferred,
            )
        await run_deferred(deferred, cfg.hooks)

    assert recovered == [want_url]

