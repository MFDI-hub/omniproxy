"""Using Proxy with httpx-based Client / AsyncClient and factory helpers."""

from __future__ import annotations

import asyncio

from omniproxy import AsyncClient, Client, Proxy


def main() -> None:
    p = Proxy("http://user:pass@192.168.1.1:8080")

    try:
        with p.get_client() as client:
            resp = client.get("https://httpbin.org/ip", timeout=10.0)
            print(resp.json())
    except Exception as exc:
        print("get_client:", exc)

    try:
        with Client(proxy=p) as client:
            resp = client.get("https://httpbin.org/ip", timeout=10.0)
            print(resp.json())
    except Exception as exc:
        print("Client(proxy=p):", exc)

    async def fetch_data() -> None:
        try:
            async with p.get_async_client() as client:
                resp = await client.get("https://httpbin.org/ip", timeout=10.0)
                print(resp.json())
        except Exception as exc:
            print("get_async_client:", exc)
        try:
            async with AsyncClient(proxy=p) as client:
                resp = await client.get("https://httpbin.org/ip", timeout=10.0)
                print(resp.json())
        except Exception as exc:
            print("AsyncClient(proxy=p):", exc)

    asyncio.run(fetch_data())


if __name__ == "__main__":
    main()
