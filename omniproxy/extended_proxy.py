from __future__ import annotations

import asyncio
import random
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
from .proxy import SimpleProxy

if TYPE_CHECKING:
    from .client import AsyncClient, Client
    from .config import HealthCheckConfig


@lru_cache(maxsize=1)
def _check_exceptions() -> tuple[type[BaseException], ...]:
    """Collect exception types treated as proxy check failures (lazy optional deps).

    Returns:
        tuple[type[BaseException], ...]: Tuple merged from asyncio, httpx, requests, aiohttp, etc.

    Example:
        >>> isinstance(_check_exceptions(), tuple)
        True
    """
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


def _pick_rotated_default_url(
    *,
    with_info: bool,
    fields: str,
    attempt: int,
    last_url: str | None,
) -> str:
    """Choose a check URL from configured templates, optionally rotating between attempts.

    Args:
        with_info (bool): Use info templates vs plain check URLs.
        fields (str): ``fields`` query fragment for info templates.
        attempt (int): Zero-based attempt index.
        last_url (str | None): Previous pick for rotation when multiple URLs exist.

    Returns:
        str: URL string to request.

    Example:
        >>> isinstance(
        ...     _pick_rotated_default_url(with_info=False, fields="", attempt=0, last_url=None), str
        ... )
        True
    """
    templates = (
        settings.default_check_info_url_templates if with_info else settings.default_check_urls
    )
    urls = [t.format(fields=fields) for t in templates] if with_info else list(templates)
    if len(urls) == 1:
        return urls[0]
    if attempt == 0 or last_url is None:
        return random.choice(urls)
    others = [u for u in urls if u != last_url]
    return random.choice(others if others else urls)


def _resolve_health_check_url(hc_url: str | None) -> str:
    """Resolve explicit health URL or fall back to global defaults.

    Args:
        hc_url (str | None): Per-config URL override.

    Returns:
        str: Concrete URL to probe.

    Example:
        >>> isinstance(_resolve_health_check_url(None), str)
        True
    """
    if hc_url:
        return hc_url
    urls = settings.health_check_urls or settings.default_check_urls
    return random.choice(urls)


def _classify_anonymity(headers: Mapping[str, str]) -> str:
    """Infer anonymity tier from response headers (transparent / anonymous / elite).

    Args:
        headers (Mapping[str, str]): Normalised header names to values.

    Returns:
        str: ``"transparent"``, ``"anonymous"``, or ``"elite"``.

    Example:
        >>> _classify_anonymity({"X-Forwarded-For": "1.2.3.4"})
        'transparent'
    """
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
    """Structured outcome returned from :func:`check_proxy` / :func:`acheck_proxy` when ``with_info=False``.

    On success, metadata is also written onto the :class:`Proxy` via
    :func:`apply_check_result_metadata`. :meth:`__bool__` delegates to :attr:`success` so
    ``if result:`` works idiomatically.

    Attributes
    ----------
    success: :class:`bool`
        ``True`` when the HTTP status (and optional JSON key checks) satisfied the call.
    latency: Optional[:class:`float`]
        Wall-clock seconds for the main request when measured; ``None`` on hard transport errors.
    exc_type
        Optional exception **type** (not an instance) caught during the attempt, if any.
    status_code: Optional[:class:`int`]
        HTTP status from the backend response when a response existed; ``None`` on connection errors.
    """

    success: bool
    latency: float | None
    exc_type: type[BaseException] | None
    status_code: int | None

    def __bool__(self) -> bool:
        """Return :attr:`success` for truthiness tests.

        Returns:
            bool: Same as :attr:`success`.

        Example:
            >>> bool(CheckResult(False, None, None, None))
            False
        """
        return self.success


