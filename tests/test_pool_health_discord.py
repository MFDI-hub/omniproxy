"""
Tests for omniproxy proxy pools, configuration, and optional Discord API checks.

- Offline tests run without environment variables.
- Integration tests (marked ``integration``) require:

  * ``PROXY_LIST`` : a **JSON array** of proxy strings (e.g. ``["http://user:pass@ip:port", ...]``).
  * ``TOKEN``      : a Discord API token for the ``Authorization`` header.

The ``.env`` file is automatically loaded by ``conftest.py`` if ``python-dotenv`` is installed.
"""

from __future__ import annotations

import asyncio
import random
import threading
import time
from typing import List

import pytest

from omniproxy import CheckResult, Proxy
from omniproxy.config import (
    CircuitBreakerConfig,
    CooldownConfig,
    GlobalConfig,
    HealthCheckConfig,
    PoolConfig,
    ScoringConfig,
    WarmupConfig,
    settings,
)
from omniproxy.constants import DEFAULT_TIMEOUT
from omniproxy.enum import PoolStructure, PoolStrategy
from omniproxy.errors import PoolCircuitOpenError, PoolExhausted
from omniproxy.extended_proxy import run_health_check
from omniproxy.pool import AsyncProxyPool, SyncProxyPool
from omniproxy.utils import OmniproxyParser, get_formatted_proxy_string


