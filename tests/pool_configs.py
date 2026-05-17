"""Named :class:`~omniproxy.config.PoolConfig` factories for tests.

Used by ``conftest`` fixtures and imported directly where a callable hook
or other dynamic piece prevents a fixture.
"""

from __future__ import annotations

from collections.abc import Callable

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
    ScoringConfig,
    SessionConfig,
    WarmupConfig,
)
from omniproxy.enum import (
    FilterMissingMetadata,
    PoolStrategy,
    SessionCooldownPolicy,
    WarmupFailurePolicy,
)

# ----------------------------------------------------------------------
# Minimal bases for isolated pool feature tests (no circuit / scoring noise)
# ----------------------------------------------------------------------


def extended_random_strategy_only_config() -> PoolConfig:
    """RANDOM strategy; no health, circuit breaker, or scoring."""
    return PoolConfig(strategy=PoolStrategy.RANDOM, health_check=None, circuit_breaker=None, scoring=None)


def extended_warmup_min_ready_health_ok_config() -> PoolConfig:
    return PoolConfig(
        health_check=HealthCheckConfig(custom_check=lambda _p: True),
        warmup=WarmupConfig(
            enabled=True,
            min_ready=1,
            timeout=5.0,
            failure_policy=WarmupFailurePolicy.RAISE,
        ),
        circuit_breaker=None,
        scoring=None,
    )


def extended_warmup_timeout_impossible_raise_config() -> PoolConfig:
    """Fails WarmupFailedError: probes never succeed; deadline very short."""
    return PoolConfig(
        health_check=HealthCheckConfig(custom_check=lambda _p: False),
        warmup=WarmupConfig(
            enabled=True,
            min_ready=99,
            timeout=0.15,
            failure_policy=WarmupFailurePolicy.RAISE,
        ),
        circuit_breaker=None,
        scoring=None,
    )


def extended_warmup_partial_unmet_config(*, acquire_timeout: float = 0.35) -> PoolConfig:
    return PoolConfig(
        health_check=HealthCheckConfig(custom_check=lambda _p: False),
        warmup=WarmupConfig(
            enabled=True,
            min_ready=5,
            timeout=0.08,
            failure_policy=WarmupFailurePolicy.PARTIAL,
        ),
        acquire_timeout=acquire_timeout,
        circuit_breaker=None,
        scoring=None,
    )


def extended_refresh_via_async_snapshot_config(
    refill_proxies: list[Proxy],
    *,
    acquire_timeout: float = 0.0,
) -> PoolConfig:
    """Hold a stable async refresh batch (copied once at factory time)."""
    batch = list(refill_proxies)

    async def _reload() -> list[Proxy]:
        return list(batch)

    return PoolConfig(
        health_check=None,
        circuit_breaker=None,
        scoring=None,
        acquire_timeout=acquire_timeout,
        refresh=RefreshConfig(async_callback=_reload),
    )


def extended_refresh_via_sync_snapshot_config(
    refill_proxies: list[Proxy],
    *,
    acquire_timeout: float = 0.0,
) -> PoolConfig:
    batch = list(refill_proxies)

    def _reload() -> list[Proxy]:
        return list(batch)

    return PoolConfig(
        health_check=None,
        circuit_breaker=None,
        scoring=None,
        acquire_timeout=acquire_timeout,
        refresh=RefreshConfig(sync_callback=_reload),
    )


_EXTENDED_LONG_CD = CooldownConfig(
    base=3600.0, min=30.0, max=7200.0, adaptive=False, failure_threshold=99
)


def extended_single_proxy_connection_wait_config(*, acquire_timeout: float = 1.0) -> PoolConfig:
    """One connection per URL; callers hold one lease while others block then succeed."""
    return PoolConfig(
        strategy=PoolStrategy.ROUND_ROBIN,
        health_check=None,
        circuit_breaker=None,
        scoring=None,
        limits=LimitsConfig(max_connections_per_proxy=1),
        cooldown=_EXTENDED_LONG_CD,
        acquire_timeout=acquire_timeout,
        wait_fallback_interval=0.03,
    )


