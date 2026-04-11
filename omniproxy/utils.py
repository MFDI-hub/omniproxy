from __future__ import annotations

import ipaddress
import re
import urllib.parse
from collections.abc import Iterable
from typing import TYPE_CHECKING

import msgspec

from .constants import (
    ALLOWED_PROTOCOLS,
    HOSTNAME_RE,
    PROXY_FORMATS_REGEXP,
    PROXY_STRUCTURAL_FIELDS,
)

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


def _proxy_format_groupdict(stripped: str) -> dict[str, str | None] | None:
    """First matching :data:`PROXY_FORMATS_REGEXP` pattern for *stripped*, or ``None``."""

    for pattern in PROXY_FORMATS_REGEXP:
        match = pattern.match(stripped)
        if match:
            return match.groupdict()
    return None


class OmniproxyParser(msgspec.Struct):
    ip: str
    port: int
    protocol: ALLOWED_PROTOCOLS = "http"
    username: str | None = None
    password: str | None = None
    rotation_url: str | None = None

    def __post_init__(self):
        # 1. Port validation
        if not (0 < self.port <= 65535):
            raise ValueError(f"Port must be between 1 and 65535, got {self.port}")

        # 2. IP validation
        v_stripped = self.ip.strip()
        if not v_stripped:
            raise ValueError("ip/host must not be empty")

        ip_to_test = v_stripped.strip("[]")
        is_valid_ip = False
        try:
            ipaddress.ip_address(ip_to_test)
            is_valid_ip = True
            # If it's a valid IPv6, ensure it's bracketed for URL safety
            if ":" in ip_to_test and not v_stripped.startswith("["):
                self.ip = f"[{v_stripped}]"
            else:
                self.ip = v_stripped
        except ValueError:
            pass

        if not is_valid_ip:
            # Reject dotted-numeric strings that are not valid IPs
            if DOTTED_NUMERIC_RE.fullmatch(v_stripped):
                raise ValueError(f"Invalid IP address: {v_stripped!r}")

            host = v_stripped.rstrip(".")
            if HOSTNAME_RE.match(host):
                self.ip = v_stripped
            else:
                raise ValueError(f"Invalid IP or hostname: {v_stripped!r}")

        # 3. URL Validation
        if self.rotation_url:
            parsed = urllib.parse.urlparse(self.rotation_url)
            if not all([parsed.scheme, parsed.netloc]):
                raise ValueError(f"Invalid rotation URL: {self.rotation_url}")

    @classmethod
    def from_string(cls, proxy_string: str) -> OmniproxyParser:
        stripped = proxy_string.strip()
        groups = _proxy_format_groupdict(stripped)
        if groups is None:
            raise ValueError(f"Unsupported proxy format: {proxy_string}")
        return cls.from_match(groups)

    @classmethod
    def batch_parse(cls, lines: Iterable[str]) -> list[OmniproxyParser]:
        """Parse each non-empty stripped line the same way as :meth:`from_string`."""

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
        raw_proto = (groups.get("protocol") or "http").lower()
        if raw_proto not in ("http", "https", "socks5", "socks4"):
            raise ValueError(f"Unsupported protocol: {raw_proto!r}")
        return cls(
            protocol=raw_proto,  # type: ignore[arg-type]
            ip=groups["ip"],
            port=int(groups["port"]),
            username=groups.get("username"),
            password=groups.get("password"),
            rotation_url=groups.get("url"),
        )


def _collapse_pattern_after_optional_fields(pattern: str) -> str:
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
    return COLLAPSE_END_BRACKETS_RE.sub("]", s)


def get_formatted_proxy_string(proxy: Proxy | OmniproxyParser, pattern: str | ProxyPattern) -> str:
    """Render *proxy* using *pattern* field tokens (protocol, username, password, ip, port, rotation_url)."""

    if isinstance(proxy, OmniproxyParser):
        dumped = msgspec.structs.asdict(proxy)
    else:
        # Assuming proxy is a type where these properties exist
        dumped = {
            "protocol": proxy.protocol,
            "ip": proxy.ip,
            "port": proxy.port,
            "username": proxy.username,
            "password": proxy.password,
            "rotation_url": proxy.rotation_url,
        }

    s = str(pattern)

    if not dumped.get("rotation_url"):
        s = REMOVE_BRACKETS_RE.sub("", s)

    if not dumped.get("username"):
        s = REMOVE_USERNAME_RE.sub("", s)
        s = REMOVE_PASSWORD_RE.sub("", s)
    elif not dumped.get("password"):
        s = REMOVE_PASSWORD_RE.sub("", s)

    s = _collapse_pattern_after_optional_fields(s)

    values: list[str] = []

    def _token_repl(m: re.Match[str]) -> str:
        t = m.group(0)
        val = dumped.get(t)
        values.append("" if val is None else str(val))
        return "{}"

    fmt = TOKENS_RE.sub(_token_repl, s)
    return fmt.format(*values)


__all__ = ["ALLOWED_PROTOCOLS", "OmniproxyParser", "get_formatted_proxy_string"]
