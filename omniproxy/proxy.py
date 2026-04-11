from __future__ import annotations

import ipaddress
import re
from typing import Any, Literal, TypedDict

import msgspec
import orjson

# from .adapter import _ExtraTypeConstructor
from .constants import (
    DEFAULT_PROXY_PATTERN_STRING,
    PROXY_METADATA_FIELDS,
    PROXY_PATTERN_ALLOWED_WORDS,
    PROXY_STRUCTURAL_FIELDS,
)
from .utils import OmniproxyParser, get_formatted_proxy_string


class PlaywrightProxySettings(TypedDict, total=False):
    server: str
    bypass: str | None
    username: str | None
    password: str | None


class ProxyPattern(str):
    """Template string containing field tokens: protocol, username, password, ip, port, rotation_url."""

    __slots__ = ()
    ALLOWED_WORDS = PROXY_PATTERN_ALLOWED_WORDS

    def __new__(cls, pattern: str) -> ProxyPattern:
        for token in re.findall(r"\w+", pattern):
            if token not in cls.ALLOWED_WORDS:
                raise ValueError(f"Unexpected word {token!r} in proxy pattern")
        return str.__new__(cls, pattern)


class Proxy(str):
    """Canonical proxy URL string with structured fields in ``__slots__`` and metadata."""

    __slots__ = (
        "anonymity",
        "ip",
        "last_checked",
        "last_status",
        "latency",
        "password",
        "port",
        "protocol",
        "rotation_url",
        "username",
    )

    default_pattern = ProxyPattern(DEFAULT_PROXY_PATTERN_STRING)
    _structural_attributes = PROXY_STRUCTURAL_FIELDS
    _metadata_attributes = PROXY_METADATA_FIELDS
    # Frozen at class body: structural + metadata names guarded by __setattr__
    _protected_attributes: frozenset[str] = frozenset(_structural_attributes + _metadata_attributes)

    def __new__(cls, proxy: str | Proxy, /, protocol: str | None = None) -> Proxy:
        # 1. FAST PATH
        if isinstance(proxy, Proxy):
            if protocol is None or protocol.lower() == (proxy.protocol or "").lower():
                return proxy

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

        return instance

    def _set_attribute(self, key: str, value: Any) -> None:
        if key not in self._metadata_attributes:
            raise AttributeError(f"'_set_attribute' only supports metadata keys, not {key!r}")
        object.__setattr__(self, key, value)

    @property
    def address(self) -> str:
        """IP address or hostname (same as :attr:`ip`)."""
        return self.ip

    @property
    def login(self) -> str | None:
        return self.username

    @property
    def url(self) -> str:
        return str(self)

    @property
    def safe_url(self) -> str:
        """URL with masked password for safe logging/display."""
        if not self.password:
            return str(self)

        auth_string = f"{self.username}:{self.password}@"
        safe_auth = f"{self.username}:***@"
        return str(self).replace(auth_string, safe_auth, 1)

    @property
    def host(self) -> str:
        """Returns just the IP and port (e.g., '127.0.0.1:8080')."""
        return f"{self.ip}:{self.port}"

    @property
    def has_auth(self) -> bool:
        """Quick check to see if the proxy requires authentication."""
        return bool(self.username and self.password)

    @property
    def is_working(self) -> bool:
        """True only if the last check explicitly succeeded."""
        if self.last_status is not None:
            return self.last_status
        # Fall back to latency if no check has been recorded yet
        return self.latency is not None and self.latency > 0

    def as_requests_proxies(self) -> dict[str, str]:
        """Shape suitable for ``requests`` / ``urllib`` ``proxies=`` mapping."""
        u = str(self)
        return {"http": u, "https": u}

    def to_dict(self) -> dict[str, Any]:
        """Structural fields plus metadata keys that are not ``None``."""
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
        """JSON serialization of :meth:`to_dict`."""
        return orjson.dumps(self.to_dict(), **dump_kw).decode()

    @property
    def server(self) -> str:
        """Server URL with fallback to HTTP if protocol is not set."""
        protocol = self.protocol or "http"
        return f"{protocol}://{self.ip}:{self.port}"

    @property
    def playwright(self) -> PlaywrightProxySettings:
        settings: PlaywrightProxySettings = {"server": self.server}
        if self.username:  # or is not None
            settings["username"] = self.username
        if self.password:
            settings["password"] = self.password
        return settings

    @property
    def version(self) -> int | None:
        """
        Returns 4 for IPv4, 6 for IPv6, or None if the address is a hostname.
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
        """Hit :attr:`rotation_url` (mobile rotation) using the configured HTTP backend."""
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
        """Async variant of :meth:`rotate`."""
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
        """
        examples:
        set_default_pattern('username:password@ip:port')
        set_default_pattern('username:password:ip:port[rotation_url]')
        set_default_pattern('protocol://username:password@ip:port')
        set_default_pattern('ip:port')
        """
        cls.default_pattern = ProxyPattern(pattern)

    @classmethod
    def validate(cls, v: str) -> Proxy:
        try:
            return cls(v)
        except Exception as er:
            raise ValueError(er) from er

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self.url})"

    def __hash__(self) -> int:
        return super().__hash__()

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Proxy):
            return self.url == other.url
        if isinstance(other, str):
            return self.url == other
        return NotImplemented

    def __setattr__(self, key: str, value: Any) -> None:
        if key in self._protected_attributes:
            raise AttributeError(
                f"attribute '{key}' of '{self.__class__.__name__}' object is not writable"
            )
        return super().__setattr__(key, value)

    def __reduce__(self) -> tuple[type[Proxy], tuple[str], dict[str, Any]]:
        state = {k: getattr(self, k) for k in self._metadata_attributes}
        return (self.__class__, (self.url,), state)

    def __setstate__(self, state: dict[str, Any]) -> None:
        for key, value in state.items():
            object.__setattr__(self, key, value)


__all__ = ["PlaywrightProxySettings", "Proxy", "ProxyPattern"]
