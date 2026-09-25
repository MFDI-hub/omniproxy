"""Read, stream, and write proxy files, including both invalid-line policies."""

from __future__ import annotations

from pathlib import Path

from omniproxy import fetch_proxies, iter_proxies_from_file, read_proxies, save_proxies
from omniproxy.enum import IoInvalidLinePolicy

from _support import title

SAMPLE = "10.0.0.1:8080\n\nnot a proxy\nsocks5://10.0.0.2:1080\n"


def file_roundtrip(directory: Path) -> None:
    source = directory / "in.txt"
    source.write_text(SAMPLE, encoding="utf-8")

    title("IoInvalidLinePolicy")
    for policy in IoInvalidLinePolicy:
        errors: list[tuple[int, str, str]] = []
        try:
            loaded = read_proxies(source, on_invalid=policy, errors_out=errors)
            print(policy.value, "loaded", [item.url for item in loaded], "errors", len(errors))
        except ValueError as exc:
            print(policy.value, "raised", exc, "errors", len(errors))

    title("iter_proxies_from_file")
    streamed = list(
        iter_proxies_from_file(source, on_invalid=IoInvalidLinePolicy.SKIP)
    )
    print("streamed", [item.protocol for item in streamed])

    title("save_proxies modes")
    destination = directory / "out.txt"
    save_proxies(destination, streamed, mode="w")
    save_proxies(destination, ["10.0.0.3:9000"], mode="a", encoding="utf-8")
    print(destination.read_text(encoding="utf-8"))


def live_fetch() -> None:
    title("fetch_proxies")
    try:
        found = fetch_proxies(
            "https://example.com/",
            timeout=8.0,
            unique=True,
            user_agent="omniproxy-examples",
            headers={"Accept": "text/html"},
        )
        print("example.com count", len(found))
    except Exception as exc:
        print("example.com", type(exc).__name__, exc)
    try:
        scraped = fetch_proxies("https://free-proxy-list.net/", timeout=15.0, unique=True)
        print("scraped count", len(scraped))
    except Exception as exc:
        print("fetch_proxies", type(exc).__name__, exc)


def main() -> None:
    directory = Path(__file__).resolve().parent / "_io_demo"
    directory.mkdir(exist_ok=True)
    try:
        file_roundtrip(directory)
    finally:
        for path in directory.glob("*"):
            path.unlink(missing_ok=True)
        directory.rmdir()
    live_fetch()


if __name__ == "__main__":
    main()
