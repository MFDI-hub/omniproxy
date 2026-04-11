"""ProxyPool: round-robin or random selection and cooldown after failures."""

from __future__ import annotations

from omniproxy import ProxyPool


def main() -> None:
    raw_proxies = ["10.0.0.1:8000", "10.0.0.2:8000", "10.0.0.3:8000"]
    pool = ProxyPool(raw_proxies, strategy="round_robin", cooldown=60.0)

    proxy1 = pool.get_next()
    try:
        raise OSError("simulated failure")
    except OSError:
        if proxy1 is not None:
            pool.mark_failed(proxy1)

    proxy2 = pool.get_next()
    pool.reset()
    print(proxy1, proxy2)
    print("pool size", len(pool))
    print("contains first", proxy1 in pool if proxy1 else None)


if __name__ == "__main__":
    main()
