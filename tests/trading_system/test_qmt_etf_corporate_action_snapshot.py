from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from tools import snapshot_qmt_etf_corporate_actions as snapshot


def test_schema_and_snapshot_hash_are_deterministic() -> None:
    left = {
        "schema": snapshot.SCHEMA,
        "generated_at": "2026-01-01T00:00:00+08:00",
        "instruments": [{"code": "510300.SH", "events": []}],
    }
    right = {
        "instruments": [{"events": [], "code": "510300.SH"}],
        "generated_at": "2026-07-26T00:00:00+08:00",
        "schema": snapshot.SCHEMA,
    }

    assert snapshot.SCHEMA == "chanlun-qmt-etf-corporate-actions"
    assert snapshot._snapshot_content_sha256(left) == snapshot._snapshot_content_sha256(
        right
    )


def test_510300_qmt_dates_and_cash_match_existing_pit_ledger() -> None:
    qmt = (
        {
            "effective_on": "2019-01-16",
            "raw": {"interest": 0.059},
        },
        {
            "effective_on": "2026-01-19",
            "raw": {"interest": 0.123},
        },
    )
    pit = (
        {"effective_on": "2019-01-16", "cash_per_share": "0.059"},
        {"effective_on": "2026-01-19", "cash_per_share": "0.123"},
    )

    result = snapshot._crosscheck_510300(qmt, pit)

    assert result["status"] == "EXACT_EFFECTIVE_DATE_AND_CASH_MATCH"
    assert result["common_events"] == 2
    assert result["cash_mismatches"] == ()


def test_empty_provider_frame_is_not_certified_as_no_event(
    monkeypatch, tmp_path: Path
) -> None:
    from xtquant import xtdata

    data_dir = tmp_path / "datadir"
    (data_dir / "DividData").mkdir(parents=True)
    monkeypatch.setattr(xtdata, "get_data_dir", lambda: str(data_dir))
    monkeypatch.setattr(
        xtdata,
        "get_instrument_detail",
        lambda _code, _full: {
            "ExchangeID": "SH",
            "InstrumentID": "512880",
            "InstrumentName": "test ETF",
            "OpenDate": "20160808",
            "ExpireDate": "99999999",
        },
    )
    monkeypatch.setattr(xtdata, "get_divid_factors", lambda _code: pd.DataFrame())

    result = snapshot.build_snapshot(
        ("512880.SH",),
        pit_database=tmp_path / "missing.sqlite3",
    )
    instrument = result["instruments"][0]

    assert instrument["status"] == "NO_ROWS_UNKNOWN_NOT_CERTIFIED_NO_EVENT"
    assert instrument["causal_application"] == (
        "NOT_ALLOWED_UNTIL_CROSS_SOURCE_CERTIFIED"
    )
    assert instrument["events"] == []


def test_effective_event_is_available_on_boundary_but_never_before() -> None:
    events = (
        {"effective_on": "2019-01-16", "raw": {"interest": 0.059}},
        {"effective_on": "2020-01-16", "raw": {"interest": 0.060}},
    )

    assert snapshot.causal_events_at(events, decision_on=date(2019, 1, 15)) == ()
    assert snapshot.causal_events_at(
        events, decision_on=date(2019, 1, 16)
    ) == events[:1]
    assert snapshot.causal_events_at(
        events, decision_on=date(2020, 1, 16)
    ) == events
