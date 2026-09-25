"""Sticky sessions for every cooldown policy: stick, rebind, block, and raise."""

from __future__ import annotations

from omniproxy import Proxy
from omniproxy.config import CooldownConfig, SessionConfig
from omniproxy.enum import SessionCooldownPolicy
from omniproxy.errors import SessionBrokenError
from omniproxy.pool import SyncProxyPool

from _support import SEEDS, offline_config, title


def _pool(policy: SessionCooldownPolicy, proxies: list[Proxy], *, threshold: int = 1) -> SyncProxyPool:
    return SyncProxyPool(
        offline_config(
            session=SessionConfig(ttl=60.0, cooldown_policy=policy),
            cooldown=CooldownConfig(
                base=30.0,
                min=5.0,
                max=60.0,
                adaptive=False,
                failure_threshold=threshold,
            ),
        ),
        proxies,
    )


def stickiness() -> None:
    title("session stays on the same proxy while it is healthy")
    for policy in SessionCooldownPolicy:
        pool = _pool(policy, [Proxy(SEEDS[0]), Proxy(SEEDS[1])])
        try:
            first = pool.acquire(session_key="user-1")
            pool.release(first)
            second = pool.acquire(session_key="user-1")
            pool.release(second)
            other = pool.acquire(session_key="user-2")
            pool.release(other)
            print(policy.value, "same", first.url == second.url, "other", other.url)
        finally:
            pool.close()


def after_failure() -> None:
    title("session cooldown policy after mark_failed")
    proxies = [Proxy(SEEDS[0]), Proxy(SEEDS[1])]
    for policy in SessionCooldownPolicy:
        pool = _pool(policy, proxies)
        try:
            first = pool.acquire(session_key="sticky")
            pool.mark_failed(first, TimeoutError)
            try:
                second = pool.acquire(session_key="sticky")
                print(policy.value, "rebound" if second.url != first.url else "same", second.url)
                pool.release(second)
            except SessionBrokenError as exc:
                print(policy.value, "SessionBrokenError", exc)
            except Exception as exc:
                print(policy.value, type(exc).__name__, exc)
        finally:
            pool.close()

    title("legacy session_id alias")
    pool = _pool(SessionCooldownPolicy.REBIND, proxies)
    try:
        first = pool.acquire(session_id="alias")
        pool.release(first)
        second = pool.acquire(session_key="alias")
        print("alias match", first.url == second.url)
        pool.release(second)
    finally:
        pool.close()


def main() -> None:
    stickiness()
    after_failure()


if __name__ == "__main__":
    main()
