import types

import pandas as pd
import pytest

from chanlun.exchange.exchange_binance import ExchangeBinance
from chanlun.exchange.exchange_binance_common import (
    BINANCE_KLINE_TIMEFRAMES,
    BINANCE_SPOT_PUBLIC_API_URL,
    BINANCE_SUPPORTED_FREQUENCIES,
    BINANCE_SYNTHETIC_FREQUENCIES,
)
from chanlun.exchange.exchange_binance_spot import ExchangeBinanceSpot
import chanlun.exchange.exchange_binance_spot as spot_module


def _real_cls(wrapper):
    return getattr(wrapper, "__wrapped__", wrapper)


class _FakeSpotClient:
    def __init__(self):
        self.urls = {"api": {"public": "https://api.binance.com/api/v3"}}


def test_spot_constructor_is_public_only_and_spot_only(monkeypatch):
    captured = {}
    client = _FakeSpotClient()

    def fake_binance(params):
        captured.update(params)
        return client

    monkeypatch.setattr(
        spot_module,
        "config_get_proxy",
        lambda: {"host": "127.0.0.1", "port": 10808},
    )
    monkeypatch.setattr(spot_module.ccxt, "binance", fake_binance, raising=False)
    monkeypatch.setattr(spot_module, "ExchangeDB", lambda _market: object())

    cls = _real_cls(ExchangeBinanceSpot)
    instance = object.__new__(cls)
    cls.__init__(instance)

    assert captured["options"] == {
        "defaultType": "spot",
        "fetchCurrencies": False,
        "fetchMarkets": {"types": ["spot"]},
    }
    assert captured["proxies"] == {
        "http": "http://127.0.0.1:10808",
        "https": "http://127.0.0.1:10808",
    }
    assert "apiKey" not in captured
    assert "secret" not in captured
    assert client.urls["api"]["public"] == BINANCE_SPOT_PUBLIC_API_URL


def test_spot_and_futures_expose_the_same_complete_kline_set():
    spot = object.__new__(_real_cls(ExchangeBinanceSpot))
    futures = object.__new__(_real_cls(ExchangeBinance))

    expected = set(BINANCE_KLINE_TIMEFRAMES)
    assert set(spot.support_frequencys()) == expected
    assert set(futures.support_frequencys()) == expected
    assert set(BINANCE_SUPPORTED_FREQUENCIES) == expected
    assert BINANCE_SYNTHETIC_FREQUENCIES == {"2m", "10m", "3h"}


@pytest.mark.parametrize(
    ("frequency", "timeframe"),
    [
        ("1m", "1m"),
        ("3m", "3m"),
        ("120m", "2h"),
        ("6h", "6h"),
        ("8h", "8h"),
        ("12h", "12h"),
        ("3d", "3d"),
        ("m", "1M"),
    ],
)
def test_spot_online_klines_uses_native_timeframes(frequency, timeframe):
    calls = []

    def fetch_ohlcv(**kwargs):
        calls.append(kwargs)
        return [
            [1_700_000_000_000, 1.0, 2.0, 0.5, 1.5, 10.0],
            [1_700_000_060_000, 1.5, 2.5, 1.0, 2.0, 11.0],
        ]

    instance = object.__new__(_real_cls(ExchangeBinanceSpot))
    instance.exchange = types.SimpleNamespace(fetch_ohlcv=fetch_ohlcv)
    instance.tz = "Asia/Shanghai"

    result = instance.online_klines("BTC/USDT", frequency)

    assert isinstance(result, pd.DataFrame)
    assert len(result) == 2
    assert calls[0]["timeframe"] == timeframe


@pytest.mark.parametrize(
    ("frequency", "timeframe"),
    [("2m", "1m"), ("10m", "5m"), ("3h", "1h")],
)
def test_spot_online_klines_synthesizes_custom_periods(
    monkeypatch, frequency, timeframe
):
    converted = []

    def fake_convert(frame, target):
        converted.append(target)
        return frame

    calls = []

    def fetch_ohlcv(**kwargs):
        calls.append(kwargs)
        return [[1_700_000_000_000, 1.0, 2.0, 0.5, 1.5, 10.0]]

    monkeypatch.setattr(
        spot_module,
        "convert_currency_kline_frequency",
        fake_convert,
    )
    instance = object.__new__(_real_cls(ExchangeBinanceSpot))
    instance.exchange = types.SimpleNamespace(fetch_ohlcv=fetch_ohlcv)
    instance.tz = "Asia/Shanghai"

    instance.online_klines("BTC/USDT", frequency)

    assert calls[0]["timeframe"] == timeframe
    assert converted == [frequency]
