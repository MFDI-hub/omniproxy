"""Pool behaviour tests using :func:`~tests.proxy_seeds.seeds`.

Strings come from the ``PROXIES`` JSON list in the environment (loaded via ``conftest``).
Assertions are based on :class:`~omniproxy.extended_proxy.Proxy` normalization (``url``,
``port``, ``_key`` / membership) and pool state, not hard-coded hosts. At least four
``PROXIES`` entries are required when using a real list.
"""

from __future__ import annotations

import asyncio
import gc
import threading
import time
import unittest.mock as mock
import warnings
import weakref
from collections import deque
from typing import Any

import pytest
from omniproxy.config import HealthCheckConfig, PoolConfig
from omniproxy.errors import (
    MissingProxyMetadata,
    NoMatchingProxy,
    PoolClosedError,
    PoolExhausted,
    PoolSaturated,
)
from omniproxy.extended_proxy import CheckResult, Proxy, apply_check_result_metadata
from omniproxy.pool import (
    AsyncPoolProtocol,
    AsyncProxyPool,
    BasePoolProtocol,
    HealthMonitor,
    MonitorablePoolProtocol,
    ProxyPool,
    SyncPoolProtocol,
    SyncProxyPool,
)

from tests.proxy_seeds import seeds

# Four distinct endpoints: rotation / filters / refresh callbacks use indices 0-3.
S0, S1, S2, S3 = seeds(4)


def _p(url: str, **meta: Any) -> Proxy:
    pr = Proxy(url)
    if meta:
        apply_check_result_metadata(pr, latency=None, anonymity=meta.get("anonymity"))
        for k, v in meta.items():
            if k != "anonymity" and v is not None:
                pr._set_attribute(k, v)  # type: ignore[attr-defined]
    return pr


# ---------------------------------------------------------------------------
# BaseProxyPool / shared
# ---------------------------------------------------------------------------


