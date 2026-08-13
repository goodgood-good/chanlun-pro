"""锁定美股生产行情只能通过长桥或盈立。"""

import pytest

from chanlun import config
from chanlun.exchange import _build_exchange, g_exchange_obj
from chanlun.market import Market


@pytest.mark.parametrize("removed_source", ["alpaca", "polygon", "ib", "tdx_us", "db"])
def test_removed_us_market_sources_fail_closed(monkeypatch, removed_source):
    monkeypatch.setattr(config, "EXCHANGE_US", removed_source)
    g_exchange_obj.pop(Market.US.value, None)

    with pytest.raises(Exception, match="不支持的美股交易所"):
        _build_exchange(Market.US)

    assert Market.US.value not in g_exchange_obj


def test_longbridge_is_the_only_cq_us_route(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(config, "EXCHANGE_US", "cq")
    monkeypatch.setattr(
        "chanlun.exchange._changqiao_market_view",
        lambda market: sentinel,
    )
    g_exchange_obj.pop(Market.US.value, None)

    _build_exchange(Market.US)

    assert g_exchange_obj.pop(Market.US.value) is sentinel
