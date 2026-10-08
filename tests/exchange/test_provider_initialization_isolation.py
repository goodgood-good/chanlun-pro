"""A slow provider must not hold unrelated markets behind its constructor."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from chanlun import exchange
from chanlun.market import Market


def test_slow_market_initialization_does_not_block_another_market(monkeypatch):
    monkeypatch.setattr(exchange, "g_exchange_obj", {})
    entered, release = Event(), Event()
    built = []
    def build(market):
        built.append(market)
        if market == Market.FX:
            entered.set()
            assert release.wait(5)
        exchange.g_exchange_obj[market.value] = object()
    monkeypatch.setattr(exchange, "_build_exchange", build)
    with ThreadPoolExecutor(max_workers=3) as pool:
        fx = pool.submit(exchange.get_exchange, Market.FX)
        assert entered.wait(2)
        same_fx = pool.submit(exchange.get_exchange, Market.FX)
        us = pool.submit(exchange.get_exchange, Market.US)
        try:
            assert us.result(timeout=1) is exchange.g_exchange_obj[Market.US.value]
            assert not same_fx.done()
        finally:
            release.set()
        assert fx.result(timeout=2) is same_fx.result(timeout=2)
    assert built.count(Market.FX) == built.count(Market.US) == 1


def test_failed_constructor_is_not_cached_and_can_recover(monkeypatch):
    monkeypatch.setattr(exchange, "g_exchange_obj", {})
    attempts = []
    def build(market):
        attempts.append(market)
        if len(attempts) == 1:
            raise RuntimeError("temporarily offline")
        exchange.g_exchange_obj[market.value] = object()
    monkeypatch.setattr(exchange, "_build_exchange", build)
    with pytest.raises(RuntimeError, match="temporarily offline"):
        exchange.get_exchange(Market.FX)
    assert Market.FX.value not in exchange.g_exchange_obj
    assert exchange.get_exchange(Market.FX) is exchange.get_exchange(Market.FX)
    assert len(attempts) == 2