class TestBaseShared:
    def test_len_active_count(self) -> None:
        pool = SyncProxyPool([S0, S1])
        assert len(pool) == 2
        pool.mark_failed(S0)
        assert len(pool) == 1

    def test_iter_snapshot(self) -> None:
        pool = SyncProxyPool([S0, S1])
        urls = {p.url for p in pool}
        assert len(urls) == 2
        pool.mark_failed(S0)
        assert len({p.url for p in pool}) == 1

    def test_contains_url_key(self) -> None:
        pool = SyncProxyPool([S0])
        assert S0 in pool
        assert Proxy(S0) in pool
        assert "http://9.9.9.9:9999" not in pool

    def test_repr_fields(self) -> None:
        pool = SyncProxyPool([S0], PoolConfig(strategy="random", structure="list", cooldown=30.0))
        r = repr(pool)
        assert "SyncProxyPool" in r
        assert "active=" in r
        assert "cooling=" in r
        assert "random" in r
        assert "list" in r
        assert "30.0" in r or "30" in r

    def test_proxies_property_purges_expired_cooldown(self) -> None:
        pool = SyncProxyPool([S0], PoolConfig(cooldown=0.01, failure_threshold=1))
        pool.mark_failed(S0)
        assert len(pool.proxies) == 0
        t0 = 1_000_000.0
        with mock.patch("time.monotonic", return_value=t0):
            pool.mark_failed(S0)
        with mock.patch("time.monotonic", return_value=t0 + 1.0):
            snap = pool.proxies
        assert len(snap) >= 1

    def test_cooling_proxies_only_cooling(self) -> None:
        pool = SyncProxyPool([S0, S1], PoolConfig(failure_threshold=1))
        pool.mark_failed(S0)
        cooling = pool.cooling_proxies
        assert len(cooling) == 1
        assert any(p.url == Proxy(S0).url for p in cooling)
        assert len(pool.cooling_proxies) == 1

    def test_reset_pool_restores(self) -> None:
        pool = SyncProxyPool([S0, S1], PoolConfig(failure_threshold=1))
        pool.mark_failed(S0)
        assert len(pool) == 1
        pool.reset_pool()
        assert len(pool) == 2
        with pool._lock:
            assert pool._state._failure_counts == {}
            assert pool._state._index == 0

    def test_mark_failed_cools_at_threshold(self) -> None:
        pool = SyncProxyPool([S0], PoolConfig(failure_threshold=2))
        pool.mark_failed(S0)
        assert len(pool) == 1
        pool.mark_failed(S0)
        assert len(pool) == 0
        assert len(pool.cooling_proxies) == 1

    def test_mark_failed_below_threshold_stays_active(self) -> None:
        pool = SyncProxyPool([S0], PoolConfig(failure_threshold=5))
        pool.mark_failed(S0)
        assert len(pool) == 1
        with pool._lock:
            assert pool._state._failure_counts[pool._key(Proxy(S0))] == 1

    def test_mark_failed_penalty_multiplier(self) -> None:
        pool = SyncProxyPool(
            [S0],
            PoolConfig(cooldown=10.0, failure_threshold=1, failure_penalties={ValueError: 3.0}),
        )
        t0 = 2_000_000.0
        with mock.patch("time.monotonic", return_value=t0):
            pool.mark_failed(S0, ValueError)
        k = pool._key(Proxy(S0))
        with pool._lock:
            until = pool._state._cooldown_until[k]
        assert until >= t0 + 29.0  # 10 * 3

    def test_mark_success_clears_failures(self) -> None:
        pool = SyncProxyPool([S0], PoolConfig(failure_threshold=5))
        pool.mark_failed(S0)
        pool.mark_success(S0)
        with pool._lock:
            assert pool._state._failure_counts.get(pool._key(Proxy(S0)), 0) == 0

    def test_on_proxy_recovered_only_after_prior_failures(self) -> None:
        recovered: list[Proxy] = []
        pool = SyncProxyPool(
            [S0],
            PoolConfig(failure_threshold=5, on_proxy_recovered=lambda p: recovered.append(p)),
        )
        pool.mark_success(S0)
        assert recovered == []
        pool.mark_failed(S0)
        pool.mark_success(S0)
        assert len(recovered) == 1

    def test_callbacks_fire(self) -> None:
        log: list[str] = []
        p1 = Proxy(S0)
        cfg = PoolConfig(
            failure_threshold=1,
            on_proxy_failed=lambda p, et: log.append("failed"),
            on_proxy_cooled_down=lambda p: log.append("cooled"),
            on_proxy_acquired=lambda p: log.append("acq"),
            on_proxy_released=lambda p: log.append("rel"),
        )
        pool = SyncProxyPool([p1], cfg)
        with pool:
            pass
        assert "acq" in log and "rel" in log
        log.clear()
        pool.mark_failed(p1)
        assert "failed" in log and "cooled" in log

    def test_cooldown_reentry_after_elapse(self) -> None:
        pool = SyncProxyPool([S0], PoolConfig(cooldown=0.05, failure_threshold=1))
        t0 = 5_000_000.0
        with mock.patch("time.monotonic", return_value=t0):
            pool.mark_failed(S0)
        assert len(pool) == 0
        with mock.patch("time.monotonic", return_value=t0 + 1.0):
            assert len(pool) == 1

    def test_failure_threshold_survives_n_minus_one(self) -> None:
        pool = SyncProxyPool([S0], PoolConfig(failure_threshold=4))
        for _ in range(3):
            pool.mark_failed(S0)
        assert len(pool) == 1
        pool.mark_failed(S0)
        assert len(pool) == 0


# ---------------------------------------------------------------------------
# SyncProxyPool
# ---------------------------------------------------------------------------


