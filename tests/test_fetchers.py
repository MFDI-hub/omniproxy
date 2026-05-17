"""Tests for omniproxy.fetchers and integration with omniproxy.refresh."""

from __future__ import annotations

import pytest
from omniproxy.fetchers.file_fetcher import FileFetcher
from omniproxy.fetchers.scrape_fetcher import ScrapeFetcher
from omniproxy.fetchers.url_fetcher import URLFetcher, UrlListFormat, parse_proxy_urls_from_payload
from omniproxy.refresh import fetch_from_fetchers


def test_parse_proxy_urls_plain_lines() -> None:
    raw = b"http://127.0.0.1:1122\r\nsocks5://9.9.9.9:99\n\n"
    assert parse_proxy_urls_from_payload(raw, fmt=UrlListFormat.PLAIN) == [
        "http://127.0.0.1:1122",
        "socks5://9.9.9.9:99",
    ]


def test_parse_proxy_urls_json_array() -> None:
    raw = rb'{"proxies":[{"host":"1.2.3.4","port":8765}]}'
    out = parse_proxy_urls_from_payload(raw, fmt=UrlListFormat.JSON)
    assert out == ["1.2.3.4:8765"]


def test_parse_proxy_urls_json_strings() -> None:
    raw = b'[\n "http://10.0.0.8:443" \t]\n'
    out = parse_proxy_urls_from_payload(raw, fmt=UrlListFormat.JSON)
    assert out == ["http://10.0.0.8:443"]


@pytest.mark.asyncio
async def test_file_fetcher_reads_tmp(tmp_path) -> None:
    p = tmp_path / "list.txt"
    p.write_text("127.0.0.1:8080\n", encoding="utf-8")
    fetcher = FileFetcher(p, on_invalid="skip")
    out = await fetcher.fetch()
    assert len(out) == 1


@pytest.mark.asyncio
async def test_url_fetcher_monkeypatch(monkeypatch: pytest.MonkeyPatch) -> None:
    body = b"http://127.0.0.1:5544\n"

    def _fake(u: str, h: dict, t: float | None) -> bytes:
        assert "example.invalid" in u
        return body

    monkeypatch.setattr(
        "omniproxy.fetchers.url_fetcher._sync_download",
        _fake,
    )
    f = URLFetcher("https://example.invalid/proxies.txt", body_format=UrlListFormat.PLAIN)
    out = await f.fetch()
    assert out == ["http://127.0.0.1:5544"]


@pytest.mark.asyncio
async def test_scrape_fetcher_regex_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    snippet = b"Free lists: http://192.168.0.77:8118 for use"
    monkeypatch.setattr(
        "omniproxy.fetchers.scrape_fetcher._sync_download",
        lambda u, h, t: snippet,
    )
    f = ScrapeFetcher("https://example.invalid/")
    lines = await f.fetch()
    assert [str(x) for x in lines] == ["http://192.168.0.77:8118"]


@pytest.mark.asyncio
async def test_scrape_fetcher_css(monkeypatch: pytest.MonkeyPatch) -> None:
    html = b"<table><tr><td class='p'>http://127.0.0.1:9000</td></tr></table>"
    monkeypatch.setattr(
        "omniproxy.fetchers.scrape_fetcher._sync_download",
        lambda u, h, t: html,
    )
    f = ScrapeFetcher("https://example.invalid/table", css_selectors=["td.p"])
    out = await f.fetch()
    assert len(out) >= 1
    merged = "".join(str(x) for x in out)
    assert "127.0.0.1:9000" in merged.replace(" ", "")


class _StaticFetcher:
    def __init__(self, seq: list[str]) -> None:
        self.seq = seq

    async def fetch(self) -> list[str]:
        return list(self.seq)


@pytest.mark.asyncio
async def test_fetch_from_fetchers_deduplicates_urls() -> None:
    proxies = await fetch_from_fetchers(
        [_StaticFetcher(["127.0.0.5:9050"]), _StaticFetcher(["127.0.0.5:9050"])]
    )
    assert len(proxies) == 1
