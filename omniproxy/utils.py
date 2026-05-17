from __future__ import annotations

import ipaddress
import re
import urllib.parse
from collections.abc import Iterable
from functools import lru_cache
from typing import TYPE_CHECKING, Any

import msgspec

from .constants import (
    ALLOWED_PROTOCOLS,
    HOSTNAME_RE,
    PROXY_FORMATS_REGEXP,
    PROXY_STRUCTURAL_FIELDS,
)
from .enum import ProxyProtocol

if TYPE_CHECKING:
    from .proxy import Proxy, ProxyPattern

# ==========================================
# Module-Level Pre-compiled Regular Expressions
# ==========================================
DOTTED_NUMERIC_RE = re.compile(r"^[\d.]+$")

# Regexes for _collapse_pattern_after_optional_fields
COLLAPSE_COLONS_RE = re.compile(r":{2,}")
COLLAPSE_ATS_RE = re.compile(r"@{2,}")
COLLAPSE_COLON_AT_RE = re.compile(r":\@+")
COLLAPSE_BRACKET_COLON_RE = re.compile(r"\[:")
COLLAPSE_COLON_BRACKET_RE = re.compile(r":\]")
COLLAPSE_START_BRACKETS_RE = re.compile(r"^\[+")
COLLAPSE_END_BRACKETS_RE = re.compile(r"\]+$")

# Regexes for get_formatted_proxy_string
REMOVE_BRACKETS_RE = re.compile(r"\[[^\]]*\]")
REMOVE_USERNAME_RE = re.compile(r"\busername\b")
REMOVE_PASSWORD_RE = re.compile(r"\bpassword\b")

# Dynamically generated regex for structural fields
_STRUCTURAL_FIELDS_PATTERN = rf"\b(?:{'|'.join(PROXY_STRUCTURAL_FIELDS)})\b"
TOKENS_RE = re.compile(_STRUCTURAL_FIELDS_PATTERN)
# ==========================================


def _validate_ip_or_normalize_host(v_stripped: str) -> str:
    """Validate and normalise hostname / IP via C-backed :mod:`ipaddress`."""
    ip_to_test = v_stripped.strip("[]")

    try:
        ipa = ipaddress.ip_address(ip_to_test)
    except ValueError:
        pass
    else:
        if isinstance(ipa, ipaddress.IPv6Address):
            if ":" in ip_to_test and not v_stripped.startswith("["):
                return f"[{v_stripped}]"
            return v_stripped
        return v_stripped

    if DOTTED_NUMERIC_RE.fullmatch(v_stripped):
        raise ValueError(f"Invalid IP address: {v_stripped!r}")

    host = v_stripped.rstrip(".")
    if HOSTNAME_RE.match(host):
        return v_stripped

    raise ValueError(f"Invalid IP or hostname: {v_stripped!r}")


def _proxy_format_groupdict(stripped: str) -> dict[str, str | None] | None:
    """Return regex ``groupdict()`` for the first matching proxy pattern, if any.

    Args:
        stripped (str): Candidate proxy string (typically already stripped).

    Returns:
        dict[str, str | None] | None: Named groups from :data:`~omniproxy.constants.PROXY_FORMATS_REGEXP`,
        or ``None`` when no pattern matches.

    Example:
        >>> from omniproxy.utils import _proxy_format_groupdict
        >>> g = _proxy_format_groupdict("127.0.0.1:8080")
        >>> g is not None and g.get("ip") == "127.0.0.1"
        True
    """

    for pattern in PROXY_FORMATS_REGEXP:
        match = pattern.match(stripped)
        if match:
            return match.groupdict()
    return None