class TestSyncPool:
    def test_get_next_returns_proxy(self) -> None:
        pool = SyncProxyPool([S0])
        assert pool.get_next().url

    def test_get_next_closed_raises(self) -> None:
        pool = SyncProxyPool([S0])
        pool.close()
        with pytest.raises(PoolClosedError):
            pool.get_next()

    def test_get_next_exhausted_all_cooling(self) -> None:
        pool = SyncProxyPool([S0], PoolConfig(failure_threshold=1))
        pool.mark_failed(S0)
        with pytest.raises(PoolExhausted):
            pool.get_next()

    def test_get_next_no_matching(self) -> None:
        a = _p(S0, country="US")
        b = _p(S1, country="FR")
        pool = SyncProxyPool([a, b])
        with pytest.raises(NoMatchingProxy):
            pool.get_next(country="DE")

    def test_get_next_refresh_on_exhausted(self) -> None:
        calls: list[int] = []

        def refresh() -> list[str]:
            calls.append(1)
            return [S3]

        pool = SyncProxyPool([], PoolConfig(refresh_callback=refresh))
        p = pool.get_next()
        assert calls == [1]
        assert p.url == Proxy(S3).url

    def test_get_next_refresh_timeout(self) -> None:
        pool = SyncProxyPool([S0], PoolConfig(refresh_timeout=0.01))
        pool._refresh_event_sync.clear()
        with pytest.raises(PoolExhausted, match="Refresh timed out"):
            pool.get_next()

    def test_context_acquire_release(self) -> None:
        pool = SyncProxyPool([S0])
        with pool as p:
            assert p.url
        # released: second with should work
        with pool as p2:
            assert p2.url

    def test_exit_auto_mark_failed(self) -> None:
        cfg = PoolConfig(auto_mark_failed_on_exception=True, failure_threshold=99)
        pool = SyncProxyPool([S0], cfg)
        with pytest.raises(RuntimeError), pool:
            raise RuntimeError("x")
        with pool._lock:
            assert pool._state._failure_counts.get(pool._key(Proxy(S0)), 0) >= 1

    def test_exit_auto_mark_success(self) -> None:
        cfg = PoolConfig(auto_mark_success_on_exit=True, failure_threshold=99)
        pool = SyncProxyPool([S0], cfg)
        pool.mark_failed(S0)
        with pool:
            pass
        with pool._lock:
            assert pool._state._failure_counts.get(pool._key(Proxy(S0)), 0) == 0

    def test_reraise_false_suppresses_exit_return(self) -> None:
        cfg = PoolConfig(reraise=False)
        pool = SyncProxyPool([S0], cfg)
        with pool:
            raise RuntimeError("inner")
        # did not propagate
        assert True

    def test_close_idempotent_and_blocks_ops(self) -> None:
        pool = SyncProxyPool([S0])
        pool.close()
        pool.close()
        assert pool.is_closed
        with pytest.raises(PoolClosedError):
            pool.get_next()

    def test_start_monitoring_thread_requires_health_check(self) -> None:
        pool = SyncProxyPool([S0])
        with pytest.raises(ValueError, match="health_check"):
            pool.start_monitoring_thread()

    def test_start_stop_monitoring_thread(self) -> None:
        hc = HealthCheckConfig(recovery_interval=0.05, url="http://127.0.0.1:9")
        pool = SyncProxyPool([S0], PoolConfig(health_check=hc))
        with mock.patch("omniproxy.pool.arun_health_check", new_callable=mock.AsyncMock) as m:
            m.return_value = (Proxy(S0), CheckResult(True, 0.01, None, 200))
            pool.start_monitoring_thread()
            time.sleep(0.15)
            pool.stop_monitoring()
        assert m.called


# ---------------------------------------------------------------------------
# AsyncProxyPool
# ---------------------------------------------------------------------------


class TestAsyncPool:
    def test_aget_next_basic(self) -> None:
        async def main() -> None:
            pool = AsyncProxyPool([S0])
            p = await pool.aget_next()
            assert p.url
            await pool.aclose()

        asyncio.run(main())

    def test_aget_next_closed(self) -> None:
        async def main() -> None:
            pool = AsyncProxyPool([S0])
            await pool.aclose()
            with pytest.raises(PoolClosedError):
                await pool.aget_next()

        asyncio.run(main())

    def test_aget_next_arefresh(self) -> None:
        async def main() -> None:
            calls: list[int] = []

            async def arefresh() -> list[str]:
                calls.append(1)
                return [S2]

            pool = AsyncProxyPool([], PoolConfig(arefresh_callback=arefresh))
            p = await pool.aget_next()
            assert calls == [1]
            assert p.url == Proxy(S2).url
            await pool.aclose()

        asyncio.run(main())

    def test_async_context_release(self) -> None:
        async def main() -> None:
            pool = AsyncProxyPool([S0])
            async with pool as p:
                assert p.url
            async with pool as p2:
                assert p2.url
            await pool.aclose()

        asyncio.run(main())

    def test_aexit_auto_mark(self) -> None:
        async def main() -> None:
            cfg = PoolConfig(
                auto_mark_failed_on_exception=True,
                auto_mark_success_on_exit=True,
                failure_threshold=99,
            )
            pool = AsyncProxyPool([S0], cfg)
            with pytest.raises(RuntimeError):
                async with pool:
                    raise RuntimeError("x")
            with pool._lock:
                assert pool._state._failure_counts.get(pool._key(Proxy(S0)), 0) >= 1
            pool.mark_success(S0)
            async with pool:
                pass
            with pool._lock:
                assert pool._state._failure_counts.get(pool._key(Proxy(S0)), 0) == 0
            await pool.aclose()

        asyncio.run(main())

    def test_aclose_idempotent(self) -> None:
        async def main() -> None:
            pool = AsyncProxyPool([S0])
            await pool.aclose()
            await pool.aclose()

        asyncio.run(main())

    def test_start_monitoring_requires_loop(self) -> None:
        pool = AsyncProxyPool([S0], PoolConfig(health_check=HealthCheckConfig()))
        with pytest.raises(RuntimeError, match="event loop"):
            pool.start_monitoring()

    def test_start_stop_monitoring_task(self) -> None:
        async def main() -> None:
            hc = HealthCheckConfig(recovery_interval=0.05, url="http://127.0.0.1:9")
            pool = AsyncProxyPool([S0], PoolConfig(health_check=hc))
            with mock.patch("omniproxy.pool.arun_health_check", new_callable=mock.AsyncMock) as m:
                m.return_value = (Proxy(S0), CheckResult(True, 0.01, None, 200))
                pool.start_monitoring()
                await asyncio.sleep(0.12)
                pool.stop_monitoring()
            assert m.called
            await pool.aclose()

        asyncio.run(main())

    def test_nested_async_with_independent_proxies(self) -> None:
        async def worker(pool: AsyncProxyPool, out: list[str]) -> None:
            async with pool as p:
                await asyncio.sleep(0.05)
                out.append(p.url)

        async def main() -> None:
            pool = AsyncProxyPool([S0, S1], PoolConfig(max_connections_per_proxy=2))
            urls: list[str] = []
            await asyncio.gather(worker(pool, urls), worker(pool, urls))
            assert len(set(urls)) == 2
            await pool.aclose()

        asyncio.run(main())


