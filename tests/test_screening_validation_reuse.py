from copy import deepcopy
from datetime import datetime

import pandas as pd
import pytest

from chanlun.screening import rules
from chanlun.screening.validation_cache import reuse_validation, validation_scope


def sample():
    context = rules.trading_context(datetime(2026, 10, 12, 15, tzinfo=rules.CN), "5m")
    dates = pd.to_datetime(
        context["expected_closes"][-250:], unit="s", utc=True
    ).tz_convert(rules.CN)
    frame = pd.DataFrame(
        {
            "date": dates,
            "open": 10.0,
            "high": 11.0,
            "low": 9.0,
            "close": 10.0,
            "volume": 1.0,
        }
    )
    return frame, context


@pytest.mark.parametrize(
    "change",
    [
        "history",
        "volume",
        "time",
        "basis",
        "suspensions",
        "session_error",
        "calendar",
        "cutoff",
    ],
)
def test_content_and_metadata_changes_cannot_reuse_a_validation(change):
    frame, context = sample()
    calls = []

    @reuse_validation
    def check(f, c):
        calls.append(True)
        return rules.frame_quality(f, c)

    with validation_scope():
        before = check(frame, context)
        assert (
            before["errors"] == []
            and check(frame, context) == before
            and len(calls) == 1
        )
        if change == "history":
            frame.loc[0, "high"] = 1.0
        elif change == "volume":
            frame.loc[0, "volume"] = -1.0
        elif change == "time":
            frame.loc[0, "date"] += pd.Timedelta(seconds=1)
        elif change == "basis":
            frame.attrs["price_basis_revision"] = "corrected"
        elif change == "suspensions":
            frame.attrs["screening_suspensions"] = {"changed": True}
        elif change == "session_error":
            frame.attrs["_screening_session_error"] = "provider changed"
        elif change == "calendar":
            context["calendar_coverage_start"] = "2026-10-12"
        elif change == "cutoff":
            context["cutoff"] += 300
        # Suspension payload need not be valid to invalidate the certificate.
        try:
            check(frame, context)
        except (ValueError, KeyError):
            pass
        assert len(calls) == 2


def test_validation_reuse_is_scoped_and_does_not_share_mutable_results(monkeypatch):
    frame, context = sample()
    original = rules.validate_frame
    calls = []

    def counted(*args):
        calls.append(True)
        return original(*args)

    monkeypatch.setattr(rules, "validate_frame", counted)
    with validation_scope():
        quality = rules.frame_quality(frame, context)
        expected = deepcopy(quality)
        quality["errors"].append("caller mutation")
        assert rules.frame_quality(frame, context) == expected and len(calls) == 1
    with validation_scope():
        assert rules.frame_quality(frame, context) == expected and len(calls) == 2


def test_unsupported_metadata_falls_back_to_checking_each_time():
    frame, context = sample()
    frame.attrs["opaque"] = object()
    calls = []

    @reuse_validation
    def check(*args):
        calls.append(True)
        return []

    with validation_scope():
        check(frame, context)
        check(frame, context)
    assert len(calls) == 2
