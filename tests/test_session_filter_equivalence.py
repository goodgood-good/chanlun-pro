from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from chanlun.screening.markets import _session_mask, market_context, external_frame
from chanlun.screening.rules import CN


def original(stamps, periods, step):
    result = pd.Series(False, index=stamps.index)
    for opening, close in periods:
        result |= (
            (stamps > opening) & (stamps <= close) & ((stamps - opening) % step == 0)
        )
    return result


@pytest.mark.parametrize(
    "periods",
    [
        [],
        [(0, 3600)],
        [(0, 3600), (7200, 10800)],
        [(0, 3600), (3600, 7200)],
        [(7200, 10800), (0, 3600)],
        [(0, 3600), (300, 4800)],
        [(0, 3600), (0, 7200)],
        [(0, 0), (60, 120)],
        [(0.5, 3600.5)],
    ],
)
@pytest.mark.parametrize("step", [60, 300, 1800])
def test_boundaries_gaps_shared_closes_and_irregular_ranges_keep_identical_bars(
    periods, step
):
    rng = np.random.default_rng(71)
    values = np.unique(
        np.concatenate(
            [
                np.arange(-1, 11001, 60),
                [-1, 0, 1, 59, 60, 61, 300, 3600, 3601, 7200, 10800],
            ]
        )
    )
    rng.shuffle(values)
    stamps = pd.Series(values, index=[f"bar-{i}" for i in range(len(values))])
    pd.testing.assert_series_equal(
        _session_mask(stamps, periods, step), original(stamps, periods, step)
    )


@pytest.mark.parametrize(
    "market,code",
    [("us", "TSLA.US"), ("hk", "00700.HK"), ("currency_spot", "BTC/USDT")],
)
@pytest.mark.parametrize("frequency", ["1m", "5m", "30m"])
def test_real_calendar_selection_matches_the_previous_filter(market, code, frequency):
    context = market_context(
        market, code, datetime(2026, 3, 10, 23, 0, tzinfo=CN), frequency
    )
    periods = context["session_periods"][-5:]
    stamps = pd.Series(
        np.arange(periods[0][0] - 60, periods[-1][1] + 120, 60, dtype=np.int64)
    )
    step = int(frequency[:-1]) * 60
    pd.testing.assert_series_equal(
        _session_mask(stamps, periods, step), original(stamps, periods, step)
    )


def test_filter_keeps_frame_order_index_and_metadata_without_modifying_source(
    monkeypatch,
):
    from types import SimpleNamespace

    periods = [(0, 600), (1200, 1800)]
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(
                [0, 300, 600, 900, 1200, 1500, 1800], unit="s", utc=True
            ),
            "code": "BTC/USDT",
            "close": np.arange(7, dtype=float),
        },
        index=[8, 4, 9, 1, 7, 3, 2],
    )
    frame.attrs = {"bar_time_label": "end", "price_basis_revision": "unchanged-source"}
    before = frame.copy(deep=True)
    exchange = SimpleNamespace(klines=lambda *a, **k: frame, kline_time_label="end")
    monkeypatch.setattr("chanlun.exchange.get_exchange", lambda *_: exchange)
    result = external_frame(
        "BTC/USDT",
        "5m",
        {"market": "currency_spot", "cutoff": 1800, "session_periods": periods},
    )
    assert list(result.index) == [4, 9, 3, 2]
    assert result.attrs["price_basis_revision"] == "unchanged-source"
    assert result.attrs["bar_time_label"] == "end"
    pd.testing.assert_frame_equal(frame, before)
