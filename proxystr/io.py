"""Load proxies from disk, scrape URLs, and export lists."""

from __future__ import annotations

import re
import ssl
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

from . import config

if TYPE_CHECKING:
    from .extended_proxy import Proxy

# Matches common proxy-like host:port patterns in HTML/text
_PROXY_LINE_PATTERN = re.compile(
    r"(?:(?:http|https|socks4|socks5)://)?"
    r"(?:[^\s:@]+:[^\s:@]+@)?"
    r"(?:\d{1,3}\.){3}\d{1,3}:\d{2,5}"
    r"|(?:[^\s:@]+:[^\s:@]+@)?[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}:\d{2,5}"
)


def read_proxies(filepath: str | Path) -> list[Proxy]:
    from .extended_proxy import Proxy

    path = Path(filepath)
    with path.open(encoding="utf-8", errors="replace") as f:
        return [Proxy(line.strip()) for line in f if line.strip()]


def save_proxies(
    filepath: str | Path,
    proxies: Iterable[str | Proxy],
    *,
    mode: str = "w",
) -> None:
    path = Path(filepath)
    lines = [str(p).strip() for p in proxies if str(p).strip()]
    with path.open(mode, encoding="utf-8") as f:
        f.write("\n".join(lines))
        if lines:
            f.write("\n")


def fetch_proxies(
    url: str,
    *,
    timeout: float | None = None,
    pattern: re.Pattern[str] | None = None,
    unique: bool = True,
) -> list[Proxy]:
    """Fetch a page over a direct connection and extract proxy-like strings."""
    from .extended_proxy import Proxy

    to = timeout if timeout is not None else config.default_timeout
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={"User-Agent": "proxystr/3"})
    with urllib.request.urlopen(req, timeout=to, context=ctx) as r:
        text = r.read().decode("utf-8", errors="replace")

    rx = pattern or _PROXY_LINE_PATTERN
    seen: set[str] = set()
    out: list[Proxy] = []
    for m in rx.finditer(text):
        raw = m.group(0).strip()
        if not raw or (unique and raw in seen):
            continue
        try:
            p = Proxy(raw)
        except ValueError:
            continue
        key = p.url
        if unique and key in seen:
            continue
        if unique:
            seen.add(key)
        out.append(p)
    return out
