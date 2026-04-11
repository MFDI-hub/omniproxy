"""Central definitions for literals, defaults, and compiled patterns used across omniproxy."""

from __future__ import annotations

import re
from typing import Literal

# --- HTTP backends ---
SUPPORTED_BACKENDS: tuple[str, ...] = (
    "httpx",
    "aiohttp",
    "requests",
    "curl_cffi",
    "tls_client",
)
VALID_BACKENDS: frozenset[str] = frozenset(SUPPORTED_BACKENDS)

# --- OmniproxyConfig (see config.OmniproxyConfig) ---
OMNIPROXY_CONFIG_PUBLIC_KEYS: frozenset[str] = frozenset(
    {
        "default_backend",
        "default_timeout",
        "default_connect_timeout",
        "default_check_url",
        "default_check_info_url_template",
    }
)

DEFAULT_BACKEND: str = "httpx"
DEFAULT_TIMEOUT: float = 10.0
DEFAULT_CHECK_URL: str = "https://api.ipify.org/?format=json"
DEFAULT_CHECK_INFO_URL_TEMPLATE: str = "http://ip-api.com/json/?fields={fields}"

# --- Proxy check / anonymity probe ---
DEFAULT_CHECK_FIELDS: str = "8211"
URL_HEADERS_PROBE: str = "https://httpbin.org/headers"
DEFAULT_RETRYABLE_HTTP_STATUSES: frozenset[int] = frozenset({502, 503, 504})
DEFAULT_CHECK_MAX_RETRIES: int = 0
DEFAULT_CHECK_RETRY_BACKOFF: float = 0.5

# --- Pool ---
ANONYMITY_RANKS: dict[str, int] = {"transparent": 1, "anonymous": 2, "elite": 3}

# --- Protocol typing & parsing (OmniproxyParser) ---
ALLOWED_PROTOCOLS = Literal["http", "https", "socks5", "socks4"]

# IPv6: (?P<ip>\[[a-fA-F0-9:]+\]|[^@:\[\]]+) allows bracketed IPv6 OR standard IPv4/Host
PROXY_FORMATS_REGEXP: list[re.Pattern[str]] = [
    re.compile(
        r"^(?P<protocol>socks[45]|https?)://"
        r"(?P<ip>\[[a-fA-F0-9:]+\]|[^@:\[\]]+):(?P<port>\d+)(?:[@|:])"
        r"(?P<username>[^@:\[\]]+):(?P<password>[^@\[\]]+)"
        r"(\[(?P<url>https?://[^\s/$.?#].[^\s]*)\])?$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?P<protocol>socks[45]|https?)://"
        r"(?P<username>[^@:\[\]]+):(?P<password>[^@:\[\]]+)@"
        r"(?P<ip>\[[a-fA-F0-9:]+\]|[^@:\[\]]+):(?P<port>\d+)"
        r"(\[(?P<url>https?://[^\s/$.?#].[^\s]*)\])?$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?P<username>[^@:\[\]|]+):(?P<password>[^@:\[\]|]+)@"
        r"(?P<ip>\[[a-fA-F0-9:]+\]|[^@:\[\]]+):(?P<port>\d+)"
        r"(\[(?P<url>https?://[^\s/$.?#].[^\s]*)\])?$"
    ),
    re.compile(
        r"^(?P<username>[^@:\[\]]+):(?P<password>[^@:\[\]]+):"
        r"(?P<ip>\[[a-fA-F0-9:]+\]|[^@:\[\]]+):(?P<port>\d+)"
        r"(\[(?P<url>https?://[^\s/$.?#].[^\s]*)\])?$"
    ),
    re.compile(
        r"^(?P<ip>\[[a-fA-F0-9:]+\]|[^@:\[\]|]+):(?P<port>\d+)(?:[@|:])"
        r"(?P<username>[^@:\[\]|]+):(?P<password>[^@:\[\]|]+)"
        r"(\[(?P<url>https?://[^\s/$.?#].[^\s]*)\])?$"
    ),
    re.compile(
        r"^(?:(?P<protocol>socks[45]|https?)://)?"
        r"(?P<ip>\[[a-fA-F0-9:]+\]|[^@:\[\]|]+):(?P<port>\d+)"
        r"(\[(?P<url>https?://[^\s/$.?#].[^\s]*)\])?$",
        re.IGNORECASE,
    ),
]

HOSTNAME_LABEL_PATTERN: str = r"(?!-)[a-zA-Z0-9-]{1,63}(?<!-)"
HOSTNAME_RE: re.Pattern[str] = re.compile(
    rf"^{HOSTNAME_LABEL_PATTERN}(\.{HOSTNAME_LABEL_PATTERN})*\.?$",
    re.IGNORECASE,
)

# --- Proxy / ProxyPattern structural field names (URL + pattern tokens) ---
PROXY_STRUCTURAL_FIELDS: tuple[str, ...] = (
    "protocol",
    "username",
    "password",
    "ip",
    "port",
    "rotation_url",
)
PROXY_METADATA_FIELDS: tuple[str, ...] = ("latency", "anonymity", "last_checked", "last_status")
PROXY_PATTERN_ALLOWED_WORDS: frozenset[str] = frozenset(PROXY_STRUCTURAL_FIELDS)
DEFAULT_PROXY_PATTERN_STRING: str = "protocol://username:password@ip:port"

# --- I/O & fetch ---
# Prefer explicit schemes; bare host:port only as a full line to avoid JS/HTML noise.
PROXY_LINE_PATTERN: re.Pattern[str] = re.compile(
    r"(?P<raw>"
    r"(?:https?|socks[45])://[^\s]+"
    r"|"
    r"(?:^\s*)(?:(?:[^\s:@]+:[^\s:@]+@)?(?:\d{1,3}\.){3}\d{1,3}:\d{2,5})\s*$"
    r"|"
    r"(?:^\s*)(?:(?:[^\s:@]+:[^\s:@]+@)?"
    r"[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?\.[a-zA-Z]{2,}:\d{2,5})\s*$"
    r")",
    re.MULTILINE,
)
DEFAULT_FETCH_USER_AGENT: str = "omniproxy/3"

# --- Backend method default timeout (aligned with DEFAULT_TIMEOUT) ---
DEFAULT_BACKEND_TIMEOUT: float = DEFAULT_TIMEOUT

__all__ = [
    "ALLOWED_PROTOCOLS",
    "ANONYMITY_RANKS",
    "DEFAULT_BACKEND",
    "DEFAULT_BACKEND_TIMEOUT",
    "DEFAULT_CHECK_FIELDS",
    "DEFAULT_CHECK_INFO_URL_TEMPLATE",
    "DEFAULT_CHECK_MAX_RETRIES",
    "DEFAULT_CHECK_RETRY_BACKOFF",
    "DEFAULT_CHECK_URL",
    "DEFAULT_FETCH_USER_AGENT",
    "DEFAULT_PROXY_PATTERN_STRING",
    "DEFAULT_RETRYABLE_HTTP_STATUSES",
    "DEFAULT_TIMEOUT",
    "HOSTNAME_LABEL_PATTERN",
    "HOSTNAME_RE",
    "OMNIPROXY_CONFIG_PUBLIC_KEYS",
    "PROXY_FORMATS_REGEXP",
    "PROXY_LINE_PATTERN",
    "PROXY_METADATA_FIELDS",
    "PROXY_PATTERN_ALLOWED_WORDS",
    "PROXY_STRUCTURAL_FIELDS",
    "SUPPORTED_BACKENDS",
    "URL_HEADERS_PROBE",
    "VALID_BACKENDS",
]
