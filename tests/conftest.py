"""Load ``.env`` before tests so ``PROXIES`` is available for :mod:`tests.proxy_seeds`.

Pool integration tests expect a JSON list; :func:`~tests.proxy_seeds.seeds` uses only those
strings when set (see module docstring there for minimum list length per test file).
"""

from __future__ import annotations

from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    pass
else:
    _root = Path(__file__).resolve().parents[1]
    load_dotenv(_root / ".env")
    load_dotenv()
