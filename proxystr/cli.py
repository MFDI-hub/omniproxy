"""Command-line entry point for bulk checks and scraping."""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="proxystr", description="Proxy string utilities")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_check = sub.add_parser("check", help="Check proxies from a file")
    p_check.add_argument("file", help="Path to newline-separated proxies")
    p_check.add_argument(
        "--backend",
        default=None,
        help="httpx | aiohttp | requests | curl_cffi | tls_client (default: config default_backend)",
    )
    p_check.add_argument("--no-async", dest="use_async", action="store_false", default=True)
    p_check.add_argument(
        "--anonymity",
        action="store_true",
        help="Run an extra headers probe to classify anonymity (slower)",
    )
    p_check.add_argument(
        "-o",
        "--output-good",
        default=None,
        help="Write working proxies to this file",
    )

    p_scrape = sub.add_parser("scrape", help="Extract proxy-like strings from a URL")
    p_scrape.add_argument("url")
    p_scrape.add_argument("-o", "--output", help="Save extracted proxies to a file")

    args = parser.parse_args(argv)

    if args.cmd == "check":
        from .extended_proxy import check_proxies
        from .io import read_proxies, save_proxies

        proxies = read_proxies(args.file)
        good, bad = check_proxies(
            proxies,
            backend=args.backend,
            detect_anonymity=args.anonymity,
            use_async=args.use_async,
        )
        print(f"ok={len(good)} fail={len(bad)}")
        if args.output_good:
            save_proxies(args.output_good, good)
        return 0

    if args.cmd == "scrape":
        from .io import fetch_proxies, save_proxies

        found = fetch_proxies(args.url)
        print(f"found={len(found)}")
        if args.output:
            save_proxies(args.output, found)
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
