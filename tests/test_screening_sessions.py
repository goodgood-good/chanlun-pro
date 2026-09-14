"""Suspension exemptions require independent records, never missing prices."""
from copy import deepcopy
from datetime import datetime

import pandas as pd
import pytest

from chanlun.screening import sessions
from chanlun.screening.rules import CN, expected_closes_between, frame_quality, trading_context


def _fixture():
    context = {**trading_context(datetime(2026,2,27,15,tzinfo=CN), "5m"), "symbol":"SH.601615"}
    first = int(datetime(2026,1,5,tzinfo=CN).timestamp())
    closes = sorted(expected_closes_between(context, first))
    frame = pd.DataFrame({"code":"SH.601615", "date":pd.to_datetime(closes,unit="s",utc=True).tz_convert(CN),
                          "open":10.,"high":11.,"low":9.,"close":10.5,"volume":100})
    began = datetime(2026,1,13,9,30,tzinfo=CN)
    ended = datetime(2026,1,22,15,tzinfo=CN)
    return frame.loc[~frame.date.between(began,ended)].copy(), context


def test_reported_halt_is_exempt_but_missing_normal_trading_bars_are_not(monkeypatch, tmp_path):
    record = {"SECUCODE":"601615.SH", "SUSPEND_START_TIME":"2026-01-13 09:30:00",
              "SUSPEND_END_TIME":"2026-01-22 15:00:00", "PREDICT_RESUME_DATE":"2026-01-26"}
    calls=[]
    def records(start):
        calls.append(start)
        return [record]
    monkeypatch.setattr(sessions, "_fetch_records", records)
    frame, context = _fixture()
    before = frame_quality(frame, context)
    assert before["errors"] == ["DATA_GAPS"]
    assert before["missing_bars"] == 384
    start = before["first_missing_at"]
    frame.attrs["screening_suspensions"] = sessions.suspension_evidence("SH.601615",start,context["cutoff"],cache_root=tmp_path)
    assert frame_quality(frame, context)["errors"] == []
    damaged = frame.drop(index=frame.index[-25])
    assert frame_quality(damaged, context)["errors"] == ["DATA_GAPS"]
    assert frame_quality(damaged, context)["missing_bars"] == 1
    # Another period consumes the same fetched records, not another HTTP request.
    sessions.suspension_evidence("SH.601615",start,context["cutoff"],cache_root=tmp_path)
    assert len(calls) == 1


def test_quote_alignment_flag_alone_never_exempts_missing_history():
    frame, context = _fixture()
    frame.attrs["suspendFlag"] = 1
    assert frame_quality(frame, context)["missing_bars"] == 384


def test_wrong_symbol_or_traded_interval_invalidates_suspension_claim(monkeypatch, tmp_path):
    monkeypatch.setattr(sessions,"_fetch_records",lambda _: [
        {"SECUCODE":"601615.SH","SUSPEND_START_TIME":"2026-01-12 09:30:00","SUSPEND_END_TIME":"2026-01-22 15:00:00"}])
    frame, context = _fixture()
    evidence = sessions.suspension_evidence("SH.601615",int(frame.date.iloc[0].timestamp()),context["cutoff"],cache_root=tmp_path)
    frame.attrs["screening_suspensions"] = evidence
    assert frame_quality(frame, context)["errors"] == ["INVALID_SESSION_EVIDENCE"]
    modified = deepcopy(evidence)
    modified["symbol"] = "SZ.000001"
    modified["revision"] = sessions._digest({k:v for k,v in modified.items() if k!="revision"})
    frame.attrs["screening_suspensions"] = modified
    assert frame_quality(frame, context)["errors"] == ["INVALID_SESSION_EVIDENCE"]


def test_unavailable_feed_does_not_create_a_suspension_cache(monkeypatch, tmp_path):
    def unavailable(_):
        raise ValueError("feed unavailable")
    monkeypatch.setattr(sessions,"_fetch_records",unavailable)
    with pytest.raises(ValueError,match="feed unavailable"):
        sessions.suspension_evidence("SH.601615",1,2,cache_root=tmp_path)
    assert not (tmp_path/"suspensions.json").exists()
