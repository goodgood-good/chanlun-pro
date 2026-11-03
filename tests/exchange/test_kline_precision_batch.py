from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from chanlun.exchange.kline_precision import _round_half_up, normalize_kline_precision


@pytest.mark.parametrize(
    "market,code,decimals",
    [
        ("a", "SH.600519", 2),
        ("a", "SH.510300", 3),
        ("us", "QQQ.US", 3),
        ("fx", "FE.CNYUSD", 5),
        ("currency_spot", "BTC/USDT", 8),
    ],
)
def test_batch_matches_decimal_at_both_sides_of_half_ticks(market, code, decimals):
    rng = np.random.default_rng(731)
    halves = np.array(
        [
            float((Decimal(int(n)) + Decimal(".5")).scaleb(-decimals))
            for n in rng.integers(-10000000, 10000000, 400)
        ]
    )
    values = np.concatenate(
        [
            halves,
            np.nextafter(halves, -np.inf),
            np.nextafter(halves, np.inf),
            rng.uniform(-100000, 100000, 400),
            [0.0, -0.0, np.nan, np.inf, -np.inf, 1e-300, -1e-300],
        ]
    )
    frame = pd.DataFrame(
        {c: np.roll(values, i) for i, c in enumerate(("open", "high", "low", "close"))}
    )
    frame["volume"] = np.arange(len(frame))
    frame.attrs = {"basis": {"revision": "unchanged"}}
    before = frame.copy(deep=True)
    expected = frame.copy(deep=True)
    for col in ("open", "high", "low", "close"):
        expected[col] = expected[col].map(lambda value: _round_half_up(value, decimals))
    actual = normalize_kline_precision(frame, market, code)
    pd.testing.assert_frame_equal(actual, expected, check_exact=True)
    for col in ("open", "high", "low", "close"):
        np.testing.assert_array_equal(
            np.signbit(actual[col]), np.signbit(expected[col])
        )
    pd.testing.assert_frame_equal(frame, before, check_exact=True)
    assert actual.attrs == frame.attrs


def test_non_float_columns_keep_scalar_conversion_and_large_integer_precision():
    frame = pd.DataFrame(
        {
            "open": [2**53 + 1, 2**53 + 3],
            "high": ["2.675", Decimal("3.145")],
            "low": np.array([2.675, 3.145], dtype=np.float32),
            "close": pd.Series([None, 3.145], dtype="Float64"),
        }
    )
    expected = frame.copy()
    for col in frame:
        expected[col] = expected[col].map(lambda value: _round_half_up(value, 2))
    pd.testing.assert_frame_equal(
        normalize_kline_precision(frame, "a", "SH.600519"), expected, check_exact=True
    )
