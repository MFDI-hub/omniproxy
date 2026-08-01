"""Focused tests for pool behaviours previously under-covered."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from omniproxy import Proxy
from omniproxy.config import (
    CircuitBreakerConfig,
    CooldownConfig,
    DeadLetterConfig,
    HealthCheckConfig,
    LifecycleHooks,
    LimitsConfig,
    PoolConfig,
    RefreshConfig,
    SessionConfig,
    WarmupConfig,
)
from omniproxy.dead_letter import DeadLetterEntry, maybe_add
from omniproxy.enum import (
    DeadLetterPersistence,
    PoolStrategy,
    SessionCooldownPolicy,
    WarmupFailurePolicy,
)
from omniproxy.errors import (
    PoolCircuitOpenError,
    PoolClosedError,
    PoolDrainingError,
    WarmupFailedError,
)
from omniproxy.extended_proxy import CheckResult
from omniproxy.hooks import run_deferred
from omniproxy.pool import AcquireOptions, AsyncProxyPool, SyncProxyPool
from omniproxy.session import SessionEntry

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


def test_pool_config_rejects_dead_letter_without_health_check() -> None:
    with pytest.raises(ValueError, match="health_check must be provided when dead_letter\\.enabled"):
        PoolConfig(dead_letter=DeadLetterConfig(enabled=True), health_check=None)


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
async def test_warmup_completed_hook_fires_on_raise_timeout(
    s0: str,
    s1: str,
    extended_warmup_timeout_raise_pool_config: PoolConfig,
) -> None:
    """on_warmup_completed must fire even when RAISE timeout fails startup."""
    completed: list[tuple[int, int]] = []
    cfg = extended_warmup_timeout_raise_pool_config.model_copy(
        update={
            "hooks": extended_warmup_timeout_raise_pool_config.hooks.model_copy(
                update={"on_warmup_completed": lambda passed, total: completed.append((passed, total))}
            )
        }
    )
    with pytest.raises(WarmupFailedError):
        async with AsyncProxyPool(cfg, [Proxy(s0), Proxy(s1)]):
            pass
    assert completed == [(0, cfg.warmup.min_ready)]


@pytest.mark.asyncio
async def test_warmup_deadline_cancels_hanging_health_checks(s0: str) -> None:
    """Warmup timeout must bound in-flight probes, not only inter-batch gaps."""
    from omniproxy.warmup import run_warmup

    cfg = PoolConfig(
        health_check=HealthCheckConfig(custom_check=lambda _p: True),
        warmup=WarmupConfig(
            enabled=True,
            min_ready=1,
            timeout=0.15,
            failure_policy=WarmupFailurePolicy.PARTIAL,
        ),
        circuit_breaker=None,
        scoring=None,
    )
    pool = AsyncProxyPool(cfg, [Proxy(s0)])
    started = asyncio.Event()
    record_calls = 0

    async def hang(_proxy, _hc):
        started.set()
        await asyncio.sleep(60)
        return _proxy, CheckResult(True, None, None, 200)

    async def tracking_record(proxy, result):
        nonlocal record_calls
        record_calls += 1
        return await AsyncProxyPool._record_health_check_result(pool, proxy, result)

    pool._record_health_check_result = tracking_record  # type: ignore[method-assign]

    loop = asyncio.get_running_loop()
    t0 = loop.time()
    ok, ready_count = await run_warmup(pool, cfg.warmup, hang)
    elapsed = loop.time() - t0

    assert started.is_set()
    assert ok is False
    assert ready_count == 0
    assert elapsed < 1.0
    # Give a cancelled probe a chance to mis-apply if cancellation were broken.
    await asyncio.sleep(0.05)
    assert record_calls == 0


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
    cfg = pool_configs.extended_quick_close_only_config(drain_timeout=0.0).model_copy(
        update={"refresh": RefreshConfig(sync_callback=list)}
    )
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


@pytest.mark.asyncio
async def test_refresh_loop_survives_unexpected_merge_errors(
    monkeypatch: pytest.MonkeyPatch,
    s0: str,
) -> None:
    cfg = pool_configs.extended_quick_close_only_config(drain_timeout=0.0).model_copy(
        update={
            "refresh": RefreshConfig(
                async_callback=lambda: asyncio.sleep(0, result=[Proxy(s0)]),
                interval_seconds=0.05,
                timeout=1.0,
            )
        }
    )
    calls = {"n": 0}

    async def flaky_merge(self) -> int:  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated merge bug")
        return 0

    monkeypatch.setattr(AsyncProxyPool, "_refresh_and_merge", flaky_merge, raising=True)

    async with AsyncProxyPool(cfg, []) as pool:
        await asyncio.sleep(0.2)
        assert calls["n"] >= 2
        assert not pool._closed


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
async def test_max_size_eviction_feeds_dead_letter_and_hook(s0: str, s1: str) -> None:
    added: list[tuple[str, str | None]] = []
    cfg = PoolConfig(
        health_check=HealthCheckConfig(custom_check=lambda _p: True),
        dead_letter=DeadLetterConfig(enabled=True, retry_interval_seconds=60.0),
        max_size=1,
        circuit_breaker=None,
        scoring=None,
        hooks=LifecycleHooks(
            on_dead_letter_added=lambda p, err: added.append((p.url, err)),
        ),
        drain_timeout=0.0,
    )
    p0 = Proxy(s0)
    p1 = Proxy(s1)
    async with AsyncProxyPool(cfg, [p0]) as pool:
        async with pool._state_lock:
            _, hooks = pool._merge_new_proxies([p1])
        await run_deferred(hooks, cfg.hooks)
        assert len(pool._proxies) == 1
        assert pool._proxies[0].url == p1.url
        assert len(pool._dead_letter_queue) == 1
        assert pool._dead_letter_queue[0].proxy.url == p0.url
        assert pool._dead_letter_queue[0].error == "max_size"
        assert added == [(p0.url, "max_size")]


@pytest.mark.asyncio
async def test_dead_letter_state_store_roundtrip(s0: str, s1: str) -> None:
    store: dict[str, str] = {}

    class MemStore:
        def get(self, key: str) -> str | None:
            return store.get(key)

        def set(self, key: str, value: str, ttl: float | None = None) -> None:
            store[key] = value

        def delete(self, key: str) -> None:
            store.pop(key, None)

    dl = DeadLetterConfig(
        enabled=True,
        persistence=DeadLetterPersistence.STATE_STORE,
        retry_interval_seconds=60.0,
    )
    cfg = PoolConfig(
        health_check=HealthCheckConfig(custom_check=lambda _p: True),
        dead_letter=dl,
        state_store_factory=MemStore,
        circuit_breaker=None,
        scoring=None,
        max_size=1,
        drain_timeout=0.0,
    )
    p0 = Proxy(s0)
    p1 = Proxy(s1)
    async with AsyncProxyPool(cfg, [p0]) as pool:
        async with pool._state_lock:
            _, hooks = pool._merge_new_proxies([p1])
        await run_deferred(hooks, cfg.hooks)
        assert store  # persisted on enqueue

    restored = AsyncProxyPool(cfg, [])
    assert len(restored._dead_letter_queue) == 1
    assert restored._dead_letter_queue[0].proxy.url == p0.url
    await restored.close()


@pytest.mark.asyncio
async def test_dead_letter_retry_worker_puts_proxy_back_in_pool(
    extended_dead_letter_retry_health_ok_pool_config: PoolConfig,
    s0: str,
) -> None:
    cfg = extended_dead_letter_retry_health_ok_pool_config

    corpse = Proxy(s0)
    async with AsyncProxyPool(cfg, []) as pool:
        async with pool._state_lock:
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
async def test_background_tasks_tracked_and_cleared_on_close(s0: str) -> None:
    cfg = PoolConfig(
        health_check=HealthCheckConfig(custom_check=lambda _p: True, check_interval=60.0),
        circuit_breaker=None,
        scoring=None,
        drain_timeout=0.0,
    )
    pool = AsyncProxyPool(cfg, [Proxy(s0)])
    await pool.__aenter__()
    assert pool._bg_health is not None
    assert pool._bg_health in pool._background_tasks
    await pool.close()
    assert pool._closed
    assert pool._bg_health is None
    assert not pool._background_tasks


@pytest.mark.asyncio
async def test_close_during_start_does_not_orphan_background_tasks(s0: str) -> None:
    """Concurrent close mid-warmup must leave the pool fully shut down."""
    entered_warmup = asyncio.Event()
    allow_warmup_finish = asyncio.Event()

    async def slow_warmup(pool, config, health_check_fn):
        entered_warmup.set()
        await allow_warmup_finish.wait()
        return True, 1

    cfg = PoolConfig(
        health_check=HealthCheckConfig(custom_check=lambda _p: True, check_interval=60.0),
        warmup=WarmupConfig(
            enabled=True,
            min_ready=1,
            timeout=5.0,
            failure_policy=WarmupFailurePolicy.RAISE,
        ),
        circuit_breaker=None,
        scoring=None,
        drain_timeout=0.0,
    )
    pool = AsyncProxyPool(cfg, [Proxy(s0)])
    with patch("omniproxy.warmup.run_warmup", side_effect=slow_warmup):
        start_task = asyncio.create_task(pool.__aenter__())
        await asyncio.wait_for(entered_warmup.wait(), timeout=2.0)
        assert pool._bg_health is not None
        assert pool._bg_health in pool._background_tasks
        await pool.close()
        allow_warmup_finish.set()
        with pytest.raises(PoolClosedError):
            await start_task
    assert pool._closed
    assert pool._bg_health is None
    assert pool._bg_dead is None
    assert pool._bg_refresh is None
    assert pool._bg_metrics is None
    assert not pool._background_tasks
    assert pool._ready.is_set()


@pytest.mark.asyncio
async def test_close_during_warmup_unblocks_pending_acquire(s0: str) -> None:
    """Close mid-warmup must wake acquirers blocked on ``_ready``."""
    entered_warmup = asyncio.Event()
    allow_warmup_finish = asyncio.Event()

    async def slow_warmup(pool, config, health_check_fn):
        entered_warmup.set()
        await allow_warmup_finish.wait()
        return True, 1

    cfg = PoolConfig(
        health_check=HealthCheckConfig(custom_check=lambda _p: True, check_interval=60.0),
        warmup=WarmupConfig(
            enabled=True,
            min_ready=1,
            timeout=5.0,
            failure_policy=WarmupFailurePolicy.RAISE,
        ),
        circuit_breaker=None,
        scoring=None,
        drain_timeout=0.0,
    )
    pool = AsyncProxyPool(cfg, [Proxy(s0)])
    with patch("omniproxy.warmup.run_warmup", side_effect=slow_warmup):
        start_task = asyncio.create_task(pool.__aenter__())
        await asyncio.wait_for(entered_warmup.wait(), timeout=2.0)
        acquire_task = asyncio.create_task(pool.acquire())
        await asyncio.sleep(0)
        assert not acquire_task.done()
        await pool.close()
        allow_warmup_finish.set()
        with pytest.raises(PoolClosedError):
            await start_task
        with pytest.raises(PoolClosedError):
            await asyncio.wait_for(acquire_task, timeout=1.0)


@pytest.mark.asyncio
async def test_health_loop_waits_for_ready_before_checking(s0: str) -> None:
    from omniproxy import warmup as warmup_mod

    checks = 0

    def counting_check(_p: Proxy) -> bool:
        nonlocal checks
        checks += 1
        return True

    cfg = PoolConfig(
        health_check=HealthCheckConfig(custom_check=counting_check, check_interval=0.05),
        warmup=WarmupConfig(
            enabled=True,
            min_ready=1,
            timeout=5.0,
            failure_policy=WarmupFailurePolicy.RAISE,
        ),
        circuit_breaker=None,
        scoring=None,
        drain_timeout=0.0,
    )

    real_run_warmup = warmup_mod.run_warmup

    async def gated_warmup(pool, config, health_check_fn):
        # Background health task is already spawned; it must not check yet.
        await asyncio.sleep(0.12)
        assert checks == 0
        return await real_run_warmup(pool, config, health_check_fn)

    with patch.object(warmup_mod, "run_warmup", side_effect=gated_warmup):
        async with AsyncProxyPool(cfg, [Proxy(s0)]) as pool:
            assert pool._ready.is_set()
            await asyncio.sleep(0.02)
    assert checks >= 1


@pytest.mark.asyncio
async def test_health_loop_survives_apply_failures(s0: str) -> None:
    """A raising _apply_check_result must not kill the background health task."""
    checks = 0

    def counting_check(_p: Proxy) -> bool:
        nonlocal checks
        checks += 1
        return True

    cfg = PoolConfig(
        health_check=HealthCheckConfig(custom_check=counting_check, check_interval=0.05),
        circuit_breaker=None,
        scoring=None,
        drain_timeout=0.0,
    )

    async with AsyncProxyPool(cfg, [Proxy(s0)]) as pool:
        assert pool._bg_health is not None
        apply_calls = 0

        def boom(proxy, result, deferred):  # type: ignore[no-untyped-def]
            nonlocal apply_calls
            apply_calls += 1
            raise RuntimeError("apply exploded")

        with patch.object(pool, "_apply_check_result", side_effect=boom):
            deadline = time.monotonic() + 2.0
            while apply_calls < 2 and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            assert apply_calls >= 2
            assert pool._bg_health is not None
            assert not pool._bg_health.done()
            assert checks >= 2


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
    rotation_response = MagicMock()
    rotation_response.status_code = 200
    mock_backend.arequest_direct = AsyncMock(return_value=rotation_response)

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


@pytest.mark.asyncio
async def test_statistics_failed_excludes_health_checks_and_is_frozen(s0: str) -> None:
    from dataclasses import FrozenInstanceError

    cfg = PoolConfig(
        acquire_timeout=1.0,
        cooldown=CooldownConfig(failure_threshold=100),
    )
    proxy = Proxy(s0)
    async with AsyncProxyPool(cfg, [proxy]) as pool:
        assert pool.statistics.failed == 0

        applied = await pool._record_health_check_result(
            proxy,
            CheckResult(success=False, latency=0.01, exc_type=TimeoutError, status_code=None),
        )
        assert applied is True
        assert pool.statistics.failed == 0

        px = await pool.acquire()
        await pool.mark_failed(px, TimeoutError)
        assert pool.statistics.failed == 1
        assert pool.statistics.served == 1

        snap = pool.statistics
        with pytest.raises(FrozenInstanceError):
            snap.failed = 99  # type: ignore[misc]
        assert pool.statistics.failed == 1


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
async def test_mark_failed_invalid_exc_still_fires_hooks(s0: str) -> None:
    """Non-exception ``exc`` must not abort mark_failed before hooks run."""
    from omniproxy.cooldown import coerce_exception_type, compute_cooldown

    assert coerce_exception_type(TimeoutError) is TimeoutError
    assert coerce_exception_type(TimeoutError("boom")) is TimeoutError
    assert coerce_exception_type(42) is None
    assert coerce_exception_type(int) is None

    # Defense in depth: compute_cooldown must not raise on bad inputs.
    assert (
        compute_cooldown(1.0, False, 1, {TimeoutError: 5.0}, 42, _min=0.1, _max=10.0)  # type: ignore[arg-type]
        == 1.0
    )

    failed: list[object] = []
    hooks = LifecycleHooks(on_proxy_failed=lambda p, e: failed.append((p.url, e)))
    cfg = PoolConfig(
        hooks=hooks,
        cooldown=CooldownConfig(
            base=1.0,
            min=0.1,
            max=10.0,
            adaptive=False,
            failure_threshold=1,
            penalties={TimeoutError: 5.0},
        ),
        health_check=None,
        circuit_breaker=None,
        scoring=None,
        acquire_timeout=5.0,
    )

    async with AsyncProxyPool(cfg, [Proxy(s0)]) as pool:
        px = await pool.acquire()
        await pool.mark_failed(px, 42)  # type: ignore[arg-type]
        assert failed == [(px.url, None)]
        # Cooldown still applied (threshold met); invalid exc just skips penalty.
        assert px.url in pool._cooldown_until


@pytest.mark.asyncio
async def test_mark_failed_rotation_error_still_fires_hooks() -> None:
    """arotate() raising after failure accounting must not drop deferred hooks."""
    failed: list[str] = []
    cooled: list[str] = []

    hooks = LifecycleHooks(
        on_proxy_failed=lambda p, _exc: failed.append(p.url),
        on_proxy_cooled_down=lambda p: cooled.append(p.url),
    )
    p = Proxy("login:pass@203.0.113.56:8899[https://rotate.invalid/flip]")
    assert p.rotation_url is not None
    cfg = PoolConfig(
        rotate_on_failure=True,
        hooks=hooks,
        cooldown=CooldownConfig(base=1.0, min=0.1, max=10.0, adaptive=False, failure_threshold=1),
        health_check=None,
        circuit_breaker=None,
        scoring=None,
        acquire_timeout=5.0,
    )

    async with AsyncProxyPool(cfg, [p]) as pool:
        px = await pool.acquire()
        with (
            patch.object(Proxy, "arotate", new_callable=AsyncMock, side_effect=RuntimeError("rotate boom")),
            pytest.raises(RuntimeError, match="rotate boom"),
        ):
            await pool.mark_failed(px, TimeoutError)
        assert failed == [p.url]
        assert cooled == [p.url]


@pytest.mark.asyncio
async def test_mark_failed_non_probe_preserves_half_open_markers(s0: str, s1: str) -> None:
    """Failing a non-probe proxy must not wipe an in-flight HALF_OPEN probe."""
    from omniproxy.enum import CircuitBreakerState

    probe = Proxy(s0)
    other = Proxy(s1)
    cfg = PoolConfig(
        circuit_breaker=CircuitBreakerConfig(
            window_seconds=60.0,
            failure_ratio=0.5,
            half_open_timeout=30.0,
            min_throughput=2,
        ),
        health_check=None,
        scoring=None,
        acquire_timeout=5.0,
    )

    async with AsyncProxyPool(cfg, [probe, other]) as pool:
        assert pool._circuit_breaker is not None
        async with pool._state_lock:
            pool._circuit_breaker.state = CircuitBreakerState.HALF_OPEN
            pool._circuit_breaker._begin_probe(time.monotonic())
            pool._connections[probe.url] = 1
            pool._connections[other.url] = 1
            pool._half_open_probe_epoch = pool._circuit_breaker.active_probe_epoch
            pool._half_open_probe_url = probe.url
            pool._half_open_probe_proxy = probe

        epoch_before = pool._half_open_probe_epoch
        await pool.mark_failed(other)

        assert pool._half_open_probe_proxy is probe
        assert pool._half_open_probe_url == probe.url
        assert pool._half_open_probe_epoch == epoch_before
        assert pool._circuit_breaker.state == CircuitBreakerState.HALF_OPEN
        assert pool._circuit_breaker._probe_in_flight is True
        assert pool._connections.get(other.url, 0) == 0
        assert pool._connections.get(probe.url, 0) == 1


@pytest.mark.asyncio
async def test_mark_success_non_probe_preserves_half_open_markers(s0: str, s1: str) -> None:
    """Succeeding a non-probe proxy must not complete or clear the HALF_OPEN probe."""
    from omniproxy.enum import CircuitBreakerState

    probe = Proxy(s0)
    other = Proxy(s1)
    cfg = PoolConfig(
        circuit_breaker=CircuitBreakerConfig(
            window_seconds=60.0,
            failure_ratio=0.5,
            half_open_timeout=30.0,
            min_throughput=2,
        ),
        health_check=None,
        scoring=None,
        acquire_timeout=5.0,
    )

    async with AsyncProxyPool(cfg, [probe, other]) as pool:
        assert pool._circuit_breaker is not None
        async with pool._state_lock:
            pool._circuit_breaker.state = CircuitBreakerState.HALF_OPEN
            pool._circuit_breaker._begin_probe(time.monotonic())
            pool._connections[probe.url] = 1
            pool._connections[other.url] = 1
            pool._half_open_probe_epoch = pool._circuit_breaker.active_probe_epoch
            pool._half_open_probe_url = probe.url
            pool._half_open_probe_proxy = probe

        epoch_before = pool._half_open_probe_epoch
        await pool.mark_success(other)

        assert pool._half_open_probe_proxy is probe
        assert pool._half_open_probe_epoch == epoch_before
        assert pool._circuit_breaker.state == CircuitBreakerState.HALF_OPEN
        assert pool._circuit_breaker._probe_in_flight is True


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
async def test_acquire_circuit_retry_does_not_replay_stale_open_hook(s0: str) -> None:
    """Circuit-open wait retries must not carry hook buffers into a later success."""
    circuit_hooks: list[str] = []
    acquired_hooks: list[str] = []

    hooks = LifecycleHooks(
        on_circuit_open=lambda: circuit_hooks.append("open"),
        on_proxy_acquired=lambda p: acquired_hooks.append(p.url),
    )
    cfg = PoolConfig(acquire_timeout=1.0, hooks=hooks)

    async with AsyncProxyPool(cfg, [Proxy(s0)]) as pool:
        check_calls = 0
        append_calls = 0

        def fake_check() -> None:
            nonlocal check_calls
            check_calls += 1
            if check_calls == 1:
                raise PoolCircuitOpenError("Circuit breaker open")

        def fake_append(deferred: list[tuple[str, tuple]]) -> None:
            nonlocal append_calls
            append_calls += 1
            if append_calls == 1:
                deferred.append(("on_circuit_open", ()))

        with (
            patch.object(pool, "_check_availability", side_effect=fake_check),
            patch.object(pool, "_append_circuit_hooks", side_effect=fake_append),
            patch.object(pool, "_wait_for_availability", new_callable=AsyncMock),
        ):
            px = await pool.acquire()

    assert check_calls >= 2
    assert append_calls >= 2
    assert acquired_hooks == [px.url]
    assert circuit_hooks == []


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


@pytest.mark.asyncio
async def test_rotate_on_acquire_failure_releases_lease(
    extended_rotate_on_acquire_pool_config: PoolConfig,
) -> None:
    """arotate() raising after _mark_acquired must not leak the connection lease."""
    p = Proxy("login:pass@203.0.113.56:8899[https://rotate.invalid/flip]")
    assert p.rotation_url is not None
    cfg = extended_rotate_on_acquire_pool_config.model_copy(
        update={"limits": LimitsConfig(max_connections_per_proxy=1)},
    )

    async with AsyncProxyPool(cfg, [p]) as pool:
        with (
            patch.object(Proxy, "arotate", new_callable=AsyncMock, side_effect=RuntimeError("rotate boom")),
            pytest.raises(RuntimeError, match="rotate boom"),
        ):
            await pool.acquire()
        assert pool._connections.get(p.url, 0) == 0

        with patch.object(Proxy, "arotate", new_callable=AsyncMock, return_value=True):
            px = await pool.acquire()
            assert px.url == p.url
            await pool.release(px)


@pytest.mark.asyncio
async def test_on_proxy_acquired_hook_failure_still_returns_proxy(
    s0: str,
) -> None:
    """Hook errors are isolated; the lease stays with the caller until release."""

    def boom(_proxy: Proxy) -> None:
        raise RuntimeError("hook boom")

    hooks = LifecycleHooks(on_proxy_acquired=boom)
    cfg = pool_configs.extended_lifecycle_hooks_config(hooks)

    async with AsyncProxyPool(cfg, [Proxy(s0)]) as pool:
        px = await pool.acquire()
        assert pool._connections.get(px.url, 0) == 1
        await pool.release(px)
        assert pool._connections.get(px.url, 0) == 0


@pytest.mark.asyncio
async def test_post_acquire_unexpected_failure_releases_lease(s0: str) -> None:
    """Any unexpected failure after the lease is taken must roll the lease back."""
    cfg = pool_configs.extended_quick_close_only_config(drain_timeout=0.0)

    async with AsyncProxyPool(cfg, [Proxy(s0)]) as pool:
        with (
            patch("omniproxy.pool.run_deferred", side_effect=RuntimeError("unexpected")),
            pytest.raises(RuntimeError, match="unexpected"),
        ):
            await pool.acquire()
        assert pool._connections.get(pool._proxies[0].url, 0) == 0


@pytest.mark.asyncio
async def test_rebind_clears_pending_when_no_eligible_replacement(s0: str, s1: str) -> None:
    """REBIND must not leave a stale pending rebind when no alternative is eligible."""
    cfg = PoolConfig(
        session=SessionConfig(ttl=60.0, cooldown_policy=SessionCooldownPolicy.REBIND),
        cooldown=CooldownConfig(base=10.0, min=0.1, max=3600.0, adaptive=False, failure_threshold=1),
        limits=LimitsConfig(max_connections_per_proxy=1),
        acquire_timeout=0.0,
        health_check=None,
        circuit_breaker=None,
        scoring=None,
    )
    p1, p2 = Proxy(s0), Proxy(s1)

    async with AsyncProxyPool(cfg, [p1, p2]) as pool:
        now = time.monotonic()
        async with pool._state_lock:
            pool._session_registry["sess"] = SessionEntry(proxy_id=p1.url, expires_at=now + 60.0)
            pool._cooldown_until[p1.url] = now + 100.0
            pool._cooldown_until[p2.url] = now + 100.0
            opts = AcquireOptions.from_kwargs(pool._config, session_key="sess")
            assert pool._get_eligible(opts) == []
            assert "sess" not in pool._pending_session_rebind
            assert "sess" in pool._session_registry

            del pool._cooldown_until[p2.url]
            eligible = pool._get_eligible(opts)
            assert [px.url for px in eligible] == [p2.url]
            assert pool._pending_session_rebind["sess"].url == p1.url
            assert "sess" not in pool._session_registry

