"""httpx Client wrappers; loaded only when httpx extra is installed."""

from __future__ import annotations


def __getattr__(name: str):
    if name == "Client":
        from .backends.httpx_client import Client as _Client

        return _Client
    if name == "AsyncClient":
        from .backends.httpx_client import AsyncClient as _AsyncClient

        return _AsyncClient
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["AsyncClient", "Client"]