# ---------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------


class TestRotation:
    def test_round_robin_order(self) -> None:
        pool = SyncProxyPool(
            [S0, S1, S2],
            PoolConfig(strategy="round_robin"),
        )
        exp = [Proxy(S0).port, Proxy(S1).port, Proxy(S2).port]
        order = [pool.get_next().port for _ in range(6)]
        assert order[:3] == exp
        assert order[3:6] == exp

    def test_round_robin_after_removal(self) -> None:
        pool = SyncProxyPool([S0, S1], strategy="round_robin")
        _ = pool.get_next()
        p2 = pool.get_next()
        pool.mark_failed(S0)
        _ = pool.get_next()
        assert p2.port == Proxy(S1).port

    def test_random_from_pool(self) -> None:
        pool = SyncProxyPool([S0, S1, S2], strategy="random")
        ports = {pool.get_next().port for _ in range(30)}
        assert ports.issubset({Proxy(S0).port, Proxy(S1).port, Proxy(S2).port})
        assert len(ports) >= 1

    def test_random_forces_list_structure(self) -> None:
        cfg = PoolConfig(strategy="random", structure="deque")
        pool = SyncProxyPool([S0], cfg)
        assert cfg.structure == "list"
        with pool._lock:
            assert isinstance(pool._state.proxies, list)


# ---------------------------------------------------------------------------
# max_connections_per_proxy
# ---------------------------------------------------------------------------


class TestMaxConnections:
    def test_saturated_all_at_limit(self) -> None:
        cfg = PoolConfig(max_connections_per_proxy=1, acquire_timeout=0.0)
        pool = SyncProxyPool([S0], cfg)
        hold = threading.Event()
        release = threading.Event()

        def holder() -> None:
            with pool:
                hold.set()
                release.wait(timeout=2.0)

        th = threading.Thread(target=holder, daemon=True)
        th.start()
        assert hold.wait(timeout=2.0)
        try:
            with pytest.raises(PoolSaturated):
                pool.get_next()
        finally:
            release.set()
            th.join(timeout=3.0)

    def test_on_saturated_callback(self) -> None:
        cfg = PoolConfig(
            max_connections_per_proxy=1, acquire_timeout=0.0, on_saturated=lambda: None
        )
        pool = SyncProxyPool([S0], cfg)
        with mock.patch.object(cfg, "on_saturated", wraps=cfg.on_saturated) as w:
            hold = threading.Event()
            release = threading.Event()

            def holder() -> None:
                with pool:
                    hold.set()
                    release.wait(timeout=2.0)

            th = threading.Thread(target=holder, daemon=True)
            th.start()
            assert hold.wait(timeout=2.0)
            try:
                with pytest.raises(PoolSaturated):
                    pool.get_next()
            finally:
                release.set()
                th.join(timeout=3.0)
            w.assert_called()

    def test_release_decrements(self) -> None:
        cfg = PoolConfig(max_connections_per_proxy=2)
        pool = SyncProxyPool([S0], cfg)
        with pool:
            k = pool._key(Proxy(S0))
            with pool._lock:
                assert pool._active_connections.get(k, 0) == 1
        with pool._lock:
            assert pool._active_connections.get(k, 0) == 0