def extended_rotate_on_acquire_config(
    *,
    acquire_timeout: float = 5.0,
) -> PoolConfig:
    """Enables rotate_on_acquire; tests usually patch Proxy.arotate."""
    return PoolConfig(
        rotate_on_acquire=True,
        health_check=None,
        circuit_breaker=None,
        scoring=None,
        limits=LimitsConfig(max_connections_per_proxy=5),
        acquire_timeout=acquire_timeout,
    )


def extended_lifecycle_hooks_config(hooks: LifecycleHooks, *, acquire_timeout: float = 5.0) -> PoolConfig:
    return PoolConfig(
        cooldown=CooldownConfig(base=3600.0, min=0.05, max=7200.0, adaptive=False, failure_threshold=99),
        limits=LimitsConfig(max_connections_per_proxy=5),
        hooks=hooks,
        health_check=None,
        circuit_breaker=None,
        scoring=None,
        acquire_timeout=acquire_timeout,
    )


def extended_dead_letter_retry_config(
    health_check: HealthCheckConfig,
    *,
    retry_interval_seconds: float = 0.05,
) -> PoolConfig:
    return PoolConfig(
        health_check=health_check,
        dead_letter=DeadLetterConfig(enabled=True, retry_interval_seconds=retry_interval_seconds),
        circuit_breaker=None,
        scoring=None,
        limits=LimitsConfig(max_connections_per_proxy=5),
        acquire_timeout=2.0,
    )


def extended_metrics_exporter_only_config(metrics_exporter: object, *, drain_timeout: float = 0.0) -> PoolConfig:
    return PoolConfig(
        metrics_exporter=metrics_exporter,
        health_check=None,
        circuit_breaker=None,
        scoring=None,
        drain_timeout=drain_timeout,
    )


def extended_quick_close_only_config(*, drain_timeout: float = 0.0) -> PoolConfig:
    """Tiny pool skeleton for close / teardown tests."""
    return PoolConfig(
        health_check=None,
        circuit_breaker=None,
        scoring=None,
        drain_timeout=drain_timeout,
        limits=LimitsConfig(max_connections_per_proxy=3),
        acquire_timeout=1.0,
    )


def extended_quick_close_acquire_zero_config(*, drain_timeout: float = 0.0) -> PoolConfig:
    """Same as :func:`extended_quick_close_only_config` but ``acquire_timeout=0`` for on-demand refresh tests."""
    return extended_quick_close_only_config(drain_timeout=drain_timeout).model_copy(
        update={"acquire_timeout": 0.0}
    )


def minimal_round_robin_config() -> PoolConfig:
    return PoolConfig(strategy=PoolStrategy.ROUND_ROBIN)


def round_robin_skip_missing_metadata_config() -> PoolConfig:
    return PoolConfig(
        strategy=PoolStrategy.ROUND_ROBIN,
        filter_missing_metadata=FilterMissingMetadata.SKIP,
    )


def cooldown_half_base_strategy(base: float, active: int, total: int) -> float:
    return base * 0.5


def custom_half_base_cooldown_pool_config(
    *,
    cooldown: CooldownConfig | None = None,
) -> PoolConfig:
    cd = cooldown or CooldownConfig(
        base=10.0,
        min=0.1,
        max=3600.0,
        adaptive=False,
        failure_threshold=1,
        strategy=cooldown_half_base_strategy,
    )
    return PoolConfig(cooldown=cd)


def weighted_scoring_from_default(base: PoolConfig) -> PoolConfig:
    return base.model_copy(
        update={
            "strategy": PoolStrategy.WEIGHTED,
            "scoring": ScoringConfig(window_seconds=60.0, min_samples=2),
        }
    )


def lowest_latency_from_default(base: PoolConfig) -> PoolConfig:
    return base.model_copy(update={"strategy": PoolStrategy.LOWEST_LATENCY})