class OmniproxyParser(msgspec.Struct):
    """Lightweight :mod:`msgspec` struct produced by :meth:`from_string` / :meth:`from_match`.

    Values are normalised and validated in :meth:`__post_init__` (port range, hostname or IP,
    optional ``rotation_url`` scheme). Use :func:`get_formatted_proxy_string` with a
    :class:`~omniproxy.proxy.ProxyPattern` to materialise canonical URL text.

    Attributes
    ----------
    ip: :class:`str`
        Hostname or IP. IPv6 literals are bracketed when required for URL safety.
    port: :class:`int`
        TCP port, strictly between ``1`` and ``65535`` inclusive.
    protocol
        Member of :data:`~omniproxy.constants.ALLOWED_PROTOCOLS`: ``http``, ``https``, ``socks4``, or ``socks5``.
    username: Optional[:class:`str`]
        Credential username when present in the parsed format.
    password: Optional[:class:`str`]
        Credential password when present.
    rotation_url: Optional[:class:`str`]
        Parsed rotation / mobile endpoint URL, if the input string carried a bracketed suffix.
    """

    ip: str
    port: int
    protocol: ALLOWED_PROTOCOLS = "http"
    username: str | None = None
    password: str | None = None
    rotation_url: str | None = None

    def __post_init__(self):
        """Validate port, host/IP, and optional rotation URL after construction.

        Returns:
            None

        Raises:
            ValueError: If port, host, or rotation URL is invalid.

        Example:
            >>> OmniproxyParser(ip="1.1.1.1", port=80).port
            80
        """
        # 1. Port validation
        if not (0 < self.port <= 65535):
            raise ValueError(f"Port must be between 1 and 65535, got {self.port}")

        # 2. IP validation — fast rejects; uncommon textual forms hit :mod:`ipaddress` once.
        v_stripped = self.ip.strip()
        if not v_stripped:
            raise ValueError("ip/host must not be empty")
        self.ip = _validate_ip_or_normalize_host(v_stripped)

        # 3. URL Validation
        if self.rotation_url:
            parsed = urllib.parse.urlparse(self.rotation_url)
            if not all([parsed.scheme, parsed.netloc]):
                raise ValueError(f"Invalid rotation URL: {self.rotation_url}")

    @classmethod
    def from_string(cls, proxy_string: str) -> OmniproxyParser:
        """Parse a single proxy string into an :class:`OmniproxyParser` instance.

        Args:
            proxy_string (str): Raw proxy line.

        Returns:
            OmniproxyParser: Parsed, validated struct.

        Raises:
            ValueError: If the format is unsupported.

        Example:
            >>> OmniproxyParser.from_string("socks5://h:1080").protocol
            'socks5'
        """
        stripped = proxy_string.strip()
        groups = _proxy_format_groupdict(stripped)
        if groups is None:
            raise ValueError(f"Unsupported proxy format: {proxy_string}")
        return cls.from_match(groups)

    @classmethod
    def batch_parse(cls, lines: Iterable[str]) -> list[OmniproxyParser]:
        """Parse each non-empty stripped line the same way as :meth:`from_string`.

        Args:
            lines (Iterable[str]): Lines that may contain proxies.

        Returns:
            list[OmniproxyParser]: One entry per non-empty valid line.

        Raises:
            ValueError: On the first line with an unsupported format.

        Example:
            >>> OmniproxyParser.batch_parse(["  ", "1.1.1.1:80"])[0].ip
            '1.1.1.1'
        """

        out: list[OmniproxyParser] = []
        for line in lines:
            s = line.strip()
            if not s:
                continue
            groups = _proxy_format_groupdict(s)
            if groups is None:
                raise ValueError(f"Unsupported proxy format: {s!r}")
            out.append(cls.from_match(groups))
        return out

    @classmethod
    def from_match(cls, groups: dict[str, str | None]) -> OmniproxyParser:
        """Build a parser struct from a regex ``groupdict`` mapping.

        Args:
            groups (dict[str, str | None]): Named capture groups (``ip``, ``port``, etc.).

        Returns:
            OmniproxyParser: Constructed instance.

        Raises:
            ValueError: If protocol is unsupported or required groups are missing.

        Example:
            >>> OmniproxyParser.from_match(
            ...     {
            ...         "protocol": "http",
            ...         "ip": "9.9.9.9",
            ...         "port": "53",
            ...         "username": None,
            ...         "password": None,
            ...         "url": None,
            ...     }
            ... ).port
            53
        """
        raw_proto = (groups.get("protocol") or "http").lower()
        try:
            _proto = ProxyProtocol(raw_proto).value
        except ValueError as e:
            raise ValueError(f"Unsupported protocol: {raw_proto!r}") from e
        # The regex patterns guarantee "ip" and "port" are captured whenever any
        # pattern matches, so these values are never None at this point.
        _ip = groups["ip"]
        _port = groups["port"]
        assert _ip is not None and _port is not None, (
            "regex match must capture 'ip' and 'port' groups"
        )
        return cls(
            protocol=_proto,
            ip=_ip,
            port=int(_port),
            username=groups.get("username"),
            password=groups.get("password"),
            rotation_url=groups.get("url"),
        )


