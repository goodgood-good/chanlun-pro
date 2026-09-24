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
        self.close_contracts = []

    def process_klines(self, klines, *, last_bar_closed=False):
        self.n = len(klines)
        self.close_contracts.append(last_bar_closed)

    def process_validated_incremental_klines(self, klines, *, last_bar_closed=False):
        self.validated_incremental_calls += 1
        self.n = len(klines)
        self.close_contracts.append(last_bar_closed)


@pytest.fixture
def mock_cl(monkeypatch):
    created = []

    def build(**kwargs):
        cd = _FakeCL()
        cd.process_klines(kwargs["frame"], last_bar_closed=kwargs["last_bar_closed"])
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
    assert mock_cl[0].close_contracts == [True, True]


def test_identical_full_frame_skips_processing_but_serializes_again(mock_cl):
    frame = _klines_df([1000, 1060], [10, 11])
    first = recompute_chart_data_from_klines(
        "a", "SYN", "1m", {}, frame, cache_key="a:SYN:1m",
    )
    second = recompute_chart_data_from_klines(
        "a", "SYN", "1m", {}, frame.copy(), cache_key="a:SYN:1m",
    )
    assert second == first
    assert second is not first
    assert len(mock_cl) == 1
    assert mock_cl[0].validated_incremental_calls == 0


def test_calendar_bar_without_close_attestation_keeps_live_contract(mock_cl):
    recompute_chart_data_from_klines(
        "a", "SYN", "d", {}, _klines_df([1000, 87400], [10, 11]),
        cache_key="a:SYN:d",
    )
    assert mock_cl[0].close_contracts == [False]


def test_unchanged_published_frame_reuses_payload_and_skips_another_disk_write(mock_cl, monkeypatch):
    from cl_app.services import chart_cache
    frame = _klines_df([1000, 1060], [10, 11])
    writes = []
    monkeypatch.setattr(chart_cache, "_persist_chart_cache_async", lambda *args: writes.append(args))
    key = "test-published-payload"
    try:
        first = recompute_chart_data_from_klines("a", "SYN", "1m", {}, frame, cache_key=key)
        chart_cache._set_chart_cache_entry(key, first, is_full_snapshot=True)
        monkeypatch.setattr(chart_compute, "serialize_chart_data_with_strict_runtime",
                            lambda **kwargs: pytest.fail("unchanged published payload was serialized"))
        second = recompute_chart_data_from_klines("a", "SYN", "1m", {}, frame.copy(), cache_key=key)
        assert second is first
        chart_cache._set_chart_cache_entry(key, second, is_full_snapshot=True)
        assert len(writes) == 1
    finally:
        with chart_cache.cache_lock:
            chart_cache.chart_data_cache.pop(key, None)


@pytest.mark.parametrize("field", ["date", "open", "high", "low", "close", "volume"])
def test_completed_last_bar_revision_rebuilds_its_dependent_confirmations(mock_cl, field):
    frame = _klines_df([1000, 1060], [10, 11])
    first = recompute_chart_data_from_klines(
        "a", "SYN", "1m", {}, frame, cache_key="a:SYN:1m",
    )
    changed = frame.copy()
    if field == "date":
        changed.loc[1, field] += pd.Timedelta(seconds=1)
    else:
        changed.loc[1, field] += 1
    second = recompute_chart_data_from_klines(
        "a", "SYN", "1m", {}, changed, cache_key="a:SYN:1m",
    )
    assert second["id"] != first["id"]
    assert len(mock_cl) == 2
    assert all(cd.close_contracts == [True] for cd in mock_cl)


