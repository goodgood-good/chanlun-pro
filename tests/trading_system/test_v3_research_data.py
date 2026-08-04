from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from tools.chanlun_v3_research_data import (
    aggregate_completed_bars,
    normalize_completed_minute_sessions,
)


CN = ZoneInfo("Asia/Shanghai")


def raw_session(session: date) -> pd.DataFrame:
    morning = datetime.combine(session, time(9, 30), tzinfo=CN)
    afternoon = datetime.combine(session, time(13, 0), tzinfo=CN)
    timestamps = (
        *(morning + timedelta(minutes=index) for index in range(120)),
        *(afternoon + timedelta(minutes=index) for index in range(121)),
    )
    rows = []
    for index, observed_at in enumerate(timestamps):
        price = 10 + index / 1000
        rows.append(
            {
                "source_time": pd.Timestamp(observed_at),
                "source_begin_time": pd.Timestamp(observed_at),
                "open": price,
                "high": price + 0.01,
                "low": price - 0.01,
                "close": price + 0.005,
                "previous_close": 9.9,
                "volume": float(index + 1),
                "amount": float((index + 1) * 10),
            }
        )
    return pd.DataFrame(rows)


def test_source_label_adapter_keeps_1300_and_merges_1500_at_completion() -> None:
    raw = raw_session(date(2025, 1, 2))
    normalized, audit = normalize_completed_minute_sessions(raw)

    assert audit["complete_source_sessions"] == 1
    assert audit["expected_regular_and_close_rows_per_session"] == 241
    assert len(normalized) == 240
    assert normalized.iloc[0]["date"].time() == time(9, 31)
    assert normalized.iloc[119]["date"].time() == time(11, 30)
    assert normalized.iloc[120]["date"].time() == time(13, 1)
    assert normalized.iloc[-1]["date"].time() == time(15, 0)
    assert normalized.iloc[-1]["source_row_count"] == 2
    assert normalized.iloc[-1]["volume"] == raw.iloc[-2:]["volume"].sum()


def test_lunch_placeholder_is_not_accepted_as_a_replacement_for_1300() -> None:
    raw = raw_session(date(2025, 1, 2))
    replacement = raw.iloc[119].copy()
    replacement["source_time"] = pd.Timestamp(
        datetime(2025, 1, 2, 11, 30, tzinfo=CN)
    )
    raw = pd.concat(
        (raw.iloc[:120], replacement.to_frame().T, raw.iloc[121:]),
        ignore_index=True,
    ).sort_values("source_time", kind="stable")
    raw["source_time"] = pd.to_datetime(raw["source_time"], utc=True).dt.tz_convert(CN)

    normalized, audit = normalize_completed_minute_sessions(raw)

    assert normalized.empty
    assert audit["complete_source_sessions"] == 0
    assert audit["rejected_sessions"][0]["observed_rows"] == 240


def test_explicit_zero_volume_1130_boundary_event_is_merged() -> None:
    raw = raw_session(date(2025, 1, 2))
    placeholder = raw.iloc[119].copy()
    placeholder["source_time"] = pd.Timestamp(
        datetime(2025, 1, 2, 11, 30, tzinfo=CN)
    )
    placeholder["source_begin_time"] = placeholder["source_time"]
    placeholder["open"] = placeholder["close"]
    placeholder["high"] = placeholder["close"]
    placeholder["low"] = placeholder["close"]
    placeholder["volume"] = 0.0
    placeholder["amount"] = float("nan")
    raw = pd.concat((raw, placeholder.to_frame().T), ignore_index=True)
    raw["source_time"] = pd.to_datetime(raw["source_time"], utc=True).dt.tz_convert(CN)
    raw = raw.sort_values("source_time", kind="stable")

    normalized, audit = normalize_completed_minute_sessions(raw)

    assert len(normalized) == 240
    assert audit["merged_1130_boundary_events"] == 1
    assert audit["merged_nonzero_1130_boundary_events"] == 0
    assert normalized.iloc[119]["source_row_count"] == 2


def test_nonzero_1130_boundary_print_is_preserved_in_completed_bar() -> None:
    raw = raw_session(date(2025, 1, 2))
    boundary = raw.iloc[119].copy()
    boundary["source_time"] = pd.Timestamp(
        datetime(2025, 1, 2, 11, 30, tzinfo=CN)
    )
    boundary["source_begin_time"] = boundary["source_time"]
    boundary["open"] = 10.2
    boundary["high"] = 10.3
    boundary["low"] = 10.1
    boundary["close"] = 10.25
    boundary["volume"] = 123.0
    boundary["amount"] = 1_234.0
    raw = pd.concat((raw, boundary.to_frame().T), ignore_index=True)
    raw["source_time"] = pd.to_datetime(raw["source_time"], utc=True).dt.tz_convert(CN)
    raw = raw.sort_values("source_time", kind="stable")

    normalized, audit = normalize_completed_minute_sessions(raw)

    completed = normalized.iloc[119]
    assert len(normalized) == 240
    assert audit["merged_nonzero_1130_boundary_events"] == 1
    assert completed["source_row_count"] == 2
    assert completed["high"] == 10.3
    assert completed["close"] == 10.25
    assert completed["volume"] == raw.iloc[119:121]["volume"].sum()


def test_incomplete_source_session_is_rejected_without_row_deletion_bias() -> None:
    raw = raw_session(date(2025, 1, 2)).drop(index=17).reset_index(drop=True)

    normalized, audit = normalize_completed_minute_sessions(raw)

    assert normalized.empty
    assert audit["rejected_sessions"][0]["observed_rows"] == 240


def test_completed_five_and_thirty_minute_aggregates_have_session_boundaries() -> None:
    normalized, _audit = normalize_completed_minute_sessions(
        raw_session(date(2025, 1, 2))
    )

    five = aggregate_completed_bars(normalized, minutes=5)
    thirty = aggregate_completed_bars(normalized, minutes=30)

    assert len(five) == 48
    assert len(thirty) == 8
    assert tuple(value.time() for value in thirty["date"]) == (
        time(10, 0),
        time(10, 30),
        time(11, 0),
        time(11, 30),
        time(13, 30),
        time(14, 0),
        time(14, 30),
        time(15, 0),
    )
