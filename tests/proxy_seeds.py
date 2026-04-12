"""Resolve proxy strings from ``PROXIES`` (JSON list in env, e.g. from ``.env``).

When ``PROXIES`` is set, :func:`seeds` returns only those entries—no synthetic padding—so
pool tests exercise real :class:`~omniproxy.extended_proxy.Proxy` parsing and pool bookkeeping
against your actual list. If the list is shorter than a test module requires, a clear
:class:`ValueError` is raised.

When ``PROXIES`` is unset or invalid, built-in RFC-style offline placeholders are used (see
``_SYNTH``), and extra slots are padded only in that mode for CI.
"""

from __future__ import annotations

import json
import os

# Offline defaults when ``PROXIES`` is unset (CI / local without .env).
_SYNTH: tuple[str, ...] = (
    "10.0.0.1:8001",
    "10.0.0.2:8002",
    "10.0.0.3:8003",
    "10.0.0.4:8004",
    "10.0.0.5:9001",
    "10.0.0.6:9002",
)

_FULL: list[str] | None = None
_FULL_FROM_ENV: bool = False


def _parse_env_proxies() -> list[str]:
    raw = os.environ.get("PROXIES", "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    out: list[str] = []
    for item in data:
        s = str(item).strip().strip('"').strip("'")
        if s:
            out.append(s)
    return out


def _ensure_full() -> None:
    global _FULL, _FULL_FROM_ENV
    if _FULL is not None:
        return
    from_env = _parse_env_proxies()
    if from_env:
        _FULL = from_env
        _FULL_FROM_ENV = True
    else:
        _FULL = list(_SYNTH)
        _FULL_FROM_ENV = False


def all_seeds() -> list[str]:
    """All entries from ``PROXIES``, or the built-in synthetic list if env is empty."""
    _ensure_full()
    return list(_FULL or ())


def seeds(n: int) -> list[str]:
    """Return the first ``n`` seed strings.

    If ``PROXIES`` is set, raises :class:`ValueError` when fewer than ``n`` entries exist
    (tests must not invent extra hosts beside your real list). Offline mode pads only the
    synthetic fallback list when ``n`` is larger than ``_SYNTH``.
    """
    _ensure_full()
    assert _FULL is not None
    base = list(_FULL)
    if _FULL_FROM_ENV:
        if len(base) < n:
            raise ValueError(
                f"PROXIES must contain at least {n} non-empty strings (found {len(base)}). "
                "Add more entries in .env or unset PROXIES to use bundled offline defaults."
            )
        return base[:n]
    i = 0
    while len(base) < n:
        base.append(f"10.0.0.{210 + i}:{9100 + i}")
        i += 1
    return base[:n]