def apply_check_result_metadata(
    proxy: SimpleProxy,
    *,
    latency: float | None,
    anonymity: str | None,
    status: bool = True,
    country: str | None = None,
    city: str | None = None,
    asn: str | None = None,
    org: str | None = None,
) -> None:
    """Write check metadata fields onto a :class:`Proxy` instance.

    Args:
        proxy (SimpleProxy): Target proxy (must support :meth:`~omniproxy.proxy.Proxy._set_attribute`).
        latency (float | None): Observed latency.
        anonymity (str | None): Classified anonymity label, if known.
        status (bool): ``last_status`` flag for this check.
        country (str | None): Optional geo field.
        city (str | None): Optional geo field.
        asn (str | None): Optional ASN label.
        org (str | None): Optional organisation label.

    Returns:
        None

    Example:
        >>> from omniproxy.extended_proxy import Proxy, apply_check_result_metadata
        >>> p = Proxy("127.0.0.1:1")
        >>> apply_check_result_metadata(p, latency=1.0, anonymity="elite")
        >>> p.anonymity
        'elite'
    """
    proxy._set_attribute("latency", latency)
    proxy._set_attribute("last_checked", time.time())
    proxy._set_attribute("last_status", status)
    if anonymity is not None:
        proxy._set_attribute("anonymity", anonymity)
    if country is not None:
        proxy._set_attribute("country", country)
    if city is not None:
        proxy._set_attribute("city", city)
    if asn is not None:
        proxy._set_attribute("asn", asn)
    if org is not None:
        proxy._set_attribute("org", org)