def _collapse_pattern_after_optional_fields(pattern: str) -> str:
    """Normalise a pattern string after removing optional username/password tokens.

    Args:
        pattern (str): Pattern containing structural field names.

    Returns:
        str: Collapsed pattern safe for token substitution.

    Example:
        >>> _collapse_pattern_after_optional_fields("http:://user@@host")
        'http://user@host'
    """
    s = pattern
    s = COLLAPSE_COLONS_RE.sub(":", s)
    s = s.replace(":@", "@")
    s = s.replace("@:", "@")
    s = COLLAPSE_ATS_RE.sub("@", s)
    s = s.replace("://@", "://")
    s = COLLAPSE_COLON_AT_RE.sub("@", s)
    s = COLLAPSE_BRACKET_COLON_RE.sub("[", s)
    s = COLLAPSE_COLON_BRACKET_RE.sub("]", s)
    s = COLLAPSE_START_BRACKETS_RE.sub("[", s)
    return COLLAPSE_END_BRACKETS_RE.sub("]", s).rstrip("@")


@lru_cache(maxsize=512)
def _preprocessed_pattern_skeleton(
    pattern: str,
    has_rotation_url: bool,
    has_username: bool,
    has_password: bool,
) -> str:
    s = pattern
    if not has_rotation_url:
        s = REMOVE_BRACKETS_RE.sub("", s)

    if not has_username:
        s = REMOVE_USERNAME_RE.sub("", s)
        s = REMOVE_PASSWORD_RE.sub("", s)
    elif not has_password:
        s = REMOVE_PASSWORD_RE.sub("", s)

    return _collapse_pattern_after_optional_fields(s)


def _substitution_kwargs(dumped: dict[str, Any]) -> dict[str, Any]:
    """Map ``None`` structural fields to empty strings so :meth:`str.format` does not emit ``'None'``."""
    out: dict[str, Any] = {}
    for k, v in dumped.items():
        out[k] = "" if v is None else v
    return out


def get_formatted_proxy_string(proxy: Proxy | OmniproxyParser, pattern: str | ProxyPattern) -> str:
    """Render *proxy* using *pattern* field tokens (protocol, username, password, ip, port, rotation_url).

    Args:
        proxy (Proxy | OmniproxyParser): Source values.
        pattern (str | ProxyPattern): Template with allowed field tokens.

    Returns:
        str: Formatted proxy string.

    Example:
        >>> from omniproxy.utils import OmniproxyParser, get_formatted_proxy_string
        >>> p = OmniproxyParser.from_string("http://a:b@1.1.1.1:8080")
        >>> get_formatted_proxy_string(p, "ip:port")
        '1.1.1.1:8080'
    """
    from .proxy import ProxyPattern

    if isinstance(proxy, OmniproxyParser):
        dumped = msgspec.structs.asdict(proxy)
    else:
        dumped = {
            "protocol": proxy.protocol,
            "ip": proxy.ip,
            "port": proxy.port,
            "username": proxy.username,
            "password": proxy.password,
            "rotation_url": proxy.rotation_url,
        }

    if isinstance(pattern, ProxyPattern):
        fmt = (
            getattr(pattern, "_fmt_auth")
            if dumped.get("username")
            else getattr(pattern, "_fmt_noauth")
        )
        if not dumped.get("rotation_url") and getattr(pattern, "_has_rotation_brackets"):
            fmt = REMOVE_BRACKETS_RE.sub("", fmt)
            fmt = _collapse_pattern_after_optional_fields(fmt)
        sub = _substitution_kwargs(dumped)
        return fmt.format_map(sub)

    s = _preprocessed_pattern_skeleton(
        str(pattern),
        bool(dumped.get("rotation_url")),
        bool(dumped.get("username")),
        bool(dumped.get("password")),
    )

    values: list[str] = []

    def _token_repl(m: re.Match[str]) -> str:
        t = m.group(0)
        val = dumped.get(t)
        values.append("" if val is None else str(val))
        return "{}"

    fmt = TOKENS_RE.sub(_token_repl, s)
    return fmt.format(*values)


__all__ = ["ALLOWED_PROTOCOLS", "OmniproxyParser", "get_formatted_proxy_string"]