# ---------------------------------------------------------------------------
# max_rps / TokenBucket
# ---------------------------------------------------------------------------


class TestRps:
    def test_second_immediate_get_saturates(self) -> None:
        pool = SyncProxyPool([S0], PoolConfig(max_rps_per_proxy=1.0, acquire_timeout=0.0))
        pool.get_next()
        with pytest.raises(PoolSaturated):
            pool.get_next()

    def test_bucket_refills(self) -> None:
        pool = SyncProxyPool([S0], PoolConfig(max_rps_per_proxy=1.0, acquire_timeout=0.0))
        t0 = 10_000_000.0
        with mock.patch("time.monotonic", return_value=t0):
            pool.get_next()
        with mock.patch("time.monotonic", return_value=t0 + 2.0):
            p = pool.get_next()
        assert p.port == Proxy(S0).port

    def test_bucket_lazy_init(self) -> None:
        pool = SyncProxyPool([S0], PoolConfig(max_rps_per_proxy=2.0))
        with pool._lock:
            assert pool._state._token_buckets is None
        pool.get_next()
        with pool._lock:
            assert pool._state._token_buckets is not None


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


class TestFilters:
    def test_min_anonymity(self) -> None:
        low = _p(S0, anonymity="transparent")
        high = _p(S1, anonymity="elite")
        pool = SyncProxyPool([low, high])
        p = pool.get_next(min_anonymity="anonymous")
        assert p.anonymity == "elite"

    def test_country_exact(self) -> None:
        a = _p(S0, country="US")
        b = _p(S1, country="FR")
        pool = SyncProxyPool([a, b])
        assert pool.get_next(country="US").country == "US"

    def test_filter_missing_skip(self) -> None:
        bare = Proxy(S0)
        tagged = _p(S1, country="US")
        pool = SyncProxyPool([bare, tagged], PoolConfig(filter_missing_metadata="skip"))
        assert pool.get_next(country="US").url == tagged.url

    def test_filter_missing_raise(self) -> None:
        bare = Proxy(S0)
        pool = SyncProxyPool([bare], PoolConfig(filter_missing_metadata="raise"))
        with pytest.raises(MissingProxyMetadata):
            pool.get_next(country="US")

    def test_index_cache_cleared_on_state_change(self) -> None:
        a = _p(S0, country="US")
        b = _p(S1, country="FR")
        pool = SyncProxyPool([a, b])
        pool.get_next(country="US")
        with pool._lock:
            assert pool._state._index_cache
            assert not pool._state._index_dirty
        pool.mark_failed(a)
        with pool._lock:
            assert pool._state._index_dirty
        # Do not hold ``_lock`` while calling ``get_next`` (same thread re-entry deadlocks).
        _ = pool.get_next()
        with pool._lock:
            assert not pool._state._index_dirty


# ---------------------------------------------------------------------------
# Structure / merge / refresh
# ---------------------------------------------------------------------------


class TestStructureMerge:
    def test_deque_backend(self) -> None:
        pool = SyncProxyPool([S0], PoolConfig(strategy="round_robin", structure="deque"))
        with pool._lock:
            assert isinstance(pool._state.proxies, deque)

    def test_merge_dedupes(self) -> None:
        pool = SyncProxyPool([S0])
        pool._merge_refreshed_proxies([S0, S1])
        with pool._lock:
            keys = {pool._key(p) for p in pool._state._prototypes}
        assert len(keys) == 2

    def test_cooling_not_remerged_until_expired(self) -> None:
        t0 = 7_000_000.0
        pool = SyncProxyPool([S0], PoolConfig(cooldown=100.0, failure_threshold=1))
        with mock.patch("time.monotonic", return_value=t0):
            pool.mark_failed(S0)
        pool._merge_refreshed_proxies([S0])
        assert len(pool) == 0
        with mock.patch("time.monotonic", return_value=t0 + 200.0):
            pool._purge_cooldown()
            pool._merge_refreshed_proxies([S0])
        assert len(pool) >= 1