def session_rebind_ttl60_from_default(base: PoolConfig) -> PoolConfig:
    return base.model_copy(
        update={"session": SessionConfig(ttl=60.0, cooldown_policy=SessionCooldownPolicy.REBIND)}
    )


_STRICT_CD1 = CooldownConfig(
    base=10.0, min=0.1, max=3600.0, adaptive=False, failure_threshold=1
)


def session_block_strict_cooldown_from_default(base: PoolConfig) -> PoolConfig:
    return base.model_copy(
        update={
            "session": SessionConfig(ttl=60.0, cooldown_policy=SessionCooldownPolicy.BLOCK),
            "cooldown": _STRICT_CD1,
        }
    )


def session_raise_strict_cooldown_from_default(base: PoolConfig) -> PoolConfig:
    return base.model_copy(
        update={
            "session": SessionConfig(ttl=60.0, cooldown_policy=SessionCooldownPolicy.RAISE),
            "cooldown": _STRICT_CD1,
        }
    )


def circuit_half_open_quick_from_default(base: PoolConfig) -> PoolConfig:
    return base.model_copy(
        update={
            "circuit_breaker": CircuitBreakerConfig(
                failure_ratio=0.5,
                half_open_timeout=0.5,
                window_seconds=60.0,
                min_throughput=2,
            )
        }
    )


def async_strict_cooldown_fail1_from_default(base: PoolConfig) -> PoolConfig:
    return base.model_copy(update={"cooldown": _STRICT_CD1})


_MISSING_COVERAGE_STABLE_CD = CooldownConfig(
    base=600.0, min=60.0, max=1800.0, adaptive=False, failure_threshold=99
)


def weighted_deterministic_bias_config() -> PoolConfig:
    return PoolConfig(
        strategy=PoolStrategy.WEIGHTED,
        scoring=ScoringConfig(window_seconds=60.0, min_samples=2),
        cooldown=_MISSING_COVERAGE_STABLE_CD,
        limits=LimitsConfig(max_connections_per_proxy=10),
    )


def lowest_latency_stable_cooldown_config() -> PoolConfig:
    return PoolConfig(
        strategy=PoolStrategy.LOWEST_LATENCY,
        scoring=ScoringConfig(window_seconds=60.0),
        cooldown=_MISSING_COVERAGE_STABLE_CD,
    )


_MONOTONIC_CUSTOM_CD = CooldownConfig(
    base=10.0,
    min=0.1,
    max=3600.0,
    adaptive=False,
    failure_threshold=1,
    strategy=cooldown_half_base_strategy,
)


def adaptive_custom_cooldown_monotonic_timing_config() -> PoolConfig:
    """Same cooldown strategy as ``custom_half_base_cooldown_pool_config``; explicit for timing test."""
    return PoolConfig(cooldown=_MONOTONIC_CUSTOM_CD)


_SESSION_CD = CooldownConfig(
    base=99.0, min=1.0, max=999.0, adaptive=False, failure_threshold=1
)


def session_block_pool_exhausted_config() -> PoolConfig:
    return PoolConfig(
        cooldown=_SESSION_CD,
        session=SessionConfig(ttl=60.0, cooldown_policy=SessionCooldownPolicy.BLOCK),
    )


def session_raise_session_broken_config() -> PoolConfig:
    return PoolConfig(
        cooldown=_SESSION_CD,
        session=SessionConfig(ttl=60.0, cooldown_policy=SessionCooldownPolicy.RAISE),
    )


def pool_with_on_exhausted(
    on_exhausted: Callable[[], None],
    *,
    cooldown: CooldownConfig | None = None,
) -> PoolConfig:
    cd = cooldown or CooldownConfig(
        base=99.0, min=1.0, max=999.0, adaptive=False, failure_threshold=1
    )
    return PoolConfig(
        cooldown=cd,
        hooks=LifecycleHooks(on_exhausted=on_exhausted),
    )


