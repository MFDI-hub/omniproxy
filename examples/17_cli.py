"""CLI ``check`` and ``scrape`` flag combinations."""

from __future__ import annotations

from pathlib import Path

from omniproxy.backends.factory import get_backend, supported_backends
from omniproxy.cli import main as cli_main

from _support import title


def _ready_backend() -> str | None:
    for name in supported_backends():
        try:
            get_backend(name)
        except ImportError:
            continue
        return name
    return None


def main() -> None:
    directory = Path(__file__).resolve().parent / "_cli_demo"
    directory.mkdir(exist_ok=True)
    source = directory / "proxies.txt"
    source.write_text("127.0.0.1:1\n10.255.255.1:9\n", encoding="utf-8")
    good = directory / "good.txt"
    scraped = directory / "scraped.txt"
    backend = _ready_backend()
    title("omniproxy check")
    argv_sets = [
        ["check", str(source), "--timeout", "1.5", "--sync"],
        ["check", str(source), "--timeout", "1.5", "--anonymity", "--sync", "-o", str(good)],
    ]
    if backend is not None:
        argv_sets.append(
            ["check", str(source), "--backend", backend, "--timeout", "1.5", "--no-async"]
        )
    for argv in argv_sets:
        try:
            code = cli_main(argv)
            print(argv[1:], "exit", code)
        except Exception as exc:
            print(argv[1:], type(exc).__name__, exc)
    if good.exists():
        print("good file", good.read_text(encoding="utf-8") or "<empty>")

    title("omniproxy scrape")
    for argv in (
        ["scrape", "https://example.com/", "--timeout", "8"],
        ["scrape", "https://example.com/", "--timeout", "8", "-o", str(scraped)],
    ):
        try:
            code = cli_main(argv)
            print(argv[1:], "exit", code)
        except Exception as exc:
            print(argv[1:], type(exc).__name__, exc)

    for path in directory.glob("*"):
        path.unlink(missing_ok=True)
    directory.rmdir()


if __name__ == "__main__":
    main()
