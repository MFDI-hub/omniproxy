"""Load proxies from disk, scrape proxy-like strings from a URL, save lists."""

from __future__ import annotations

from pathlib import Path

from omniproxy import fetch_proxies, read_proxies, save_proxies


def main() -> None:
    tmp = Path(__file__).resolve().parent / "_sample_proxies.txt"
    tmp.write_text("10.0.0.1:8080\nsocks5://10.0.0.2:1080\n", encoding="utf-8")
    try:
        proxies = read_proxies(tmp)
        save_proxies(tmp.with_name("_sample_out.txt"), proxies)
    finally:
        tmp.unlink(missing_ok=True)
        tmp.with_name("_sample_out.txt").unlink(missing_ok=True)

    try:
        scraped = fetch_proxies("https://free-proxy-list.net/", timeout=15.0)
        print("scraped count:", len(scraped))
    except Exception as exc:
        print("fetch_proxies (network-dependent):", exc)


if __name__ == "__main__":
    main()
