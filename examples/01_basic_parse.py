"""Basic proxy parsing, properties, requests-shaped proxies map, and default pattern."""

from __future__ import annotations

from omniproxy import Proxy


def main() -> None:
    p1 = Proxy("http://user:pass@192.168.1.1:8080")
    p2 = Proxy("192.168.1.1:8080:user:pass")
    p3 = Proxy("socks5://10.0.0.1:1080[https://rotate.proxy.com/api]")

    print(p1.url)
    print(p1.address, p1.port, p1.protocol)
    print(p1.as_requests_proxies())
    print(p1.to_dict())
    print(p1.playwright)

    Proxy.set_default_pattern("ip:port:username:password")
    print(p1.url)

    _ = (p2, p3)


if __name__ == "__main__":
    main()
