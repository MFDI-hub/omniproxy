from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from .backends.factory import get_backend
from .config import settings
from .constants import (
    DEFAULT_CHECK_FIELDS,
    DEFAULT_CHECK_MAX_RETRIES,
    DEFAULT_CHECK_RETRY_BACKOFF,
    DEFAULT_RETRYABLE_HTTP_STATUSES,
    URL_HEADERS_PROBE,
)
from .proxy import Proxy as BaseProxy

if TYPE_CHECKING:
    from .client import AsyncClient, Client
    from .extended_proxy import HealthCheckConfig


@lru_cache(maxsize=1)
def _check_exceptions() -> tuple[type[BaseException], ...]:
    """Build exception tuple on each call so optional deps installed after import are included."""
    out: list[type[BaseException]] = [
        asyncio.TimeoutError,
    ]

    try:
        from python_socks._errors import ProxyConnectionError, ProxyTimeoutError  # type: ignore

        out.extend([ProxyConnectionError, ProxyTimeoutError])
    except ImportError:
        pass

    try:
        import httpx  # type: ignore

        out.append(httpx.HTTPError)
    except ImportError:
        pass
    try:
        import requests  # type: ignore

        out.append(requests.RequestException)
    except ImportError:
        pass
    try:
        import aiohttp  # type: ignore

        out.append(aiohttp.ClientError)
    except ImportError:
        pass
    return tuple(out)


def _default_check_url(*, with_info: bool, fields: str) -> str:
    if with_info:
        return settings.default_check_info_url_template.format(fields=fields)
    return settings.default_check_url


def _classify_anonymity(headers: Mapping[str, str]) -> str:
    for leak in ("x-forwarded-for", "via", "forwarded"):
        for k, v in headers.items():
            if k.lower() == leak and str(v or "").strip():
                return "transparent"
    for k in headers:
        if k.lower() == "proxy-connection":
            return "anonymous"
    return "elite"


@dataclass(slots=True)
class CheckResult:
    """Outcome of :func:`check_proxy` / :func:`acheck_proxy` when ``with_info`` is False."""

    success: bool
    latency: float | None
    exc_type: type[BaseException] | None
    status_code: int | None

    def __bool__(self) -> bool:
        return self.success


def apply_check_result_metadata(
    proxy: BaseProxy,
    *,
    latency: float | None,
    anonymity: str | None,
    status: bool = True,
) -> None:
    proxy._set_attribute("latency", latency)
    proxy._set_attribute("last_checked", time.time())
    proxy._set_attribute("last_status", status)
    if anonymity is not None:
        proxy._set_attribute("anonymity", anonymity)


class Proxy(BaseProxy):
    __slots__ = ()

    def get_info(self, fields: str = DEFAULT_CHECK_FIELDS, **kwargs: Any) -> dict[str, str]:
        return check_proxy(self, with_info=True, fields=fields, **kwargs)[1]  # type: ignore[return-value]

    async def aget_info(self, fields: str = DEFAULT_CHECK_FIELDS, **kwargs: Any) -> dict[str, str]:
        return (await acheck_proxy(self, with_info=True, fields=fields, **kwargs))[1]  # type: ignore[return-value]

    def check(
        self, url: str | None = None, raise_on_error: bool = False, **kwargs: Any
    ) -> CheckResult:
        return check_proxy(self, url=url, raise_on_error=raise_on_error, **kwargs)[1]  # type: ignore[return-value]

    async def acheck(
        self, url: str | None = None, raise_on_error: bool = False, **kwargs: Any
    ) -> CheckResult:
        return (await acheck_proxy(self, url=url, raise_on_error=raise_on_error, **kwargs))[1]  # type: ignore[return-value]

    def get_client(self) -> Client:
        from .backends.httpx_client import Client

        return Client(proxy=self)

    def get_async_client(self) -> AsyncClient:
        from .backends.httpx_client import AsyncClient

        return AsyncClient(proxy=self)


async def _aget_with_anonymity_probe(
    backend_impl: Any,
    url: str,
    proxy: Proxy,
    *,
    timeout: float,
    kwargs: dict[str, Any],
) -> tuple[Any, str | None, float]:
    """Concurrent probe + main check; *latency* is wall time for the main check only."""
    task_anon = asyncio.create_task(
        backend_impl.aget(URL_HEADERS_PROBE, proxy, timeout=timeout, **kwargs)
    )
    t0 = time.perf_counter()
    response = await backend_impl.aget(url, proxy, timeout=timeout, **kwargs)
    latency = time.perf_counter() - t0
    res_anon = await task_anon
    anonymity: str | None = None
    if (
        response.status_code == 200
        and not isinstance(res_anon, Exception)
        and res_anon.status_code == 200
        and isinstance(res_anon.json_data, dict)
        and (hdrs := res_anon.json_data.get("headers"))
        and isinstance(hdrs, dict)
    ):
        anonymity = _classify_anonymity(hdrs)
    return response, anonymity, latency


