"""File, URL, and scrape fetchers, including every list format and bad-line policy."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from omniproxy.enum import IoInvalidLinePolicy, ProxyProtocol
from omniproxy.fetchers import FileFetcher, ScrapeFetcher, URLFetcher, UrlListFormat
from omniproxy.fetchers.url_fetcher import parse_proxy_urls_from_payload
from omniproxy.refresh import fetch_from_fetchers

from _support import title

PLAIN = b"10.0.0.1:8080\nsocks5://10.0.0.2:1080\n"
JSON_SHAPES = {
    "strings": b'["10.1.0.1:1", "10.1.0.2:2"]',
    "proxy": b'[{"proxy": "10.2.0.1:8080"}]',
    "url": b'[{"url": "socks5://10.2.0.2:1080"}]',
    "host_port": b'[{"host": "10.3.0.1", "port": 8080}]',
    "ip_port": b'[{"ip": "10.3.0.2", "port": "9090"}]',
    "address_port": b'[{"address": "10.3.0.3", "port": 7000}]',
    "wrapped": b'{"data": ["10.4.0.1:1"]}',
}


def url_formats() -> None:
    title("UrlListFormat x payload shape")
    payloads = {"plain": PLAIN, **JSON_SHAPES, "invalid-json": b"not-json"}
    for fmt in UrlListFormat:
        for name, payload in payloads.items():
            lines = parse_proxy_urls_from_payload(payload, fmt=fmt)
            print(f"{fmt.value:5} {name:14} {lines}")

    title("URLFetcher protocol prefix (constructed, not downloaded)")
    for protocol in (None, *ProxyProtocol):
        for fmt in UrlListFormat:
            fetcher = URLFetcher(
                "https://example.com/proxies.txt",
                body_format=fmt,
                protocol=None if protocol is None else protocol.value,
                headers={"Accept": "text/plain"},
                timeout=5.0,
                text_encoding="utf-8",
            )
            print("built", fmt.value, getattr(protocol, "value", None), type(fetcher).__name__)


async def file_fetchers(directory: Path) -> None:
    title("FileFetcher on_invalid")
    path = directory / "list.txt"
    path.write_text("10.8.0.1:8000\nbad line\n10.8.0.2:8001\n", encoding="utf-8")
    for policy in IoInvalidLinePolicy:
        fetcher = FileFetcher(path, on_invalid=policy, encoding="utf-8")
        try:
            found = await fetcher.fetch()
            print(policy.value, [str(item) for item in found])
        except ValueError as exc:
            print(policy.value, "raised", exc)

    mixed = await fetch_from_fetchers(
        [FileFetcher(path, on_invalid=IoInvalidLinePolicy.SKIP)],
        timeout=5.0,
    )
    print("fetch_from_fetchers", len(mixed))


def scrape_shapes() -> None:
    title("ScrapeFetcher constructors")
    constructors = (
        ScrapeFetcher("https://example.com/"),
        ScrapeFetcher("https://example.com/", regex=re.compile(r"\d+\.\d+\.\d+\.\d+:\d+")),
        ScrapeFetcher(
            "https://example.com/",
            css_selectors=["td"],
            attribute="data-proxy",
            timeout=5.0,
            headers={"Accept": "text/html"},
        ),
        ScrapeFetcher(
            "https://example.com/",
            custom_extractor=lambda body: ["10.9.0.1:8000"] if b"example" in body.lower() else [],
        ),
    )
    for fetcher in constructors:
        print("scrape", type(fetcher).__name__, fetcher)


def main() -> None:
    url_formats()
    directory = Path(__file__).resolve().parent / "_fetcher_demo"
    directory.mkdir(exist_ok=True)
    try:
        asyncio.run(file_fetchers(directory))
    finally:
        for path in directory.glob("*"):
            path.unlink(missing_ok=True)
        directory.rmdir()
    scrape_shapes()


if __name__ == "__main__":
    main()
