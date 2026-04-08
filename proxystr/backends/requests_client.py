"""requests-based sync/async backend."""

from __future__ import annotations

import asyncio
from typing import Any

from ..proxy import Proxy
from .base import BackendResponse, BaseBackend


class RequestsBackend(BaseBackend):
    name = "requests"

    def get(self, url: str, proxy: Proxy, *, timeout: float = 10.0, **kwargs: Any) -> BackendResponse:
        import requests

        proxies = proxy.dict
        r = requests.get(url, proxies=proxies, timeout=timeout, **kwargs)
        jd = None
        try:
            jd = r.json()
        except Exception:
            pass
        return BackendResponse(
            status_code=r.status_code,
            headers={k: v for k, v in r.headers.items()},
            json_data=jd,
            text=r.text,
        )

    async def aget(self, url: str, proxy: Proxy, *, timeout: float = 10.0, **kwargs: Any) -> BackendResponse:
        return await asyncio.to_thread(self.get, url, proxy, timeout=timeout, **kwargs)
