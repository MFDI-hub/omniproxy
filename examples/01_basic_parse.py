"""Every proxy string shape, protocol, pattern, and structural property."""

from __future__ import annotations

import copy

from omniproxy import Proxy, ProxyPattern
from omniproxy.constants import DEFAULT_PROXY_PATTERN_STRING
from omniproxy.enum import ProxyProtocol
from omniproxy.utils import get_formatted_proxy_string

from _support import title

FORMATS = (
    "10.0.0.1:8080",
    "10.0.0.1:8080:user:pass",
    "user:pass@10.0.0.1:8080",
    "10.0.0.1:8080|user:pass",
    "http://user:pass@10.0.0.1:8080",
    "https://10.0.0.1:8443",
    "socks4://10.0.0.1:1080",
    "socks5://user:pass@10.0.0.1:1080",
    "socks5://10.0.0.1:1080[https://rotate.example/api]",
    "[2001:db8::1]:8080",
    "http://user:pass@[2001:db8::1]:8080",
    "login:password@210.173.88.77:3001[https://rotate.example/api?key=1]",
)

PATTERNS = (
    "protocol://username:password@ip:port",
    "ip:port",
    "ip:port:username:password",
    "protocol://ip:port",
    ProxyPattern("username:password@ip:port"),
)


def main() -> None:
    title("input formats")
    for raw in FORMATS:
        proxy = Proxy(raw)
        print(
            f"{raw!r:62} -> {proxy.url} "
            f"auth={proxy.has_auth} rot={proxy.rotation_url is not None}"
        )

    title("protocol override")
    base = "10.0.0.1:8080"
    for protocol in ProxyProtocol:
        print(protocol.value, Proxy(base, protocol=protocol.value).url)

    title("properties")
    proxy = Proxy("http://user:secret@10.0.0.1:8080[https://rotate.example/api]")
    print("url         ", proxy.url)
    print("safe_url    ", proxy.safe_url)
    print("server      ", proxy.server)
    print("address     ", proxy.address)
    print("host        ", proxy.host)
    print("ip/port     ", proxy.ip, proxy.port)
    print("login       ", proxy.login)
    print("protocol    ", proxy.protocol)
    print("version     ", proxy.version)
    print("playwright  ", proxy.playwright)
    print("requests    ", proxy.as_requests_proxies())
    print("dict        ", proxy.to_dict())
    print("json        ", proxy.to_json_string())
    print("bool/working", bool(proxy), proxy.is_working)
    print("validate    ", Proxy.validate("10.0.0.2:9090").url)
    print("repr        ", repr(proxy))

    ipv6 = Proxy("[2001:db8::1]:8080")
    print("ipv6 version", ipv6.version, ipv6.ip)

    title("patterns")
    original = Proxy.default_pattern
    try:
        sample = Proxy("http://user:pass@10.0.0.1:8080")
        for pattern in PATTERNS:
            Proxy.set_default_pattern(pattern)
            rendered = Proxy("http://user:pass@10.0.0.1:8080")
            formatted = get_formatted_proxy_string(sample, pattern)
            print(f"pattern={pattern!s:42} new={rendered.url} format={formatted}")
    finally:
        Proxy.set_default_pattern(original or DEFAULT_PROXY_PATTERN_STRING)

    title("ordering, equality, copy")
    from omniproxy import apply_check_result_metadata

    slow = Proxy("10.0.0.1:8000")
    fast = Proxy("10.0.0.2:8000")
    apply_check_result_metadata(slow, latency=2.0, anonymity="transparent")
    apply_check_result_metadata(fast, latency=0.2, anonymity="elite")
    print("fast < slow", fast < slow)
    print("equal self ", fast == Proxy(fast.url))
    print("hash stable", hash(fast) == hash(Proxy(fast.url)))
    cloned = copy.copy(fast)
    deep = copy.deepcopy(fast)
    print("copy latency", cloned.latency, deep.anonymity)

    try:
        fast.port = 1  # type: ignore[misc]
    except AttributeError as exc:
        print("frozen slot ", exc)


if __name__ == "__main__":
    main()
