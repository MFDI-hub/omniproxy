"""Session stickiness resolver."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .proxy import Proxy
    from .config import SessionConfig

@dataclass(slots=True)
class SessionEntry:
    proxy_id: str
    expires_at: float


def resolve_session(
    session_key: str,
    registry: dict[str, SessionEntry],
    proxies: list[Proxy],
    config: SessionConfig,
    now: float | None = None,
) -> Proxy | None:
    """Return the session’s proxy, or raise/return None based on policy.

    Called under the pool’s state lock.  Raises :exc:`SessionBrokenError` if needed.
    """
    from .errors import SessionBrokenError

    if now is None:
        now: int | float = time.monotonic()
    entry: SessionEntry | None = registry.get(session_key)
    if entry is None:
        return None

    if entry.expires_at <= now:
        # expired
        del registry[session_key]
        if config.cooldown_policy == "raise":
            raise SessionBrokenError(f"Session '{session_key}' expired")
        # otherwise, REBIND → we return None and let caller pick a new proxy
        return None

    # Look up the proxy object
    proxy: Proxy | None = next((p for p in proxies if p.url == entry.proxy_id), None)
    # Never-checked proxies report ``is_working`` as false until metadata exists; stickiness
    # should only treat explicitly failed checks as unhealthy.
    explicit_fail = getattr(proxy, "last_status", None) is False if proxy is not None else True
    if proxy is None or explicit_fail:
        # proxy removed or unhealthy
        if config.cooldown_policy == "raise":
            raise SessionBrokenError(f"Session '{session_key}' proxy gone/unhealthy")
        if config.cooldown_policy == "block":
            # Keep session but don't hand out a proxy → acquisition will fail with PoolExhausted
            return None
        # REBIND
        del registry[session_key]
        return None

    return proxy


def unbind_session(
    session_key: str,
    registry: dict[str, SessionEntry],
    deferred: list,
) -> None:
    """Remove a session binding."""
    registry.pop(session_key, None)


__all__: list[str] = ["SessionEntry", "resolve_session", "unbind_session"]