_STRESS_CD = CooldownConfig(
    base=3600.0, min=30.0, max=7200.0, adaptive=False, failure_threshold=99
)


def sync_burst_connection_limits_config() -> PoolConfig:
    return PoolConfig(
        limits=LimitsConfig(max_connections_per_proxy=5),
        cooldown=_STRESS_CD,
        acquire_timeout=5.0,
    )


def async_task_contention_limits_config() -> PoolConfig:
    return PoolConfig(
        limits=LimitsConfig(max_connections_per_proxy=10),
        cooldown=_STRESS_CD,
        acquire_timeout=5.0,
    )


_COOLDOWN_HOOK_COMPREHENSIVE = CooldownConfig(
    base=50.0, min=1.0, max=99.0, adaptive=False, failure_threshold=1
)


def pool_with_exhausted_hook_comprehensive(on_exhausted: Callable[[], None]) -> PoolConfig:
    return PoolConfig(
        cooldown=_COOLDOWN_HOOK_COMPREHENSIVE,
        hooks=LifecycleHooks(on_exhausted=on_exhausted),
    )


def comprehensive_filter_missing_raise_config() -> PoolConfig:
    return PoolConfig(
        strategy=PoolStrategy.ROUND_ROBIN,
        filter_missing_metadata=FilterMissingMetadata.RAISE,
    )


def comprehensive_session_rebind_120_config() -> PoolConfig:
    return PoolConfig(
        strategy=PoolStrategy.ROUND_ROBIN,
        session=SessionConfig(ttl=120.0, cooldown_policy=SessionCooldownPolicy.REBIND),
    )


def comprehensive_session_raise_cooling_config() -> PoolConfig:
    return PoolConfig(
        strategy=PoolStrategy.ROUND_ROBIN,
        cooldown=_SESSION_CD,
        session=SessionConfig(ttl=120.0, cooldown_policy=SessionCooldownPolicy.RAISE),
    )


_CB_STRICT = CooldownConfig(
    base=600.0, min=60.0, max=1800.0, adaptive=False, failure_threshold=5
)


def comprehensive_circuit_breaker_acquire_block_config() -> PoolConfig:
    return PoolConfig(
        strategy=PoolStrategy.ROUND_ROBIN,
        cooldown=_CB_STRICT,
        scoring=ScoringConfig(),
        circuit_breaker=CircuitBreakerConfig(
            window_seconds=60.0,
            failure_ratio=0.45,
            half_open_timeout=0.5,
            min_throughput=2,
        ),
    )


def offline_quick_cooldown_recovery_config(offline: PoolConfig) -> PoolConfig:
    return offline.model_copy(
        update={
            "cooldown": CooldownConfig(
                base=0.05, min=0.01, max=3600.0, adaptive=False, failure_threshold=1
            ),
            "circuit_breaker": None,
        }
    )


def offline_circuit_half_open_recover_config(offline: PoolConfig) -> PoolConfig:
    return offline.model_copy(
        update={
            "circuit_breaker": CircuitBreakerConfig(
                failure_ratio=0.5,
                half_open_timeout=0.5,
                window_seconds=60.0,
                min_throughput=2,
            )
        }
    )


def offline_session_stickiness_rebind_config(offline: PoolConfig) -> PoolConfig:
    return offline.model_copy(
        update={
            "session": SessionConfig(ttl=60.0, cooldown_policy=SessionCooldownPolicy.REBIND)
        }
    )


def discord_pool_without_circuit_config(discord: PoolConfig) -> PoolConfig:
    return discord.model_copy(update={"circuit_breaker": None})


def discord_integration_strict_circuit_config(discord: PoolConfig) -> PoolConfig:
    return discord.model_copy(
        update={
            "circuit_breaker": CircuitBreakerConfig(
                failure_ratio=0.45,
                half_open_timeout=5.0,
                window_seconds=60.0,
                min_throughput=2,
            )
        }
    )
