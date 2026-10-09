"""Official history distinguishes repeated halts from actual missing prices."""

from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
from unittest.mock import Mock

import pandas as pd
import pytest
import requests

from chanlun.screening import sessions, sse_stock_suspensions as sse
from chanlun.screening.rules import (
    trading_context,
    expected_closes_between,
    frame_quality,
)

FIXTURE = (
    Path(__file__).parent / "fixtures/suspensions/sse_600228_20260608_20260625.json"
)


def ts(day, clock="15:00"):
    return int(
        datetime.fromisoformat(day + "T" + clock)
        .replace(tzinfo=sessions.CN)
        .timestamp()
    )


@pytest.fixture
def body():
    return json.loads(FIXTURE.read_bytes())


def test_actual_repeated_halts_remove_false_gaps_but_keep_a_real_missing_candle(
    body, monkeypatch, tmp_path
):
    # The previous latest-only feed contains June 17-24, losing June 8.
    context = {
        **trading_context(datetime(2026, 6, 25, 15, tzinfo=sessions.CN), "5m"),
        "symbol": "SH.600228",
    }
    closes = sorted(expected_closes_between(context, ts("2026-06-05", "09:30")))
    frame = pd.DataFrame(
        {
            "code": "SH.600228",
            "date": pd.to_datetime(closes, unit="s", utc=True).tz_convert(sessions.CN),
            "open": 10.0,
            "high": 11.0,
            "low": 9.0,
            "close": 10.0,
            "volume": 100.0,
        }
    )
    raw = frame.copy()
    halted = sse.intervals_from_records(
        "SH.600228", body["result"], closes[0], context["cutoff"]
    )
    for first, last in halted:
        frame = frame.loc[
            ~frame.date.between(
                pd.Timestamp(first, unit="s", tz=sessions.CN),
                pd.Timestamp(last, unit="s", tz=sessions.CN),
            )
        ].copy()
    monkeypatch.setattr(sse, "_fetch", Mock(return_value=body["result"]))
    proof = sessions.suspension_evidence(
        "SH.600228", ts("2026-06-08", "09:35"), context["cutoff"], cache_root=tmp_path
    )
    frame.attrs["screening_suspensions"] = proof
    assert frame_quality(frame, context)["errors"] == []
    assert frame_quality(frame.drop(frame.index[-2]), context)["missing_bars"] == 1
    old = deepcopy(proof)
    old["intervals"] = [halted[1]]
    old["revision"] = sessions._digest(
        {k: v for k, v in old.items() if k != "revision"}
    )
    frame.attrs["screening_suspensions"] = old
    assert frame_quality(frame, context)["missing_bars"] == 48
    raw.attrs["screening_suspensions"] = proof
    assert frame_quality(raw, context)["errors"] == ["INVALID_SESSION_EVIDENCE"]


@pytest.mark.parametrize(
    "slot,first,last",
    [
        ("WH", "09:15", "15:00"),
        ("AM", "09:15", "11:30"),
        ("PM", "13:00", "15:00"),
        ("930", "09:30", "10:30"),
    ],
)
def test_stock_slots_follow_stock_mapping_not_fund_mapping(body, slot, first, last):
    row = {**body["result"][1], "stopTime": slot}
    assert sse.intervals_from_records(
        "SH.600228", [row], ts("2026-06-08", "09:30"), ts("2026-06-09")
    ) == [[ts("2026-06-08", first), ts("2026-06-08", last)]]


@pytest.mark.parametrize(
    "changes",
    [
        {"stopTime": "9h"},
        {"type": "unknown"},
        {"controlType": "CB"},
        {"endStopDate": ""},
        {"endStopDate": "20260609"},
    ],
)
def test_unknown_temporary_or_unclosed_records_never_excuse_gaps(body, changes):
    assert not sse.intervals_from_records(
        "SH.600228",
        [{**body["result"][1], **changes}],
        ts("2026-06-08", "09:30"),
        ts("2026-06-25"),
    )


def fake_session(monkeypatch, bodies):
    session = Mock()
    session.__enter__ = Mock(return_value=session)
    session.__exit__ = Mock(return_value=False)
    session.get.side_effect = [Mock(json=Mock(return_value=body)) for body in bodies]
    monkeypatch.setattr(sse.requests, "Session", lambda: session)
    return session


