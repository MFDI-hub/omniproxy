"""Pytest configuration and shared fixtures for omniproxy tests."""

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Generator, List

import pytest
from dotenv import load_dotenv

from omniproxy import Proxy
from omniproxy.config import (
    CircuitBreakerConfig,
    CooldownConfig,
    HealthCheckConfig,
    LifecycleHooks,
    LimitsConfig,
    PoolConfig,
    ScoringConfig,
    SessionConfig,
)
from omniproxy.enum import PoolStrategy, SessionCooldownPolicy
from tests import pool_configs
from tests.proxy_seeds import all_seeds, seeds

# Load .env from project root
env_path = Path(__file__).parent.parent / ".env"
if env_path.exists():
    load_dotenv(env_path)


def pytest_configure(config):
    config.addinivalue_line("markers", "live: mark test as requiring live proxies / network")
    config.addinivalue_line("markers", "integration: mark test as requiring Discord token + proxy list")


# ----------------------------------------------------------------------
# Proxy seeds
# ----------------------------------------------------------------------

@pytest.fixture(scope="session")
def proxy_strings() -> List[str]:
    """All proxy strings from PROXY_LIST or synthetic fallback."""
    return all_seeds()


@pytest.fixture(scope="session")
def proxy_list(proxy_strings: List[str]) -> List[Proxy]:
    """Proxy objects for all seeds."""
    return [Proxy(s) for s in proxy_strings]


@pytest.fixture(scope="session")
def s0(proxy_strings: List[str]) -> str:
    return seeds(1)[0]


@pytest.fixture(scope="session")
def s1(proxy_strings: List[str]) -> str:
    return seeds(2)[1]


@pytest.fixture(scope="session")
def s2(proxy_strings: List[str]) -> str:
    return seeds(3)[2]


@pytest.fixture(scope="session")
def s3(proxy_strings: List[str]) -> str:
    return seeds(4)[3]


# ----------------------------------------------------------------------
# Helper to create Proxy with metadata
# ----------------------------------------------------------------------
def proxy_with_meta(url: str, **meta: Any) -> Proxy:
    """Return a Proxy instance with custom attributes set."""
    p = Proxy(url)
    for k, v in meta.items():
        if v is not None:
            p._set_attribute(k, v)
    return p


# ----------------------------------------------------------------------
# Pool configuration
# ----------------------------------------------------------------------

@pytest.fixture
def default_pool_config() -> PoolConfig:
    """Standard config for most unit tests (TTL=300)."""
    return PoolConfig(
        strategy=PoolStrategy.ROUND_ROBIN,
        cooldown=CooldownConfig(
            base=10.0, min=0.1, max=3600.0, adaptive=False, failure_threshold=2
        ),
        acquire_timeout=5.0,
        wait_fallback_interval=0.1,
        health_check=None,
        scoring=ScoringConfig(window_seconds=60.0, eviction_threshold=0.2),
        circuit_breaker=CircuitBreakerConfig(
            window_seconds=60.0, failure_ratio=0.5, half_open_timeout=10.0, min_throughput=2
        ),
        limits=LimitsConfig(max_connections_per_proxy=2, max_rps_per_proxy=5.0),
        session=SessionConfig(cooldown_policy=SessionCooldownPolicy.REBIND, ttl=300.0),
        log_level=50,
    )


@pytest.fixture
def offline_pool_config() -> PoolConfig:
    """Config with short TTL (60s), as used by many component tests."""
    return PoolConfig(
        strategy=PoolStrategy.ROUND_ROBIN,
        cooldown=CooldownConfig(
            base=10.0, min=0.1, max=3600.0, adaptive=False, failure_threshold=2
        ),
        acquire_timeout=5.0,
        wait_fallback_interval=0.1,
        health_check=None,
        scoring=ScoringConfig(window_seconds=60.0, eviction_threshold=0.2),
        circuit_breaker=CircuitBreakerConfig(
            window_seconds=60.0, failure_ratio=0.5, half_open_timeout=10.0, min_throughput=2
        ),
        limits=LimitsConfig(max_connections_per_proxy=2, max_rps_per_proxy=5.0),
        session=SessionConfig(ttl=60.0, cooldown_policy=SessionCooldownPolicy.REBIND),
        log_level=50,
    )