async def acheck_proxy(
    proxy: Proxy | str,
    url: str | None = None,
    with_info: bool = False,
    fields: str = DEFAULT_CHECK_FIELDS,
    raise_on_error: bool = False,
    backend: str | None = None,
    timeout: float | None = None,
    detect_anonymity: bool = False,
    expected_status: int | None = 200,
    expected_fields: set[str] | None = None,
    *,
    max_retries: int = DEFAULT_CHECK_MAX_RETRIES,
    retry_backoff: float = DEFAULT_CHECK_RETRY_BACKOFF,
    retry_on_status: frozenset[int] | None = None,
    **kwargs: Any,
) -> tuple[Proxy, CheckResult | dict | bool]:
    if not isinstance(proxy, Proxy):
        proxy = Proxy(proxy)

    if not url:
        url = _default_check_url(with_info=with_info, fields=fields)

    backend_impl = get_backend(backend)
    to = timeout if timeout is not None else settings.default_timeout
    retry_status = (
        retry_on_status if retry_on_status is not None else DEFAULT_RETRYABLE_HTTP_STATUSES
    )
    check_exc = _check_exceptions()
    attempts = max(0, int(max_retries)) + 1
    last_error: BaseException | None = None

    for attempt in range(attempts):
        try:
            anonymity: str | None = None

            if detect_anonymity:
                response, anonymity, latency = await _aget_with_anonymity_probe(
                    backend_impl, url, proxy, timeout=to, kwargs=kwargs
                )
            else:
                t0 = time.perf_counter()
                response = await backend_impl.aget(url, proxy, timeout=to, **kwargs)
                latency = time.perf_counter() - t0

            status_ok = (
                response.status_code == expected_status
                if expected_status is not None
                else 200 <= response.status_code < 300
            )

            if status_ok:
                if expected_fields:
                    data = response.json_data
                    if not isinstance(data, dict) or not expected_fields.issubset(data.keys()):
                        apply_check_result_metadata(
                            proxy, latency=latency, anonymity=None, status=False
                        )
                        if with_info:
                            return proxy, False
                        return proxy, CheckResult(False, latency, None, response.status_code)

                if with_info:
                    apply_check_result_metadata(proxy, latency=latency, anonymity=anonymity)
                    return proxy, response.json_data or {}
                apply_check_result_metadata(proxy, latency=latency, anonymity=anonymity)
                return proxy, CheckResult(True, latency, None, response.status_code)

            if response.status_code in retry_status and attempt < attempts - 1:
                await asyncio.sleep(retry_backoff * (attempt + 1))
                continue
            if with_info:
                return proxy, False
            return proxy, CheckResult(False, latency, None, response.status_code)

        except check_exc as er:
            last_error = er
            if raise_on_error:
                raise type(er)(f"{proxy.url} --> {er}") from er
            if attempt < attempts - 1:
                await asyncio.sleep(retry_backoff * (attempt + 1))
                continue
            if with_info:
                return proxy, False
            return proxy, CheckResult(False, None, type(er), None)

    if last_error and raise_on_error:
        raise type(last_error)(f"{proxy.url} --> {last_error}") from last_error
    if with_info:
        return proxy, False
    return proxy, CheckResult(False, None, type(last_error) if last_error else None, None)


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
        success = [(px, info) for px, info in results if info is not False]
        failed = [(px, info) for px, info in results if info is False]
    else:
        success = [px for px, status in results if status]
        failed = [px for px, status in results if not status]

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
    expected_status: int | None = 200,
    expected_fields: set[str] | None = None,
    *,
    max_retries: int = DEFAULT_CHECK_MAX_RETRIES,
    retry_backoff: float = DEFAULT_CHECK_RETRY_BACKOFF,
    retry_on_status: frozenset[int] | None = None,
    **kwargs: Any,
) -> tuple[Proxy, CheckResult | dict | bool]:
    if not isinstance(proxy, Proxy):
        proxy = Proxy(proxy)

    if not url:
        url = _default_check_url(with_info=with_info, fields=fields)

    backend_impl = get_backend(backend)
    to = timeout if timeout is not None else settings.default_timeout
    retry_status = (
        retry_on_status if retry_on_status is not None else DEFAULT_RETRYABLE_HTTP_STATUSES
    )
    check_exc = _check_exceptions()
    attempts = max(0, int(max_retries)) + 1
    last_error: BaseException | None = None

    def _probe_headers() -> str | None:
        try:
            hresp = backend_impl.get(URL_HEADERS_PROBE, proxy, timeout=to, **kwargs)
            if hresp.status_code == 200 and hresp.json_data and isinstance(hresp.json_data, dict):
                hdrs = hresp.json_data.get("headers") or {}
                if isinstance(hdrs, dict):
                    return _classify_anonymity(hdrs)
        except check_exc:
            return None
        return None

    for attempt in range(attempts):
        try:
            anonymity: str | None = None

            if detect_anonymity:
                t0 = time.perf_counter()
                response = backend_impl.get(url, proxy, timeout=to, **kwargs)
                latency = time.perf_counter() - t0
                if response.status_code == 200:
                    anonymity = _probe_headers()
            else:
                t0 = time.perf_counter()
                response = backend_impl.get(url, proxy, timeout=to, **kwargs)
                latency = time.perf_counter() - t0

            # Determine success: respect expected_status if set, otherwise require 200
            status_ok = (
                response.status_code == expected_status
                if expected_status is not None
                else 200 <= response.status_code < 300
            )

            if status_ok:
                # Validate expected fields if requested — requires JSON body
                if expected_fields:
                    data = response.json_data
                    if not isinstance(data, dict) or not expected_fields.issubset(data.keys()):
                        # Reachable but response body didn't match — not retryable
                        apply_check_result_metadata(
                            proxy, latency=latency, anonymity=None, status=False
                        )
                        if with_info:
                            return proxy, False
                        return proxy, CheckResult(False, latency, None, response.status_code)

                if with_info:
                    apply_check_result_metadata(proxy, latency=latency, anonymity=anonymity)
                    return proxy, response.json_data or {}
                apply_check_result_metadata(proxy, latency=latency, anonymity=anonymity)
                return proxy, CheckResult(True, latency, None, response.status_code)

            if response.status_code in retry_status and attempt < attempts - 1:
                time.sleep(retry_backoff * (attempt + 1))
                continue
            if with_info:
                return proxy, False
            return proxy, CheckResult(False, latency, None, response.status_code)

        except check_exc as er:
            last_error = er
            if raise_on_error:
                raise type(er)(f"{proxy.url} --> {er}") from er
            if attempt < attempts - 1:
                time.sleep(retry_backoff * (attempt + 1))
                continue
            if with_info:
                return proxy, False
            return proxy, CheckResult(False, None, type(er), None)

    if last_error and raise_on_error:
        raise type(last_error)(f"{proxy.url} --> {last_error}") from last_error
    if with_info:
        return proxy, False
    return proxy, CheckResult(False, None, type(last_error) if last_error else None, None)


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
    *,
    max_workers: int | None = None,
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

    workers = max_workers if max_workers is not None else min(32, max(1, len(proxy_list)))
    success: list[Any] = []
    failed: list[Any] = []

    def _one(p: Proxy | str) -> tuple[Any, Any]:
        return check_proxy(
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

    indexed: list[tuple[int, tuple[Any, Any]]] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_one, p): i for i, p in enumerate(proxy_list)}
        for fut in as_completed(futs):
            idx = futs[fut]
            indexed.append((idx, fut.result()))
    indexed.sort(key=lambda x: x[0])
    for _, (proxy, info) in indexed:
        if with_info:
            (success if info is not False else failed).append((proxy, info))
        else:
            (success if info else failed).append(proxy)

    return success, failed


