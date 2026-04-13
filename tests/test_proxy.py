import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from omniproxy import PlaywrightProxySettings, Proxy
from pydantic import BaseModel, ConfigDict, field_validator


class Account(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    proxy: Proxy

    @field_validator("proxy", mode="before")
    @classmethod
    def _coerce_proxy(cls, v: object) -> Proxy:
        if isinstance(v, Proxy):
            return v
        if isinstance(v, str):
            return Proxy(v)
        raise TypeError(f"proxy must be str or Proxy, got {type(v).__name__}")


class TestProxy(unittest.TestCase):
    def test_input_formats(self):
        default = "http://login:password@210.173.88.77:3001"
        self.assertEqual(Proxy("login:password@210.173.88.77:3001"), default)
        self.assertEqual(Proxy("login:password:210.173.88.77:3001"), default)
        self.assertEqual(Proxy("210.173.88.77:3001:login:password"), default)
        self.assertEqual(Proxy("210.173.88.77:3001|login:password"), default)
        self.assertEqual(Proxy("http://login:password@210.173.88.77:3001"), default)
        self.assertEqual(
            str(Proxy("https://login:password@210.173.88.77:3001")),
            "https://login:password@210.173.88.77:3001",
        )
        self.assertEqual(
            str(Proxy("socks5://login:password@210.173.88.77:3001")),
            "socks5://login:password@210.173.88.77:3001",
        )
        self.assertEqual(
            str(Proxy("socks4://login:password@210.173.88.77:3001")),
            "socks4://login:password@210.173.88.77:3001",
        )
        self.assertEqual(str(Proxy("socks5://210.173.88.77:3001")), "socks5://210.173.88.77:3001")
        self.assertEqual(str(Proxy("socks5://myproxy.com:3001")), "socks5://myproxy.com:3001")

        self.assertEqual(
            Proxy("login:password@210.173.88.77:3001[https://myproxy.com?refresh=123]"), default
        )

    def test_wrong_input_formats(self):
        with self.assertRaises(ValueError):
            Proxy("login:pass:word@210.173.88.77:3001")
        with self.assertRaises(ValueError):
            Proxy("login:pass@word@210.173.88.77:3001")
        with self.assertRaises(ValueError):
            Proxy("login:pass|word@210.173.88.77:3001")
        with self.assertRaises(ValueError):
            Proxy("login:password@210.173.88.77:300111")
        with self.assertRaises(ValueError):
            Proxy("login:password@210.173.88.77.23:3001")
        with self.assertRaises(ValueError):
            Proxy("login:password@210.173.88:3001")
        with self.assertRaises(ValueError):
            Proxy("login:password@myproxy.c om:3001")
        with self.assertRaises(ValueError):
            Proxy("login:password@210.173.88.999:3001")
        with self.assertRaises(ValueError):
            Proxy("socks6://login:password@210.173.88.999:3001")
        with self.assertRaises(ValueError):
            Proxy("http:/login:password@210.173.88.999:3001")
        with self.assertRaises(ValueError):
            Proxy("socks://login:password@210.173.88.999:3001")

        with self.assertRaises(ValueError):
            Proxy("login:password@210.173.88.77:3001[https://myproxy.c om?refresh=123]")

    def test_ip(self):
        p = Proxy("login:password@210.173.88.77:3001")
        self.assertEqual(p.ip, "210.173.88.77")
        self.assertEqual(p.address, "210.173.88.77")

    def test_port(self):
        self.assertEqual(Proxy("login:password@210.173.88.77:3001").port, 3001)

    def test_rotation_url(self):
        p = Proxy("login:password@210.173.88.77:3001[https://myproxy.com?refresh=123]")
        self.assertEqual(p.rotation_url, "https://myproxy.com?refresh=123")

    def test_login(self):
        p = Proxy("login:password@210.173.88.77:3001")
        self.assertEqual(p.login, "login")
        self.assertEqual(p.username, "login")

    def test_password(self):
        self.assertEqual(Proxy("login:password@210.173.88.77:3001").password, "password")

    def test_protocol(self):
        self.assertEqual(Proxy("login:password@210.173.88.77:3001").protocol, "http")
        self.assertEqual(Proxy("socks5://login:password@210.173.88.77:3001").protocol, "socks5")

    def test_url(self):
        self.assertEqual(
            Proxy("210.173.88.77:3001:login:password").url,
            "http://login:password@210.173.88.77:3001",
        )
        self.assertEqual(
            Proxy("socks5://210.173.88.77:3001:login:password").url,
            "socks5://login:password@210.173.88.77:3001",
        )

    def test_as_requests_proxies(self):
        d1 = {
            "http": "http://login:password@210.173.88.77:3001",
            "https": "http://login:password@210.173.88.77:3001",
        }
        d2 = {
            "http": "socks5://login:password@210.173.88.77:3001",
            "https": "socks5://login:password@210.173.88.77:3001",
        }
        self.assertEqual(Proxy("210.173.88.77:3001:login:password").as_requests_proxies(), d1)
        self.assertEqual(
            Proxy("socks5://210.173.88.77:3001:login:password").as_requests_proxies(), d2
        )

    def test_server(self):
        self.assertEqual(
            Proxy("socks5://210.173.88.77:3001:login:password").server,
            "socks5://210.173.88.77:3001",
        )
        self.assertEqual(
            Proxy("210.173.88.77:3001:login:password").server, "http://210.173.88.77:3001"
        )

    def test_playwright(self):
        r1 = PlaywrightProxySettings(
            server="http://210.173.88.77:3001",
            password="password",
            username="login",
        )
        r2 = PlaywrightProxySettings(
            server="http://210.173.88.77:3001",
        )
        self.assertEqual(Proxy("210.173.88.77:3001:login:password").playwright, r1)
        self.assertEqual(Proxy("210.173.88.77:3001").playwright, r2)

    @patch("omniproxy.backends.factory.get_backend")
    def test_rotate(self, mock_get_backend):
        backend = MagicMock()
        backend.request_direct.return_value = MagicMock(status_code=200)
        backend.arequest_direct = AsyncMock(return_value=MagicMock(status_code=200))
        mock_get_backend.return_value = backend
        p = Proxy("login:password@210.173.88.77:3001[https://github.com]")
        self.assertTrue(p.rotate())
        self.assertTrue(asyncio.run(p.arotate()))
        backend.request_direct.assert_called()
        backend.arequest_direct.assert_called()

    def test_string(self):
        self.assertTrue(isinstance(Proxy("210.173.88.77:3001"), str))

    def test_pydantic(self):
        self.assertTrue(isinstance(Account(proxy=Proxy("210.173.88.77:3001")).proxy, Proxy))
        self.assertTrue(isinstance(Account(proxy="210.173.88.77:3001").proxy, Proxy))


if __name__ == "__main__":
    unittest.main()
