"""httpx Client wrappers; loaded only when httpx extra is installed."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .backends.httpx_client import AsyncClient, Client


def __getattr__(name: str):
    """Lazily resolve ``Client`` / ``AsyncClient`` from the httpx backend.

    Deferring the import keeps ``omniproxy`` importable when the optional
    ``httpx`` extra is not installed, while still letting ``omniproxy.client``
    expose the wrappers when accessed.

    Args:
        name (str): Attribute being accessed. Must be ``"Client"`` or
            ``"AsyncClient"``.

    Returns:
        type: The corresponding httpx-backed client class.

    Raises:
        AttributeError: If ``name`` is not a known attribute on this module.

    Example:
        >>> from omniproxy import client as c  # doctest: +SKIP
        >>> c.Client  # doctest: +SKIP

    Version:
        Added in 4.0.0.
    """
    if name == "Client":
        from .backends.httpx_client import Client as _Client

        return _Client
    if name == "AsyncClient":
        from .backends.httpx_client import AsyncClient as _AsyncClient

        return _AsyncClient
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["AsyncClient", "Client"]
