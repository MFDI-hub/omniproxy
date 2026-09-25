"""httpx Client / AsyncClient plus a direct request through every installed backend."""

from __future__ import annotations

import asyncio

from omniproxy import Proxy
from omniproxy.backends.factory import get_backend, supported_backends
from omniproxy.client import AsyncClient, Client
from omniproxy.enum import HttpBackend

from _support import title

URL = "https://httpbin.org/ip"
TIMEOUT = 8.0


def main() -> None:
    proxy = Proxy("http://127.0.0.1:1")
    title("httpx wrappers")
    for label, opener in (
        ("get_client", lambda: proxy.get_client()),
        ("Client", lambda: Client(proxy=proxy)),
    ):
        try:
            with opener() as client:
                response = client.get(URL, timeout=TIMEOUT)
                print(label, response.status_code, response.text[:120])
        except Exception as exc:
            print(label, type(exc).__name__, exc)

    async def async_clients() -> None:
        for label, opener in (
            ("get_async_client", lambda: proxy.get_async_client()),
            ("AsyncClient", lambda: AsyncClient(proxy=proxy)),
        ):
            try:
                async with opener() as client:
                    response = await client.get(URL, timeout=TIMEOUT)
                    print(label, response.status_code, response.text[:120])
            except Exception as exc:
                print(label, type(exc).__name__, exc)

    asyncio.run(async_clients())

    title("backend.request through each HttpBackend")
    print("declared", [item.value for item in HttpBackend])
    print("supported", supported_backends())
    for name in supported_backends():
        try:
            backend = get_backend(name)
        except ImportError as exc:
            print(name, "missing", exc)
            continue
        try:
            response = backend.get(URL, proxy, timeout=TIMEOUT)
            print(name, response.status_code)
        except Exception as exc:
            print(name, type(exc).__name__, exc)


if __name__ == "__main__":
    main()
