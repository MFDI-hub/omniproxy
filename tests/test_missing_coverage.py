"""Additional tests covering gaps in omniproxy v2.1 test suite.

Add to tests/ or merge into existing test files.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any
from unittest import mock

import pytest
from omniproxy.config import (
    HealthCheckConfig,
    LifecycleHooks,
    LimitsConfig,
    PoolConfig,
    ScoringConfig,
    settings,
)
from omniproxy.enum import PoolStrategy, SessionCooldownPolicy
from omniproxy.errors import (
    PoolExhausted,
    PoolSaturated,
    SessionBrokenError,
)
from omniproxy.extended_proxy import (
    CheckResult,
    Proxy,
    check_proxy,
    run_health_check,
)
from omniproxy.pool import AsyncProxyPool, SyncProxyPool
from omniproxy.utils import OmniproxyParser, get_formatted_proxy_string

from tests.proxy_seeds import seeds

S0, S1, S2, S3 = seeds(4)


# -------------------------------------------------------------------
# Helper: build proxies with metadata for filtering / scoring tests
# -------------------------------------------------------------------
def _meta_proxy(url: str, **meta: Any) -> Proxy:
    p = Proxy(url)
    for k, v in meta.items():
        if v is not None:
            p._set_attribute(k, v)
    return p


# ===================================================================
# 1. Proxy read‑only attributes, comparisons, serialisation
# ===================================================================
class TestProxyAttributes:
    def test_attribute_immutable(self) -> None:
        p = Proxy(S0)
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

    def test_serialization_roundtrip(self) -> None:
        p = _meta_proxy(S0, latency=1.5, country="US")
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
    def test_custom_expected_status(self, mock_be: mock.MagicMock) -> None:
        backend = mock.MagicMock()
        resp = mock.MagicMock()
        resp.status_code = 201
        resp.json_data = {}
        backend.get.return_value = resp
        mock_be.return_value = backend

        _, result = check_proxy(S0, expected_status=201, max_retries=0)
        assert result.success is True

    @mock.patch("omniproxy.extended_proxy.get_backend")
    def test_expected_fields_validate(self, mock_be: mock.MagicMock) -> None:
        backend = mock.MagicMock()
        resp = mock.MagicMock()
        resp.status_code = 200
        resp.json_data = {"key": "val", "extra": 1}
        backend.get.return_value = resp
        mock_be.return_value = backend

        # correct subset
        _, result = check_proxy(S0, expected_fields={"key"}, max_retries=0)
        assert result.success

        # missing field
        _, result = check_proxy(S0, expected_fields={"missing"}, max_retries=0)
        assert not result.success

    @mock.patch("omniproxy.extended_proxy.get_backend")
    def test_retry_on_status(self, mock_be: mock.MagicMock) -> None:
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
            S0, max_retries=1, retry_backoff=0.01, retry_on_status=frozenset({503})
        )
        assert result.success

    @mock.patch("omniproxy.extended_proxy.get_backend")
    def test_detect_anonymity(self, mock_be: mock.MagicMock) -> None:
        backend = mock.MagicMock()
        main_resp = mock.MagicMock()
        main_resp.status_code = 200
        main_resp.json_data = {}
        probe_resp = mock.MagicMock()
        probe_resp.status_code = 200
        probe_resp.json_data = {"headers": {"X-Forwarded-For": "1.2.3.4"}}
        backend.get.side_effect = [main_resp, probe_resp]
        mock_be.return_value = backend

        proxy, result = check_proxy(S0, detect_anonymity=True, max_retries=0)
        assert result.success
        assert proxy.anonymity == "transparent"


# ===================================================================
# 3. Health check fallback & headers
# ===================================================================
class TestHealthCheckFallback:
    @mock.patch("omniproxy.extended_proxy.check_proxy")
    def test_fallback_to_global_defaults(self, mock_check: mock.MagicMock) -> None:
        mock_check.return_value = (Proxy(S0), CheckResult(True, 0.1, None, 200))
        hc = HealthCheckConfig(url=None)  # no explicit URL
        _, result = run_health_check(S0, hc)
        assert result.success
        # verify check_proxy received a URL from global defaults
        call_args = mock_check.call_args[1]
        assert call_args["url"] in settings.default_check_urls

    @mock.patch("omniproxy.extended_proxy.check_proxy")
    def test_custom_headers_forwarded(self, mock_check: mock.MagicMock) -> None:
        mock_check.return_value = (Proxy(S0), CheckResult(True, 0.1, None, 200))
        hc = HealthCheckConfig(
            url="http://example.com",
            headers={"X-Custom": "foo"},
        )
        run_health_check(S0, hc)
        assert mock_check.call_args[1]["headers"] == {"X-Custom": "foo"}


# ===================================================================
# 4. Weighted & lowest‑latency strategies + scoring / eviction
# ===================================================================
class TestWeightedLowestLatency:
    def test_weighted_selection(self) -> None:
        # Create proxies with different scores via mark_success/fail
        p1 = Proxy(S0)
        p2 = Proxy(S1)
        cfg = PoolConfig(
            strategy=PoolStrategy.WEIGHTED,
            scoring=ScoringConfig(window_seconds=60, min_samples=2),
            failure_threshold=100,
            acquire_timeout=1.0,
            cooldown=600,
        )
        pool = SyncProxyPool([p1, p2], cfg)
        # Make p1 faster than p2
        for _ in range(10):
            pool.mark_success(p1, latency=0.1)
            pool.mark_success(p2, latency=1.0)
        # Allow scoring to be computed
        with pool._lock:
            for k, s in pool._state._scores.items():
                s.compute_score(cfg.scoring, pool._state._nolock_pool_avg_latency())
        # p1 should be preferred repeatedly
        first = pool.get_next()
        assert first == p1
        pool.close()

    def test_lowest_latency_selection(self) -> None:
        p1 = Proxy(S0)
        p2 = Proxy(S1)
        cfg = PoolConfig(
            strategy=PoolStrategy.LOWEST_LATENCY,
            scoring=ScoringConfig(window_seconds=60, min_samples=2),
            failure_threshold=100,
            acquire_timeout=1.0,
            cooldown=600,
        )
        pool = SyncProxyPool([p1, p2], cfg)
        pool.mark_success(p1, latency=0.2)
        pool.mark_success(p2, latency=0.1)
        chosen = pool.get_next()
        assert chosen == p2  # lower latency
        pool.close()

    def test_eviction_removes_low_scored_proxy(self) -> None:
        # Force a proxy to have a very low score, then wait grace period
        p_good = Proxy(S0)
        p_bad = Proxy(S1)
        cfg = PoolConfig(
            strategy=PoolStrategy.ROUND_ROBIN,
            scoring=ScoringConfig(
                window_seconds=60,
                eviction_threshold=0.3,
                eviction_grace_period=0.0,
                min_samples=1,
            ),
            failure_threshold=100,
            cooldown=600,
        )
        pool = SyncProxyPool([p_good, p_bad], cfg)
        # Give good proxy success, bad proxy many failures so score < threshold
        pool.mark_success(p_good, latency=0.1)
        for _ in range(5):
            pool.mark_failed(p_bad)
        # Manually compute scores and call eviction (normally triggered on next get_next)
        with pool._lock:
            for k, s in pool._state._scores.items():
                s.compute_score(cfg.scoring, pool._state._nolock_pool_avg_latency())
            pool._state._nolock_evict_if_needed()
        # p_bad should be evicted
        assert p_bad not in pool.proxies
        pool.close()


# ===================================================================
# 5. Adaptive cooldown with custom strategy
# ===================================================================
class TestAdaptiveCooldown:
    def test_custom_cooldown_strategy(self) -> None:
        def custom(base: float, active: int, total: int) -> float:
            return base * 0.5

        cfg = PoolConfig(
            cooldown=10,
            failure_threshold=1,
            cooldown_strategy=custom,
            min_cooldown=0.1,
        )
        pool = SyncProxyPool([S0], cfg)
        t0 = 1e6
        with mock.patch("time.monotonic", return_value=t0):
            pool.mark_failed(S0)
        k = pool._key(Proxy(S0))
        with pool._lock:
            until = pool._state._cooldown_until[k]
        # base 10 * 0.5 = 5, plus minimum doesn't clip (5 > 0.1)
        assert until == t0 + 5.0
        pool.close()


# ===================================================================
# 6. Session cooldown policies BLOCK / RAISE
# ===================================================================
class TestSessionPolicies:
    def test_policy_block_raises_on_missing_bound(self) -> None:
        cfg = PoolConfig(
            session_ttl=60,
            session_cooldown_policy=SessionCooldownPolicy.BLOCK,
            failure_threshold=1,
        )
        pool = SyncProxyPool([S0], cfg)
        # create a bound session
        pool.get_next(session_id="s1")
        # now make proxy unavailable (fail it)
        pool.mark_failed(S0)  # cooldown
        # attempt to reuse session
        with pytest.raises(PoolSaturated, match=r"(?i)sticky"):
            pool.get_next(session_id="s1")
        pool.close()

    def test_policy_raise_raises_session_broken(self) -> None:
        cfg = PoolConfig(
            session_ttl=60,
            session_cooldown_policy=SessionCooldownPolicy.RAISE,
            failure_threshold=1,
        )
        pool = SyncProxyPool([S0], cfg)
        pool.get_next(session_id="s1")
        pool.mark_failed(S0)
        with pytest.raises(SessionBrokenError):
            pool.get_next(session_id="s1")
        pool.close()


# ===================================================================
# 7. on_exhausted hook
# ===================================================================
class TestExhaustedHook:
    def test_on_exhausted_called(self) -> None:
        exhausted_calls = []

        def on_exhausted() -> None:
            exhausted_calls.append(1)

        cfg = PoolConfig(
            failure_threshold=1,
            hooks=LifecycleHooks(on_exhausted=on_exhausted),
            # no refresh callback, so exhaustion raises after hook
        )
        pool = SyncProxyPool([S0], cfg)
        pool.mark_failed(S0)  # cooldown
        with pytest.raises(PoolExhausted):
            pool.get_next()
        assert exhausted_calls == [1]
        pool.close()


# ===================================================================
# 8. PoolConfig enum coercion from strings
# ===================================================================
class TestConfigEnumCoercion:
    def test_strategy_string_to_enum(self) -> None:
        with pytest.warns(UserWarning, match="strategy='weighted'"):
            cfg = PoolConfig(strategy="weighted")
        assert cfg.strategy == PoolStrategy.WEIGHTED
        cfg = PoolConfig(structure="deque")
        assert cfg.structure.value == "deque"  # PoolStructure.DEQUE


# ===================================================================
# 9. OmniproxyConfig backend / URL validation
# ===================================================================
class TestOmniproxyConfigValidation:
    def test_backend_validation(self) -> None:
        with pytest.raises(ValueError, match="Unknown backend"):
            settings.default_backend = "invalid_backend"

    def test_check_url_list_non_empty_required(self) -> None:
        with pytest.raises(ValueError):
            settings.default_check_urls = []

    def test_health_check_url_list_can_be_empty(self) -> None:
        settings.health_check_urls = []  # allowed
        assert settings.health_check_urls == []


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
    def test_sync_thread_contention(self) -> None:
        """Multiple threads acquire/release concurrently without deadlock."""
        cfg = PoolConfig(
            limits=LimitsConfig(max_connections_per_proxy=5),
            failure_threshold=100,
            acquire_timeout=5.0,
        )
        pool = SyncProxyPool([S0, S1], cfg)
        errors = []
        threads_done = threading.Event()

        def worker() -> None:
            try:
                for _ in range(20):
                    with pool:
                        time.sleep(0.001)
            except Exception as e:
                errors.append(e)
            finally:
                threads_done.set()

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert not errors, f"Thread errors: {errors}"
        pool.close()

    @pytest.mark.asyncio
    async def test_async_task_contention(self) -> None:
        """Multiple asyncio tasks acquiring concurrently."""
        cfg = PoolConfig(
            limits=LimitsConfig(max_connections_per_proxy=10),
            failure_threshold=100,
            acquire_timeout=5.0,
        )
        pool = AsyncProxyPool([S0, S1], cfg)

        async def worker() -> None:
            async with pool:
                await asyncio.sleep(0.001)

        tasks = [asyncio.create_task(worker()) for _ in range(30)]
        await asyncio.gather(*tasks)
        await pool.aclose()
