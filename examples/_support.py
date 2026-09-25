"""Helpers shared by the example scripts."""

from __future__ import annotations

import logging
from typing import Any

from omniproxy import Proxy, apply_check_result_metadata
from omniproxy.config import CooldownConfig, PoolConfig, ScoringConfig, WarmupConfig
from omniproxy.enum import PoolStrategy

logging.getLogger("omniproxy").setLevel(logging.WARNING)

SEEDS = [f"10.0.0.{index}:8000" for index in range(1, 9)]


def title(text: str) -> None:
    print(f"\n== {text}")


def meta(raw: str, **fields: Any) -> Proxy:
    """Build a proxy and attach metadata the pool filters can see."""
    proxy = Proxy(raw)
    geo_keys = ("latency", "anonymity", "country", "city", "asn", "org")
    if any(key in fields for key in geo_keys):
        apply_check_result_metadata(
            proxy,
            latency=fields.get("latency"),
            anonymity=fields.get("anonymity"),
            country=fields.get("country"),
            city=fields.get("city"),
            asn=fields.get("asn"),
            org=fields.get("org"),
        )
    if "tags" in fields and fields["tags"] is not None:
        proxy._set_attribute("tags", tuple(fields["tags"]))
    return proxy


def offline_config(**updates: Any) -> PoolConfig:
    """Pool config that acquires immediately and does not probe the network."""
    config = PoolConfig(
        strategy=PoolStrategy.ROUND_ROBIN,
        scoring=ScoringConfig(min_samples=1),
        cooldown=CooldownConfig(
            base=30.0,
            min=1.0,
            max=60.0,
            adaptive=False,
            failure_threshold=1,
        ),
        acquire_timeout=0.0,
        warmup=WarmupConfig(enabled=False),
        log_level=logging.ERROR,
    )
    if updates:
        return config.model_copy(update=updates)
    return config
