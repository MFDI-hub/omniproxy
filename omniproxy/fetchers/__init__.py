"""Pluggable proxy sources for pool refresh."""

from __future__ import annotations

from .base import ProxyFetcher
from .defaults import default_fetchers
from .file_fetcher import FileFetcher
from .scrape_fetcher import ScrapeFetcher
from .url_fetcher import URLFetcher, UrlListFormat

__all__: list[str] = [
    "FileFetcher",
    "ProxyFetcher",
    "ScrapeFetcher",
    "URLFetcher",
    "UrlListFormat",
    "default_fetchers",
]
