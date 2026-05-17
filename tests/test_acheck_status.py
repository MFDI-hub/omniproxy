"""``acheck_proxy`` status handling; backend is mocked (no live traffic to ``PROXY_LIST``)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from omniproxy import Proxy, acheck_proxy


@pytest.mark.asyncio
async def test_non_200_returns_false_tuple(s0: str) -> None:
    backend = MagicMock()
    resp = MagicMock()
    resp.status_code = 503
    resp.json_data = None
    backend.aget = AsyncMock(return_value=resp)

    with patch("omniproxy.extended_proxy.get_backend", return_value=backend):
        proxy, result = await acheck_proxy(s0, max_retries=0)

    assert isinstance(proxy, Proxy)
    assert not result
    assert result.status_code == 503
