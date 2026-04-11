import unittest

from omniproxy import ProxyPool


class TestProxyPool(unittest.TestCase):
    def test_len_contains_iter(self):
        pool = ProxyPool(["10.0.0.1:8000", "10.0.0.2:8000"])
        self.assertEqual(len(pool), 2)
        p = pool.get_next()
        assert p is not None
        self.assertIn(p, pool)
        urls = {str(x) for x in pool}
        self.assertIn("http://10.0.0.1:8000", urls)

    def test_round_robin_index_stable(self):
        pool = ProxyPool(["10.0.0.1:8000", "10.0.0.2:8000"], strategy="round_robin")
        _ = pool.get_next()
        p2 = pool.get_next()
        pool.mark_failed("10.0.0.1:8000")
        _ = pool.get_next()
        self.assertIsNotNone(p2)


if __name__ == "__main__":
    unittest.main()
