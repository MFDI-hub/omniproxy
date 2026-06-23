"""Load proxies from a newline-separated text file (async-friendly via thread offload)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from ..enum import IoInvalidLinePolicy
from ..io import read_proxies
from ..proxy import Proxy


class FileFetcher:
    """Read proxies from disk using :func:`~omniproxy.io.read_proxies` semantics.

    The file is read on a worker thread to avoid blocking the event loop.

    Attributes:
        _path (Path): File path.
        _encoding (str): Text encoding used to open the file.
        _on_invalid (IoInvalidLinePolicy | str): Bad-line policy.

    Version:
        Added in 4.0.0.
    """

    __slots__ = ("_encoding", "_on_invalid", "_path")

    def __init__(
        self,
        path: str | Path,
        *,
        encoding: str = "utf-8",
        on_invalid: IoInvalidLinePolicy | str = IoInvalidLinePolicy.SKIP,
    ) -> None:
        """Build a file-backed fetcher.

        Args:
            path (str | Path): Path to the proxy file.
            encoding (str): Text encoding for the file.
            on_invalid (IoInvalidLinePolicy | str): How to handle invalid
                lines; defaults to :attr:`IoInvalidLinePolicy.SKIP` so a
                noisy file does not break refresh.

        Version:
            Added in 4.0.0.
        """
        self._path = Path(path)
        self._encoding = encoding
        self._on_invalid = on_invalid

    async def fetch(self) -> list[Proxy | str]:
        """Read the proxy file on a worker thread.

        Returns:
            list[Proxy | str]: Parsed proxies in file order. Invalid lines
            are skipped or raise depending on the configured policy.

        Version:
            Added in 4.0.0.
        """
        def _read():
            return read_proxies(
                self._path,
                encoding=self._encoding,
                on_invalid=self._on_invalid,
            )

        return list(await asyncio.to_thread(_read))
