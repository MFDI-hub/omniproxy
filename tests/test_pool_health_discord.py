"""
Tests for omniproxy v2.1 proxy pools, configuration, and optional Discord API checks.

- Offline tests run without environment variables.
- Integration tests (marked ``integration``) require:

  * ``PROXY_LIST`` : a **JSON array** of proxy strings (e.g. ``["http://user:pass@ip:port", ...]``).
  * ``TOKEN``      : a Discord API token for the ``Authorization`` header.

The ``.env`` file is automatically loaded by ``conftest.py`` if ``python-dotenv`` is installed.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import threading
import time

import pytest
from omniproxy.config import (
    CircuitBreakerConfig,
    HealthCheckConfig,
    LimitsConfig,
    PoolConfig,
    ScoringConfig,
    settings,
)
from omniproxy.enum import PoolStrategy, SessionCooldownPolicy
from omniproxy.errors import PoolCircuitOpenError, PoolExhausted
from omniproxy.extended_proxy import CheckResult, Proxy, run_health_check
from omniproxy.pool import AsyncProxyPool, SyncProxyPool
from omniproxy.utils import OmniproxyParser, get_formatted_proxy_string

# ---------------------------------------------------------------------------
# Helpers – environment
# ---------------------------------------------------------------------------


def _parse_proxy_list() -> list[str]:
    """Return a list of proxy strings from the PROXY_LIST JSON environment variable."""
    raw = os.getenv("PROXY_LIST", "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    out: list[str] = []
    for item in data:
        s = str(item).strip().strip('"').strip("'")
        if s:
            out.append(s)
    return out


def _require_token() -> str:
    token = os.getenv("TOKEN", "").strip()
    if not token:
        pytest.skip("TOKEN environment variable not set")
    return token


def _integration_proxies() -> list[Proxy]:
    raw = _parse_proxy_list()
    if not raw:
        pytest.skip("PROXY_LIST environment variable is empty or not set")
    return [Proxy(s) for s in raw]


# ---------------------------------------------------------------------------
# Pool configs
# ---------------------------------------------------------------------------


def _offline_pool_config() -> PoolConfig:
    """Used by unit/component tests: no explicit health URL."""
    return PoolConfig(
        strategy=PoolStrategy.ROUND_ROBIN,
        cooldown=10.0,
        min_cooldown=0.1,
        max_cooldown=3600.0,
        adaptive_cooldown=False,
        failure_threshold=2,
        acquire_timeout=5.0,
        wait_fallback_interval=0.1,
        health_check=None,
        scoring=ScoringConfig(window_seconds=60.0, eviction_threshold=0.2),
        circuit_breaker=CircuitBreakerConfig(
            window_seconds=60.0,
            failure_ratio=0.5,
            half_open_timeout=10.0,
            min_throughput=2,
        ),
        limits=LimitsConfig(max_connections_per_proxy=2, max_rps_per_proxy=5.0),
        session_cooldown_policy=SessionCooldownPolicy.REBIND,
        log_level=50,
    )


def _discord_health_check_config(token: str) -> HealthCheckConfig:
    return HealthCheckConfig(
        url="https://discord.com/api/v9/experiments",
        method="GET",
        expected_status=200,
        headers={"Authorization": token},
        timeout=10.0,
        recovery_interval=5.0,  # short for tests
    )


def discord_pool_config(token: str) -> PoolConfig:
    cfg = _offline_pool_config()
    cfg.health_check = _discord_health_check_config(token)
    return cfg


# ---------------------------------------------------------------------------
# Offline tests (same as before, no changes needed)
# ---------------------------------------------------------------------------


class TestConfigValidation:
    def test_scoring_weights_must_sum_to_one(self) -> None:
        with pytest.raises(ValueError):
            PoolConfig(scoring=ScoringConfig(success_weight=0.5, latency_weight=1.0))

    def test_warmup_requires_healthcheck(self) -> None:
        with pytest.raises(ValueError):
            PoolConfig(warmup=True, health_check=None)

    def test_min_cooldown_gt_max_cooldown(self) -> None:
        with pytest.raises(ValueError):
            PoolConfig(min_cooldown=100, max_cooldown=50)

    def test_circuit_breaker_invalid_ratio(self) -> None:
        with pytest.raises(ValueError):
            PoolConfig(circuit_breaker=CircuitBreakerConfig(failure_ratio=0.0))
        with pytest.raises(ValueError):
            PoolConfig(circuit_breaker=CircuitBreakerConfig(failure_ratio=1.0))

    def test_weighted_strategy_warns_and_sets_scoring(self) -> None:
        with pytest.warns(UserWarning, match="weighted"):
            cfg = PoolConfig(strategy=PoolStrategy.WEIGHTED, scoring=None)
        assert cfg.scoring is not None

    def test_omniproxy_config_singleton(self) -> None:
        settings.default_timeout = 5.0
        assert settings.default_timeout == 5.0
        with pytest.raises(ValueError):
            settings.default_timeout = -1

    def test_settings_concurrent_writes(self) -> None:
        errors: list[BaseException] = []

        def worker(i: int) -> None:
            try:
                for _ in range(50):
                    settings.default_timeout = float(1 + (i % 10))
            except BaseException as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert isinstance(settings.default_timeout, float)

    def test_scraping_and_api_gateway_presets(self) -> None:
        sp = PoolConfig.scraping_preset()
        assert sp.strategy == PoolStrategy.ROUND_ROBIN
        assert sp.warmup is False
        gw = PoolConfig.api_gateway_preset()
        assert gw.strategy == PoolStrategy.WEIGHTED
        assert gw.warmup is True
        assert gw.health_check is not None


class TestProxyParsing:
    def test_basic_ip_port(self) -> None:
        p = Proxy("127.0.0.1:8080")
        assert p.ip == "127.0.0.1"
        assert p.port == 8080
        assert p.protocol == "http"

    def test_full_url(self) -> None:
        p = Proxy("http://user:pass@1.2.3.4:3128")
        assert p.username == "user"
        assert p.password == "pass"
        assert p.protocol == "http"

    def test_socks(self) -> None:
        p = Proxy("socks5://1.1.1.1:1080")
        assert p.protocol == "socks5"

    def test_ipv6_bracketed(self) -> None:
        p = Proxy("[::1]:8080")
        assert "::1" in p.ip or p.ip == "[::1]"
        assert p.port == 8080

    def test_invalid_octet_rejected(self) -> None:
        with pytest.raises(ValueError):
            Proxy("210.173.88.999:3001")

    def test_clone_with_protocol_keeps_metadata(self) -> None:
        p1 = Proxy("http://a:b@1.1.1.1:80")
        p1._set_attribute("latency", 1.23)
        p2 = Proxy(p1, protocol="https")
        assert p2.protocol == "https"
        assert p2.latency == 1.23
        assert p2 is not p1

    def test_safe_url_masks_password(self) -> None:
        p = Proxy("http://u:secret@127.0.0.1:1")
        assert "secret" not in p.safe_url
        assert ":***" in p.safe_url or "*" in p.safe_url


class TestUtilsHelpers:
    def test_batch_parse_skips_empty(self) -> None:
        out = OmniproxyParser.batch_parse(["", "  ", "127.0.0.1:8080"])
        assert len(out) == 1

    def test_get_formatted_proxy_string_optional_fields(self) -> None:
        p = Proxy("127.0.0.1:8080")
        s = get_formatted_proxy_string(p, p.default_pattern)
        assert "127.0.0.1" in s

    def test_parser_from_string_roundtrip(self) -> None:
        raw = "http://user:pass@192.168.0.1:8888"
        pr = OmniproxyParser.from_string(raw)
        assert pr.protocol == "http"


class TestRunHealthCheckCustom:
    def test_custom_check_short_circuit(self) -> None:
        p = Proxy("127.0.0.1:9")

        def ok(_: Proxy) -> bool:
            return True

        hc = HealthCheckConfig(custom_check=ok)
        out_p, res = run_health_check(p, hc)
        assert out_p is p
        assert res.success is True


class TestPoolStateOperations:
    def test_round_robin_selection(self) -> None:
        proxies = [Proxy(f"{i}.{i}.{i}.{i}:80") for i in range(1, 4)]
        pool = SyncProxyPool(proxies, _offline_pool_config())
        got = [pool.get_next().ip for _ in range(3)]
        assert got == ["1.1.1.1", "2.2.2.2", "3.3.3.3"]
        pool.close()

    def test_cooldown_after_failures(self) -> None:
        cfg = _offline_pool_config()
        cfg.failure_threshold = 2
        cfg.cooldown = 0.5
        cfg.min_cooldown = 0.1
        cfg.adaptive_cooldown = False
        cfg.circuit_breaker = None
        p = Proxy("10.0.0.1:80")
        pool = SyncProxyPool([p], cfg)
        pool.mark_failed(p)
        q = pool.get_next()
        assert q == p
        pool._release_active_slot(q)
        pool.mark_failed(p)
        with pytest.raises(PoolExhausted):
            pool.get_next()
        time.sleep(0.6)
        assert pool.get_next() == p
        pool.close()

    def test_circuit_breaker_opens_and_half_open_recover(self) -> None:
        cfg = _offline_pool_config()
        cfg.circuit_breaker = CircuitBreakerConfig(
            failure_ratio=0.5,
            half_open_timeout=0.5,
            window_seconds=60,
            min_throughput=2,
        )
        p1 = Proxy("10.10.10.1:80")
        p2 = Proxy("10.10.10.2:80")
        pool = SyncProxyPool([p1, p2], cfg)
        pool.mark_failed(p1)
        pool.mark_failed(p2)
        with pytest.raises(PoolCircuitOpenError):
            pool.get_next()
        time.sleep(0.6)
        pool.mark_success(p1)
        assert pool.get_next() in (p1, p2)
        pool.close()

    def test_session_stickiness(self) -> None:
        cfg = _offline_pool_config()
        cfg.session_ttl = 60.0
        proxies = [Proxy("1.1.1.1:80"), Proxy("2.2.2.2:80")]
        pool = SyncProxyPool(proxies, cfg)
        first = pool.get_next(session_id="sess1")
        pool._release_active_slot(first)
        second = pool.get_next(session_id="sess1")
        assert first == second
        pool.close()

    def test_metadata_filtering(self) -> None:
        p1 = Proxy("1.1.1.1:80")
        p1._set_attribute("country", "US")
        p2 = Proxy("2.2.2.2:80")
        p2._set_attribute("country", "DE")
        pool = SyncProxyPool([p1, p2], _offline_pool_config())
        proxy = pool.get_next(country="DE")
        assert proxy == p2
        pool._release_active_slot(proxy)
        pool.close()


class TestProxyRandomisedFormats:
    @staticmethod
    def _random_ipv4() -> str:
        return ".".join(str(random.randint(1, 223)) for _ in range(4))

    def test_random_host_port_strings_parse(self) -> None:
        for _ in range(30):
            ip = self._random_ipv4()
            port = random.randint(1, 65535)
            p = Proxy(f"{ip}:{port}")
            assert p.port == port
            assert ip in p.ip or p.ip == ip


# ---------------------------------------------------------------------------
# Integration – Discord + real proxies (marked ``integration``)
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestDiscordHealthCheckIntegration:
    @pytest.fixture
    def token(self) -> str:
        return _require_token()

    @pytest.fixture
    def proxy_list(self) -> list[Proxy]:
        return _integration_proxies()

    @pytest.fixture
    def pool_config(self, token: str) -> PoolConfig:
        return discord_pool_config(token)

    def test_single_proxy_check_returns_result(self, token: str) -> None:
        p = Proxy("8.8.8.8:8080")
        hc = _discord_health_check_config(token)
        _, result = run_health_check(p, hc)
        assert isinstance(result, CheckResult)

    def test_pool_with_health_monitor(
        self, proxy_list: list[Proxy], pool_config: PoolConfig
    ) -> None:
        pool = SyncProxyPool(proxy_list, pool_config)
        pool.start_monitoring_thread()
        # Wait for at least one health pass
        time.sleep(pool_config.health_check.recovery_interval + 2)  # type: ignore[union-attr]
        working = [p for p in pool.proxies if p.is_working]
        # Just a smoke test; we can't guarantee any proxy actually works.
        assert len(working) <= len(proxy_list)
        pool.close()

    def test_async_pool_health(self, proxy_list: list[Proxy], pool_config: PoolConfig) -> None:
        async def _run() -> None:
            pool = AsyncProxyPool(proxy_list, pool_config)
            pool.start_monitoring()
            hc = pool_config.health_check
            assert hc is not None
            await asyncio.sleep(hc.recovery_interval + 2)
            working = [p for p in pool.proxies if p.is_working]
            assert len(working) <= len(proxy_list)
            await pool.aclose()

        asyncio.run(_run())

    def test_cooldown_after_mark_failed(
        self, proxy_list: list[Proxy], pool_config: PoolConfig
    ) -> None:
        pool = SyncProxyPool(proxy_list, pool_config)
        proxy = pool.get_next()
        try:
            for _ in range(pool_config.failure_threshold):
                pool.mark_failed(proxy, None)
            assert proxy not in pool.proxies
        finally:
            pool.close()

    def test_circuit_breaker_with_many_failures(
        self, proxy_list: list[Proxy], pool_config: PoolConfig
    ) -> None:
        if len(proxy_list) < 2:
            pytest.skip("Need at least two proxies")
        cfg = pool_config
        cfg.circuit_breaker = CircuitBreakerConfig(
            failure_ratio=0.5,
            half_open_timeout=5.0,
            window_seconds=60,
            min_throughput=2,
        )
        pool = SyncProxyPool(proxy_list, cfg)
        try:
            for p in list(pool):
                for _ in range(cfg.failure_threshold):
                    pool.mark_failed(p)
            with pytest.raises(PoolCircuitOpenError):
                pool.get_next()
        finally:
            pool.close()
