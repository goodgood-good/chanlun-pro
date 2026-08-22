from __future__ import annotations

from decimal import Decimal
import types

import pandas as pd
import pytest

from chanlun.cl_utils.strict_chart_runtime import build_strict_chart_cd
from chanlun.decision_support.trading_system.runtime_config import (
    strict_snapshot_price_metadata,
)
from chanlun.exchange.exchange_binance import ExchangeBinance
from chanlun.exchange.exchange_binance_common import (
    BINANCE_OHLC_NORMALIZATION_REVISION,
    normalize_binance_kline_frame,
)
from chanlun.exchange.exchange_binance_spot import ExchangeBinanceSpot


def _real_cls(wrapper):
    return getattr(wrapper, "__wrapped__", wrapper)


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "code": ["BTC/USDT"] * 5,
            "date": pd.to_datetime(
                [1_700_000_000 + index * 60 for index in range(5)],
                unit="s",
                utc=True,
            ),
            "open": [1.000000004, 1.1, 1.2, 1.1, 1.3],
            "high": [1.2, 1.3, 1.4, 1.3, 1.5],
            "low": [0.9, 1.0, 1.1, 1.0, 1.2],
            "close": [1.1, 1.2, 1.1, 1.2, 1.4],
            "volume": [1.0] * 5,
        }
    )


class _FakeDB:
    def __init__(self, frame: pd.DataFrame):
        self.frame = frame

    def klines(self, _code, _frequency, args=None):
        return self.frame.copy()

    def insert_klines(self, _code, _frequency, _klines):
        return None


@pytest.mark.parametrize("market", ["currency", "currency_spot"])
def test_normalized_binance_frame_has_authoritative_price_basis(market):
    result = normalize_binance_kline_frame(
        _frame(), market=market, code="BTC/USDT"
    )

    assert result is not None
    assert result.iloc[0]["open"] == pytest.approx(1.0)
    assert result.attrs["structure_price_quantum"] == "0.00000001"
    assert result.attrs["price_basis_provider"] == "binance"
    assert result.attrs["price_basis_adjustment"] == "none"
    assert result.attrs["price_basis_revision"].startswith("sha256:")
    metadata = strict_snapshot_price_metadata(result)
    assert metadata.structure_price_quantum == Decimal("0.00000001")


def test_binance_price_basis_revision_is_stable_and_market_specific():
    futures = normalize_binance_kline_frame(
        _frame(), market="currency", code="BTC/USDT"
    )
    same_futures = normalize_binance_kline_frame(
        _frame(), market="currency", code="BTC/USDT"
    )
    spot = normalize_binance_kline_frame(
        _frame(), market="currency_spot", code="BTC/USDT"
    )

    assert BINANCE_OHLC_NORMALIZATION_REVISION
    assert futures.attrs["price_basis_revision"] == same_futures.attrs[
        "price_basis_revision"
    ]
    assert futures.attrs["price_basis_revision"] != spot.attrs[
        "price_basis_revision"
    ]


@pytest.mark.parametrize(
    ("cls", "market"),
    [
        (ExchangeBinance, "currency"),
        (ExchangeBinanceSpot, "currency_spot"),
    ],
    ids=["futures", "spot"],
)
def test_binance_online_klines_are_accepted_by_strict_runtime(cls, market):
    rows = [
        [
            1_700_000_000_000 + index * 60_000,
            row.open,
            row.high,
            row.low,
            row.close,
            row.volume,
        ]
        for index, row in enumerate(_frame().itertuples(index=False))
    ]
    instance = object.__new__(_real_cls(cls))
    instance.exchange = types.SimpleNamespace(
        fetch_ohlcv=lambda **_kwargs: rows
    )
    instance.tz = "UTC"

    frame = instance.online_klines("BTC/USDT", "1m")
    runtime = build_strict_chart_cd(
        market=market,
        code="BTC/USDT",
        frequency="1m",
        frame=frame,
    )

    assert runtime.error_code is None
    assert runtime.cd is not None


@pytest.mark.parametrize(
    ("cls", "market"),
    [
        (ExchangeBinance, "currency"),
        (ExchangeBinanceSpot, "currency_spot"),
    ],
    ids=["futures", "spot"],
)
@pytest.mark.parametrize("cache_state", ["empty", "populated"])
def test_binance_cached_kline_paths_keep_price_basis(cls, market, cache_state):
    source = _frame()
    cached = source.iloc[:0] if cache_state == "empty" else source.iloc[:2]
    instance = object.__new__(_real_cls(cls))
    instance.db_exchange = _FakeDB(cached)
    instance.increment_klines_by_online = (
        lambda _code, _frequency, start_date=None, args=None: source.copy()
    )

    frame = instance.klines("BTC/USDT", "1m")

    assert len(frame) == len(source)
    assert frame.attrs["price_basis_provider"] == "binance"
    assert frame.attrs["price_basis_adjustment"] == "none"
    assert strict_snapshot_price_metadata(frame).structure_price_quantum == Decimal(
        "0.00000001"
    )


def test_binance_price_basis_rejects_non_binance_market():
    with pytest.raises(ValueError, match="unsupported Binance market"):
        normalize_binance_kline_frame(_frame(), market="a", code="BTC/USDT")