class TestConfigValidation:
    def test_scoring_weights_must_sum_to_one(self) -> None:
        with pytest.raises(ValueError):
            PoolConfig(scoring=ScoringConfig(success_weight=0.5, latency_weight=1.0))

    def test_warmup_requires_healthcheck(self) -> None:
        with pytest.raises(ValueError):
            PoolConfig(warmup=WarmupConfig(enabled=True), health_check=None)

    def test_min_cooldown_gt_max_cooldown(self) -> None:
        with pytest.raises(ValueError):
            PoolConfig(cooldown=CooldownConfig(min=100.0, max=50.0))

    def test_circuit_breaker_invalid_ratio(self) -> None:
        with pytest.raises(ValueError):
            PoolConfig(circuit_breaker=CircuitBreakerConfig(failure_ratio=0.0))
        with pytest.raises(ValueError):
            PoolConfig(circuit_breaker=CircuitBreakerConfig(failure_ratio=1.0))

    def test_weighted_strategy_requires_scoring(self) -> None:
        with pytest.raises(ValueError, match="weighted"):
            PoolConfig(strategy=PoolStrategy.WEIGHTED)

    def test_strategy_and_structure_accept_strings(self) -> None:
        cfg_rr = PoolConfig(strategy="round_robin")  # type: ignore[arg-type]
        assert cfg_rr.strategy == PoolStrategy.ROUND_ROBIN
        cfg_dq = PoolConfig(structure="deque")  # type: ignore[arg-type]
        assert cfg_dq.structure == PoolStructure.DEQUE

    def test_global_settings_defaults(self) -> None:
        assert settings.default_timeout == DEFAULT_TIMEOUT

    def test_global_singleton_is_frozen(self) -> None:
        with pytest.raises(Exception):
            settings.default_timeout = -1.0  # type: ignore[misc]

    def test_concurrent_reads(self) -> None:
        collected: list[float] = []

        def worker() -> None:
            for _ in range(50):
                collected.append(settings.default_timeout)  # type: ignore[list-item]

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)
        assert collected and all(isinstance(v, float) for v in collected)

    def test_global_config_constructors(self) -> None:
        with pytest.raises(ValueError, match="Unknown backend"):
            GlobalConfig(default_backend="invalid_backend_xyz")
        with pytest.raises(ValueError):
            GlobalConfig(default_check_urls=())
        hc_empty = GlobalConfig(health_check_urls=())
        assert hc_empty.health_check_urls == ()

    def test_scraping_and_api_gateway_presets(self) -> None:
        sp = PoolConfig.scraping_preset()
        assert sp.strategy == PoolStrategy.ROUND_ROBIN
        assert not sp.warmup.enabled
        gw = PoolConfig.api_gateway_preset()
        assert gw.strategy == PoolStrategy.WEIGHTED
        assert gw.warmup.enabled
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
    def test_round_robin_selection(self, offline_pool_config: PoolConfig) -> None:
        proxies = [Proxy(f"{i}.{i}.{i}.{i}:80") for i in range(1, 4)]
        pool = SyncProxyPool(offline_pool_config, proxies)
        got = [pool.acquire().ip for _ in range(3)]
        assert got == ["1.1.1.1", "2.2.2.2", "3.3.3.3"]
        pool.close()

    def test_cooldown_then_recover_after_delay(
        self, offline_quick_cooldown_recovery_pool_config: PoolConfig
    ) -> None:
        p = Proxy("10.0.0.1:80")
        cfg = offline_quick_cooldown_recovery_pool_config
        pool = SyncProxyPool(cfg, [p])
        pool.acquire()
        pool.mark_failed(p)
        with pytest.raises(PoolExhausted):
            pool.acquire()
        time.sleep(0.07)
        again = pool.acquire()
        assert again.url == p.url
        pool.release(again)
        pool.close()

    def test_circuit_breaker_opens_and_half_open_recover(
        self, offline_circuit_half_open_recover_pool_config: PoolConfig
    ) -> None:
        cfg = offline_circuit_half_open_recover_pool_config
        p1 = Proxy("10.10.10.1:80")
        p2 = Proxy("10.10.10.2:80")
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

    def test_session_stickiness(self, offline_session_stickiness_rebind_pool_config: PoolConfig) -> None:
        cfg = offline_session_stickiness_rebind_pool_config
        proxies = [Proxy("1.1.1.1:80"), Proxy("2.2.2.2:80")]
        pool = SyncProxyPool(cfg, proxies)
        first = pool.acquire(session_key="sess1")
        pool.release(first)
        second = pool.acquire(session_key="sess1")
        assert first.url == second.url
        pool.release(second)
        pool.close()

    def test_metadata_filtering(self, offline_pool_config: PoolConfig) -> None:
        p1 = Proxy("1.1.1.1:80")
        p1._set_attribute("country", "US")
        p2 = Proxy("2.2.2.2:80")
        p2._set_attribute("country", "DE")
        pool = SyncProxyPool(offline_pool_config, [p1, p2])
        proxy = pool.acquire(country="DE")
        assert proxy.url == p2.url
        pool.release(proxy)
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
    def test_single_proxy_check_returns_result(self, discord_health_config: HealthCheckConfig) -> None:
        p = Proxy("8.8.8.8:8080")
        _, result = run_health_check(p, discord_health_config)
        assert isinstance(result, CheckResult)

    def test_sync_pool_acquire_smoke(
        self, integration_proxies: List[Proxy], discord_pool_without_circuit_pool_config: PoolConfig
    ) -> None:
        cfg = discord_pool_without_circuit_pool_config
        pool = SyncProxyPool(cfg, integration_proxies)
        p = pool.acquire()
        pool.release(p)
        pool.close()

    def test_async_pool_smoke_acquire(
        self, integration_proxies: List[Proxy], discord_pool_config: PoolConfig
    ) -> None:
        async def _run() -> None:
            async with AsyncProxyPool(discord_pool_config, integration_proxies) as pool:
                p = await pool.acquire()
                assert isinstance(p, Proxy)
                await pool.release(p)

        asyncio.run(_run())

    def test_cooldown_after_mark_failed(
        self,
        integration_proxies: List[Proxy],
        discord_pool_without_circuit_pool_config: PoolConfig,
    ) -> None:
        pool = SyncProxyPool(discord_pool_without_circuit_pool_config, integration_proxies)
        thr = discord_pool_without_circuit_pool_config.cooldown.failure_threshold
        try:
            # Acquire every proxy in the pool first
            acquired = [pool.acquire() for _ in integration_proxies]
            # Mark each of them as failed enough times to trigger cooldown
            for proxy in acquired:
                for _ in range(thr):
                    pool.mark_failed(proxy, None)
            # Now the pool should be exhausted
            with pytest.raises(PoolExhausted):
                pool.acquire()
        finally:
            pool.close()

    def test_circuit_breaker_with_many_failures(
        self, integration_proxies: List[Proxy], discord_integration_strict_circuit_pool_config: PoolConfig
    ) -> None:
        if len(integration_proxies) < 2:
            pytest.skip("Need at least two proxies")
        cfg2 = discord_integration_strict_circuit_pool_config
        thr = cfg2.cooldown.failure_threshold
        pool = SyncProxyPool(cfg2, integration_proxies)
        try:
            for px in integration_proxies:
                for _ in range(thr):
                    pool.mark_failed(px, None)
            with pytest.raises(PoolCircuitOpenError):
                pool.acquire()
        finally:
            pool.close()

