from __future__ import annotations

import re
from typing import Any, Literal, TypedDict

from pydantic.networks import HttpUrl

from .adapter import _ExtraTypeConstructor
from .utils import ProxyStringParser, get_fromated_proxy_string


class PlaywrightProxySettings(TypedDict, total=False):
    server: str
    bypass: str | None
    username: str | None
    password: str | None


class ProxyPattern(str):
    allowed_words = ("protocol", "username", "password", "ip", "port", "rotation_url")

    def __init__(self, pattern: str) -> None:
        self.validate()

    def validate(self) -> None:
        for r in re.findall(r"\w+", self):
            if r not in self.allowed_words:
                raise ValueError(f"Unexpected word '{r}' in the pattern")


class Proxy(str, metaclass=_ExtraTypeConstructor):
    default_pattern = ProxyPattern("protocol://username:password@ip:port")
    _structural_attributes = (
        "protocol",
        "username",
        "password",
        "ip",
        "port",
        "rotation_url",
    )
    _metadata_attributes = ("latency", "anonymity", "last_checked")
    _protected_attributes = _structural_attributes + _metadata_attributes

    def __new__(cls, proxy: str, /, protocol: str | None = None) -> Proxy:
        if isinstance(proxy, Proxy):
            proxy = get_fromated_proxy_string(
                proxy, "protocol://username:password@ip:port[rotation_url]"
            )
        proxy = ProxyStringParser.from_string(proxy)
        if protocol:
            proxy = proxy.model_copy(update={"protocol": protocol})
        elif proxy.protocol == "https":
            proxy = proxy.model_copy(update={"protocol": "http"})
        proxy_string = get_fromated_proxy_string(proxy, cls.default_pattern)
        instance = str.__new__(cls, proxy_string)
        instance.__dict__.update(proxy.model_dump())
        object.__setattr__(instance, "latency", None)
        object.__setattr__(instance, "anonymity", None)
        object.__setattr__(instance, "last_checked", None)
        return instance

    def _set_attribute(self, key: str, value: Any) -> None:
        if key not in self._metadata_attributes:
            raise AttributeError(
                f"'_set_attribute' only supports metadata keys, not {key!r}"
            )
        object.__setattr__(self, key, value)

    @property
    def refresh_url(self) -> HttpUrl | None:
        return self.rotation_url

    @property
    def host(self) -> str:
        return self.ip

    @property
    def login(self) -> str | None:
        return self.username

    @property
    def url(self) -> str:
        return get_fromated_proxy_string(
            self, ProxyPattern("protocol://username:password@ip:port")
        )

    @property
    def dict(self) -> dict[str, str]:
        """
        returns commonly used pattern of proxies like in requests
        """
        if "http" in self.protocol:
            return {"http": self.url, "https": self.url}
        return {self.protocol: self.url}

    @property
    def proxies(self) -> dict[str, str]:
        return self.dict

    @property
    def server(self) -> str:
        return f"{self.protocol}://{self.ip}:{self.port}"

    @property
    def playwright(self) -> PlaywrightProxySettings:
        return PlaywrightProxySettings(
            server=self.server,
            password=self.password,
            username=self.username,
        )

    def rotate(self, method: Literal["GET", "POST"] = "GET", **kwargs) -> bool:
        """for mobile proxy only"""
        import httpx

        if not self.rotation_url:
            raise ValueError("This proxy hasn't rotation_url")
        r = httpx.request(method, self.rotation_url, **kwargs)
        return r.status_code == 200

    async def arotate(self, method: Literal["GET", "POST"] = "GET", **kwargs) -> bool:
        """for mobile proxy only"""
        import httpx

        if not self.rotation_url:
            raise ValueError("This proxy hasn't rotation_url")
        async with httpx.AsyncClient(follow_redirects=True) as client:
            r = await client.request(method, self.rotation_url, **kwargs)
            return r.status_code == 200

    def refresh(self, method: Literal["GET", "POST"] = "GET", **kwargs) -> bool:
        """for mobile proxy only"""
        return self.rotate(**kwargs)

    async def arefresh(self, method: Literal["GET", "POST"] = "GET", **kwargs) -> bool:
        """for mobile proxy only"""
        return await self.arotate(**kwargs)

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
    def validate(cls, v: str) -> str:
        try:
            return cls(v)
        except Exception as er:
            raise ValueError(er)

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self.url})"

    def __hash__(self) -> int:
        return hash(self.url)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Proxy):
            return self.url == other.url
        else:
            try:
                return self.url == Proxy(str(other)).url
            except Exception:
                pass

    def __setattr__(self, key: str, value: Any) -> None:
        if key in self._protected_attributes:
            raise AttributeError(
                f"attribute '{key}' of '{self.__class__.__name__}' object is not writable"
            )
        return super().__setattr__(key, value)

    def json(self) -> dict[str, Any]:
        out = dict((k, self.__dict__[k]) for k in self._structural_attributes)
        for k in self._metadata_attributes:
            v = self.__dict__.get(k)
            if v is not None:
                out[k] = v
        return out
