"""``SyncProxyPool`` smoke tests using ``PROXIES`` from the environment (see ``tests/proxy_seeds``)."""

import unittest

from omniproxy.extended_proxy import Proxy
from omniproxy.pool import SyncProxyPool

from tests.proxy_seeds import seeds

S0, S1 = seeds(2)


class TestProxyPool(unittest.TestCase):
    def test_len_contains_iter(self):
        pool = SyncProxyPool([S0, S1])
        self.assertEqual(len(pool), 2)
        p = pool.get_next()
        assert p is not None
        self.assertIn(p, pool)
        canonical = {Proxy(S0).url, Proxy(S1).url}
        self.assertIn(p.url, canonical)
        urls = {str(x) for x in pool}
        self.assertTrue(canonical.issubset(urls))

    def test_round_robin_index_stable(self):
        pool = SyncProxyPool([S0, S1], strategy="round_robin")
        first = pool.get_next()
        second = pool.get_next()
        pool.mark_failed(S0)
        third = pool.get_next()
        self.assertEqual(first.url, Proxy(S0).url)
        self.assertEqual(second.url, Proxy(S1).url)
        self.assertEqual(third.url, Proxy(S1).url)


if __name__ == "__main__":
    unittest.main()
