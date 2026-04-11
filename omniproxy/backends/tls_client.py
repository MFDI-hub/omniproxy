"""tls_client backend."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from ..constants import DEFAULT_BACKEND_TIMEOUT
from ..proxy import Proxy
from .base import BackendResponse, BaseBackend


class TlsClientBackend(BaseBackend):
    name = "tls_client"

    def get(
        self, url: str, proxy: Proxy, *, timeout: float = DEFAULT_BACKEND_TIMEOUT, **kwargs: Any
    ) -> BackendResponse:
        try:
            import tls_client  # type: ignore
        except ImportError as e:
            raise ImportError("Install with 'uv add omniproxy --extra tls_client'") from e

        session = tls_client.Session(
            client_identifier=kwargs.pop("client_identifier", "chrome_120"),
            random_tls_extension_order=kwargs.pop("random_tls_extension_order", True),
        )
        proxies = (
            proxy.as_requests_proxies()
            if "http" in proxy.protocol
            else {"http": proxy.url, "https": proxy.url}
        )
        r = session.get(
            url,
            proxies=proxies,
            timeout_seconds=int(timeout),
            **kwargs,
        )
        jd = None
        with contextlib.suppress(Exception):
            jd = r.json()
        hdrs = r.headers if isinstance(r.headers, dict) else dict(r.headers)
        return BackendResponse(
            status_code=r.status_code,
            headers=hdrs,
            json_data=jd,
            text=r.text if hasattr(r, "text") else str(r.content or ""),
        )

    async def aget(
        self, url: str, proxy: Proxy, *, timeout: float = DEFAULT_BACKEND_TIMEOUT, **kwargs: Any
    ) -> BackendResponse:
        return await asyncio.to_thread(self.get, url, proxy, timeout=timeout, **kwargs)

    def request_direct(
        self, method: str, url: str, *, timeout: float = DEFAULT_BACKEND_TIMEOUT, **kwargs: Any
    ) -> BackendResponse:
        try:
            import tls_client
        except ImportError as e:
            raise ImportError("Install with 'uv add omniproxy --extra tls_client'") from e

        session = tls_client.Session(
            client_identifier=kwargs.pop("client_identifier", "chrome_120"),
            random_tls_extension_order=kwargs.pop("random_tls_extension_order", True),
        )

        METHOD_MAP = {
            "GET": session.get,
            "POST": session.post,
            "PUT": session.put,
            "PATCH": session.patch,
            "DELETE": session.delete,
            "HEAD": session.head,
            "OPTIONS": session.options,
        }

        m = method.upper()
        caller = METHOD_MAP.get(m)
        if caller is None:
            raise ValueError(
                f"tls_client backend does not support method {method!r}. "
                f"Supported: {', '.join(METHOD_MAP)}"
            )

        r = caller(url, proxies=None, timeout_seconds=int(timeout), **kwargs)
        jd = None
        with contextlib.suppress(Exception):
            jd = r.json()
        hdrs = r.headers if isinstance(r.headers, dict) else dict(r.headers)
        return BackendResponse(
            status_code=r.status_code,
            headers=hdrs,
            json_data=jd,
            text=r.text if hasattr(r, "text") else str(r.content or ""),
        )

    async def arequest_direct(
        self, method: str, url: str, *, timeout: float = DEFAULT_BACKEND_TIMEOUT, **kwargs: Any
    ) -> BackendResponse:
        return await asyncio.to_thread(
            lambda: self.request_direct(method, url, timeout=timeout, **kwargs)
        )
