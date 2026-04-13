import asyncio
import json
import os
import unittest
from pathlib import Path

import httpx
import python_socks
from omniproxy import Proxy, acheck_proxies, acheck_proxy, check_proxies, check_proxy


def _read_multiline_proxies_from_dotenv() -> str | None:
    """Return full ``PROXIES=[...]`` JSON text from ``.env`` when dotenv only loaded the first line."""
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if not env_path.is_file():
        return None
    lines = env_path.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if not line.lstrip().startswith("PROXIES="):
            continue
        after = line.split("=", 1)[1].lstrip()
        if after[:1] in {'"', "'"}:
            return None
        parts = [after]
        depth = after.count("[") - after.count("]")
        j = i + 1
        while depth > 0 and j < len(lines):
            seg = lines[j].strip()
            j += 1
            if not seg:
                continue
            parts.append(seg)
            depth += seg.count("[") - seg.count("]")
        candidate = "\n".join(parts)
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            return None
        if isinstance(data, list):
            return candidate
        return None
    return None


def _proxies_raw_json_string() -> str:
    env_raw = os.environ.get("PROXIES", "").strip()
    if env_raw:
        try:
            data = json.loads(env_raw)
            if isinstance(data, list):
                return env_raw
        except json.JSONDecodeError:
            pass
    from_file = _read_multiline_proxies_from_dotenv()
    return from_file or env_raw or ""


def _proxies_json_list() -> list[str]:
    raw = _proxies_raw_json_string().strip()
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


_PROXY_LIST = _proxies_json_list()
_ENV_HTTP = os.environ.get("REAL_HTTP_PROXY", "").strip()
_ENV_SOCKS = os.environ.get("REAL_SOCKS5_PROXY", "").strip()
_SOCKS_FROM_LIST = next(
    (p for p in _PROXY_LIST if "socks5://" in p.lower() or "socks4://" in p.lower()),
    "",
)

# Live checks: ``OMNIPROXY_LIVE_TESTS=1`` plus a parsed ``PROXIES`` list and/or explicit
# ``REAL_HTTP_PROXY`` / ``REAL_SOCKS5_PROXY``. All-HTTP lists use a second entry for
# ``self.sp`` when no ``socks5://`` URL appears in ``PROXIES``.
REAL_HTTP_PROXY = _ENV_HTTP or (_PROXY_LIST[0] if _PROXY_LIST else "127.0.0.1:8080")
if _ENV_SOCKS or _SOCKS_FROM_LIST:
    REAL_SOCKS5_PROXY = _ENV_SOCKS or _SOCKS_FROM_LIST
elif len(_PROXY_LIST) >= 2:
    REAL_SOCKS5_PROXY = _PROXY_LIST[1]
elif _PROXY_LIST:
    REAL_SOCKS5_PROXY = _PROXY_LIST[0]
else:
    REAL_SOCKS5_PROXY = "socks5://127.0.0.1:1080"

_LIVE_CONFIGURED = bool(_PROXY_LIST) or (bool(_ENV_HTTP) and bool(_ENV_SOCKS))
LIVE = os.environ.get("OMNIPROXY_LIVE_TESTS") == "1" and _LIVE_CONFIGURED

_LIVE_SKIP_MSG = (
    "Set OMNIPROXY_LIVE_TESTS=1 and PROXIES as a JSON array (multi-line unquoted in .env is "
    "read from the file when needed), and/or REAL_HTTP_PROXY and REAL_SOCKS5_PROXY."
)

_SOCKS_FAIL_ERRORS = (
    python_socks._errors.ProxyConnectionError,
    python_socks._errors.ProxyTimeoutError,
)


