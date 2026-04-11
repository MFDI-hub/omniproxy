"""Rotation / refresh URL (common for mobile or API-triggered IP changes)."""

from __future__ import annotations

import asyncio

from omniproxy import Proxy


def main() -> None:
    p = Proxy("http://user:pass@192.168.1.1:8080[https://api.proxyservice.com/refresh?key=123]")
    try:
        success = p.rotate()
        if success:
            print("Mobile proxy IP rotated successfully!")
        else:
            print("rotate returned False (non-200 or unreachable).")
    except Exception as exc:
        print("rotate:", exc)

    async def arun() -> None:
        try:
            ok = await p.arotate()
            print("arotate:", ok)
        except Exception as exc:
            print("arotate:", exc)

    asyncio.run(arun())


if __name__ == "__main__":
    main()
