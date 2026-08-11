import pytest

import chanlun.exchange as exchange_module
from chanlun.exchange import exchange_cq
from chanlun.exchange.exchange_cq import ExchangeChangQiao
from chanlun.market import Market

_CQ_TYPE = ExchangeChangQiao.__wrapped__


class _FakeChangQiao:
    def __init__(self):
        self.calls = []

    def all_stocks(self, market):
        self.calls.append(("all_stocks", market))
        return [{"code": market}]

    def now_trading(self, market: str):
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
    assert hk.now_trading("hk") is False
    assert us.now_trading("us") is True
    with pytest.raises(AttributeError):
        hk.market = "us"


def test_longbridge_symbol_contract_rejects_unknown_formats():
    assert _CQ_TYPE._to_lb_symbol("KH.00700") == "00700.HK"
    assert _CQ_TYPE._to_lb_symbol("TSLA.US") == "TSLA.US"
    assert _CQ_TYPE._market_of_code("SH.600519") == "a"
    assert _CQ_TYPE._market_of_code("00700.HK") == "hk"
    with pytest.raises(ValueError):
        _CQ_TYPE._to_lb_symbol("UNKNOWN")
    with pytest.raises(ValueError):
        _CQ_TYPE._market_of_code("BTC.FX")
