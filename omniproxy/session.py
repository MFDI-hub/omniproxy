"""Session stickiness resolver."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import SessionConfig
    from .extended_proxy import Proxy

@dataclass(slots=True)
class SessionEntry:
    """A registered sticky session binding.

    Attributes:
        proxy_id (str): Canonical proxy URL bound to the session.
        expires_at (float): Monotonic timestamp at which the binding
            expires.

    Version:
        Added in 4.0.0.
    """

    proxy_id: str
    expires_at: float


def resolve_session(
    session_key: str,
    registry: dict[str, SessionEntry],
    proxies: list[Proxy],
    config: SessionConfig,
    now: float | None = None,
    pending_rebind: dict[str, Proxy] | None = None,
) -> Proxy | None:
    """Resolve a sticky session to its bound :class:`Proxy`.

    Called under the pool's state lock. Expired or unhealthy bindings are
    handled per ``config.cooldown_policy``:

    * ``REBIND``: drop the binding, return ``None`` so the caller picks a new
      proxy.
    * ``BLOCK``: keep the binding, return ``None`` so acquisition fails with
      :exc:`PoolExhausted`.
    * ``RAISE``: raise :exc:`SessionBrokenError`.

    Args:
        session_key (str): Sticky session identifier.
        registry (dict[str, SessionEntry]): Mutable session registry; expired
            or invalidated entries are removed.
        proxies (list[Proxy]): Current pool proxies.
        config (SessionConfig): Session policy.
        now (float | None): Monotonic timestamp for the resolution.
            Defaults to ``time.monotonic()``.
        pending_rebind (dict[str, Proxy] | None): When provided, the bound
            proxy is stored here before the registry entry is dropped on
            ``REBIND`` (cooldown or unhealthy).

    Returns:
        Proxy | None: The bound proxy if available, otherwise ``None``.

    Raises:
        SessionBrokenError: When the binding is invalid and
            ``config.cooldown_policy`` is ``RAISE``.

    Version:
        Added in 4.0.0.
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
        if pending_rebind is not None and proxy is not None and explicit_fail:
            pending_rebind[session_key] = proxy
        del registry[session_key]
        return None

    return proxy


def unbind_session(
    session_key: str,
    registry: dict[str, SessionEntry],
    _deferred: list,
) -> None:
    """Remove a sticky session binding.

    Args:
        session_key (str): Identifier of the session to unbind.
        registry (dict[str, SessionEntry]): Session registry to mutate. The
            entry is removed if present; missing keys are ignored.
        _deferred (list): Reserved for future hook deferrals (currently
            unused but kept for API stability).

    Returns:
        None

    Version:
        Added in 4.0.0.
    """
    registry.pop(session_key, None)


__all__: list[str] = ["SessionEntry", "resolve_session", "unbind_session"]