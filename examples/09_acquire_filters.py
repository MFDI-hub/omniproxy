"""Acquire filters: tags, country, anonymity, latency, callbacks, and missing-metadata policies."""

from __future__ import annotations

from omniproxy import ProxyPool
from omniproxy.enum import AnonymityTier, FilterMissingMetadata
from omniproxy.pool import AcquireOptions

from _support import SEEDS, meta, offline_config, title


def _show(label: str, config_updates: dict, proxies: list, **filters: object) -> None:
    pool = ProxyPool(offline_config(**config_updates), proxies)
    try:
        proxy = pool.acquire(**filters)
        print(
            label,
            proxy.url,
            "country",
            proxy.country,
            "anonymity",
            proxy.anonymity,
            "tags",
            getattr(proxy, "tags", ()),
            "latency",
            proxy.latency,
        )
        pool.release(proxy)
    except Exception as exc:
        print(label, type(exc).__name__, exc)
    finally:
        pool.close()


def main() -> None:
    transparent = meta(SEEDS[3], anonymity="transparent", latency=1.5, country="US", tags=("us",))
    fr_anon = meta(SEEDS[1], country="FR", anonymity="anonymous", latency=0.8, tags=("eu",))
    us_elite = meta(
        SEEDS[0],
        country="US",
        city="Ashburn",
        anonymity="elite",
        latency=0.2,
        asn="AS1",
        org="Example",
        tags=("fast", "us"),
    )
    bare = meta(SEEDS[2], tags=())
    fleet = [transparent, fr_anon, us_elite, bare]

    title("metadata filters")
    _show("country=US", {}, fleet, country="US")
    _show("country=FR", {}, fleet, country="FR")
    for tier in AnonymityTier:
        _show(f"min_anonymity={tier.value}", {}, fleet, min_anonymity=tier.value)
    _show("max_latency kw", {}, fleet, max_latency=0.5)
    _show("max_latency config", {"max_latency": 0.3}, fleet)
    _show("tags fast", {}, fleet, tags={"fast"})
    _show("tags eu", {}, fleet, tags={"eu"})
    _show("acquire_tags", {"acquire_tags": {"eu"}}, fleet)
    _show("accept elite", {}, fleet, accept_callback=lambda proxy: proxy.anonymity == "elite")
    _show(
        "pool accept",
        {"accept_callback": lambda proxy, _filters: proxy.country == "FR"},
        fleet,
    )

    title("FilterMissingMetadata x min_anonymity on a proxy with no anonymity")
    untagged = [meta(SEEDS[4])]
    for policy in FilterMissingMetadata:
        _show(policy.value, {"filter_missing_metadata": policy}, untagged, min_anonymity="elite")

    title("misses")
    _show("no country", {}, fleet, country="JP")
    try:
        options = AcquireOptions.from_kwargs(
            offline_config(max_latency=1.0, acquire_tags={"fast"}),
            session_id="legacy-alias",
            unknown_filter=True,
        )
        print(
            "AcquireOptions",
            options.session_key,
            options.max_latency,
            options.tags,
        )
    except Exception as exc:
        print("AcquireOptions", type(exc).__name__, exc)


if __name__ == "__main__":
    main()