# ---------------------------------------------------------------------------
# Deprecated ProxyPool + protocols + HealthMonitor
# ---------------------------------------------------------------------------


class TestShimProtocolsHealth:
    def test_proxy_pool_deprecation(self) -> None:
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            ProxyPool([S0])
        assert any(issubclass(x.category, DeprecationWarning) for x in w)

    def test_protocol_isinstance(self) -> None:
        sp = SyncProxyPool([S0])
        ap = AsyncProxyPool([S0])
        assert isinstance(sp, BasePoolProtocol)
        assert isinstance(sp, SyncPoolProtocol)
        assert isinstance(sp, MonitorablePoolProtocol)
        assert isinstance(ap, BasePoolProtocol)
        assert isinstance(ap, AsyncPoolProtocol)
        assert isinstance(ap, MonitorablePoolProtocol)

    def test_health_monitor_marks_and_on_check_complete(self) -> None:
        async def main() -> None:
            results: list[tuple[bool, type[BaseException] | None]] = [(True, None)]
            completes: list[Any] = []

            async def fake_arun(proxy: Proxy, hc: Any) -> tuple[Proxy, CheckResult]:
                ok, et = results.pop(0)
                return (proxy, CheckResult(ok, 0.01, et, 200 if ok else 500))

            pool = AsyncProxyPool(
                [S0],
                PoolConfig(
                    health_check=HealthCheckConfig(
                        recovery_interval=0.05, url="http://127.0.0.1:9"
                    ),
                    on_check_complete=lambda p, r: completes.append((p.url, r.success)),
                ),
            )
            with mock.patch("omniproxy.pool.arun_health_check", side_effect=fake_arun):
                pool.start_monitoring()
                await asyncio.sleep(0.12)
                pool.stop_monitoring()
            assert completes
            await pool.aclose()

        asyncio.run(main())

    def test_health_monitor_stops_when_pool_closed(self) -> None:
        async def main() -> None:
            async def fake_arun(proxy: Proxy, hc: Any) -> tuple[Proxy, CheckResult]:
                return (proxy, CheckResult(True, 0.01, None, 200))

            pool = AsyncProxyPool(
                [S0],
                PoolConfig(
                    health_check=HealthCheckConfig(recovery_interval=0.05, url="http://127.0.0.1:9")
                ),
            )
            with mock.patch("omniproxy.pool.arun_health_check", side_effect=fake_arun):
                pool.start_monitoring()
                await asyncio.sleep(0.08)
                await pool.aclose()
                await asyncio.sleep(0.1)
            # no assertion on internals; aclose should not hang

        asyncio.run(main())

    def test_health_monitor_mark_failed_on_bad_result(self) -> None:
        async def main() -> None:
            async def fake_arun(proxy: Proxy, hc: Any) -> tuple[Proxy, CheckResult]:
                return (proxy, CheckResult(False, 0.01, OSError, 500))

            calls: list[int] = []
            orig = AsyncProxyPool.mark_failed

            def track(self: AsyncProxyPool, p: Proxy | str, exc_type: type | None = None) -> None:
                calls.append(1)
                return orig(self, p, exc_type)

            pool = AsyncProxyPool(
                [S0],
                PoolConfig(
                    health_check=HealthCheckConfig(
                        recovery_interval=0.05, url="http://127.0.0.1:9"
                    ),
                    failure_threshold=99,
                ),
            )
            with mock.patch("omniproxy.pool.arun_health_check", side_effect=fake_arun):
                with mock.patch.object(AsyncProxyPool, "mark_failed", track):
                    pool.start_monitoring()
                    await asyncio.sleep(0.12)
                    pool.stop_monitoring()
            assert calls
            await pool.aclose()

        asyncio.run(main())


def test_health_monitor_gc_without_close(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    hc = HealthCheckConfig(recovery_interval=0.05, url="http://127.0.0.1:9")
    pool = SyncProxyPool([S0], PoolConfig(health_check=hc))
    pref = weakref.ref(pool)
    mon = HealthMonitor(pool)
    del pool
    gc.collect()
    assert pref() is None
    with mock.patch("omniproxy.pool.arun_health_check", new_callable=mock.AsyncMock) as m:
        m.return_value = (Proxy(S0), CheckResult(True, 0.01, None, 200))
        with caplog.at_level(logging.WARNING):
            asyncio.run(mon.run())
    text = caplog.text.lower()
    assert "garbage-collected" in text
