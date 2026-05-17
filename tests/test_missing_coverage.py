"""Additional tests covering gaps in omniproxy v2.1 test suite.

Add to tests/ or merge into existing test files.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from unittest import mock

import pytest
from omniproxy import CheckResult, Proxy, check_proxy
from omniproxy.config import GlobalConfig, HealthCheckConfig, PoolConfig, settings
from omniproxy.enum import PoolStructure
from omniproxy.errors import (
    PoolExhausted,
    SessionBrokenError,
)
from omniproxy.extended_proxy import run_health_check
from omniproxy.pool import AsyncProxyPool, SyncProxyPool
from omniproxy.utils import OmniproxyParser, get_formatted_proxy_string

from tests.conftest import proxy_with_meta
from tests.pool_configs import pool_with_on_exhausted


# ===================================================================
# 1. Proxy read‑only attributes, comparisons, serialisation
# ===================================================================
class TestProxyAttributes:
    def test_attribute_immutable(self, s0: str) -> None:
        p = Proxy(s0)
        with pytest.raises(AttributeError):
            p.ip = "1.2.3.4"

    def test_comparison_ordering(self) -> None:
        fast = Proxy("1.1.1.1:80")
        fast._set_attribute("latency", 0.1)
        fast._set_attribute("last_checked", 100)
        slow = Proxy("2.2.2.2:80")
        slow._set_attribute("latency", 0.5)
        slow._set_attribute("last_checked", 100)
        # fast < slow due to lower latency
        assert fast < slow
        assert fast <= slow
        assert not fast > slow
        assert fast <= fast
        # equal latency → fallback to last_checked (more recent = smaller negated ts)
        mid = Proxy("3.3.3.3:80")
        mid._set_attribute("latency", 0.1)
        mid._set_attribute("last_checked", 200)
        assert mid < fast  # same latency, but more recent last_checked

    def test_serialization_roundtrip(self, s0: str) -> None:
        p = proxy_with_meta(s0, latency=1.5, country="US")
        d = p.to_dict()
        assert d.get("latency") == 1.5
        js = p.to_json_string()
        data = json.loads(js)
        assert data["country"] == "US"

        # pickle roundtrip via __reduce__
        import pickle

        p2 = pickle.loads(pickle.dumps(p))
        assert p2 == p
        assert p2.latency == p.latency


# ===================================================================
# 2. Advanced check_proxy behaviours (mocked backend)
# ===================================================================
class TestCheckProxyAdvanced:
    @mock.patch("omniproxy.extended_proxy.get_backend")
    def test_custom_expected_status(self, mock_be: mock.MagicMock, s0: str) -> None:
        backend = mock.MagicMock()
        resp = mock.MagicMock()
        resp.status_code = 201
        resp.json_data = {}
        backend.get.return_value = resp
        mock_be.return_value = backend

        _, result = check_proxy(s0, expected_status=201, max_retries=0)
        assert result.success is True

    @mock.patch("omniproxy.extended_proxy.get_backend")
    def test_expected_fields_validate(self, mock_be: mock.MagicMock, s0: str) -> None:
        backend = mock.MagicMock()
        resp = mock.MagicMock()
        resp.status_code = 200
        resp.json_data = {"key": "val", "extra": 1}
        backend.get.return_value = resp
        mock_be.return_value = backend

        # correct subset
        _, result = check_proxy(s0, expected_fields={"key"}, max_retries=0)
        assert result.success

        # missing field
        _, result = check_proxy(s0, expected_fields={"missing"}, max_retries=0)
        assert not result.success

    @mock.patch("omniproxy.extended_proxy.get_backend")
    def test_retry_on_status(self, mock_be: mock.MagicMock, s0: str) -> None:
        backend = mock.MagicMock()
        resp_bad = mock.MagicMock()
        resp_bad.status_code = 503
        resp_bad.json_data = None
        resp_ok = mock.MagicMock()
        resp_ok.status_code = 200
        resp_ok.json_data = {}
        backend.get.side_effect = [resp_bad, resp_ok]
        mock_be.return_value = backend

        _, result = check_proxy(
            s0, max_retries=1, retry_backoff=0.01, retry_on_status=frozenset({503})
        )
        assert result.success

    @mock.patch("omniproxy.extended_proxy.get_backend")
    def test_detect_anonymity(self, mock_be: mock.MagicMock, s0: str) -> None:
        backend = mock.MagicMock()
        main_resp = mock.MagicMock()
        main_resp.status_code = 200
        main_resp.json_data = {}
        probe_resp = mock.MagicMock()
        probe_resp.status_code = 200
        probe_resp.json_data = {"headers": {"X-Forwarded-For": "1.2.3.4"}}
        backend.get.side_effect = [main_resp, probe_resp]
        mock_be.return_value = backend

        proxy, result = check_proxy(s0, detect_anonymity=True, max_retries=0)
        assert result.success
        assert proxy.anonymity == "transparent"


# ===================================================================
# 3. Health check fallback & headers
# ===================================================================
class TestHealthCheckFallback:
    @mock.patch("omniproxy.extended_proxy.check_proxy")
    def test_fallback_to_global_defaults(self, mock_check: mock.MagicMock, s0: str) -> None:
        mock_check.return_value = (Proxy(s0), CheckResult(True, 0.1, None, 200))
        hc = HealthCheckConfig(url=None)  # no explicit URL
        _, result = run_health_check(s0, hc)
        assert result.success
        # verify check_proxy received a URL from global defaults
        call_args = mock_check.call_args[1]
        assert call_args["url"] in settings.default_check_urls

    @mock.patch("omniproxy.extended_proxy.check_proxy")
    def test_custom_headers_forwarded(self, mock_check: mock.MagicMock, s0: str) -> None:
        mock_check.return_value = (Proxy(s0), CheckResult(True, 0.1, None, 200))
        hc = HealthCheckConfig(
            url="http://example.com",
            headers={"X-Custom": "foo"},
        )
        run_health_check(s0, hc)
        assert mock_check.call_args[1]["headers"] == {"X-Custom": "foo"}


# ===================================================================
# 4. Weighted & lowest‑latency strategies + scoring / eviction
# ===================================================================
class TestWeightedLowestLatency:
    def test_weighted_bias_toward_faster_proxy(self, s0: str, s1: str, weighted_deterministic_bias_pool_config) -> None:
        cfg = weighted_deterministic_bias_pool_config
        p1, p2 = Proxy(s0), Proxy(s1)
        pool = SyncProxyPool(cfg, [p1, p2])
        try:
            for _ in range(15):
                pool.mark_success(p1, latency=0.05)
                pool.mark_success(p2, latency=1.5)
            # Deterministic midpoint draw: With two proxies and w_fast >= w_slow,
            # (w_fast + w_slow) / 2 falls in the fast proxy's cumulative segment.
            with mock.patch(
                "omniproxy.strategies.random.uniform",
                side_effect=lambda a, b: (a + b) / 2,
            ):
                for _ in range(10):
                    got = pool.acquire()
                    assert got.url == p1.url
                    pool.release(got)
        finally:
            pool.close()

    def test_lowest_latency_selection(self, s0: str, s1: str, lowest_latency_stable_cooldown_pool_config) -> None:
        p1, p2 = Proxy(s0), Proxy(s1)
        cfg = lowest_latency_stable_cooldown_pool_config
        pool = SyncProxyPool(cfg, [p1, p2])
        try:
            pool.mark_success(p1, latency=0.2)
            pool.mark_success(p2, latency=0.1)
            chosen = pool.acquire()
            assert chosen.url == p2.url
            pool.release(chosen)
        finally:
            pool.close()


# ===================================================================
# 5. Adaptive cooldown with custom strategy
# ===================================================================
class TestAdaptiveCooldown:
    def test_custom_cooldown_strategy_duration(self, s0: str, adaptive_cooldown_monotonic_timing_pool_config) -> None:
        cfg = adaptive_cooldown_monotonic_timing_pool_config
        p = Proxy(s0)
        pool = SyncProxyPool(cfg, [p])
        t0 = 1e6
        with mock.patch("time.monotonic", return_value=t0):
            pool.mark_failed(p)
        with pytest.raises(PoolExhausted):
            pool.acquire()
        with mock.patch("time.monotonic", return_value=t0 + 6.0):
            q = pool.acquire()
            pool.release(q)
        pool.close()


# ===================================================================
# 6. Session cooldown policies BLOCK / RAISE
# ===================================================================
class TestSessionPolicies:
    def test_policy_block_raises_pool_exhausted(self, s0: str, session_block_exhaust_pool_config) -> None:
        cfg = session_block_exhaust_pool_config
        pool = SyncProxyPool(cfg, [Proxy(s0)])
        pool.acquire(session_key="s1")
        pool.mark_failed(Proxy(s0))
        with pytest.raises(PoolExhausted):
            pool.acquire(session_key="s1")
        pool.close()

    def test_policy_raise_raises_session_broken(self, s0: str, session_raise_broken_pool_config) -> None:
        cfg = session_raise_broken_pool_config
        pool = SyncProxyPool(cfg, [Proxy(s0)])
        pool.acquire(session_key="s1")
        pool.mark_failed(Proxy(s0))
        with pytest.raises(SessionBrokenError):
            pool.acquire(session_key="s1")
        pool.close()


# ===================================================================
# 7. on_exhausted hook
# ===================================================================
class TestExhaustedHook:
    def test_on_exhausted_called(self, s0: str) -> None:
        exhausted_calls: list[int] = []

        def on_exhausted() -> None:
            exhausted_calls.append(1)

        cfg = pool_with_on_exhausted(on_exhausted)
        pool = SyncProxyPool(cfg, [Proxy(s0)])
        pool.mark_failed(Proxy(s0))
        with pytest.raises(PoolExhausted):
            pool.acquire()
        assert exhausted_calls == [1]
        pool.close()


# ===================================================================
# 8. PoolConfig enum coercion from strings
# ===================================================================
class TestConfigEnumCoercion:
    def test_weighted_requires_explicit_scoring(self) -> None:
        with pytest.raises(ValueError):
            PoolConfig(strategy="weighted")  # type: ignore[arg-type]

    def test_structure_string_accepted(self) -> None:
        cfg = PoolConfig(structure="deque")  # type: ignore[arg-type]
        assert cfg.structure == PoolStructure.DEQUE


# ===================================================================
# 9. OmniproxyConfig backend / URL validation
# ===================================================================
class TestOmniproxyConfigValidation:
    def test_health_check_urls_may_be_empty_on_new_instance(self) -> None:
        g = GlobalConfig(health_check_urls=())
        assert g.health_check_urls == ()


# ===================================================================
# 10. Utils – batch_parse unsupported & pattern full collapse
# ===================================================================
class TestUtilsEdgeCases:
    def test_batch_parse_invalid_raises(self) -> None:
        with pytest.raises(ValueError):
            OmniproxyParser.batch_parse(["not:a:proxy"])

    def test_get_formatted_string_full_optional_collapse(self) -> None:
        # proxy with all fields
        p = Proxy("socks5://user:pass@192.168.1.1:1080[https://rot.example.com]")
        formatted = get_formatted_proxy_string(
            p, "protocol://username:password@ip:port[rotation_url]"
        )
        # Should contain protocol, ip, port, username, password; rotation_url must appear
        assert "socks5" in formatted
        assert "user" in formatted
        assert "pass" in formatted
        assert "192.168.1.1" in formatted
        assert "rot.example.com" in formatted

        # Now without rotation_url
        p2 = Proxy("http://a:b@10.0.0.1:8080")
        formatted2 = get_formatted_proxy_string(p2, "ip:port@username:password[rotation_url]")
        # rotation_url bracket segment removed because absent
        assert "[" not in formatted2
        assert "10.0.0.1:8080@" in formatted2  # username:password present
        # without username
        p3 = Proxy("10.0.0.1:8080")
        formatted3 = get_formatted_proxy_string(p3, "ip:port@username:password[rotation_url]")
        # username and password removed; only ip:port remains
        assert formatted3 == "10.0.0.1:8080"


# ===================================================================
# 11. Concurrency stress test (thread+async)
# ===================================================================
class TestPoolConcurrencyStress:
    def test_sync_burst_without_threads(self, s0: str, s1: str, sync_burst_limits_pool_config) -> None:
        cfg = sync_burst_limits_pool_config
        pool = SyncProxyPool(cfg, [Proxy(s0), Proxy(s1)])
        for _ in range(64):
            px = pool.acquire()
            time.sleep(0.0005)
            pool.release(px)
        pool.close()

    @pytest.mark.asyncio
    async def test_async_task_contention(self, s0: str, s1: str, async_task_contention_limits_pool_config) -> None:
        cfg = async_task_contention_limits_pool_config

        async with AsyncProxyPool(cfg, [Proxy(s0), Proxy(s1)]) as pool:

            async def worker() -> None:
                for _ in range(20):
                    px = await pool.acquire()
                    await asyncio.sleep(0.001)
                    await pool.release(px)

            await asyncio.gather(*(worker() for _ in range(25)))
