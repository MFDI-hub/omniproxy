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
    """Synchronously download ``url`` over HTTPS for the scraper.

    Args:
        url (str): Target URL.
        headers (dict[str, str]): Extra request headers; injects a default
            ``User-Agent`` when missing.
        timeout (float | None): Socket timeout; defaults to
            ``settings.default_timeout``.

    Returns:
        bytes: Raw response body.

    Version:
        Added in 4.0.0.
    """
    to = timeout if timeout is not None else settings.default_timeout
    ctx = ssl.create_default_context()
    hdrs = dict(headers)
    if "User-Agent" not in hdrs:
        hdrs["User-Agent"] = DEFAULT_FETCH_USER_AGENT
    req = urllib_request.Request(url, headers=hdrs)
    with urllib_request.urlopen(req, timeout=to, context=ctx) as r:
        return r.read()


class ScrapeFetcher:
    """Download an HTML page and extract proxy-shaped strings from it.

    Three extraction strategies are available, in order of priority:

    1. ``custom_extractor`` - user-supplied callable receiving the raw bytes.
    2. ``css_selectors`` - parse HTML with ``beautifulsoup4`` and collect
       text or an attribute from matched elements.
    3. Regex scan (default) - search the body with ``PROXY_LINE_PATTERN`` or
       a user-supplied regex.

    Attributes:
        _url (str): Page URL to fetch.
        _css_selectors (list[str] | None): CSS selectors when scraping HTML.
        _pattern (re.Pattern[str]): Regex used to find proxy strings.
        _headers (dict[str, str]): Extra request headers.
        _timeout (float | None): Socket timeout in seconds.
        _attribute (str | None): Element attribute to prefer over text.
        _custom_extractor (Callable[[bytes], list[str]] | None): Optional
            override extractor.

    Version:
        Added in 4.0.0.
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
        """Construct a scraping fetcher.

        Args:
            url (str): Page to retrieve.
            css_selectors (list[str] | None): When set, each selector's
                matched elements contribute ``attribute`` or text content.
            regex (re.Pattern[str] | None): Overrides
                :data:`PROXY_LINE_PATTERN` when scanning the body.
            headers (Mapping[str, str] | None): Extra request headers.
            timeout (float | None): Socket timeout in seconds.
            attribute (str | None): Element attribute to read in preference
                to text (typical ``"href"``).
            custom_extractor (Callable[[bytes], list[str]] | None): If
                provided, called with the raw response bytes instead of the
                built-in extractors.

        Version:
            Added in 4.0.0.
        """
        self._url = url
        self._css_selectors = css_selectors
        self._pattern = regex or PROXY_LINE_PATTERN
        self._headers = dict(headers) if headers else {}
        self._timeout = timeout
        self._attribute = attribute
        self._custom_extractor = custom_extractor

    def _extract_via_bs4(self, html: bytes) -> list[str]:
        """Extract proxy-shaped strings from HTML using BeautifulSoup.

        Args:
            html (bytes): Raw response body.

        Returns:
            list[str]: Deduplicated raw strings collected from selector matches.

        Raises:
            ImportError: If ``beautifulsoup4`` is not installed.

        Version:
            Added in 4.0.0.
        """
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
        """Extract proxy-shaped strings by scanning ``body`` with the regex.

        Args:
            body (bytes): Raw response body.

        Returns:
            list[str]: Deduplicated proxy strings in first-seen order.

        Version:
            Added in 4.0.0.
        """
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
        """Download the page and extract proxy strings.

        Network errors yield an empty list so a single bad page cannot
        derail the refresh cycle. When CSS selectors are configured, the
        method parses HTML on a worker thread and validates each candidate;
        unparseable lines fall back to a regex sweep.

        Returns:
            list[Proxy | str]: Validated :class:`Proxy` objects when using
            CSS selectors, otherwise raw strings ready for downstream
            validation.

        Raises:
            ImportError: When CSS selectors are configured but
                ``beautifulsoup4`` is not installed.

        Version:
            Added in 4.0.0.
        """
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
