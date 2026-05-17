"""Basic smoke tests for SyncProxyPool using new config."""

from omniproxy import Proxy
from omniproxy.pool import SyncProxyPool


def test_acquire_twice(proxy_list, default_pool_config):
    pool = SyncProxyPool(default_pool_config, proxy_list)
    p = pool.acquire()
    pool.release(p)
    p2 = pool.acquire()
    pool.release(p2)
    pool.close()


def test_round_robin(s0, s1, default_pool_config):
    cfg = default_pool_config
    pool = SyncProxyPool(cfg, [Proxy(s0), Proxy(s1)])
    first = pool.acquire()
    second = pool.acquire()
    third = pool.acquire()
    assert first.url != second.url
    assert first.url == third.url
    pool.release(first)
    pool.release(second)
    pool.close()


def test_acquire_release_context_manager(proxy_list, default_pool_config):
    pool = SyncProxyPool(default_pool_config, proxy_list)
    p = pool.acquire()
    try:
        assert isinstance(p, Proxy)
    finally:
        pool.release(p)
    p2 = pool.acquire()
    try:
        assert p2 is not None
    finally:
        pool.release(p2)
    pool.close()