"""Foreign-exchange failures remain bounded and missing returns stay unknown."""
from types import SimpleNamespace

import pytest
from pytdx.errors import TdxConnectionError

from chanlun.exchange import exchange_tdx_fx as fx


def test_fx_initialization_stops_after_bounded_connection_attempts(monkeypatch):
    cls = fx.ExchangeTDXFX.__wrapped__
    connects, options, resets = [], [], []
    class Client:
        def __init__(self, **kwargs):
            options.append(kwargs)
        def connect(self, *args, **kwargs):
            connects.append(kwargs)
            return self
        def __enter__(self):
            raise TdxConnectionError("offline")
        def __exit__(self, *_):
            return False
    def reset(self):
        resets.append(True)
        if len(resets) > 2:
            raise RuntimeError("test stopped the old unbounded loop")
        self.connect_info = {"ip": "127.0.0.1", "port": 7727}
        return self.connect_info
    monkeypatch.setattr(fx, "TdxExHq_API", Client)
    monkeypatch.setattr(fx, "FileCacheDB", lambda: object())
    monkeypatch.setattr(fx, "db", SimpleNamespace(cache_get=lambda _: {"ip": "127.0.0.1", "port": 7727}))
    monkeypatch.setattr(cls, "reset_tdx_ip", reset)
    with pytest.raises(TdxConnectionError, match="offline"):
        cls()
    assert len(connects) == 2
    assert len(resets) == 1
    assert all(o["auto_retry"] is False for o in options)
    assert all(c["time_out"] == 5 for c in connects)


@pytest.mark.parametrize("previous,expected", [(100, 10.0), (110, 0.0), (0, None), (None, None), (float("nan"), None)])
def test_fx_quote_return_preserves_unknown_reference_price(monkeypatch, previous, expected):
    class Client:
        def __init__(self, **_):
            pass
        def connect(self, *_, **__):
            return self
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False
        def get_instrument_quote(self, *_):
            return [{"price":110., "pre_close":previous, "bid1":109., "ask1":111., "low":100.,
                     "high":112., "zongliang":1000., "open":105.}]
    monkeypatch.setattr(fx, "TdxExHq_API", Client)
    ex = object.__new__(fx.ExchangeTDXFX.__wrapped__)
    ex.connect_info = {"ip": "127.0.0.1", "port": 7727}
    ex.to_tdx_code = lambda _: (10, "USDCNY")
    assert ex.ticks(["FE.USDCNY"])["FE.USDCNY"].rate == expected


def test_fx_does_not_claim_always_open_without_a_verified_session():
    ex = object.__new__(fx.ExchangeTDXFX.__wrapped__)
    assert ex.now_trading("fx") is None


def test_failed_fx_singleton_is_rebuilt_after_provider_recovery(monkeypatch):
    from chanlun import exchange, config, fun
    cls = fx.ExchangeTDXFX.__wrapped__
    available = [False]
    class Client:
        def __init__(self, **_):
            pass
        def connect(self, *_, **__):
            return self
        def __enter__(self):
            if not available[0]:
                raise TdxConnectionError("offline")
            return self
        def __exit__(self, *_):
            return False
        def get_markets(self):
            return [{"short_name":"FE","category":4,"market":10,"name":"fixture"}]
    monkeypatch.setattr(fx, "TdxExHq_API", Client)
    monkeypatch.setattr(fx, "FileCacheDB", lambda: object())
    monkeypatch.setattr(fx, "db", SimpleNamespace(cache_get=lambda _: {"ip":"127.0.0.1","port":7727}))
    monkeypatch.setattr(cls, "reset_tdx_ip", lambda self: self.connect_info)
    monkeypatch.setattr(fx, "ExchangeTDXFX", fun.singleton(cls))
    monkeypatch.setattr(config, "EXCHANGE_FX", "tdx_fx")
    monkeypatch.setattr(exchange, "g_exchange_obj", {})
    from chanlun.market import Market
    with pytest.raises(TdxConnectionError):
        exchange.get_exchange(Market.FX)
    assert "fx" not in exchange.g_exchange_obj
    available[0] = True
    recovered = exchange.get_exchange(Market.FX)
    assert recovered.market_maps["FE"]["market"] == 10
    assert exchange.get_exchange(Market.FX) is recovered
