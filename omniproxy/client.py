"""httpx Client wrappers; loaded only when httpx extra is installed."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .backends.httpx_client import AsyncClient, Client


def __getattr__(name: str):
    """Resolve ``Client`` or ``AsyncClient`` from ``backends.httpx_client`` lazily.

    Args:
        name (str): ``"Client"`` or ``"AsyncClient"``.

    Returns:
        type: The httpx-backed client class.

    Raises:
        AttributeError: If *name* is not recognized.

    Example:
        >>> from omniproxy import client as c  # doctest: +SKIP
        >>> c.Client  # doctest: +SKIP
    """
    if name == "Client":
        from .backends.httpx_client import Client as _Client

        return _Client
    if name == "AsyncClient":
        from .backends.httpx_client import AsyncClient as _AsyncClient

        return _AsyncClient
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["AsyncClient", "Client"]
