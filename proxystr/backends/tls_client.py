"""tls_client backend."""

from __future__ import annotations

import asyncio
from typing import Any

from ..proxy import Proxy
from .base import BackendResponse, BaseBackend


class TlsClientBackend(BaseBackend):
    name = "tls_client"

    def get(self, url: str, proxy: Proxy, *, timeout: float = 10.0, **kwargs: Any) -> BackendResponse:
        try:
            import tls_client
        except ImportError as e:
            raise ImportError("Install with 'uv add proxystr --extra tls_client'") from e

        session = tls_client.Session(
            client_identifier=kwargs.pop("client_identifier", "chrome_120"),
            random_tls_extension_order=kwargs.pop("random_tls_extension_order", True),
        )
        proxies = proxy.dict if "http" in proxy.protocol else {"http": proxy.url, "https": proxy.url}
        r = session.get(
            url,
            proxies=proxies,
            timeout_seconds=int(timeout),
            **kwargs,
        )
        jd = None
        try:
            jd = r.json()
        except Exception:
            pass
        hdrs = r.headers if isinstance(r.headers, dict) else dict(r.headers)
        return BackendResponse(
            status_code=r.status_code,
            headers=hdrs,
            json_data=jd,
            text=r.text if hasattr(r, "text") else str(r.content or ""),
        )

    async def aget(self, url: str, proxy: Proxy, *, timeout: float = 10.0, **kwargs: Any) -> BackendResponse:
        return await asyncio.to_thread(self.get, url, proxy, timeout=timeout, **kwargs)
