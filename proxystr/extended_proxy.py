from __future__ import annotations

import asyncio
import time
from typing import Any, Mapping

from pydantic.networks import HttpUrl
from python_socks._errors import ProxyConnectionError, ProxyTimeoutError

from . import config
from .backends.factory import get_backend
from .proxy import Proxy as BaseProxy

URL_FOR_CHECK = "https://api.ipify.org/?format=json"
URL_FOR_CHECK_WHITH_INFO = "http://ip-api.com/json/?fields={fields}"
DEFAULT_CHECK_FIELDS = "8211"
# Echo endpoint to inspect forwarded headers (optional anonymity probe)
URL_HEADERS_PROBE = "https://httpbin.org/headers"


def _check_exceptions() -> tuple[type[BaseException], ...]:
    out: list[type[BaseException]] = [
        ProxyConnectionError,
        ProxyTimeoutError,
        asyncio.TimeoutError,
    ]
    try:
        import httpx

        out.append(httpx.HTTPError)
    except ImportError:
        pass
    try:
        import requests

        out.append(requests.RequestException)
    except ImportError:
        pass
    try:
        import aiohttp

        out.append(aiohttp.ClientError)
    except ImportError:
        pass
    return tuple(out)


_CHECK_EXCEPTIONS = _check_exceptions()


def _classify_anonymity(headers: Mapping[str, str]) -> str:
    lower = {k.lower(): v for k, v in headers.items()}
    for leak in ("x-forwarded-for", "via", "forwarded"):
        if leak in lower and str(lower[leak]).strip():
            return "transparent"
    if "proxy-connection" in lower:
        return "anonymous"
    return "elite"


def _apply_check_metadata(
    proxy: Proxy,
    *,
    latency: float | None,
    anonymity: str | None,
) -> None:
    proxy._set_attribute("latency", latency)
    proxy._set_attribute("last_checked", time.time())
    if anonymity is not None:
        proxy._set_attribute("anonymity", anonymity)


class Proxy(BaseProxy):
    def get_info(self, fields: str = DEFAULT_CHECK_FIELDS, **kwargs: Any) -> dict[str, str]:
        return check_proxy(self, with_info=True, fields=fields, **kwargs)[1]

    async def aget_info(self, fields: str = DEFAULT_CHECK_FIELDS, **kwargs: Any) -> dict[str, str]:
        return (await acheck_proxy(self, with_info=True, fields=fields, **kwargs))[1]

    def check(self, url: HttpUrl = URL_FOR_CHECK, raise_on_error=False, **kwargs: Any) -> bool:
        return check_proxy(self, url=url, raise_on_error=raise_on_error, **kwargs)[1]

    async def acheck(
        self, url: HttpUrl = URL_FOR_CHECK, raise_on_error=False, **kwargs: Any
    ) -> bool:
        return (await acheck_proxy(self, url=url, raise_on_error=raise_on_error, **kwargs))[1]

    def get_client(self):
        from .backends.httpx_client import Client

        return Client(proxy=self)

    def get_async_client(self):
        from .backends.httpx_client import AsyncClient

        return AsyncClient(proxy=self)


async def acheck_proxy(
    proxy: Proxy | str,
    url: str | None = None,
    with_info: bool = False,
    fields: str = DEFAULT_CHECK_FIELDS,
    raise_on_error: bool = False,
    backend: str | None = None,
    timeout: float | None = None,
    detect_anonymity: bool = False,
    **kwargs: Any,
) -> tuple[Proxy, bool | dict]:
    if not isinstance(proxy, Proxy):
        proxy = Proxy(proxy)

    if not url:
        url = URL_FOR_CHECK_WHITH_INFO.format(fields=fields) if with_info else URL_FOR_CHECK

    backend_impl = get_backend(backend)
    to = timeout if timeout is not None else config.default_timeout
    anonymity: str | None = None

    try:
        t0 = time.perf_counter()
        response = await backend_impl.aget(url, proxy, timeout=to, **kwargs)
        latency = time.perf_counter() - t0

        if response.status_code == 200:
            if detect_anonymity and not with_info:
                try:
                    hresp = await backend_impl.aget(URL_HEADERS_PROBE, proxy, timeout=to, **kwargs)
                    if (
                        hresp.status_code == 200
                        and hresp.json_data
                        and isinstance(hresp.json_data, dict)
                    ):
                        hdrs = hresp.json_data.get("headers") or {}
                        if isinstance(hdrs, dict):
                            anonymity = _classify_anonymity(hdrs)
                except _CHECK_EXCEPTIONS:
                    anonymity = None

            if with_info:
                _apply_check_metadata(proxy, latency=latency, anonymity=anonymity)
                return proxy, response.json_data or {}
            _apply_check_metadata(proxy, latency=latency, anonymity=anonymity)
            return proxy, True

    except _CHECK_EXCEPTIONS as er:
        if raise_on_error:
            raise type(er)(f"{proxy.url} --> {er}").with_traceback(er.__traceback__)
        return proxy, False

    return proxy, False


