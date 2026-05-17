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
    """How to interpret the response body."""

    AUTO = "auto"
    PLAIN = "plain"
    JSON = "json"


def _sync_download(url: str, headers: dict[str, str], timeout: float | None) -> bytes:
    to = timeout if timeout is not None else settings.default_timeout
    ctx = ssl.create_default_context()
    hdrs = dict(headers)
    if "User-Agent" not in hdrs:
        hdrs["User-Agent"] = DEFAULT_FETCH_USER_AGENT
    req = urllib_request.Request(url, headers=hdrs)
    with urllib_request.urlopen(req, timeout=to, context=ctx) as r:
        return r.read()


def _flatten_json_item(item: Any) -> list[str]:
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
    """Split *raw* body into proxy line strings (not yet validated)."""
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
    """Download a remote resource and yield proxy addresses (validated later by the pool)."""

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
        self._url = url
        self._headers = dict(headers) if headers else {}
        self._timeout = timeout
        self._fmt = body_format
        self._encoding = text_encoding

    async def fetch(self) -> list[str]:
        try:
            body = await asyncio.to_thread(_sync_download, self._url, self._headers, self._timeout)
        except URLError:
            return []

        return parse_proxy_urls_from_payload(body, text_encoding=self._encoding, fmt=self._fmt)
