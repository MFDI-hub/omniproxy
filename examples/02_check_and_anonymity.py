"""Sync/async health checks, IP-API info, bulk checking, and anonymity probe."""

from __future__ import annotations

import asyncio

from omniproxy import Proxy, check_proxies


def main() -> None:
    p = Proxy("http://user:pass@192.168.1.1:8080")

    is_alive = p.check(timeout=5.0)
    print("check:", is_alive)

    info = p.get_info(detect_anonymity=True, timeout=5.0)
    print(f"latency: {p.latency}s, anonymity: {p.anonymity}")
    print(info)

    async def check_async() -> None:
        alive = await p.acheck(timeout=5.0)
        ainfo = await p.aget_info(detect_anonymity=True, timeout=5.0)
        print("async:", alive, ainfo)

    asyncio.run(check_async())

    proxies_to_test = ["1.1.1.1:80", "2.2.2.2:80"]
    good, bad = check_proxies(proxies_to_test, timeout=10.0)
    print(f"{len(good)} proxies are working, {len(bad)} failed.")


if __name__ == "__main__":
    main()
