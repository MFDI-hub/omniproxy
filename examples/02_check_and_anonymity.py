"""Health checks: sync and async, every backend, anonymity, and bulk options."""

from __future__ import annotations

import asyncio

from omniproxy import (
    CheckResult,
    Proxy,
    acheck_proxies,
    acheck_proxy,
    apply_check_result_metadata,
    check_proxies,
    check_proxy,
)
from omniproxy.backends.factory import get_backend, supported_backends
from omniproxy.enum import AnonymityLeakHeader, AnonymityTier, HttpBackend

from _support import title

TARGET = "127.0.0.1:1"
LIVE_TIMEOUT = 2.0


def _installed_backends() -> list[str]:
    ready: list[str] = []
    for name in supported_backends():
        try:
            get_backend(name)
        except ImportError as exc:
            print(f"backend {name}: not installed ({exc})")
            continue
        ready.append(name)
    return ready


def offline() -> None:
    title("anonymity tiers and leak headers")
    for tier in AnonymityTier:
        proxy = Proxy("10.0.0.9:8000")
        apply_check_result_metadata(proxy, latency=0.4, anonymity=tier.value)
        print(tier.value, proxy.anonymity, proxy.latency)
    print("leak headers:", [header.value for header in AnonymityLeakHeader])

    title("CheckResult")
    ok = CheckResult(success=True, latency=0.2, exc_type=None, status_code=200)
    bad = CheckResult(success=False, latency=None, exc_type=TimeoutError, status_code=None, error="timeout")
    print("truthy", bool(ok), "falsy", bool(bad), "error", bad.error)


def live(backends: list[str]) -> None:
    title("single check permutations")
    proxy = Proxy(TARGET)
    primary = backends[0]
    flag_sets = (
        {},
        {"max_retries": 1, "retry_backoff": 0.0, "retry_on_status": frozenset({502, 503, 504})},
        {"expected_status": 204},
        {"expected_fields": {"ip"}},
        {"raise_on_error": False},
    )
    for extra in flag_sets:
        try:
            _checked, result = check_proxy(proxy, backend=primary, timeout=LIVE_TIMEOUT, **extra)
            print("sync", primary, extra or "{}", bool(result))
        except Exception as exc:
            print("sync", primary, extra or "{}", type(exc).__name__, exc)
    for backend in backends[1:]:
        try:
            _checked, result = check_proxy(proxy, backend=backend, timeout=LIVE_TIMEOUT)
            print("sync", backend, bool(result))
        except Exception as exc:
            print("sync", backend, type(exc).__name__, exc)

    async def async_checks() -> None:
        for backend in backends:
            try:
                _checked, result = await acheck_proxy(proxy, backend=backend, timeout=LIVE_TIMEOUT)
                print("async", backend, bool(result))
            except Exception as exc:
                print("async", backend, type(exc).__name__, exc)

    asyncio.run(async_checks())

    title("bulk check permutations")
    batch = [TARGET, "10.255.255.1:9"]
    for use_async in (False, True):
        try:
            good, bad = check_proxies(
                batch,
                backend=primary,
                timeout=LIVE_TIMEOUT,
                detect_anonymity=False,
                use_async=use_async,
                max_retries=0,
            )
            print(f"bulk use_async={use_async} good={len(good)} bad={len(bad)}")
        except Exception as exc:
            print("bulk", use_async, type(exc).__name__, exc)

    async def bulk_async() -> None:
        try:
            good, bad = await acheck_proxies(batch, backend=primary, timeout=LIVE_TIMEOUT, max_concurrent=2)
            print(f"acheck_proxies good={len(good)} bad={len(bad)}")
        except Exception as exc:
            print("acheck_proxies", type(exc).__name__, exc)

    asyncio.run(bulk_async())

    title("instance helpers")
    try:
        print("p.check", bool(proxy.check(timeout=LIVE_TIMEOUT, backend=primary)))
    except Exception as exc:
        print("p.check", type(exc).__name__, exc)
    try:
        print("p.get_info", proxy.get_info(timeout=LIVE_TIMEOUT, backend=primary))
    except Exception as exc:
        print("p.get_info", type(exc).__name__, exc)


def main() -> None:
    print("HttpBackend members:", [item.value for item in HttpBackend])
    offline()
    backends = _installed_backends()
    if backends:
        live(backends)
    else:
        print("no HTTP backend installed; skipped live checks")


if __name__ == "__main__":
    main()