async def acheck_proxies(
    proxy_list: list[Proxy | str],
    url: str | None = None,
    with_info: bool = False,
    fields: str = DEFAULT_CHECK_FIELDS,
    raise_on_error: bool = False,
    backend: str | None = None,
    timeout: float | None = None,
    detect_anonymity: bool = False,
    **kwargs: Any,
) -> tuple[list[Proxy], list[Proxy]] | tuple[list[tuple[Proxy, dict]], list[tuple[Proxy, bool]]]:
    tasks = [
        acheck_proxy(
            p,
            url,
            with_info,
            fields,
            raise_on_error,
            backend=backend,
            timeout=timeout,
            detect_anonymity=detect_anonymity,
            **kwargs,
        )
        for p in proxy_list
    ]
    results = await asyncio.gather(*tasks)

    if with_info:
        success = [(proxy, info) for proxy, info in results if info]
        failed = [(proxy, info) for proxy, info in results if not info]
    else:
        success = [proxy for proxy, status in results if status]
        failed = [proxy for proxy, status in results if not status]

    return success, failed


def check_proxy(
    proxy: Proxy | str,
    url: str | None = None,
    with_info: bool = False,
    fields: str = DEFAULT_CHECK_FIELDS,
    raise_on_error: bool = False,
    backend: str | None = None,
    timeout: float | None = None,
    detect_anonymity: bool = False,
    **kwargs: Any,
) -> tuple[Proxy, bool | dict]:
    if not isinstance(proxy, Proxy):
        proxy = Proxy(proxy)

    if not url:
        url = URL_FOR_CHECK_WHITH_INFO.format(fields=fields) if with_info else URL_FOR_CHECK

    backend_impl = get_backend(backend)
    to = timeout if timeout is not None else config.default_timeout
    anonymity: str | None = None

    try:
        t0 = time.perf_counter()
        response = backend_impl.get(url, proxy, timeout=to, **kwargs)
        latency = time.perf_counter() - t0

        if response.status_code == 200:
            if detect_anonymity and not with_info:
                try:
                    hresp = backend_impl.get(URL_HEADERS_PROBE, proxy, timeout=to, **kwargs)
                    if (
                        hresp.status_code == 200
                        and hresp.json_data
                        and isinstance(hresp.json_data, dict)
                    ):
                        hdrs = hresp.json_data.get("headers") or {}
                        if isinstance(hdrs, dict):
                            anonymity = _classify_anonymity(hdrs)
                except _CHECK_EXCEPTIONS:
                    anonymity = None

            if with_info:
                _apply_check_metadata(proxy, latency=latency, anonymity=anonymity)
                return proxy, response.json_data or {}
            _apply_check_metadata(proxy, latency=latency, anonymity=anonymity)
            return proxy, True

    except _CHECK_EXCEPTIONS as er:
        if raise_on_error:
            raise type(er)(f"{proxy.url} --> {er}").with_traceback(er.__traceback__)
        return proxy, False

    return proxy, False


def check_proxies(
    proxy_list: list[Proxy | str],
    url: str | None = None,
    with_info: bool = False,
    fields: str = DEFAULT_CHECK_FIELDS,
    raise_on_error: bool = False,
    use_async: bool = True,
    backend: str | None = None,
    timeout: float | None = None,
    detect_anonymity: bool = False,
    **kwargs: Any,
) -> tuple[list[Proxy], list[Proxy]] | tuple[list[tuple[Proxy, dict]], list[tuple[Proxy, bool]]]:
    if use_async:
        return asyncio.run(
            acheck_proxies(
                proxy_list,
                url,
                with_info,
                fields,
                raise_on_error,
                backend=backend,
                timeout=timeout,
                detect_anonymity=detect_anonymity,
                **kwargs,
            )
        )
    success = []
    failed = []
    for p in proxy_list:
        proxy, info = check_proxy(
            p,
            url,
            with_info,
            fields,
            raise_on_error,
            backend=backend,
            timeout=timeout,
            detect_anonymity=detect_anonymity,
            **kwargs,
        )
        if with_info:
            (success if info else failed).append((proxy, info))
        else:
            (success if info else failed).append(proxy)
    return success, failed
