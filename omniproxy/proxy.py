from __future__ import annotations

import ipaddress
import re
from typing import Any, Literal, TypedDict, cast

import msgspec
import orjson
from typing_extensions import Self

# from .adapter import _ExtraTypeConstructor
from .constants import (
    DEFAULT_PROXY_PATTERN_STRING,
    PROXY_METADATA_FIELDS,
    PROXY_PATTERN_ALLOWED_WORDS,
    PROXY_STRUCTURAL_FIELDS,
)
from .utils import OmniproxyParser, get_formatted_proxy_string


class PlaywrightProxySettings(TypedDict, total=False):
    """Dictionary shape accepted by Playwright APIs such as ``browser.new_context(proxy=...)``.

    All keys are optional at the type level (``total=False``); supply at least ``server`` for a
    typical forward proxy. Use :attr:`Proxy.playwright` on :class:`Proxy` to build a ready-made
    instance from a parsed proxy string.

    Attributes
    ----------
    server: :class:`str`
        Full proxy endpoint URL, e.g. ``http://127.0.0.1:8080`` or a ``socks5://`` URL.
        Mirrors :attr:`Proxy.server`.
    bypass: Optional[:class:`str`]
        Comma-separated hostnames that should not use the proxy, if the driver supports it.
    username: Optional[:class:`str`]
        Proxy authentication username when required.
    password: Optional[:class:`str`]
        Proxy authentication password when required.
    """

    server: str
    bypass: str | None
    username: str | None
    password: str | None


class ProxyPattern(str):
    """Subclass of :class:`str` that validates a format template for :class:`Proxy` stringification.

    The template may interleave literal characters with **structural field names** (whole words)
    drawn from :attr:`ALLOWED_WORDS`. Each alphanumeric token in the pattern that matches the
    regex ``\\w+`` must be one of those names; any other token raises :exc:`ValueError`.

    .. note::

        Field names are replaced by :func:`~omniproxy.utils.get_formatted_proxy_string` when
        rendering a :class:`Proxy` or :class:`~omniproxy.utils.OmniproxyParser`. Optional segments
        (credentials, ``rotation_url`` in brackets) are collapsed when values are missing.

    Attributes
    ----------
    ALLOWED_WORDS
        Class attribute: :class:`frozenset` of permitted template tokens, aligned with
        :data:`~omniproxy.constants.PROXY_STRUCTURAL_FIELDS` — ``protocol``, ``username``,
        ``password``, ``ip``, ``port``, ``rotation_url``.
    """

    __slots__ = ()
    ALLOWED_WORDS = PROXY_PATTERN_ALLOWED_WORDS

    def __new__(cls, pattern: str) -> ProxyPattern:
        """Validate *pattern* tokens and return an immutable :class:`ProxyPattern` string.

        Args:
            pattern (str): Template containing only allowed word tokens.

        Returns:
            ProxyPattern: Validated pattern subclassing ``str``.

        Raises:
            ValueError: If an unknown token appears in *pattern*.

        Example:
            >>> ProxyPattern("protocol://ip:port")
            ProxyPattern('protocol://ip:port')
        """
        for token in re.findall(r"\w+", pattern):
            if token not in cls.ALLOWED_WORDS:
                raise ValueError(f"Unexpected word {token!r} in proxy pattern")
        return str.__new__(cls, pattern)


