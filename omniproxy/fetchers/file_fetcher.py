"""Load proxies from a newline-separated text file (async-friendly via thread offload)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from ..enum import IoInvalidLinePolicy
from ..io import read_proxies
from ..proxy import Proxy


class FileFetcher:
    """Read proxies from disk using the same parsing rules as :func:`~omniproxy.io.read_proxies`."""

    __slots__ = ("_encoding", "_on_invalid", "_path")

    def __init__(
        self,
        path: str | Path,
        *,
        encoding: str = "utf-8",
        on_invalid: IoInvalidLinePolicy | str = IoInvalidLinePolicy.SKIP,
    ) -> None:
        self._path = Path(path)
        self._encoding = encoding
        self._on_invalid = on_invalid

    async def fetch(self) -> list[Proxy | str]:
        def _read():
            return read_proxies(
                self._path,
                encoding=self._encoding,
                on_invalid=self._on_invalid,
            )

        return list(await asyncio.to_thread(_read))
