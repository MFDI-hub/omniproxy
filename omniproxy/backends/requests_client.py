"""requests-based sync/async backend."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from ..constants import DEFAULT_BACKEND_TIMEOUT
from ..proxy import Proxy
from .base import BackendResponse, BaseBackend


class RequestsBackend(BaseBackend):
    name = "requests"

    def get(
        self, url: str, proxy: Proxy, *, timeout: float = DEFAULT_BACKEND_TIMEOUT, **kwargs: Any
    ) -> BackendResponse:
        import requests  # type: ignore

        proxies = proxy.as_requests_proxies()
        r = requests.get(url, proxies=proxies, timeout=timeout, **kwargs)
        jd = None
        with contextlib.suppress(Exception):
            jd = r.json()
        return BackendResponse(
            status_code=r.status_code,
            headers=dict(r.headers.items()),
            json_data=jd,
            text=r.text,
        )

    async def aget(
        self, url: str, proxy: Proxy, *, timeout: float = DEFAULT_BACKEND_TIMEOUT, **kwargs: Any
    ) -> BackendResponse:
        return await asyncio.to_thread(self.get, url, proxy, timeout=timeout, **kwargs)

    def request_direct(
        self, method: str, url: str, *, timeout: float = DEFAULT_BACKEND_TIMEOUT, **kwargs: Any
    ) -> BackendResponse:
        import requests  # type: ignore

        r = requests.request(method.upper(), url, timeout=timeout, **kwargs)
        jd = None
        with contextlib.suppress(Exception):
            jd = r.json()
        return BackendResponse(
            status_code=r.status_code,
            headers=dict(r.headers.items()),
            json_data=jd,
            text=r.text,
        )

    async def arequest_direct(
        self, method: str, url: str, *, timeout: float = DEFAULT_BACKEND_TIMEOUT, **kwargs: Any
    ) -> BackendResponse:
        return await asyncio.to_thread(
            lambda: self.request_direct(method, url, timeout=timeout, **kwargs)
        )