class TestCheck(unittest.TestCase):
    def setUp(self):
        self.p = Proxy(REAL_HTTP_PROXY)
        self.fp = Proxy("bsdfsdfbi:fsdfsdfy@84.246.87.222:60111")
        self.sp = Proxy(REAL_SOCKS5_PROXY)
        self.fsp = Proxy("socks5://bsdfsdfbi:fsdfsdfy@84.246.87.222:60111")
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

    def tearDown(self):
        self.loop.close()

    @unittest.skipUnless(LIVE, _LIVE_SKIP_MSG)
    def test_proxy_get_info(self):
        self.assertTrue(isinstance(self.p.get_info(), dict))
        self.assertTrue(isinstance(asyncio.run(self.p.aget_info()), dict))

    @unittest.skipUnless(LIVE, _LIVE_SKIP_MSG)
    def test_proxy_check(self):
        self.assertTrue(self.p.check())
        self.assertTrue(asyncio.run(self.p.acheck()))

    @unittest.skipUnless(LIVE, _LIVE_SKIP_MSG)
    def test_check_proxy(self):
        self.assertTrue(check_proxy(self.p)[1])
        self.assertTrue(check_proxy(self.sp)[1])
        self.assertTrue(isinstance(check_proxy(self.p, with_info=True)[1], dict))
        self.assertTrue(isinstance(check_proxy(self.sp, with_info=True)[1], dict))

    @unittest.skipUnless(LIVE, _LIVE_SKIP_MSG)
    def test_acheck_proxy(self):
        tasks = [
            acheck_proxy(self.p),
            acheck_proxy(self.sp),
            acheck_proxy(self.p, with_info=True),
            acheck_proxy(self.sp, with_info=True),
        ]
        r = self.loop.run_until_complete(asyncio.gather(*tasks))
        self.assertTrue(r[0][1])
        self.assertTrue(r[1][1])
        self.assertTrue(isinstance(r[2][1], dict))
        self.assertTrue(isinstance(r[3][1], dict))

    @unittest.skipUnless(LIVE, _LIVE_SKIP_MSG)
    def test_acheck_proxies(self):
        tasks = [
            acheck_proxies([self.p]),
            acheck_proxies([self.sp], with_info=True),
        ]
        r = self.loop.run_until_complete(asyncio.gather(*tasks))
        self.assertEqual(r[0][0][0], self.p)
        self.assertEqual(r[1][0][0][0], self.sp)
        self.assertTrue(isinstance(r[1][0][0][1], dict))

    @unittest.skipUnless(LIVE, _LIVE_SKIP_MSG)
    def test_check_proxies(self):
        self.assertEqual(check_proxies([self.p])[0][0], self.p)
        self.assertEqual(check_proxies([self.sp], use_async=False)[0][0], self.sp)
        self.assertTrue(isinstance(check_proxies([self.p], with_info=True)[0][0][1], dict))
        self.assertTrue(
            isinstance(check_proxies([self.sp], with_info=True, use_async=False)[0][0][1], dict)
        )

    def test_fail_check_proxy(self):
        quick = 3.0
        self.assertFalse(check_proxy(self.fp, timeout=quick)[1])
        self.assertFalse(check_proxy(self.fsp, timeout=quick)[1])

        with self.assertRaises(httpx.HTTPError):
            check_proxy(self.fp, raise_on_error=True, timeout=quick)
        with self.assertRaises(_SOCKS_FAIL_ERRORS):
            check_proxy(self.fsp, raise_on_error=True, timeout=quick)

    def test_fail_acheck_proxy(self):
        quick = 3.0
        r = self.loop.run_until_complete(
            asyncio.gather(
                acheck_proxy(self.fp, timeout=quick), acheck_proxy(self.fsp, timeout=quick)
            )
        )
        self.assertFalse(r[0][1])
        self.assertFalse(r[1][1])

        with self.assertRaises(httpx.HTTPError):
            asyncio.run(acheck_proxy(self.fp, raise_on_error=True, timeout=quick))
        with self.assertRaises(_SOCKS_FAIL_ERRORS):
            asyncio.run(acheck_proxy(self.fsp, raise_on_error=True, timeout=quick))

    def test_fail_check_proxies(self):
        quick = 3.0
        self.assertEqual(check_proxies([self.fp], timeout=quick)[1][0], self.fp)
        self.assertEqual(check_proxies([self.fsp], use_async=False, timeout=quick)[1][0], self.fsp)

        with self.assertRaises(httpx.HTTPError):
            check_proxies([self.fp], raise_on_error=True, timeout=quick)
        with self.assertRaises(_SOCKS_FAIL_ERRORS):
            check_proxies([self.fsp], raise_on_error=True, use_async=False, timeout=quick)


if __name__ == "__main__":
    unittest.main()
