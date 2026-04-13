"""``acheck_proxy`` status handling; backend is mocked (no live traffic to ``PROXIES``)."""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from omniproxy import Proxy, acheck_proxy

from tests.proxy_seeds import seeds

S0 = seeds(1)[0]


class TestAcheckStatus(unittest.TestCase):
    def test_non_200_returns_false_tuple(self) -> None:
        backend = MagicMock()
        resp = MagicMock()
        resp.status_code = 503
        resp.json_data = None
        backend.aget = AsyncMock(return_value=resp)

        async def run() -> tuple[Proxy, object]:
            with patch("omniproxy.extended_proxy.get_backend", return_value=backend):
                return await acheck_proxy(S0, max_retries=0)

        proxy, ok = asyncio.run(run())
        self.assertIsInstance(proxy, Proxy)
        self.assertFalse(ok)
        self.assertEqual(ok.status_code, 503)


if __name__ == "__main__":
    unittest.main()