def run_health_check(
    proxy: Proxy | str,
    hc: HealthCheckConfig,
    *,
    backend: str | None = None,
) -> tuple[Proxy, CheckResult]:
    """
    Run a full health check against *proxy* using *hc* config.
    """
    from .config import settings

    url = hc.url or settings.default_check_url

    p, result = check_proxy(
        proxy,
        url=url,
        with_info=False,
        expected_status=hc.expected_status,
        expected_fields=hc.expected_fields,
        backend=backend,
        timeout=hc.timeout,
        headers=hc.headers or None,
    )

    return p, result  # type: ignore[return-value]


async def arun_health_check(
    proxy: Proxy | str,
    hc: HealthCheckConfig,
    *,
    backend: str | None = None,
) -> tuple[Proxy, CheckResult]:
    """Async variant of :func:`run_health_check`."""
    from .config import settings

    url = hc.url or settings.default_check_url

    p, result = await acheck_proxy(
        proxy,
        url=url,
        with_info=False,
        expected_status=hc.expected_status,
        expected_fields=hc.expected_fields,
        backend=backend,
        timeout=hc.timeout,
        headers=hc.headers or None,
    )

    return p, result  # type: ignore[return-value]


__all__ = [
    "DEFAULT_CHECK_FIELDS",
    "URL_HEADERS_PROBE",
    "CheckResult",
    "Proxy",
    "acheck_proxies",
    "acheck_proxy",
    "apply_check_result_metadata",
    "check_proxies",
    "check_proxy",
]
