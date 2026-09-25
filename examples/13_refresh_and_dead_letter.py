"""Refresh callbacks, fallbacks, size bounds, and both dead-letter stores."""

from __future__ import annotations

import asyncio

from omniproxy import Proxy
from omniproxy.config import DeadLetterConfig, HealthCheckConfig, LifecycleHooks, RefreshConfig
from omniproxy.enum import DeadLetterPersistence
from omniproxy.pool import AsyncProxyPool

from _support import SEEDS, offline_config, title


class MemoryStore:
    """In-process StateStore used by dead-letter persistence."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.data.get(key)

    def set(self, key: str, value: str, ttl: float | None = None) -> None:
        self.data[key] = value

    def delete(self, key: str) -> None:
        self.data.pop(key, None)


def _health() -> HealthCheckConfig:
    return HealthCheckConfig(custom_check=lambda _proxy: True)


async def callback_sources() -> None:
    title("refresh callback order")

    def sync_list() -> list[str]:
        return [SEEDS[0]]

    async def async_list() -> list[str]:
        return [SEEDS[1]]

    def broken() -> list[str]:
        raise RuntimeError("primary source down")

    def fallback_sync() -> list[str]:
        return [SEEDS[2]]

    async def fallback_async() -> list[Proxy]:
        return [Proxy(SEEDS[3])]

    cases = {
        "sync": RefreshConfig(sync_callback=sync_list, timeout=2.0, interval_seconds=30.0),
        "async": RefreshConfig(async_callback=async_list, timeout=2.0, interval_seconds=30.0),
        "fallback-sync": RefreshConfig(
            sync_callback=broken,
            fallback_sync_callbacks=[fallback_sync],
            timeout=2.0,
            interval_seconds=30.0,
        ),
        "fallback-async": RefreshConfig(
            async_callback=broken,
            fallback_async_callbacks=[fallback_async],
            timeout=2.0,
            interval_seconds=30.0,
        ),
    }
    for label, refresh in cases.items():
        config = offline_config(refresh=refresh, min_size=1)
        async with AsyncProxyPool(config, initial_proxies=[]) as pool:
            proxy = await pool.acquire()
            print(label, proxy.url)
            await pool.release(proxy)


async def dead_letter(persistence: DeadLetterPersistence, store: MemoryStore | None) -> None:
    events: list[str] = []

    def on_added(proxy: Proxy, reason: str | None) -> None:
        events.append(f"dead:{proxy.ip}:{reason}")

    def on_evict(proxy: Proxy, reason: str) -> None:
        events.append(f"evict:{proxy.ip}:{reason}")

    def on_refresh(added: int) -> None:
        events.append(f"refresh:{added}")

    async def source() -> list[str]:
        return [SEEDS[4], SEEDS[5], SEEDS[6]]

    config = offline_config(
        max_size=2,
        min_size=1,
        health_check=_health(),
        refresh=RefreshConfig(async_callback=source, timeout=2.0, interval_seconds=30.0),
        dead_letter=DeadLetterConfig(
            enabled=True,
            max_size=10,
            retry_interval_seconds=None,
            persistence=persistence,
        ),
        state_store_factory=None if store is None else (lambda: store),
        hooks=LifecycleHooks(
            on_dead_letter_added=on_added,
            on_auto_evicted=on_evict,
            on_refresh_completed=on_refresh,
        ),
    )
    async with AsyncProxyPool(config, initial_proxies=[]) as pool:
        proxy = await pool.acquire()
        print(persistence.value, "acquired", proxy.ip, "events", events)
        await pool.release(proxy)
    if store is not None:
        print("state store keys", list(store.data))


def main() -> None:
    asyncio.run(callback_sources())
    title("dead letter memory and state store")
    asyncio.run(dead_letter(DeadLetterPersistence.MEMORY, None))
    asyncio.run(dead_letter(DeadLetterPersistence.STATE_STORE, MemoryStore()))


if __name__ == "__main__":
    main()