@pytest.fixture
def mock_backend() -> Generator[Any, None, None]:
    from unittest.mock import MagicMock, AsyncMock, patch
    mock = MagicMock(spec=['get', 'aget', 'request_direct', 'arequest_direct'])
    mock.get = MagicMock()
    mock.aget = AsyncMock()
    with patch("omniproxy.extended_proxy.get_backend", return_value=mock):
        yield mock


# ----------------------------------------------------------------------
# Live / integration helpers
# ----------------------------------------------------------------------

def _parse_proxy_list() -> List[str]:
    raw = os.getenv("PROXY_LIST", "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [str(item).strip().strip('"').strip("'") for item in data if str(item).strip()]


def _live_configured() -> bool:
    proxies = _parse_proxy_list()
    token = os.getenv("TOKEN", "").strip()
    return bool(proxies and token)


@pytest.fixture
def live_proxies() -> List[Proxy]:
    """Return real proxies from PROXY_LIST when live tests are enabled."""
    if os.getenv("OMNIPROXY_LIVE_TESTS") != "1" or not _live_configured():
        pytest.skip("Live tests not enabled (set OMNIPROXY_LIVE_TESTS=1 and PROXY_LIST+TOKEN)")
    return [Proxy(s) for s in _parse_proxy_list()]


@pytest.fixture
def integration_proxies() -> List[Proxy]:
    """Return real proxies for integration tests (requires PROXY_LIST & TOKEN)."""
    proxies = _parse_proxy_list()
    if not proxies:
        pytest.skip("PROXY_LIST environment variable is empty or not set")
    token = os.getenv("TOKEN", "").strip()
    if not token:
        pytest.skip("TOKEN environment variable not set")
    return [Proxy(s) for s in proxies]


@pytest.fixture
def discord_token() -> str:
    token = os.getenv("TOKEN", "").strip()
    if not token:
        pytest.skip("TOKEN environment variable not set")
    return token


@pytest.fixture
def discord_health_config(discord_token: str) -> HealthCheckConfig:
    return HealthCheckConfig(
        url="https://discord.com/api/v9/experiments",
        method="GET",
        expected_status=200,
        headers={"Authorization": discord_token},
        timeout=10.0,
        recovery_interval=5.0,
    )


@pytest.fixture
def live_pool_config(discord_health_config: HealthCheckConfig) -> PoolConfig:
    return PoolConfig.scraping_preset().model_copy(update={"health_check": discord_health_config})


@pytest.fixture
def discord_pool_config(offline_pool_config: PoolConfig, discord_health_config: HealthCheckConfig) -> PoolConfig:
    """Integration pool config based on offline_pool_config with Discord health check."""
    return offline_pool_config.model_copy(update={"health_check": discord_health_config})


# ---------------------------------------------------------------------
# Specialized pool configs (see tests.pool_configs for builders)
# ---------------------------------------------------------------------


@pytest.fixture
def minimal_round_robin_pool_config() -> PoolConfig:
    return pool_configs.minimal_round_robin_config()


@pytest.fixture
def round_robin_skip_missing_metadata_pool_config() -> PoolConfig:
    return pool_configs.round_robin_skip_missing_metadata_config()


@pytest.fixture
def weighted_scoring_pool_config(default_pool_config: PoolConfig) -> PoolConfig:
    return pool_configs.weighted_scoring_from_default(default_pool_config)


@pytest.fixture
def lowest_latency_strategy_pool_config(default_pool_config: PoolConfig) -> PoolConfig:
    return pool_configs.lowest_latency_from_default(default_pool_config)


@pytest.fixture
def session_rebind_ttl60_pool_config(default_pool_config: PoolConfig) -> PoolConfig:
    return pool_configs.session_rebind_ttl60_from_default(default_pool_config)


@pytest.fixture
def session_block_strict_cooldown_pool_config(default_pool_config: PoolConfig) -> PoolConfig:
    return pool_configs.session_block_strict_cooldown_from_default(default_pool_config)


@pytest.fixture
def session_raise_strict_cooldown_pool_config(default_pool_config: PoolConfig) -> PoolConfig:
    return pool_configs.session_raise_strict_cooldown_from_default(default_pool_config)


@pytest.fixture
def circuit_half_open_quick_pool_config(default_pool_config: PoolConfig) -> PoolConfig:
    return pool_configs.circuit_half_open_quick_from_default(default_pool_config)


@pytest.fixture
def custom_half_base_cooldown_pool_config() -> PoolConfig:
    return pool_configs.custom_half_base_cooldown_pool_config()


@pytest.fixture
def async_strict_cooldown_pool_config(default_pool_config: PoolConfig) -> PoolConfig:
    return pool_configs.async_strict_cooldown_fail1_from_default(default_pool_config)


@pytest.fixture
def weighted_deterministic_bias_pool_config() -> PoolConfig:
    return pool_configs.weighted_deterministic_bias_config()


@pytest.fixture
def lowest_latency_stable_cooldown_pool_config() -> PoolConfig:
    return pool_configs.lowest_latency_stable_cooldown_config()


@pytest.fixture
def adaptive_cooldown_monotonic_timing_pool_config() -> PoolConfig:
    return pool_configs.adaptive_custom_cooldown_monotonic_timing_config()


@pytest.fixture
def session_block_exhaust_pool_config() -> PoolConfig:
    return pool_configs.session_block_pool_exhausted_config()


@pytest.fixture
def session_raise_broken_pool_config() -> PoolConfig:
    return pool_configs.session_raise_session_broken_config()


@pytest.fixture
def sync_burst_limits_pool_config() -> PoolConfig:
    return pool_configs.sync_burst_connection_limits_config()


@pytest.fixture
def async_task_contention_limits_pool_config() -> PoolConfig:
    return pool_configs.async_task_contention_limits_config()


@pytest.fixture
def comprehensive_filter_missing_raise_pool_config() -> PoolConfig:
    return pool_configs.comprehensive_filter_missing_raise_config()


@pytest.fixture
def comprehensive_session_rebind_120_pool_config() -> PoolConfig:
    return pool_configs.comprehensive_session_rebind_120_config()


@pytest.fixture
def comprehensive_session_raise_cooling_pool_config() -> PoolConfig:
    return pool_configs.comprehensive_session_raise_cooling_config()


@pytest.fixture
def comprehensive_circuit_acquire_block_pool_config() -> PoolConfig:
    return pool_configs.comprehensive_circuit_breaker_acquire_block_config()


@pytest.fixture
def offline_quick_cooldown_recovery_pool_config(offline_pool_config: PoolConfig) -> PoolConfig:
    return pool_configs.offline_quick_cooldown_recovery_config(offline_pool_config)


@pytest.fixture
def offline_circuit_half_open_recover_pool_config(offline_pool_config: PoolConfig) -> PoolConfig:
    return pool_configs.offline_circuit_half_open_recover_config(offline_pool_config)


@pytest.fixture
def offline_session_stickiness_rebind_pool_config(offline_pool_config: PoolConfig) -> PoolConfig:
    return pool_configs.offline_session_stickiness_rebind_config(offline_pool_config)


@pytest.fixture
def discord_pool_without_circuit_pool_config(discord_pool_config: PoolConfig) -> PoolConfig:
    return pool_configs.discord_pool_without_circuit_config(discord_pool_config)


@pytest.fixture
def discord_integration_strict_circuit_pool_config(discord_pool_config: PoolConfig) -> PoolConfig:
    return pool_configs.discord_integration_strict_circuit_config(discord_pool_config)


# ----------------------------------------------------------------------
# Extended / gap-coverage configs (tests.test_pool_features_extended)
# ----------------------------------------------------------------------


@pytest.fixture
def extended_random_strategy_pool_config() -> PoolConfig:
    return pool_configs.extended_random_strategy_only_config()


@pytest.fixture
def extended_warmup_min_ready_ok_pool_config() -> PoolConfig:
    return pool_configs.extended_warmup_min_ready_health_ok_config()


@pytest.fixture
def extended_warmup_timeout_raise_pool_config() -> PoolConfig:
    return pool_configs.extended_warmup_timeout_impossible_raise_config()


@pytest.fixture
def extended_warmup_partial_unmet_pool_config() -> PoolConfig:
    return pool_configs.extended_warmup_partial_unmet_config()


@pytest.fixture
def extended_refresh_async_from_seed_pool_config(s2: str) -> PoolConfig:
    return pool_configs.extended_refresh_via_async_snapshot_config([Proxy(s2)], acquire_timeout=0.0)


@pytest.fixture
def extended_refresh_sync_from_seed_pool_config(s1: str) -> PoolConfig:
    return pool_configs.extended_refresh_via_sync_snapshot_config([Proxy(s1)], acquire_timeout=0.0)


@pytest.fixture
def extended_single_proxy_connection_wait_pool_config() -> PoolConfig:
    return pool_configs.extended_single_proxy_connection_wait_config()


@pytest.fixture
def extended_quick_close_acquire_zero_pool_config() -> PoolConfig:
    return pool_configs.extended_quick_close_acquire_zero_config()


@pytest.fixture
def extended_quick_close_sync_pool_config() -> PoolConfig:
    return pool_configs.extended_quick_close_only_config(drain_timeout=0.0)


@pytest.fixture
def extended_rotate_on_acquire_pool_config() -> PoolConfig:
    return pool_configs.extended_rotate_on_acquire_config()


@pytest.fixture
def recording_metrics_exporter() -> Any:
    class _GaugeRecorder:
        """Minimal :protocol:`MetricsExporter` for tests."""

        def __init__(self) -> None:
            self.gauges: list[tuple[str, float, dict[str, str] | None]] = []

        def emit_gauge(
            self, name: str, value: float, tags: dict[str, str] | None = None
        ) -> None:
            self.gauges.append((name, float(value), tags))

        def emit_counter(
            self, name: str, value: float, tags: dict[str, str] | None = None
        ) -> None:
            pass

        def close(self) -> None:
            pass

    return _GaugeRecorder()


@pytest.fixture
def extended_metrics_exporter_pool_config(recording_metrics_exporter: Any) -> PoolConfig:
    return pool_configs.extended_metrics_exporter_only_config(recording_metrics_exporter)


@pytest.fixture
def extended_hooks_tracking() -> dict[str, list[Any]]:
    return {"acquired": [], "released": [], "failed": [], "recovered": []}


@pytest.fixture
def extended_lifecycle_hooks_pool_config(extended_hooks_tracking: dict[str, list[Any]]) -> PoolConfig:
    t = extended_hooks_tracking

    def _acc(p: Proxy) -> None:
        t["acquired"].append(p.url)

    def _rel(p: Proxy) -> None:
        t["released"].append(p.url)

    def _fail(p: Proxy, exc: type | None) -> None:
        t["failed"].append((p.url, exc))

    def _recover(p: Proxy) -> None:
        t["recovered"].append(p.url)

    hooks = LifecycleHooks(
        on_proxy_acquired=_acc,
        on_proxy_released=_rel,
        on_proxy_failed=_fail,
        on_proxy_recovered=_recover,
    )
    return pool_configs.extended_lifecycle_hooks_config(hooks)


@pytest.fixture
def extended_dead_letter_retry_health_ok_pool_config() -> PoolConfig:
    return pool_configs.extended_dead_letter_retry_config(
        HealthCheckConfig(custom_check=lambda _p: True)
    )