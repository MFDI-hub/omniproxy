"""Fetch proxy lists over HTTP(S) as plain text or JSON."""

from __future__ import annotations

import asyncio
import ssl
from collections.abc import Mapping
from enum import Enum
from typing import Any
from urllib import request as urllib_request
from urllib.error import URLError

import orjson

from ..config import settings
from ..constants import DEFAULT_FETCH_USER_AGENT


class UrlListFormat(str, Enum):
    """How :class:`URLFetcher` interprets the response body.

    Members:
        AUTO: Sniff JSON when the body starts with ``{`` or ``[``, else plain.
        PLAIN: Treat the body as newline-separated plain text.
        JSON: Force JSON parsing; non-JSON bodies return an empty list.

    Version:
        Added in 4.0.0.
    """

    AUTO = "auto"
    PLAIN = "plain"
    JSON = "json"


def _sync_download(url: str, headers: dict[str, str], timeout: float | None) -> bytes:
    """Synchronously download ``url`` over HTTPS.

    Args:
        url (str): Target URL.
        headers (dict[str, str]): Extra request headers; a default
            ``User-Agent`` is added when missing.
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


def _flatten_json_item(item: Any) -> list[str]:
    """Convert one JSON entry into one or more proxy strings.

    Supports:

    * Plain strings.
    * Dicts with ``proxy`` or ``url`` string fields.
    * Dicts with ``host`` / ``ip`` / ``address`` plus a ``port``.

    Args:
        item (Any): JSON value extracted from a list.

    Returns:
        list[str]: Zero or more proxy strings.

    Version:
        Added in 4.0.0.
    """
    if isinstance(item, str):
        s = item.strip()
        return [s] if s else []
    if isinstance(item, dict):
        if isinstance(item.get("proxy"), str):
            s = item["proxy"].strip()
            return [s] if s else []
        if isinstance(item.get("url"), str):
            s = item["url"].strip()
            return [s] if s else []
        host = item.get("host") or item.get("ip") or item.get("address")
        port = item.get("port")
        if isinstance(host, str) and isinstance(port, (int, float, str)):
            line = f"{host.strip()}:{int(port)}"
            return [line]
        if isinstance(host, str) and "port" in item and isinstance(item["port"], (int, float, str)):
            line = f"{host.strip()}:{int(item['port'])}"
            return [line]
    return []


def parse_proxy_urls_from_payload(
    raw: bytes,
    *,
    text_encoding: str = "utf-8",
    fmt: UrlListFormat = UrlListFormat.AUTO,
) -> list[str]:
    """Split a raw body into proxy line strings (not yet validated).

    Args:
        raw (bytes): Raw response payload.
        text_encoding (str): Encoding to use when decoding the body.
        fmt (UrlListFormat): How to interpret the body.

    Returns:
        list[str]: Candidate proxy strings; further validation happens
        when the strings are converted to :class:`Proxy`.

    Version:
        Added in 4.0.0.
    """
    text = raw.decode(text_encoding, errors="replace")

    use_json = fmt == UrlListFormat.JSON or (
        fmt == UrlListFormat.AUTO and text.lstrip().startswith(("{", "["))
    )
    if fmt == UrlListFormat.PLAIN:
        use_json = False

    if use_json:
        try:
            data = orjson.loads(raw)
        except orjson.JSONDecodeError:
            if fmt == UrlListFormat.JSON:
                return []
            # AUTO fallback — treat as plain text
            return [ln.strip() for ln in text.splitlines() if ln.strip()]

        return _extract_strings_from_json(data)

    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def _extract_strings_from_json(data: Any) -> list[str]:
    """Pull proxy strings from a variety of common JSON shapes.

    Supports raw lists, and dicts with any of ``proxies``, ``data``,
    ``results``, ``items``, ``list``, or ``hosts``. Falls back to scanning
    other list-typed values as a last resort.

    Args:
        data (Any): Decoded JSON value.

    Returns:
        list[str]: Zero or more proxy strings.

    Version:
        Added in 4.0.0.
    """
    out: list[str] = []
    if isinstance(data, list):
        for item in data:
            out.extend(_flatten_json_item(item))
        return out

    if isinstance(data, dict):
        for key in ("proxies", "data", "results", "items", "list"):
            nested = data.get(key)
            if isinstance(nested, list):
                for item in nested:
                    out.extend(_flatten_json_item(item))
                if out:
                    return out

        nested = data.get("hosts")
        if isinstance(nested, list):
            for item in nested:
                out.extend(_flatten_json_item(item))
            return out

        for val in data.values():
            if isinstance(val, list) and val and isinstance(val[0], (str, dict)):
                for item in val:
                    out.extend(_flatten_json_item(item))
                if out:
                    return out

    return out


class URLFetcher:
    """Download a remote proxy list and return raw proxy strings.

    Validation into :class:`Proxy` instances is deferred to downstream
    consumers (typically the pool's refresh helpers).

    Attributes:
        _url (str): Source URL.
        _headers (dict[str, str]): Extra request headers.
        _timeout (float | None): Per-request socket timeout.
        _fmt (UrlListFormat): Body interpretation policy.
        _encoding (str): Text encoding used for plain-text bodies.

    Version:
        Added in 4.0.0.
    """

    __slots__ = ("_encoding", "_fmt", "_headers", "_timeout", "_url")

    def __init__(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        body_format: UrlListFormat = UrlListFormat.AUTO,
        text_encoding: str = "utf-8",
    ) -> None:
        """Build a URL-backed fetcher.

        Args:
            url (str): Source URL.
            headers (Mapping[str, str] | None): Extra request headers.
            timeout (float | None): Socket timeout in seconds; defaults
                to ``settings.default_timeout``.
            body_format (UrlListFormat): Body interpretation policy.
            text_encoding (str): Decoding for plain-text bodies.

        Version:
            Added in 4.0.0.
        """
        self._url = url
        self._headers = dict(headers) if headers else {}
        self._timeout = timeout
        self._fmt = body_format
        self._encoding = text_encoding

    async def fetch(self) -> list[str]:
        """Download and parse the proxy list.

        Network errors are swallowed and yield an empty list so a single
        flaky source cannot break the refresh cycle.

        Returns:
            list[str]: Candidate proxy strings; an empty list when the
            download fails or the body is unparseable.

        Version:
            Added in 4.0.0.
        """
        try:
            body = await asyncio.to_thread(_sync_download, self._url, self._headers, self._timeout)
        except URLError:
            return []

        return parse_proxy_urls_from_payload(body, text_encoding=self._encoding, fmt=self._fmt)
