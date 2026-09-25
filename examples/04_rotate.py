"""Rotation URLs: GET and POST, sync and async, every backend, and pool rotate flags."""

from __future__ import annotations

import asyncio
import warnings

from omniproxy import Proxy
from omniproxy.backends.factory import get_backend, supported_backends
from omniproxy.config import PoolConfig
from omniproxy.enum import HttpVerb

from _support import offline_config, title

ROTATE = "http://user:pass@10.0.0.1:8080[https://example.com/]"


def _backends() -> list[str | None]:
    names: list[str | None] = [None]
    for name in supported_backends():
        try:
            get_backend(name)
        except ImportError:
            print("skip backend", name)
            continue
        names.append(name)
    return names


def offline_flags() -> None:
    title("missing rotation_url")
    bare = Proxy("10.0.0.1:8080")
    try:
        bare.rotate()
    except ValueError as exc:
        print("rotate", exc)

    async def missing_async() -> None:
        try:
            await bare.arotate(method=HttpVerb.POST)
        except ValueError as exc:
            print("arotate", exc)

    asyncio.run(missing_async())

    title("use_rotation_urls x rotate_on_acquire x rotate_on_failure")
    for use_urls in (False, True):
        for on_acquire in (False, True):
            for on_failure in (False, True):
                with warnings.catch_warnings(record=True) as caught:
                    warnings.simplefilter("always")
                    config = PoolConfig(
                        use_rotation_urls=use_urls,
                        rotate_on_acquire=on_acquire,
                        rotate_on_failure=on_failure,
                    )
                warning = caught[0].message if caught else ""
                print(
                    f"urls={use_urls} acquire={on_acquire} failure={on_failure} "
                    f"warning={warning}"
                )
                _ = config

    title("pool acquire does not call rotation when the proxy has no rotation_url")
    from omniproxy.pool import SyncProxyPool

    config = offline_config(rotate_on_acquire=True, rotate_on_failure=True, use_rotation_urls=True)
    pool = SyncProxyPool(config, [Proxy("10.0.0.4:8000")])
    try:
        proxy = pool.acquire()
        pool.mark_failed(proxy, TimeoutError)
        print("acquire+fail without rotation_url", proxy.url)
    finally:
        pool.close()


def live() -> None:
    title("rotate verb x backend")
    proxy = Proxy(ROTATE)
    backends = _backends()
    for method in HttpVerb:
        for backend in backends:
            try:
                ok = proxy.rotate(method=method, backend=backend, timeout=5.0)
                print("rotate", method.value, backend, ok)
            except Exception as exc:
                print("rotate", method.value, backend, type(exc).__name__, exc)

    async def async_rotate() -> None:
        for method in HttpVerb:
            try:
                ok = await proxy.arotate(method=method.value, timeout=5.0)
                print("arotate", method.value, ok)
            except Exception as exc:
                print("arotate", method.value, type(exc).__name__, exc)

    asyncio.run(async_rotate())


def main() -> None:
    offline_flags()
    live()


if __name__ == "__main__":
    main()
