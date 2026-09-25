"""Built-in public proxy list sources and an opt-in fetcher factory."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from ..enum import ProxyProtocol
from .url_fetcher import URLFetcher, UrlListFormat

ProxyCatalogProtocol = Literal["all", "http", "socks4", "socks5"]
_FilterProtocol = Literal["http", "socks4", "socks5"]
_FILTER_PROTOCOLS: dict[str, _FilterProtocol] = {
    "http": "http",
    "socks4": "socks4",
    "socks5": "socks5",
}

_PROXYSCRAPE_V4_BASE = (
    "https://api.proxyscrape.com/v4/free-proxy-list/get"
    "?request=display_proxies&proxy_format=protocolipport&format=text"
)
_MONOSANS_BASE = "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies"
_PROXIFLY_CDN_BASE = "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies"


@dataclass(frozen=True, slots=True)
class _DefaultSource:
    """One public proxy list endpoint."""

    url: str
    protocols: frozenset[ProxyCatalogProtocol]
    protocol_prefix: str | None = None


_DEFAULT_SOURCES: tuple[_DefaultSource, ...] = (
    _DefaultSource(
        url=f"{_PROXYSCRAPE_V4_BASE}",
        protocols=frozenset({"all"}),
    ),
    _DefaultSource(
        url=f"{_MONOSANS_BASE}/all.txt",
        protocols=frozenset({"all"}),
    ),
    _DefaultSource(
        url=f"{_PROXIFLY_CDN_BASE}/all/data.txt",
        protocols=frozenset({"all"}),
    ),
    _DefaultSource(
        url=f"{_PROXYSCRAPE_V4_BASE}&protocol=http",
        protocols=frozenset({"http"}),
    ),
    _DefaultSource(
        url=f"{_MONOSANS_BASE}/http.txt",
        protocols=frozenset({"http"}),
        protocol_prefix="http",
    ),
    _DefaultSource(
        url=f"{_PROXIFLY_CDN_BASE}/protocols/http/data.txt",
        protocols=frozenset({"http"}),
        protocol_prefix="http",
    ),
    _DefaultSource(
        url=f"{_PROXYSCRAPE_V4_BASE}&protocol=socks4",
        protocols=frozenset({"socks4"}),
    ),
    _DefaultSource(
        url=f"{_MONOSANS_BASE}/socks4.txt",
        protocols=frozenset({"socks4"}),
        protocol_prefix="socks4",
    ),
    _DefaultSource(
        url=f"{_PROXIFLY_CDN_BASE}/protocols/socks4/data.txt",
        protocols=frozenset({"socks4"}),
        protocol_prefix="socks4",
    ),
    _DefaultSource(
        url=f"{_PROXYSCRAPE_V4_BASE}&protocol=socks5",
        protocols=frozenset({"socks5"}),
    ),
    _DefaultSource(
        url=f"{_MONOSANS_BASE}/socks5.txt",
        protocols=frozenset({"socks5"}),
        protocol_prefix="socks5",
    ),
    _DefaultSource(
        url=f"{_PROXIFLY_CDN_BASE}/protocols/socks5/data.txt",
        protocols=frozenset({"socks5"}),
        protocol_prefix="socks5",
    ),
)


def _normalize_protocols(
    protocols: Sequence[str | ProxyProtocol] | None,
) -> frozenset[ProxyCatalogProtocol] | None:
    if protocols is None:
        return None
    out: set[ProxyCatalogProtocol] = set()
    for item in protocols:
        key = item.value if isinstance(item, ProxyProtocol) else str(item)
        catalog = _FILTER_PROTOCOLS.get(key.lower())
        if catalog is None:
            raise ValueError(
                f"Unsupported protocol filter: {item!r}. "
                "Use 'http', 'socks4', or 'socks5'."
            )
        out.add(catalog)
    if not out:
        raise ValueError("protocols must contain at least one of http, socks4, socks5")
    return frozenset(out)


def _sources_for(protocols: frozenset[ProxyCatalogProtocol] | None) -> tuple[_DefaultSource, ...]:
    if protocols is None:
        return tuple(s for s in _DEFAULT_SOURCES if "all" in s.protocols)
    return tuple(
        s for s in _DEFAULT_SOURCES if s.protocols and s.protocols <= protocols
    )


def default_fetchers(
    *,
    protocols: Sequence[str | ProxyProtocol] | None = None,
) -> list[URLFetcher]:
    """Return :class:`URLFetcher` instances for verified public proxy list URLs.

    When ``protocols`` is omitted, three multi-protocol sources are returned
    (ProxyScrape, monosans, proxifly). When one or more protocols are given,
    only sources for those protocols are included (three per protocol).

    Args:
        protocols (Sequence[str | ProxyProtocol] | None): Optional filter such
            as ``("socks5",)`` or ``[ProxyProtocol.HTTP]``.

    Returns:
        list[URLFetcher]: Fetchers ready to pass to :class:`AsyncProxyPool`.

    Example:
        >>> from omniproxy.fetchers import default_fetchers
        >>> len(default_fetchers())
        3
        >>> len(default_fetchers(protocols=("socks5",)))
        3

    Version:
        Added in 4.0.1.
    """
    normalized = _normalize_protocols(protocols)
    return [
        URLFetcher(
            source.url,
            body_format=UrlListFormat.PLAIN,
            protocol=source.protocol_prefix,
        )
        for source in _sources_for(normalized)
    ]


__all__: list[str] = ["default_fetchers"]