@pytest.mark.parametrize("field", ["date", "open", "high", "low", "close", "volume"])
def test_middle_bar_fact_change_rebuilds_runtime(mock_cl, field):
    frame = _klines_df([1000, 1060, 1120], [10, 11, 12])
    first = recompute_chart_data_from_klines(
        "a", "SYN", "1m", {}, frame, cache_key="a:SYN:1m",
    )
    changed = frame.copy()
    if field == "date":
        changed.loc[1, field] += pd.Timedelta(seconds=1)
    else:
        changed.loc[1, field] += 1
    second = recompute_chart_data_from_klines(
        "a", "SYN", "1m", {}, changed, cache_key="a:SYN:1m",
    )
    assert second["id"] != first["id"]
    assert len(mock_cl) == 2


@pytest.mark.parametrize(
    "field,value",
    [("price_basis_revision", "sha256:changed"),
     ("structure_price_quantum", "0.1"),
     ("price_basis_provider", "changed"),
     ("price_basis_adjustment", "changed")],
)
def test_identical_prices_do_not_reuse_different_basis(mock_cl, field, value):
    frame = _klines_df([1000, 1060], [10, 11])
    first = recompute_chart_data_from_klines(
        "a", "SYN", "1m", {}, frame, cache_key="a:SYN:1m",
    )
    changed = frame.copy()
    changed.attrs[field] = value
    second = recompute_chart_data_from_klines(
        "a", "SYN", "1m", {}, changed, cache_key="a:SYN:1m",
    )
    assert second["id"] != first["id"]


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


def test_identical_closed_frame_preserves_actual_analysis_memo(monkeypatch):
    kline_recompute.reset_cl_pool()
    key = "a:SYNMEMO:1m"
    frame = _synth_klines(240)
    try:
        first = recompute_chart_data_from_klines(
            "a", "SYNMEMO", "1m", {}, frame, cache_key=key,
        )
        cd = kline_recompute._cl_pool[key]["cl"]
        analysis_memo = cd._strict_structure_memo["evidence"]
        assert analysis_memo is not None
        calls = []
        original = cd.process_validated_incremental_klines

        def process(updated):
            calls.append(updated)
            return original(updated)

        monkeypatch.setattr(cd, "process_validated_incremental_klines", process)
        second = recompute_chart_data_from_klines(
            "a", "SYNMEMO", "1m", {}, frame.copy(), cache_key=key,
        )
        assert calls == []
        assert second == first
        assert cd._strict_structure_memo["evidence"] is analysis_memo
    finally:
        kline_recompute.reset_cl_pool()


def test_source_deadline_is_replaced_without_mutating_or_reserializing_published_data(monkeypatch):
    from cl_app.services import chart_cache
    key = "source-deadline-reuse"
    frame = _synth_klines(240)
    frame.attrs["_history_source_valid_until"] = 100_000.0
    monkeypatch.setattr(chart_cache, "_persist_chart_cache_async", lambda *args: None)
    kline_recompute.reset_cl_pool()
    try:
        first = recompute_chart_data_from_klines("a", "SYNMEMO", "1m", {}, frame, cache_key=key)
        chart_cache._set_chart_cache_entry(key, first, is_full_snapshot=True)
        assert first["_history_source_valid_until"] == 100_000.0
        monkeypatch.setattr(chart_compute, "serialize_chart_data_with_strict_runtime",
                            lambda **kwargs: pytest.fail("only source validation changed"))
        frame.attrs["_history_source_valid_until"] = 103_600.0
        renewed = recompute_chart_data_from_klines("a", "SYNMEMO", "1m", {}, frame, cache_key=key)
        assert first["_history_source_valid_until"] == 100_000.0
        assert renewed["_history_source_valid_until"] == 103_600.0
        assert renewed["strict_structure"] is first["strict_structure"]
        del frame.attrs["_history_source_valid_until"]
        fresh = recompute_chart_data_from_klines("a", "SYNMEMO", "1m", {}, frame, cache_key=key)
        assert "_history_source_valid_until" not in fresh
    finally:
        kline_recompute.reset_cl_pool()
        with chart_cache.cache_lock:
            chart_cache.chart_data_cache.pop(key, None)


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
