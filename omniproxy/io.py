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
    """Read newline-separated proxies from a text file.

    Args:
        filepath (str | Path): Path to the file.
        encoding (str): Text encoding for the file open call.
        on_invalid (Literal["raise", "skip"]): Whether invalid lines raise or are skipped.
        errors_out (list[tuple[int, str, str]] | None): If set, append ``(lineno, line, error)``.

    Returns:
        list[Proxy]: Parsed proxies in file order (blank lines skipped).

    Raises:
        ValueError: When ``on_invalid="raise"`` and a line fails validation.

    Example:
        >>> from pathlib import Path
        >>> from tempfile import TemporaryDirectory
        >>> from omniproxy.io import read_proxies
        >>> port = None
        >>> with TemporaryDirectory() as d:
        ...     p = Path(d) / "p.txt"
        ...     _ = p.write_text("127.0.0.1:8080" + chr(10), encoding="utf-8")
        ...     port = read_proxies(p)[0].port
        >>> port
        8080
    """
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
    """Write proxies to a newline-separated text file (blank entries skipped).

    Args:
        filepath (str | Path): Output path.
        proxies (Iterable[str | Proxy]): Lines to write (coerced with ``str()``).
        mode (str): File open mode (default truncate-and-write).
        encoding (str): Text encoding.

    Returns:
        None

    Example:
        >>> from pathlib import Path
        >>> from tempfile import TemporaryDirectory
        >>> from omniproxy.io import save_proxies
        >>> with TemporaryDirectory() as d:
        ...     out = Path(d) / "p.txt"
        ...     save_proxies(out, ["127.0.0.1:1"])
        ...     out.read_text().strip()
        '127.0.0.1:1'
    """
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
    """Stream one :class:`~omniproxy.extended_proxy.Proxy` per non-empty line.

    Args:
        filepath (str | Path): Path to the file.
        encoding (str): Text encoding.
        on_invalid (Literal["raise", "skip"]): Behaviour on invalid lines.
        errors_out (list[tuple[int, str, str]] | None): Optional collector for errors.

    Returns:
        Iterable[Proxy]: Lazy generator of proxies.

    Raises:
        ValueError: When ``on_invalid="raise"`` and a line is invalid.

    Example:
        >>> from pathlib import Path
        >>> from tempfile import TemporaryDirectory
        >>> from omniproxy.io import iter_proxies_from_file
        >>> port = None
        >>> with TemporaryDirectory() as d:
        ...     fp = Path(d) / "t.txt"
        ...     _ = fp.write_text("127.0.0.1:9" + chr(10), encoding="utf-8")
        ...     port = next(iter_proxies_from_file(fp)).port
        >>> port
        9
    """

    path = Path(filepath)
    with path.open(encoding=encoding, errors="replace") as f:
        yield from _iter_proxies_from_text_stream(f, on_invalid=on_invalid, errors_out=errors_out)


def _iter_proxies_from_text_stream(
    stream: IO[str],
    *,
    on_invalid: Literal["raise", "skip"],
    errors_out: list[tuple[int, str, str]] | None,
) -> Iterable[Proxy]:
    """Yield proxies from a text stream (one non-empty line at a time).

    Args:
        stream (IO[str]): Readable text stream.
        on_invalid (Literal["raise", "skip"]): Error handling for bad lines.
        errors_out (list[tuple[int, str, str]] | None): Optional error collector.

    Returns:
        Iterable[Proxy]: Generator of :class:`~omniproxy.extended_proxy.Proxy` instances.

    Raises:
        ValueError: When ``on_invalid="raise"`` and parsing fails.

    Example:
        >>> from io import StringIO
        >>> from omniproxy.io import _iter_proxies_from_text_stream
        >>> next(_iter_proxies_from_text_stream(StringIO("127.0.0.1:3" + chr(10)), on_invalid="raise", errors_out=None)).port
        3
    """
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
    """Fetch a page over a direct (non-proxy) HTTPS connection and extract proxies.

    Args:
        url (str): Page URL to download.
        timeout (float | None): Socket timeout; defaults to ``settings.default_timeout``.
        pattern (re.Pattern[str] | None): Regex with a ``raw`` named group; default built-in.
        unique (bool): De-duplicate by raw string and canonical proxy URL.
        user_agent (str | None): Override User-Agent (otherwise default fetch UA).
        headers (Mapping[str, str] | None): Extra request headers.

    Returns:
        list[Proxy]: Successfully parsed proxies found in the body.

    Example:
        >>> from omniproxy.io import fetch_proxies
        >>> fetch_proxies.__name__
        'fetch_proxies'
    """
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
