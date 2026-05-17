"""Tests for proxy check functions: acheck_proxy, check_proxy, etc."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from omniproxy import CheckResult, Proxy, acheck_proxy, check_proxy
from omniproxy.extended_proxy import arun_health_check, run_health_check


class TestCheckProxy:
    def test_non_200_returns_false(self, s0: str, mock_backend: MagicMock):
        resp = MagicMock()
        resp.status_code = 503
        resp.json_data = None
        mock_backend.aget = AsyncMock(return_value=resp)

        async def run():
            return await acheck_proxy(s0, max_retries=0)

        proxy, result = asyncio.run(run())
        assert isinstance(proxy, Proxy)
        assert result.success is False
        assert result.status_code == 503

    @pytest.mark.live
    def test_live_proxy_check(self, live_proxies):
        """Requires OMNIPROXY_LIVE_TESTS=1 and PROXY_LIST."""
        p = live_proxies[0]
        _, result = check_proxy(p, timeout=10.0)
        assert result.success is True or result.success is False

    @pytest.mark.live
    @pytest.mark.asyncio
    @pytest.mark.filterwarnings("ignore::curl_cffi.utils.CurlCffiWarning")
    async def test_live_acheck(self, live_proxies):
        p = live_proxies[0]
        _, result = await acheck_proxy(p, timeout=10.0)
        assert isinstance(result, CheckResult)

    def test_mocked_custom_expected_status(self, s0: str, mock_backend: MagicMock):
        resp = MagicMock()
        resp.status_code = 201
        resp.json_data = {}
        mock_backend.get.return_value = resp

        _, result = check_proxy(s0, expected_status=201, max_retries=0)
        assert result.success is True

    def test_mocked_expected_fields(self, s0: str, mock_backend: MagicMock):
        resp = MagicMock()
        resp.status_code = 200
        resp.json_data = {"key": "value"}
        mock_backend.get.return_value = resp

        _, result = check_proxy(s0, expected_fields={"key"}, max_retries=0)
        assert result.success is True

        _, result = check_proxy(s0, expected_fields={"missing"}, max_retries=0)
        assert result.success is False

    def test_mocked_retry_on_status(self, s0: str, mock_backend: MagicMock):
        resp_bad = MagicMock(status_code=503, json_data=None)
        resp_ok = MagicMock(status_code=200, json_data={})
        mock_backend.get.side_effect = [resp_bad, resp_ok]

        _, result = check_proxy(
            s0, max_retries=1, retry_backoff=0.01, retry_on_status=frozenset({503})
        )
        assert result.success is True

    def test_detect_anonymity(self, s0: str, mock_backend: MagicMock):
        main_resp = MagicMock(status_code=200, json_data={})
        probe_resp = MagicMock(status_code=200, json_data={"headers": {"X-Forwarded-For": "1.2.3.4"}})
        mock_backend.get.side_effect = [main_resp, probe_resp]

        proxy, result = check_proxy(s0, detect_anonymity=True, max_retries=0)
        assert result.success is True
        assert proxy.anonymity == "transparent"