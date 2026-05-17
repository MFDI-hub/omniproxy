"""Pull proxy endpoints from HTML using regex or CSS selectors (selectors require beautifulsoup4)."""

from __future__ import annotations

import asyncio
import re
import ssl
from collections.abc import Callable, Mapping
from urllib import request as urllib_request
from urllib.error import URLError

from ..config import settings
from ..constants import DEFAULT_FETCH_USER_AGENT, PROXY_LINE_PATTERN
from ..proxy import Proxy


def _sync_download(url: str, headers: dict[str, str], timeout: float | None) -> bytes:
    to = timeout if timeout is not None else settings.default_timeout
    ctx = ssl.create_default_context()
    hdrs = dict(headers)
    if "User-Agent" not in hdrs:
        hdrs["User-Agent"] = DEFAULT_FETCH_USER_AGENT
    req = urllib_request.Request(url, headers=hdrs)
    with urllib_request.urlopen(req, timeout=to, context=ctx) as r:
        return r.read()


class ScrapeFetcher:
    """Download HTML and extract proxy-shaped strings.

    When *css_selectors* is omitted, the entire body is scanned with ``PROXY_LINE_PATTERN``.
    When *css_selectors* is set, optional dependency ``beautifulsoup4`` is required to run CSS
    selection (``BeautifulSoup`` + ``.select``).
    """

    __slots__ = (
        "_attribute",
        "_css_selectors",
        "_custom_extractor",
        "_headers",
        "_pattern",
        "_timeout",
        "_url",
    )

    def __init__(
        self,
        url: str,
        *,
        css_selectors: list[str] | None = None,
        regex: re.Pattern[str] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        attribute: str | None = None,
        custom_extractor: Callable[[bytes], list[str]] | None = None,
    ) -> None:
        """
        Args:
            url: Page to retrieve.
            css_selectors: If provided, each selector's matching elements contribute text or *attribute*.
            regex: Overrides the default proxy line regex when scanning raw HTML/text.
            attribute: Element attribute to prefer (e.g. ``\"href\"``); otherwise text content is used.
            custom_extractor: If set, called with raw response bytes instead of builtin extraction.
        """
        self._url = url
        self._css_selectors = css_selectors
        self._pattern = regex or PROXY_LINE_PATTERN
        self._headers = dict(headers) if headers else {}
        self._timeout = timeout
        self._attribute = attribute
        self._custom_extractor = custom_extractor

    def _extract_via_bs4(self, html: bytes) -> list[str]:
        try:
            from bs4 import BeautifulSoup  # type: ignore[import-untyped]
        except ImportError as e:
            raise ImportError(
                "ScrapeFetcher with css_selectors requires optional dependency "
                '`beautifulsoup4` (install omniproxy with extra "scrape" or '
                "`pip install beautifulsoup4`)."
            ) from e

        text = html.decode("utf-8", errors="replace")
        soup = BeautifulSoup(text, "html.parser")
        seen: set[str] = set()
        out: list[str] = []
        for sel in self._css_selectors or []:
            for node in soup.select(sel):
                piece: str | None = None
                if self._attribute:
                    raw = node.get(self._attribute)
                    if isinstance(raw, str):
                        piece = raw.strip()
                if not piece:
                    piece = node.get_text(separator="\n", strip=True)
                if not piece:
                    continue
                for line in piece.splitlines():
                    s = line.strip()
                    if s and s not in seen:
                        seen.add(s)
                        out.append(s)
        return out

    def _extract_via_regex(self, body: bytes) -> list[str]:
        text = body.decode("utf-8", errors="replace")
        seen: set[str] = set()
        items: list[str] = []
        for m in self._pattern.finditer(text):
            try:
                raw = m.group("raw")
            except IndexError:
                raw = m.group(0)
            s = raw.strip()
            if s and s not in seen:
                seen.add(s)
                items.append(s)
        return items

    async def fetch(self) -> list[Proxy | str]:
        try:
            body = await asyncio.to_thread(_sync_download, self._url, self._headers, self._timeout)
        except URLError:
            return []

        if self._custom_extractor is not None:
            return self._custom_extractor(body)

        if self._css_selectors:
            try:
                lines = await asyncio.to_thread(self._extract_via_bs4, body)
            except ImportError:
                raise
            if not lines:
                return []

            proxies: list[Proxy | str] = []
            for line in lines:
                try:
                    proxies.append(Proxy(line))
                except ValueError:
                    for m in self._pattern.finditer(line):
                        try:
                            raw = m.group("raw").strip()
                        except IndexError:
                            raw = m.group(0).strip()
                        if raw:
                            try:
                                proxies.append(Proxy(raw))
                            except ValueError:
                                continue
            return proxies

        return await asyncio.to_thread(self._extract_via_regex, body)
