"""The strict chart runtime remains prefix-causal under incremental refresh."""

import math

import pandas as pd
import pytest

from chanlun.cl_utils.strict_chart_runtime import StrictChartRuntimeResult
from cl_app.services import chart_compute, kline_recompute
from cl_app.services.kline_recompute import recompute_chart_data_from_klines


def _klines_df(timestamps, prices):
    return pd.DataFrame(
        {
            "date": pd.to_datetime(list(timestamps), unit="s", utc=True),
            "open": list(prices),
            "high": [price + 0.5 for price in prices],
            "low": [price - 0.5 for price in prices],
            "close": list(prices),
            "volume": [1000] * len(timestamps),
        }
    )


class _FakeCL:
    def __init__(self):
        self.n = 0
        self.validated_incremental_calls = 0

    def process_klines(self, klines):
        self.n = len(klines)

    def process_validated_incremental_klines(self, klines):
        self.validated_incremental_calls += 1
        self.n = len(klines)


@pytest.fixture
def mock_cl(monkeypatch):
    created = []

    def build(**kwargs):
        cd = _FakeCL()
        cd.process_klines(kwargs["frame"])
        created.append(cd)
        return StrictChartRuntimeResult.success(cd)

    monkeypatch.setattr(chart_compute, "build_strict_chart_cd", build)
    monkeypatch.setattr(
        chart_compute,
        "serialize_chart_data_with_strict_runtime",
        lambda **kwargs: {
            "n": kwargs["strict_runtime"].cd.n,
            "id": id(kwargs["strict_runtime"].cd),
        },
    )
    kline_recompute.reset_cl_pool()
    yield created
    kline_recompute.reset_cl_pool()


def test_reuse_when_prefix_stable(mock_cl):
    first = recompute_chart_data_from_klines(
        "a", "SYN", "1m", {}, _klines_df([1000, 1060], [10, 11]),
        cache_key="a:SYN:1m",
    )
    second = recompute_chart_data_from_klines(
        "a", "SYN", "1m", {}, _klines_df([1000, 1060, 1120], [10, 11, 12]),
        cache_key="a:SYN:1m",
    )

    assert first["id"] == second["id"]
    assert mock_cl[0].validated_incremental_calls == 1


def test_new_runtime_when_first_date_changes(mock_cl):
    first = recompute_chart_data_from_klines(
        "a", "SYN", "1m", {}, _klines_df([1000, 1060], [10, 11]),
        cache_key="a:SYN:1m",
    )
    second = recompute_chart_data_from_klines(
        "a", "SYN", "1m", {}, _klines_df([940, 1000, 1060], [9, 10, 11]),
        cache_key="a:SYN:1m",
    )

    assert first["id"] != second["id"]


@pytest.mark.parametrize("revised_field", ("date", "volume"))
def test_new_runtime_when_immutable_prefix_fact_changes(
    mock_cl,
    revised_field,
):
    base = _klines_df([1000, 1060, 1120], [10, 11, 12])
    first = recompute_chart_data_from_klines(
        "a", "SYN", "1m", {}, base,
        cache_key="a:SYN:1m",
    )
    revised = base.copy()
    if revised_field == "date":
        revised.loc[1, "date"] = pd.Timestamp(1070, unit="s", tz="UTC")
    else:
        revised.loc[1, "volume"] += 1

    second = recompute_chart_data_from_klines(
        "a", "SYN", "1m", {}, revised,
        cache_key="a:SYN:1m",
    )

    assert first["id"] != second["id"]


def test_presentation_preferences_do_not_rebuild_runtime(mock_cl):
    first = recompute_chart_data_from_klines(
        "a", "SYN", "1m", {"chart_show_fx": "1"},
        _klines_df([1000, 1060], [10, 11]), cache_key="a:SYN:1m",
    )
    second = recompute_chart_data_from_klines(
        "a", "SYN", "1m", {"chart_show_fx": "0"},
        _klines_df([1000, 1060, 1120], [10, 11, 12]), cache_key="a:SYN:1m",
    )

    assert first["id"] == second["id"]


