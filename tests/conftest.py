"""Load ``.env`` before tests so ``PROXY_LIST`` is available for :mod:`tests.proxy_seeds`.

Pool integration tests expect a JSON list; :func:`~tests.proxy_seeds.seeds` uses only those
strings when set (see module docstring there for minimum list length per test file).
"""

from __future__ import annotations

from pathlib import Path
import asyncio
try:
    from dotenv import load_dotenv
except ImportError:
    pass
else:
    _root = Path(__file__).resolve().parents[1]
    load_dotenv(_root / ".env")
    load_dotenv()

if hasattr(asyncio, 'WindowsSelectorEventLoopPolicy'):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())