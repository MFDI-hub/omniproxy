#!/usr/bin/env python3
"""Compare a full manual proxy check with the pool warmup and acquire path.

Both paths use the same fetched sample, check URL, timeout, and concurrency.
The manual path waits for every proxy. The pool path matches
``examples/07_default_fetchers.py``: warmup stops at ``min_ready`` (default 1)
and ``acquire()`` returns one proxy.

The two passes run one after the other on copies of the sample, so a proxy
can pass one pass and fail the other.

Usage:
  uv run python scripts/compare_check_vs_pool.py
  uv run python scripts/compare_check_vs_pool.py --sample 200 --max-latency 3
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time
from collections.abc import Sequence

from omniproxy import Proxy, acheck_proxies, settings
from omniproxy.config import (
    HealthCheckConfig,
    LifecycleHooks,
    PoolConfig,
    ScoringConfig,
    WarmupConfig,
)
from omniproxy.enum import PoolStrategy, WarmupFailurePolicy
from omniproxy.errors import NoMatchingProxy, PoolExhausted, PoolSaturated
from omniproxy.fetchers import default_fetchers
from omniproxy.pool import AsyncProxyPool
from omniproxy.refresh import fetch_from_fetchers

_CHECK_URL = settings.default_check_urls[0]


def _copies(proxies: Sequence[Proxy]) -> list[Proxy]:
    return [Proxy(proxy.url) for proxy in proxies]


def _fast(proxies: Sequence[Proxy], max_latency: float) -> list[Proxy]:
    matched = [
        proxy
        for proxy in proxies
        if proxy.last_status is True
        and proxy.latency is not None
        and proxy.latency <= max_latency
    ]
    matched.sort(key=lambda proxy: proxy.latency if proxy.latency is not None else float("inf"))
    return matched


def _print_ranked(proxies: Sequence[Proxy], limit: int) -> None:
    shown = list(proxies)[:limit]
    if not shown:
        print("    (none)")
        return
    for proxy in shown:
        latency = proxy.latency
        label = f"{latency:.3f}s" if latency is not None else "n/a"
        print(f"    {proxy}  latency={label}")
    hidden = len(proxies) - len(shown)
    if hidden > 0:
        print(f"    ... {hidden} more")


async def _manual_check(
    sample: Sequence[Proxy],
    *,
    timeout: float,
    concurrency: int,
    max_latency: float,
    top: int,
) -> list[Proxy]:
    print()
    print("manual check (acheck_proxies, every proxy)")
    started = time.perf_counter()
    good, bad = await acheck_proxies(
        _copies(sample),
        url=_CHECK_URL,
        timeout=timeout,
        max_concurrent=concurrency,
    )
    elapsed = time.perf_counter() - started
    ok = list(good)
    fast = _fast(ok, max_latency)
    print(f"  elapsed: {elapsed:.1f}s")
    print(f"  checked: {len(sample)}")
    print(f"  http ok: {len(ok)}")
    print(f"  http fail: {len(bad)}")
    print(f"  fast (<= {max_latency:.1f}s): {len(fast)}")
    _print_ranked(fast, top)
    return fast


async def _pool_check(
    sample: Sequence[Proxy],
    *,
    timeout: float,
    concurrency: int,
    max_latency: float,
    min_ready: int,
    warmup_timeout: float,
    acquire_timeout: float,
    top: int,
) -> tuple[list[Proxy], Proxy | None]:
    print()
    print(f"pool check (warmup min_ready={min_ready}, then one acquire)")
    warmup_ready: list[int] = []

    def _on_warmup_completed(ready_count: int, _min_ready: int) -> None:
        warmup_ready.append(ready_count)

    config = PoolConfig(
        strategy=PoolStrategy.LOWEST_LATENCY,
        scoring=ScoringConfig(),
        max_latency=max_latency,
        health_check=HealthCheckConfig(
            url=_CHECK_URL,
            timeout=timeout,
            max_concurrent_checks=concurrency,
            max_latency=max_latency,
        ),
        warmup=WarmupConfig(
            enabled=True,
            min_ready=min_ready,
            timeout=warmup_timeout,
            failure_policy=WarmupFailurePolicy.PARTIAL,
        ),
        hooks=LifecycleHooks(on_warmup_completed=_on_warmup_completed),
        acquire_timeout=acquire_timeout,
        log_level=logging.ERROR,
    )
    started = time.perf_counter()
    acquired: Proxy | None = None
    async with AsyncProxyPool(config, initial_proxies=_copies(sample)) as pool:
        async with pool._state_lock:
            snapshot = list(pool._proxies)
        try:
            acquired = await pool.acquire()
        except (PoolExhausted, PoolSaturated, NoMatchingProxy) as exc:
            print(f"  acquire failed: {type(exc).__name__}: {exc}")
        else:
            await pool.release(acquired)
    elapsed = time.perf_counter() - started

    checked = [proxy for proxy in snapshot if proxy.last_status is not None]
    accepted = [proxy for proxy in snapshot if proxy.last_status is True]
    fast = _fast(accepted, max_latency)
    ready = warmup_ready[0] if warmup_ready else 0

    print(f"  elapsed: {elapsed:.1f}s")
    print(f"  warmup timeout: {warmup_timeout:.1f}s")
    print(f"  warmup ready_count: {ready}")
    print(f"  checked before acquire: {len(checked)}")
    print(f"  unchecked (cancelled or never started): {len(snapshot) - len(checked)}")
    print(f"  accepted by health check: {len(accepted)}")
    print(f"  fast (<= {max_latency:.1f}s): {len(fast)}")
    _print_ranked(fast, top)
    if acquired is None:
        print("  acquired: none")
    else:
        latency = acquired.latency
        label = f"{latency:.3f}s" if latency is not None else "n/a"
        print(f"  acquired: {acquired}  latency={label}")
    return fast, acquired


def _print_overlap(
    manual_fast: Sequence[Proxy],
    pool_fast: Sequence[Proxy],
    acquired: Proxy | None,
) -> None:
    manual_urls = {proxy.url for proxy in manual_fast}
    pool_urls = {proxy.url for proxy in pool_fast}
    print()
    print("overlap (same sample, sequential passes)")
    print(f"  in both fast sets: {len(manual_urls & pool_urls)}")
    print(f"  manual only: {len(manual_urls - pool_urls)}")
    print(f"  pool only: {len(pool_urls - manual_urls)}")
    if acquired is None:
        print("  acquired in manual fast set: n/a")
    else:
        print(f"  acquired in manual fast set: {acquired.url in manual_urls}")


async def _run(args: argparse.Namespace) -> None:
    logging.getLogger("omniproxy").setLevel(logging.ERROR)
    fetchers = default_fetchers()
    print("default sources:", len(fetchers))
    print("check url:", _CHECK_URL)
    print(
        f"sample cap: {args.sample}  timeout: {args.timeout:.1f}s  "
        f"concurrency: {args.concurrency}  max_latency: {args.max_latency:.1f}s"
    )

    try:
        proxies = await fetch_from_fetchers(fetchers, timeout=args.fetch_timeout)
    except Exception as exc:
        print("fetch (network-dependent):", exc)
        return

    sample = proxies[: args.sample]
    print(f"fetched proxies: {len(proxies)}")
    print(f"sample used: {len(sample)}")
    if not sample:
        print("no proxies to compare")
        return

    manual_fast = await _manual_check(
        sample,
        timeout=args.timeout,
        concurrency=args.concurrency,
        max_latency=args.max_latency,
        top=args.top,
    )
    pool_fast, acquired = await _pool_check(
        sample,
        timeout=args.timeout,
        concurrency=args.concurrency,
        max_latency=args.max_latency,
        min_ready=args.min_ready,
        warmup_timeout=args.warmup_timeout,
        acquire_timeout=args.acquire_timeout,
        top=args.top,
    )
    _print_overlap(manual_fast, pool_fast, acquired)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare acheck_proxies with the pool warmup/acquire path on one sample."
    )
    parser.add_argument("--sample", type=int, default=500, help="How many fetched proxies to check")
    parser.add_argument("--timeout", type=float, default=8.0, help="Per-proxy check timeout in seconds")
    parser.add_argument("--concurrency", type=int, default=32, help="In-flight checks for both paths")
    parser.add_argument("--max-latency", type=float, default=3.0, help="Latency cap in seconds")
    parser.add_argument("--min-ready", type=int, default=1, help="Pool warmup stops at this many successes")
    parser.add_argument("--warmup-timeout", type=float, default=60.0, help="Pool warmup deadline in seconds")
    parser.add_argument("--acquire-timeout", type=float, default=30.0, help="pool.acquire timeout in seconds")
    parser.add_argument("--fetch-timeout", type=float, default=15.0, help="Proxy-list fetch timeout in seconds")
    parser.add_argument("--top", type=int, default=10, help="How many fast proxies to print per path")
    args = parser.parse_args()
    if args.sample < 1:
        parser.error("--sample must be >= 1")
    if args.concurrency < 1:
        parser.error("--concurrency must be >= 1")
    if args.max_latency <= 0:
        parser.error("--max-latency must be > 0")
    if args.min_ready < 1:
        parser.error("--min-ready must be >= 1")
    if args.top < 0:
        parser.error("--top must be >= 0")
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