class Proxy(SimpleProxy):
    """Public subclass of :class:`~omniproxy.proxy.Proxy` used throughout omniproxy I/O and pooling.

    Adds convenience wrappers around :func:`check_proxy`, :func:`acheck_proxy`, and JSON **info**
    endpoints, plus factories for :class:`~omniproxy.backends.httpx_client.Client` /
    :class:`~omniproxy.backends.httpx_client.AsyncClient`. All structural and metadata **slots**
    are identical to the base class; behaviour differs only through these methods.

    .. seealso::

        :class:`~omniproxy.proxy.Proxy` for the full slot / immutability reference.
    """

    __slots__ = ()

    def get_info(self, fields: str = DEFAULT_CHECK_FIELDS, **kwargs: Any) -> dict[str, str]:
        """Run a synchronous info check and return the JSON mapping.

        Args:
            fields (str): ``fields`` argument for default info URL templates.
            **kwargs (Any): Forwarded to :func:`check_proxy`.

        Returns:
            dict[str, str]: Parsed JSON body from the info endpoint.

        Example:
            >>> Proxy("127.0.0.1:1").get_info  # doctest: +ELLIPSIS
            <bound method Proxy.get_info of ...>
        """
        return check_proxy(self, with_info=True, fields=fields, **kwargs)[1]  # type: ignore[return-value]

    async def aget_info(self, fields: str = DEFAULT_CHECK_FIELDS, **kwargs: Any) -> dict[str, str]:
        """Async variant of :meth:`get_info`.

        Args:
            fields (str): ``fields`` for info URL templates.
            **kwargs (Any): Forwarded to :func:`acheck_proxy`.

        Returns:
            dict[str, str]: Parsed JSON body.

        Example:
            >>> Proxy("127.0.0.1:1").aget_info  # doctest: +ELLIPSIS
            <bound method Proxy.aget_info of ...>
        """
        return (await acheck_proxy(self, with_info=True, fields=fields, **kwargs))[1]  # type: ignore[return-value]

    def check(
        self, url: str | None = None, raise_on_error: bool = False, **kwargs: Any
    ) -> CheckResult:
        """Run :func:`check_proxy` on this instance and return only the :class:`CheckResult`.

        Args:
            url (str | None): Explicit URL; default rotates configured check URLs.
            raise_on_error (bool): Whether to re-raise transport errors.
            **kwargs (Any): Forwarded to :func:`check_proxy`.

        Returns:
            CheckResult: Structured outcome.

        Example:
            >>> Proxy("127.0.0.1:1").check  # doctest: +ELLIPSIS
            <bound method Proxy.check of ...>
        """
        return check_proxy(self, url=url, raise_on_error=raise_on_error, **kwargs)[1]  # type: ignore[return-value]

    async def acheck(
        self, url: str | None = None, raise_on_error: bool = False, **kwargs: Any
    ) -> CheckResult:
        """Async variant of :meth:`check`.

        Args:
            url (str | None): Optional explicit check URL.
            raise_on_error (bool): Propagate errors when ``True``.
            **kwargs (Any): Forwarded to :func:`acheck_proxy`.

        Returns:
            CheckResult: Structured outcome.

        Example:
            >>> Proxy("127.0.0.1:1").acheck  # doctest: +ELLIPSIS
            <bound method Proxy.acheck of ...>
        """
        return (await acheck_proxy(self, url=url, raise_on_error=raise_on_error, **kwargs))[1]  # type: ignore[return-value]

    def get_client(self) -> Client:
        """Build a synchronous :class:`~omniproxy.backends.httpx_client.Client` using this proxy.

        Returns:
            Client: Configured httpx client.

        Example:
            >>> Proxy("127.0.0.1:1").get_client  # doctest: +ELLIPSIS
            <bound method Proxy.get_client of ...>
        """
        from .backends.httpx_client import Client

        return Client(proxy=self)

    def get_async_client(self) -> AsyncClient:
        """Build an :class:`~omniproxy.backends.httpx_client.AsyncClient` using this proxy.

        Returns:
            AsyncClient: Configured async httpx client.

        Example:
            >>> Proxy("127.0.0.1:1").get_async_client  # doctest: +ELLIPSIS
            <bound method Proxy.get_async_client of ...>
        """
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
    """Run main check concurrently with a headers probe for anonymity classification.

    Args:
        backend_impl (Any): Backend instance implementing ``aget``.
        url (str): Primary check URL.
        proxy (Proxy): Proxy to route through.
        timeout (float): Per-request timeout.
        kwargs (dict[str, Any]): Extra keyword args forwarded to ``aget``.

    Returns:
        tuple[Any, str | None, float]: ``(response, anonymity_label, main_latency)``.

    Example:
        >>> _aget_with_anonymity_probe.__name__
        '_aget_with_anonymity_probe'
    """
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
    """Async HTTP check through *proxy* with optional info JSON and retries.

    Args:
        proxy (Proxy | str): Target proxy instance or raw string.
        url (str | None): Explicit URL; if omitted, uses rotated defaults from settings.
        with_info (bool): If ``True``, return parsed JSON dict (or ``False`` on failure).
        fields (str): ``fields`` template parameter when using info URLs.
        raise_on_error (bool): Re-raise backend errors after wrapping when ``True``.
        backend (str | None): Backend name override.
        timeout (float | None): Per-request timeout override.
        detect_anonymity (bool): Run extra probe to classify anonymity.
        expected_status (int | None): Treat this status as success, or any 2xx if ``None``.
        expected_fields (set[str] | None): Require these keys in JSON body when set.
        max_retries (int): Extra attempts after the first.
        retry_backoff (float): Base seconds multiplied by attempt index for backoff sleeps.
        retry_on_status (frozenset[int] | None): HTTP statuses that trigger retry.
        **kwargs (Any): Forwarded to backend ``aget``.

    Returns:
        tuple[Proxy, CheckResult | dict | bool]: Updated proxy plus result payload.

    Example:
        >>> acheck_proxy.__name__
        'acheck_proxy'
    """
    if not isinstance(proxy, Proxy):
        proxy = Proxy(proxy)

    backend_impl = get_backend(backend)
    to = timeout if timeout is not None else settings.default_timeout
    retry_status = (
        retry_on_status if retry_on_status is not None else DEFAULT_RETRYABLE_HTTP_STATUSES
    )
    check_exc = _check_exceptions()
    attempts = max(0, int(max_retries)) + 1
    last_error: BaseException | None = None
    last_pick: str | None = None

    for attempt in range(attempts):
        if url:
            current_url = url
        else:
            current_url = _pick_rotated_default_url(
                with_info=with_info,
                fields=fields,
                attempt=attempt,
                last_url=last_pick,
            )
            last_pick = current_url
        try:
            anonymity: str | None = None

            if detect_anonymity:
                response, anonymity, latency = await _aget_with_anonymity_probe(
                    backend_impl, current_url, proxy, timeout=to, kwargs=kwargs
                )
            else:
                t0 = time.perf_counter()
                response = await backend_impl.aget(current_url, proxy, timeout=to, **kwargs)
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
    """Check many proxies concurrently via :func:`acheck_proxy`.

    Args:
        proxy_list (list[Proxy | str]): Proxies to test in parallel.
        url (str | None): Shared explicit URL or default rotation.
        with_info (bool): Whether to collect JSON info tuples vs boolean results.
        fields (str): Info-template ``fields`` parameter.
        raise_on_error (bool): Forwarded to each :func:`acheck_proxy` call.
        backend (str | None): Shared backend override.
        timeout (float | None): Shared timeout override.
        detect_anonymity (bool): Forwarded per-proxy.
        **kwargs (Any): Extra args forwarded to each check.

    Returns:
        tuple[list[Proxy], list[Proxy]] | tuple[list[tuple[Proxy, dict]], list[tuple[Proxy, bool]]]:
        ``(good, bad)`` partition in original completion order for the async path.

    Example:
        >>> acheck_proxies.__name__
        'acheck_proxies'
    """
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
    """Synchronous HTTP check through *proxy* (same semantics as :func:`acheck_proxy`).

    Args:
        proxy (Proxy | str): Target proxy or raw string.
        url (str | None): Explicit URL or default rotation.
        with_info (bool): Return JSON dict on success when ``True``.
        fields (str): Info URL ``fields`` fragment.
        raise_on_error (bool): Propagate wrapped errors when ``True``.
        backend (str | None): Backend override.
        timeout (float | None): Timeout override.
        detect_anonymity (bool): Extra probe for anonymity tier.
        expected_status (int | None): Success status or 2xx range if ``None``.
        expected_fields (set[str] | None): Required JSON keys when set.
        max_retries (int): Retry budget.
        retry_backoff (float): Backoff base seconds.
        retry_on_status (frozenset[int] | None): Retriable HTTP statuses.
        **kwargs (Any): Forwarded to backend ``get``.

    Returns:
        tuple[Proxy, CheckResult | dict | bool]: Proxy plus outcome.

    Example:
        >>> check_proxy.__name__
        'check_proxy'
    """
    if not isinstance(proxy, Proxy):
        proxy = Proxy(proxy)

    backend_impl = get_backend(backend)
    to = timeout if timeout is not None else settings.default_timeout
    retry_status = (
        retry_on_status if retry_on_status is not None else DEFAULT_RETRYABLE_HTTP_STATUSES
    )
    check_exc = _check_exceptions()
    attempts = max(0, int(max_retries)) + 1
    last_error: BaseException | None = None
    last_pick: str | None = None

    def _probe_headers() -> str | None:
        """Classify anonymity from a sync probe to :data:`~omniproxy.constants.URL_HEADERS_PROBE`.

        Returns:
            str | None: Label from :func:`_classify_anonymity`, or ``None`` on errors.

        Example:
            >>> _classify_anonymity({"Via": "1.1 proxy"})
            'transparent'
        """
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
        if url:
            current_url = url
        else:
            current_url = _pick_rotated_default_url(
                with_info=with_info,
                fields=fields,
                attempt=attempt,
                last_url=last_pick,
            )
            last_pick = current_url
        try:
            anonymity: str | None = None

            if detect_anonymity:
                t0 = time.perf_counter()
                response = backend_impl.get(current_url, proxy, timeout=to, **kwargs)
                latency = time.perf_counter() - t0
                if response.status_code == 200:
                    anonymity = _probe_headers()
            else:
                t0 = time.perf_counter()
                response = backend_impl.get(current_url, proxy, timeout=to, **kwargs)
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
    """Check a list of proxies using asyncio or a thread pool.

    Args:
        proxy_list (list[Proxy | str]): Inputs to check.
        url (str | None): Shared URL override.
        with_info (bool): Collect per-proxy JSON when ``True``.
        fields (str): Info URL fields template value.
        raise_on_error (bool): Forwarded to each check.
        use_async (bool): If ``True``, run :func:`acheck_proxies` inside ``asyncio.run``.
        backend (str | None): Backend override.
        timeout (float | None): Timeout override.
        detect_anonymity (bool): Forwarded per check.
        max_workers (int | None): Thread pool size when ``use_async`` is ``False``.
        **kwargs (Any): Forwarded to underlying check functions.

    Returns:
        tuple[list[Proxy], list[Proxy]] | tuple[list[tuple[Proxy, dict]], list[tuple[Proxy, bool]]]:
        ``(successes, failures)`` shaped like :func:`acheck_proxies`.

    Example:
        >>> check_proxies.__name__
        'check_proxies'
    """
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
        """Run :func:`check_proxy` for a single list entry (thread worker).

        Args:
            p (Proxy | str): One proxy from *proxy_list*.

        Returns:
            tuple[Any, Any]: ``(proxy, result)`` pair from :func:`check_proxy`.

        Example:
            >>> check_proxies._one
            'check_proxies'
        """
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
    url = _resolve_health_check_url(hc.url)

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
    """Async variant of :func:`run_health_check`.

    Args:
        proxy (Proxy | str): Proxy to test.
        hc (HealthCheckConfig): Health probe configuration.
        backend (str | None): Optional backend override.

    Returns:
        tuple[Proxy, CheckResult]: Proxy plus :class:`CheckResult`.

    Example:
        >>> arun_health_check.__name__
        'arun_health_check'
    """
    url = _resolve_health_check_url(hc.url)

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
    "arun_health_check",
    "check_proxies",
    "check_proxy",
    "run_health_check",
]
