"""Load proxies from disk, scrape URLs, and export lists."""

from __future__ import annotations

import re
import ssl
import urllib.request
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import IO, TYPE_CHECKING, Literal

from .config import settings
from .constants import DEFAULT_FETCH_USER_AGENT, PROXY_LINE_PATTERN

if TYPE_CHECKING:
    from .extended_proxy import Proxy


def read_proxies(
    filepath: str | Path,
    *,
    encoding: str = "utf-8",
    on_invalid: Literal["raise", "skip"] = "raise",
    errors_out: list[tuple[int, str, str]] | None = None,
) -> list[Proxy]:
    """Read newline-separated proxies. Invalid lines raise or are skipped per *on_invalid*."""
    from .extended_proxy import Proxy

    path = Path(filepath)
    out: list[Proxy] = []
    with path.open(encoding=encoding, errors="replace") as f:
        for lineno, line in enumerate(f, start=1):
            s = line.strip()
            if not s:
                continue
            try:
                out.append(Proxy(s))
            except ValueError as e:
                if errors_out is not None:
                    errors_out.append((lineno, s, str(e)))
                if on_invalid == "raise":
                    raise ValueError(f"line {lineno}: invalid proxy {s!r}: {e}") from e
    return out


def save_proxies(
    filepath: str | Path,
    proxies: Iterable[str | Proxy],
    *,
    mode: str = "w",
    encoding: str = "utf-8",
) -> None:
    path = Path(filepath)
    lines = [str(p).strip() for p in proxies if str(p).strip()]
    with path.open(mode, encoding=encoding) as f:
        f.writelines(line + "\n" for line in lines)


def iter_proxies_from_file(
    filepath: str | Path,
    *,
    encoding: str = "utf-8",
    on_invalid: Literal["raise", "skip"] = "raise",
    errors_out: list[tuple[int, str, str]] | None = None,
) -> Iterable[Proxy]:
    """Stream one :class:`Proxy` per non-empty line (memory-friendly for large files)."""

    path = Path(filepath)
    with path.open(encoding=encoding, errors="replace") as f:
        yield from _iter_proxies_from_text_stream(f, on_invalid=on_invalid, errors_out=errors_out)


def _iter_proxies_from_text_stream(
    stream: IO[str],
    *,
    on_invalid: Literal["raise", "skip"],
    errors_out: list[tuple[int, str, str]] | None,
) -> Iterable[Proxy]:
    from .extended_proxy import Proxy

    for lineno, line in enumerate(stream, start=1):
        s = line.strip()
        if not s:
            continue
        try:
            yield Proxy(s)
        except ValueError as e:
            if errors_out is not None:
                errors_out.append((lineno, s, str(e)))
            if on_invalid == "raise":
                raise ValueError(f"line {lineno}: invalid proxy {s!r}: {e}") from e


def fetch_proxies(
    url: str,
    *,
    timeout: float | None = None,
    pattern: re.Pattern[str] | None = None,
    unique: bool = True,
    user_agent: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> list[Proxy]:
    """Fetch a page over a direct connection and extract proxy-like strings."""
    from .extended_proxy import Proxy

    to = timeout if timeout is not None else settings.default_timeout
    ctx = ssl.create_default_context()
    hdrs: dict[str, str] = {}
    if headers:
        hdrs.update(headers)
    if user_agent is not None:
        hdrs["User-Agent"] = user_agent
    elif "User-Agent" not in hdrs:
        hdrs["User-Agent"] = DEFAULT_FETCH_USER_AGENT
    req = urllib.request.Request(url, headers=hdrs)
    with urllib.request.urlopen(req, timeout=to, context=ctx) as r:
        text = r.read().decode("utf-8", errors="replace")

    rx = pattern or PROXY_LINE_PATTERN
    seen: set[str] = set()
    out: list[Proxy] = []
    for m in rx.finditer(text):
        raw = m.group("raw").strip()
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


__all__ = ["fetch_proxies", "iter_proxies_from_file", "read_proxies", "save_proxies"]