class Proxy(str):
    """Immutable subclass of :class:`str` representing one proxy endpoint.

    The value of the string is the canonical URL produced from parsed structural fields using
    :attr:`default_pattern` (a :class:`ProxyPattern`). Parsed **structural** and **metadata**
    attributes live on ``__slots__``; both groups are **read-only** after construction
    (:meth:`__setattr__` blocks writes). To record check outcomes or geo data, use the extended
    :class:`~omniproxy.extended_proxy.Proxy` together with
    :func:`~omniproxy.extended_proxy.apply_check_result_metadata`, which calls the internal
    :meth:`_set_attribute` for metadata keys only.

    .. note::

        For typing and tooling, structural field names match :data:`~omniproxy.constants.PROXY_STRUCTURAL_FIELDS`
        and metadata names match :data:`~omniproxy.constants.PROXY_METADATA_FIELDS`.

    Attributes
    ----------
    protocol: :class:`str`
        Normalised scheme: ``http``, ``https``, ``socks4``, or ``socks5``.
    username: Optional[:class:`str`]
        Username for proxy authentication, if present in the source string.
    password: Optional[:class:`str`]
        Password for proxy authentication, if present.
    ip: :class:`str`
        Hostname or IP literal. IPv6 addresses may include brackets (e.g. ``\"[2001:db8::1]\"``)
        after validation in :class:`~omniproxy.utils.OmniproxyParser`.
    port: :class:`int`
        TCP port in the inclusive range ``1``-``65535``.
    rotation_url: Optional[:class:`str`]
        Optional URL used for mobile / upstream rotation (parsed from bracket suffix in some formats).
        Used by :meth:`rotate` / :meth:`arotate`.
    latency: Optional[:class:`float`]
        Last measured round-trip latency in seconds from a check, if set.
    anonymity: Optional[:class:`str`]
        Last classified tier: typically ``transparent``, ``anonymous``, or ``elite``.
    last_checked: Optional[:class:`float`]
        Unix timestamp from the last successful metadata write via checks.
    last_status: Optional[:class:`bool`]
        Whether the last explicit check considered this proxy working.
    country: Optional[:class:`str`]
        Optional country label from geo / info APIs.
    city: Optional[:class:`str`]
        Optional city label.
    asn: Optional[:class:`str`]
        Optional autonomous system identifier or label.
    org: Optional[:class:`str`]
        Optional organisation name from info endpoints.

    Class attributes
    ----------------
    default_pattern: :class:`ProxyPattern`
        Template applied when constructing new instances (see :meth:`set_default_pattern`).
    """

    __slots__ = (
        "anonymity",
        "asn",
        "city",
        "country",
        "ip",
        "last_checked",
        "last_status",
        "latency",
        "org",
        "password",
        "port",
        "protocol",
        "rotation_url",
        "username",
    )

    # Explicit annotations are required for static type checkers to treat __slots__ as instance attributes.
    # Without them checkers report '"Proxy" has no attribute "X"' for every slot access.
    protocol: str
    ip: str
    port: int
    username: str | None
    password: str | None
    rotation_url: str | None
    latency: float | None
    anonymity: str | None
    last_checked: float | None
    last_status: bool | None
    country: str | None
    city: str | None
    asn: str | None
    org: str | None

    default_pattern = ProxyPattern(DEFAULT_PROXY_PATTERN_STRING)
    _structural_attributes = PROXY_STRUCTURAL_FIELDS
    _metadata_attributes = PROXY_METADATA_FIELDS
    # Frozen at class body: structural + metadata names guarded by __setattr__
    _protected_attributes: frozenset[str] = frozenset(_structural_attributes + _metadata_attributes)

    def __new__(cls, proxy: str | Proxy, /, protocol: str | None = None) -> Self:
        """Create a :class:`Proxy` from a string or existing instance, optionally changing protocol.

        Args:
            proxy (str | Proxy): Raw proxy text or an existing instance.
            protocol (str | None): If set, override ``http``/``https``/``socks4``/``socks5``.

        Returns:
            Proxy: New immutable proxy string subclass with populated slots.

        Raises:
            ValueError: If *proxy* is invalid or *protocol* is unsupported.

        Example:
            >>> Proxy("socks5://1.1.1.1:1080").protocol
            'socks5'
        """
        # 1. FAST PATH
        if isinstance(proxy, Proxy):
            if protocol is None or protocol.lower() == (proxy.protocol or "").lower():
                return cast(Self, proxy)

            dumped_data = proxy.to_dict()
            p = protocol.lower()
            if p not in ("http", "https", "socks5", "socks4"):
                raise ValueError(f"Unsupported protocol: {protocol!r}")

            dumped_data["protocol"] = p
            proxy_model = OmniproxyParser(**dumped_data)

        # 2. RAW STRING PATH
        else:
            proxy_model = OmniproxyParser.from_string(proxy)
            if protocol:
                p = protocol.lower()
                if p not in ("http", "https", "socks5", "socks4"):
                    raise ValueError(f"Unsupported protocol: {protocol!r}")
                proxy_model = msgspec.structs.replace(proxy_model, protocol=p)

        # BUG FIX: Move this out of the `else` block to apply to both paths
        dumped_data = msgspec.structs.asdict(proxy_model)

        # 3. Create the underlying string representation
        proxy_string = get_formatted_proxy_string(proxy_model, cls.default_pattern)
        instance = super().__new__(cls, proxy_string)

        # 4. Populate structural attributes natively
        for attr in cls._structural_attributes:
            object.__setattr__(instance, attr, dumped_data.get(attr))

        # 5. Initialize metadata attributes
        object.__setattr__(instance, "latency", None)
        object.__setattr__(instance, "anonymity", None)
        object.__setattr__(instance, "last_checked", None)
        object.__setattr__(instance, "last_status", None)
        object.__setattr__(instance, "country", None)
        object.__setattr__(instance, "city", None)
        object.__setattr__(instance, "asn", None)
        object.__setattr__(instance, "org", None)

        return instance

    def _set_attribute(self, key: str, value: Any) -> None:
        """Set a single metadata slot (used by health/check helpers).

        Args:
            key (str): Metadata attribute name (must be in ``_metadata_attributes``).
            value (Any): New value.

        Returns:
            None

        Raises:
            AttributeError: If *key* is not a metadata field.

        Example:
            >>> p = Proxy("127.0.0.1:1")
            >>> p._set_attribute("latency", 12.3)
            >>> p.latency
            12.3
        """
        if key not in self._metadata_attributes:
            raise AttributeError(f"'_set_attribute' only supports metadata keys, not {key!r}")
        object.__setattr__(self, key, value)

    @property
    def address(self) -> str:
        """IP address or hostname (alias of :attr:`ip`).

        Returns:
            str: Same value as :attr:`ip`.

        Example:
            >>> Proxy("127.0.0.1:1").address
            '127.0.0.1'
        """
        return self.ip

    @property
    def login(self) -> str | None:
        """Alias for :attr:`username`.

        Returns:
            str | None: Auth username if present.

        Example:
            >>> Proxy("http://u:p@127.0.0.1:1").login
            'u'
        """
        return self.username

    @property
    def url(self) -> str:
        """Canonical string form of this proxy (same as ``str(proxy)``).

        Returns:
            str: Full proxy URL string.

        Example:
            >>> Proxy("127.0.0.1:80").url.startswith("http")
            True
        """
        return str(self)

    @property
    def safe_url(self) -> str:
        """URL with masked password for safe logging/display.

        Returns:
            str: String suitable for logs when credentials are present.

        Example:
            >>> ":***" in Proxy("http://u:secret@127.0.0.1:1").safe_url
            True
        """
        if not self.password:
            return str(self)

        auth_string = f"{self.username}:{self.password}@"
        safe_auth = f"{self.username}:***@"
        return str(self).replace(auth_string, safe_auth, 1)

    @property
    def host(self) -> str:
        """Host and port without scheme or credentials.

        Returns:
            str: ``"{ip}:{port}"`` string.

        Example:
            >>> Proxy("http://u:p@10.0.0.1:3128").host
            '10.0.0.1:3128'
        """
        return f"{self.ip}:{self.port}"

    @property
    def has_auth(self) -> bool:
        """Whether both username and password are set.

        Returns:
            bool: ``True`` if auth pair is present.

        Example:
            >>> Proxy("http://a:b@127.0.0.1:1").has_auth
            True
        """
        return bool(self.username and self.password)

    @property
    def is_working(self) -> bool:
        """Whether the proxy is considered working from last check or latency heuristics.

        Returns:
            bool: ``True`` if ``last_status`` is truthy or latency suggests success.

        Example:
            >>> p = Proxy("127.0.0.1:1")
            >>> p._set_attribute("last_status", True)
            >>> p.is_working
            True
        """
        if self.last_status is not None:
            return self.last_status
        # Fall back to latency if no check has been recorded yet
        return self.latency is not None and self.latency > 0

    def _ordering_key(self) -> tuple[float, float, str]:
        """Stable sort key: lower latency first, then fresher ``last_checked``, then :attr:`url`.

        Missing latency sorts after any measured value. Missing ``last_checked`` sorts after
        entries with the same latency that have a timestamp (newer timestamps win ties).
        """
        lat = self.latency
        lat_key = float("inf") if lat is None else float(lat)
        ts = self.last_checked
        # Negate so ascending tuple order prefers larger (more recent) timestamps
        ts_key = float("inf") if ts is None else -float(ts)
        return (lat_key, ts_key, self.url)

    def as_requests_proxies(self) -> dict[str, str]:
        """Build a ``proxies`` dict for ``requests`` / ``urllib`` style APIs.

        Returns:
            dict[str, str]: ``{"http": url, "https": url}`` using this proxy's string form.

        Example:
            >>> Proxy("http://127.0.0.1:9").as_requests_proxies()["http"].startswith("http")
            True
        """
        u = str(self)
        return {"http": u, "https": u}

    def to_dict(self) -> dict[str, Any]:
        """Serialize structural fields and non-``None`` metadata.

        Returns:
            dict[str, Any]: Mapping suitable for JSON or logging.

        Example:
            >>> "ip" in Proxy("127.0.0.1:1").to_dict()
            True
        """
        out: dict[str, Any] = {}
        for attrs, omit_none in (
            (self._structural_attributes, False),
            (self._metadata_attributes, True),
        ):
            for k in attrs:
                v = getattr(self, k)
                if omit_none and v is None:
                    continue
                out[k] = v
        return out

    def to_json_string(self, **dump_kw: Any) -> str:
        """JSON-serialize :meth:`to_dict` using ``orjson``.

        Args:
            **dump_kw (Any): Forwarded to :func:`orjson.dumps`.

        Returns:
            str: UTF-8 decoded JSON text.

        Example:
            >>> "ip" in Proxy("127.0.0.1:1").to_json_string()
            True
        """
        return orjson.dumps(self.to_dict(), **dump_kw).decode()

    @property
    def server(self) -> str:
        """Playwright-style ``server`` URL (scheme + host + port).

        Returns:
            str: ``"{protocol}://{ip}:{port}"`` with ``http`` default if protocol missing.

        Example:
            >>> Proxy("127.0.0.1:8080").server
            'http://127.0.0.1:8080'
        """
        protocol = self.protocol or "http"
        return f"{protocol}://{self.ip}:{self.port}"

    @property
    def playwright(self) -> PlaywrightProxySettings:
        """TypedDict payload for Playwright proxy configuration.

        Returns:
            PlaywrightProxySettings: At minimum ``server``; adds credentials when present.

        Example:
            >>> Proxy("http://u:p@127.0.0.1:1").playwright.get("username")
            'u'
        """
        settings: PlaywrightProxySettings = {"server": self.server}
        if self.username:  # or is not None
            settings["username"] = self.username
        if self.password:
            settings["password"] = self.password
        return settings

    @property
    def version(self) -> int | None:
        """IP version for numeric addresses, or ``None`` for hostnames.

        Returns:
            int | None: ``4`` or ``6`` for parsed IPs; ``None`` if :attr:`ip` is not numeric.

        Example:
            >>> Proxy("127.0.0.1:1").version
            4
        """
        # Strip brackets from IPv6 (e.g., "[2001:db8::1]" -> "2001:db8::1")
        ip_string = self.ip.strip("[]")
        try:
            return ipaddress.ip_address(ip_string).version
        except ValueError:
            # If it fails, it's a hostname based on your check_ip validator
            return None

    def rotate(
        self,
        method: Literal["GET", "POST"] = "GET",
        *,
        backend: str | None = None,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> bool:
        """Call :attr:`rotation_url` directly (no proxy) to trigger upstream rotation.

        Args:
            method (Literal["GET", "POST"]): HTTP verb for the rotation request.
            backend (str | None): Backend name; default from ``settings.default_backend``.
            timeout (float | None): Request timeout override.
            **kwargs (Any): Extra args forwarded to the backend ``request_direct``.

        Returns:
            bool: ``True`` if the response status code is 200.

        Raises:
            ValueError: If :attr:`rotation_url` is unset.

        Example:
            >>> Proxy("http://127.0.0.1:1").rotate  # doctest: +ELLIPSIS
            <bound method Proxy.rotate of ...>
        """
        from .backends.factory import get_backend
        from .config import settings

        if not self.rotation_url:
            raise ValueError("This proxy has no rotation_url")
        impl = get_backend(backend)
        to = timeout if timeout is not None else settings.default_timeout
        r = impl.request_direct(method, str(self.rotation_url), timeout=to, **kwargs)
        return r.status_code == 200

    async def arotate(
        self,
        method: Literal["GET", "POST"] = "GET",
        *,
        backend: str | None = None,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> bool:
        """Async variant of :meth:`rotate`.

        Args:
            method (Literal["GET", "POST"]): HTTP verb.
            backend (str | None): Backend override.
            timeout (float | None): Timeout override.
            **kwargs (Any): Forwarded to ``arequest_direct``.

        Returns:
            bool: ``True`` if status code is 200.

        Raises:
            ValueError: If :attr:`rotation_url` is unset.

        Example:
            >>> Proxy("http://127.0.0.1:1").arotate  # doctest: +ELLIPSIS
            <bound method Proxy.arotate of ...>
        """
        from .backends.factory import get_backend
        from .config import settings

        if not self.rotation_url:
            raise ValueError("This proxy has no rotation_url")
        impl = get_backend(backend)
        to = timeout if timeout is not None else settings.default_timeout
        r = await impl.arequest_direct(method, str(self.rotation_url), timeout=to, **kwargs)
        return r.status_code == 200

    @classmethod
    def set_default_pattern(cls, pattern: str | ProxyPattern) -> None:
        """Set :attr:`default_pattern` used when stringifying new :class:`Proxy` instances.

        Args:
            pattern (str | ProxyPattern): Allowed-token template (see :class:`ProxyPattern`).

        Returns:
            None

        Example:
            >>> Proxy.set_default_pattern.__name__
            'set_default_pattern'
        """
        cls.default_pattern = ProxyPattern(pattern)

    @classmethod
    def validate(cls, v: str) -> Proxy:
        """Parse *v* or raise ``ValueError`` with the original error chained.

        Args:
            v (str): Raw proxy string.

        Returns:
            Proxy: Valid instance.

        Raises:
            ValueError: If parsing fails.

        Example:
            >>> Proxy.validate("127.0.0.1:1").port
            1
        """
        try:
            return cls(v)
        except Exception as er:
            raise ValueError(er) from er

    def __repr__(self) -> str:
        """Return ``Proxy(<url>)``-style representation.

        Returns:
            str: Debug-friendly constructor-like string.

        Example:
            >>> "Proxy(" in repr(Proxy("127.0.0.1:1"))
            True
        """
        return f"{self.__class__.__name__}({self.url})"

    def __hash__(self) -> int:
        """Hash based on the underlying canonical URL string.

        Returns:
            int: Hash compatible with :meth:`__eq__`.

        Example:
            >>> isinstance(hash(Proxy("127.0.0.1:1")), int)
            True
        """
        return super().__hash__()

    def __eq__(self, other: object) -> bool:
        """Compare by canonical URL to another :class:`Proxy` or plain string.

        Args:
            other (object): Right-hand side.

        Returns:
            bool: Whether URLs match; ``NotImplemented`` for unsupported types.

        Example:
            >>> Proxy("127.0.0.1:1") == "http://127.0.0.1:1"
            True
        """
        if isinstance(other, Proxy):
            return self.url == other.url
        if isinstance(other, str):
            return self.url == other
        return NotImplemented

    def __le__(self, other: object) -> bool:
        if not isinstance(other, Proxy):
            return NotImplemented
        return self._ordering_key() <= other._ordering_key()

    def __ge__(self, other: object) -> bool:
        if not isinstance(other, Proxy):
            return NotImplemented
        return self._ordering_key() >= other._ordering_key()

    def __lt__(self, other: object) -> bool:
        """Compare for ``sorted()`` / ``<``: lower latency first, then newer ``last_checked``.

        Args:
            other (object): Another :class:`Proxy` or subclass.

        Returns:
            bool: Ordering per :meth:`_ordering_key`.

        Raises:
            TypeError: Implicitly if *other* is not a :class:`Proxy`; returns ``NotImplemented``.
        """
        if not isinstance(other, Proxy):
            return NotImplemented
        return self._ordering_key() < other._ordering_key()

    def __gt__(self, other: object) -> bool:
        """Inverse of :meth:`__lt__` for ``>`` comparisons."""
        if not isinstance(other, Proxy):
            return NotImplemented
        return self._ordering_key() > other._ordering_key()

    def __setattr__(self, key: str, value: Any) -> None:
        """Block writes to structural and metadata slot names after construction.

        Args:
            key (str): Attribute name.
            value (Any): Attempted new value.

        Returns:
            NotImplemented | None: Delegates to ``str`` for non-protected keys.

        Raises:
            AttributeError: If *key* is a protected structural/metadata field.

        Example:
            >>> p = Proxy("127.0.0.1:1")
            >>> try:
            ...     p.ip = "x"
            ... except AttributeError:
            ...     True
            ... else:
            ...     False
            True
        """
        if key in self._protected_attributes:
            raise AttributeError(
                f"attribute '{key}' of '{self.__class__.__name__}' object is not writable"
            )
        return super().__setattr__(key, value)

    def __reduce__(self) -> tuple[type[Proxy], tuple[str], dict[str, Any]]:
        """Pickle support: reconstruct from URL plus metadata state dict.

        Returns:
            tuple[type[Proxy], tuple[str], dict[str, Any]]: ``reduce`` protocol tuple.

        Example:
            >>> Proxy("127.0.0.1:1").__reduce__()[1][0].startswith("http")
            True
        """
        state = {k: getattr(self, k) for k in self._metadata_attributes}
        return (self.__class__, (self.url,), state)

    def __setstate__(self, state: dict[str, Any]) -> None:
        """Restore metadata slots after unpickling.

        Args:
            state (dict[str, Any]): Metadata mapping from :meth:`__reduce__`.

        Returns:
            None

        Example:
            >>> p = Proxy("127.0.0.1:1")
            >>> p.__setstate__({"latency": 1.0})
            >>> p.latency
            1.0
        """
        for key, value in state.items():
            object.__setattr__(self, key, value)

    def __getstate__(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in self._metadata_attributes}

    def __bool__(self) -> bool:
        return self.is_working

    def __copy__(self) -> Proxy:
        return self

    def __deepcopy__(self, memo: dict) -> Proxy:
        return self


__all__ = ["PlaywrightProxySettings", "Proxy", "ProxyPattern"]
