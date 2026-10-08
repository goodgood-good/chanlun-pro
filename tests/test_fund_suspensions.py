from datetime import datetime
import pandas as pd
import pytest
from chanlun.screening.fund_suspensions import intervals_from_records, SOURCE_URL
from chanlun.screening.sessions import CN, SCHEMA, _digest, exempt_closes


def test_fund_trading_halt_covers_only_official_period_and_never_subscription_stops():
    clock = lambda time: int(
        datetime.fromisoformat("2026-04-09T" + time).replace(tzinfo=CN).timestamp()
    )
    row = {
        "secCode": "513100",
        "dateSource": "2",
        "startStopType": "LSTP",
        "stopTime": "915",
        "startStopDate": "20260409",
        "endStopDate": "20260409",
    }
    intervals = intervals_from_records(
        "SH.513100",
        [row, {**row, "dateSource": "1", "stopTime": "WH"}],
        clock("09:30"),
        clock("15:00"),
    )
    assert intervals == [[clock("09:15"), clock("10:30")]]
    proof = {
        "schema": SCHEMA,
        "symbol": "SH.513100",
        "source_url": SOURCE_URL,
        "checked_through": clock("15:00"),
        "intervals": intervals,
    }
    frame = pd.DataFrame(
        {
            "code": ["SH.513100"],
            "date": [pd.Timestamp(clock("10:35"), unit="s", tz=CN)],
            "volume": [100.0],
        }
    )
    frame.attrs["screening_suspensions"] = {**proof, "revision": _digest(proof)}
    expected = {clock("09:35"), clock("10:30"), clock("10:35")}
    assert exempt_closes(
        frame,
        {"frequency": "5m", "cutoff": clock("15:00"), "symbol": "SH.513100"},
        expected,
    ) == {clock("09:35"), clock("10:30")}
    frame.loc[0, "date"] = pd.Timestamp(clock("10:30"), unit="s", tz=CN)
    with pytest.raises(ValueError, match="conflicts"):
        exempt_closes(
            frame,
            {"frequency": "5m", "cutoff": clock("15:00"), "symbol": "SH.513100"},
            expected,
        )


def test_fund_record_for_another_symbol_cannot_excuse_gaps():
    with pytest.raises(ValueError, match="identity"):
        intervals_from_records("SH.513100", [{"secCode": "510300"}], 0, 100)
