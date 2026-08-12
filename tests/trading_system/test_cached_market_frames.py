from __future__ import annotations

from datetime import date
import sqlite3

import pandas as pd
import pytest

from tools.cached_market_frames import (
    _apply_qmt_dr_adjustments,
    _events_in_interval,
    discover_minute_symbols,
    provider_to_project_code,
)


@pytest.mark.parametrize(
    ("provider", "project"),
    (
        ("510300.SH", "SH.510300"),
        ("159915.SZ", "SZ.159915"),
        ("430047.BJ", "BJ.430047"),
    ),
)
def test_provider_to_project_code(provider: str, project: str) -> None:
    assert provider_to_project_code(provider) == project


def test_provider_to_project_code_rejects_unknown_identity() -> None:
    with pytest.raises(ValueError, match="unsupported cached A-share symbol"):
        provider_to_project_code("000300.CSI")


def test_discover_minute_symbols_is_read_only_and_filters_period(tmp_path) -> None:
    database = tmp_path / "bars.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE bars (
                symbol TEXT, period TEXT, adj_type TEXT, bar_time TEXT
            )
            """
        )
        connection.executemany(
            "INSERT INTO bars VALUES (?, ?, ?, ?)",
            (
                ("510300.SH", "P_Min1", "S_Unsplit", "2020-01-02 09:30:00"),
                ("510300.SH", "P_Min1", "S_Unsplit", "2020-01-02 09:31:00"),
                ("510050.SH", "P_Min1", "S_Unsplit", "2020-01-02 09:30:00"),
                ("000300.CSI", "P_Day1", "S_Unsplit", "2020-01-02 00:00:00"),
            ),
        )
    rows = discover_minute_symbols(database)
    assert [item["provider_symbol"] for item in rows] == [
        "510050.SH",
        "510300.SH",
    ]
    assert rows[1]["project_code"] == "SH.510300"
    assert rows[1]["rows"] == 2


def test_qmt_dr_adjustment_is_effective_date_causal() -> None:
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(
                ["2020-01-02 10:00:00", "2020-01-03 10:00:00"]
            ),
            "open": [10.0, 10.0],
            "high": [10.0, 10.0],
            "low": [10.0, 10.0],
            "close": [10.0, 10.0],
        }
    )
    adjusted = _apply_qmt_dr_adjustments(
        frame,
        (
            {
                "effective_on": date(2020, 1, 3),
                "raw_price_divisor": "1.1",
            },
        ),
    )
    assert adjusted["close"].tolist() == pytest.approx([10.0, 11.0])


def test_only_events_inside_source_interval_are_applied() -> None:
    events = tuple(
        {"effective_on": date.fromisoformat(value), "raw_price_divisor": "1.1"}
        for value in ("2019-12-31", "2020-01-03", "2020-01-06")
    )
    selected = _events_in_interval(
        events,
        start=date(2020, 1, 2),
        end=date(2020, 1, 3),
    )
    assert [item["effective_on"].isoformat() for item in selected] == [
        "2020-01-03"
    ]