def test_official_overlapping_query_accepts_a_halt_beginning_before_window(
    body, monkeypatch
):
    body["result"] = body["result"][:1]
    body["pageHelp"]["total"] = 1
    session = fake_session(monkeypatch, [body])
    assert sse._fetch("SH.600228", "2026-06-20", "2026-06-25") == body["result"]
    assert session.trust_env is False


@pytest.mark.parametrize(
    "mutation",
    [
        "wrong_code",
        "wrong_window",
        "wrong_page",
        "wrong_sql",
        "bad_total",
        "empty_page",
    ],
)
def test_bad_query_contract_is_not_cached_or_treated_as_no_halts(
    body, monkeypatch, mutation
):
    if mutation == "wrong_code":
        body["result"][0]["productCode"] = "600000"
    if mutation == "wrong_window":
        body["result"][0]["startStopDate"] = "20260701"
    if mutation == "wrong_page":
        body["pageHelp"]["pageNo"] = 2
    if mutation == "wrong_sql":
        body["sqlId"] = "SSE_PL_JYTS_TFPXX_JJ"
    if mutation == "bad_total":
        body["pageHelp"]["total"] = True
    if mutation == "empty_page":
        body["result"] = []
    fake_session(monkeypatch, [body])
    with pytest.raises(ValueError):
        sse._fetch("SH.600228", "2026-06-08", "2026-06-25")


def test_page_repetition_cannot_masquerade_as_complete_history(body, monkeypatch):
    body["pageHelp"]["total"] = 4
    page2 = deepcopy(body)
    page2["pageHelp"]["pageNo"] = 2
    fake_session(monkeypatch, [body, page2])
    with pytest.raises(ValueError, match="repeated"):
        sse._fetch("SH.600228", "2026-06-08", "2026-06-25")


@pytest.mark.parametrize(
    "changes",
    [
        {"actionErrors": ["query failure"]},
        {"fieldErrors": {"startStopDate": "invalid"}},
        {"pageHelp": None},
    ],
)
def test_error_envelope_cannot_authorize_a_suspension_even_with_rows(
    body, monkeypatch, changes
):
    fake_session(monkeypatch, [{**body, **changes}])
    with pytest.raises(ValueError, match="query failed"):
        sse._fetch("SH.600228", "2026-06-08", "2026-06-25")


def test_source_cache_reuse_and_failed_refresh_do_not_publish_invalid_identity(
    body, tmp_path, monkeypatch
):
    fetch = Mock(return_value=body["result"])
    monkeypatch.setattr(sse, "_fetch", fetch)
    now = ts("2026-06-25") + 1
    monkeypatch.setattr(sse.time, "time", lambda: now)
    args = ("SH.600228", ts("2026-06-08", "09:35"), ts("2026-06-25"))
    proof = sse.suspension_evidence(*args, cache_root=tmp_path)
    assert sse.suspension_evidence(*args, cache_root=tmp_path) == proof
    assert fetch.call_count == 1
    path = tmp_path / "sse_stock_suspensions_SH.600228.json"
    saved = path.read_bytes()
    now += 3601
    fetch.return_value = [{**body["result"][0], "productCode": "600000"}]
    with pytest.raises(ValueError, match="identity"):
        sse.suspension_evidence(*args, cache_root=tmp_path)
    assert path.read_bytes() == saved
    fetch.side_effect = requests.ConnectionError("isolated failure")
    with pytest.raises(requests.ConnectionError):
        sse.suspension_evidence(*args, cache_root=tmp_path)


def test_official_source_does_not_authorize_another_exchange(body):
    proof = {
        "schema": sessions.SCHEMA,
        "symbol": "SZ.000001",
        "source_url": sse.SOURCE_URL,
        "checked_through": 1000,
        "intervals": [[0, 1000]],
    }
    frame = pd.DataFrame(
        {"date": pd.to_datetime([900], unit="s", utc=True), "volume": [0.0]}
    )
    frame.attrs["screening_suspensions"] = {
        **proof,
        "revision": sessions._digest(proof),
    }
    with pytest.raises(ValueError, match="identity"):
        sessions.exempt_closes(
            frame, {"symbol": "SZ.000001", "frequency": "5m", "cutoff": 1000}, {900}
        )