def test_no_reuse_without_cache_key(mock_cl):
    frame = _klines_df([1000, 1060], [10, 11])
    first = recompute_chart_data_from_klines("a", "SYN", "1m", {}, frame)
    second = recompute_chart_data_from_klines("a", "SYN", "1m", {}, frame)

    assert len(mock_cl) == 2
    assert mock_cl[0] is not mock_cl[1]
    assert first["id"] != second["id"]


def _synth_klines(n, start_ts=1_600_000_000):
    rows = []
    for index in range(n):
        value = (
            100
            + 18 * math.sin(index / 17.0)
            + 7 * math.sin(index / 5.0)
            + 2.5 * math.sin(index / 2.0)
        )
        next_value = (
            100
            + 18 * math.sin((index + 0.5) / 17.0)
            + 7 * math.sin((index + 0.5) / 5.0)
            + 2.5 * math.sin((index + 0.5) / 2.0)
        )
        rows.append(
            {
                "date": pd.Timestamp(start_ts + index * 60, unit="s", tz="UTC"),
                "open": value,
                "high": max(value, next_value) + 0.5,
                "low": min(value, next_value) - 0.5,
                "close": next_value,
                "volume": 1000 + index,
            }
        )
    frame = pd.DataFrame(rows)
    frame.attrs.update(
        structure_price_quantum="0.01",
        price_basis_revision="sha256:incremental-test-basis",
        price_basis_provider="test",
        price_basis_adjustment="none",
    )
    return frame


def test_incremental_equals_full_end_to_end():
    kline_recompute.reset_cl_pool()
    klines = _synth_klines(240)
    cache_key = "a:SYNINC:1m"
    try:
        for size in range(60, 241, 30):
            prefix = klines.iloc[:size].copy()
            prefix.attrs.update(klines.attrs)
            full = recompute_chart_data_from_klines(
                "a", "SYNINC", "1m", {}, prefix.copy()
            )
            incremental = recompute_chart_data_from_klines(
                "a", "SYNINC", "1m", {}, prefix.copy(), cache_key=cache_key
            )
            assert incremental == full, f"prefix {size}: incremental != full"
    finally:
        kline_recompute.reset_cl_pool()


def test_incremental_equals_full_on_mid_bar_revision():
    kline_recompute.reset_cl_pool()
    base = _synth_klines(200)
    cache_key = "a:SYNMID:1m"
    try:
        recompute_chart_data_from_klines(
            "a", "SYNMID", "1m", {}, base.copy(), cache_key=cache_key
        )
        revised = base.copy()
        revised.attrs.update(base.attrs)
        revised.loc[100, "close"] += 5.0
        revised.loc[100, "high"] += 5.0
        revised.loc[100, "low"] -= 5.0
        full = recompute_chart_data_from_klines("a", "SYNMID", "1m", {}, revised.copy())
        incremental = recompute_chart_data_from_klines(
            "a", "SYNMID", "1m", {}, revised.copy(), cache_key=cache_key
        )
        assert incremental == full
    finally:
        kline_recompute.reset_cl_pool()


def test_incremental_equals_full_on_last_bar_update():
    kline_recompute.reset_cl_pool()
    base = _synth_klines(200)
    cache_key = "a:SYNUPD:1m"
    try:
        recompute_chart_data_from_klines(
            "a", "SYNUPD", "1m", {}, base.copy(), cache_key=cache_key
        )
        updated = base.copy()
        updated.attrs.update(base.attrs)
        updated.loc[updated.index[-1], "close"] += 3.0
        updated.loc[updated.index[-1], "high"] += 3.0
        full = recompute_chart_data_from_klines("a", "SYNUPD", "1m", {}, updated.copy())
        incremental = recompute_chart_data_from_klines(
            "a", "SYNUPD", "1m", {}, updated.copy(), cache_key=cache_key
        )
        assert incremental == full
    finally:
        kline_recompute.reset_cl_pool()
