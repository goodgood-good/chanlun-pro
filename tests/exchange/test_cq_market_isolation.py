import pytest

import chanlun.exchange as exchange_module
from chanlun.exchange import exchange_cq
from chanlun.market import Market


class _FakeChangQiao:
    def __init__(self):
        self.default_market = "us"
        self.calls = []

    def all_stocks(self, market=None):
        self.calls.append(("all_stocks", market))
        return [{"code": market}]

    def now_trading(self, market="us"):
        self.calls.append(("now_trading", market))
        return market == "us"


def test_get_exchange_uses_stable_market_views_for_shared_cq_singleton(monkeypatch):
    backend = _FakeChangQiao()
    monkeypatch.setattr(exchange_module, "g_exchange_obj", {})
    monkeypatch.setattr(exchange_module.config, "EXCHANGE_HK", "cq")
    monkeypatch.setattr(exchange_module.config, "EXCHANGE_US", "cq")
    monkeypatch.setattr(exchange_cq, "ExchangeChangQiao", lambda: backend)

    exchange_module._build_exchange(Market.HK)
    exchange_module._build_exchange(Market.US)
    hk = exchange_module.g_exchange_obj[Market.HK.value]
    us = exchange_module.g_exchange_obj[Market.US.value]

    assert hk is not us
    assert hk.market == "hk"
    assert us.market == "us"
    assert hk.all_stocks() == [{"code": "hk"}]
    assert us.all_stocks() == [{"code": "us"}]
    assert hk.now_trading() is False
    assert us.now_trading() is True
    assert backend.default_market == "us"
    with pytest.raises(AttributeError):
        hk.market = "us"
