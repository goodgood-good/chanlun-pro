from __future__ import annotations

from datetime import date, datetime
import json
from zoneinfo import ZoneInfo

import pytest

from chanlun.persistence.fingerprints import sha256_json
from chanlun.exchange.trading_session import (
    DEFAULT_OFFICIAL_TRADING_CALENDAR_PATH,
    official_trading_session_evidence,
)


CN = ZoneInfo("Asia/Shanghai")


def _observed(day: date, hour: int = 16) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=CN)


def test_pinned_sse_calendar_resolves_current_future_and_weekday_holiday() -> None:
    observed = _observed(date(2026, 7, 31), 1)

    current = official_trading_session_evidence(
        session=date(2026, 7, 31),
        observed_at=observed,
        calendar_path=DEFAULT_OFFICIAL_TRADING_CALENDAR_PATH,
    )
    future = official_trading_session_evidence(
        session=date(2026, 8, 3),
        observed_at=observed,
        calendar_path=DEFAULT_OFFICIAL_TRADING_CALENDAR_PATH,
    )
    holiday = official_trading_session_evidence(
        session=date(2026, 5, 1),
        observed_at=observed,
        calendar_path=DEFAULT_OFFICIAL_TRADING_CALENDAR_PATH,
    )

    assert current is not None and current["classification"] == "TRADING_SESSION"
    assert future is not None and future["classification"] == "TRADING_SESSION"
    assert holiday is not None
    assert holiday["classification"] == "NON_TRADING_SESSION"
    assert holiday["reason_code"] == "SSE_NON_TRADING_SESSION_CONFIRMED"
    assert current["calendar_document"]["trading_days"][-1] == "2026-12-31"
    assert len(current["calendar_document"]["trading_days"]) == 242
    assert current["source_document"]["published_on"] == "2025-12-22"


@pytest.mark.parametrize(
    ("session", "observed"),
    (
        (date(2026, 1, 5), _observed(date(2025, 12, 21))),
        (date(2026, 1, 5), _observed(date(2025, 12, 22), 23)),
        (date(2027, 1, 4), _observed(date(2026, 12, 31))),
    ),
)
def test_official_calendar_is_not_used_before_publication_or_outside_coverage(
    session, observed,
) -> None:
    assert official_trading_session_evidence(
        session=session, observed_at=observed,
        calendar_path=DEFAULT_OFFICIAL_TRADING_CALENDAR_PATH,
    ) is None


def test_rehashed_calendar_or_source_tampering_is_rejected(tmp_path) -> None:
    original_calendar = json.loads(
        DEFAULT_OFFICIAL_TRADING_CALENDAR_PATH.read_text(encoding="utf-8")
    )
    original_source_path = DEFAULT_OFFICIAL_TRADING_CALENDAR_PATH.with_suffix(
        ".source.json"
    )
    original_source = json.loads(original_source_path.read_text(encoding="utf-8"))
    calendar_path = tmp_path / "calendar.json"
    source_path = calendar_path.with_suffix(".source.json")
    source_path.write_text(json.dumps(original_source), encoding="utf-8")

    forged_calendar = dict(original_calendar)
    forged_calendar["trading_days"] = [
        value
        for value in forged_calendar["trading_days"]
        if value != "2026-07-31"
    ]
    calendar_identity = {
        key: forged_calendar[key]
        for key in forged_calendar
        if key != "calendar_fingerprint"
    }
    forged_calendar["calendar_fingerprint"] = sha256_json(calendar_identity)
    calendar_path.write_text(json.dumps(forged_calendar), encoding="utf-8")
    with pytest.raises(ValueError, match="official calendar trading days"):
        official_trading_session_evidence(
            session=date(2026, 7, 31),
            observed_at=_observed(date(2026, 7, 31)),
            calendar_path=calendar_path,
        )

    forged_source = dict(original_source)
    forged_source["announcement_id"] = "SSE-ANNOUNCEMENT-FORGED"
    forged_source["content_sha256"] = sha256_json(
        {
            key: forged_source[key]
            for key in forged_source
            if key != "content_sha256"
        }
    )
    forged_calendar = dict(original_calendar)
    forged_calendar["source_fingerprint"] = forged_source["content_sha256"]
    forged_calendar["calendar_fingerprint"] = sha256_json(
        {
            key: forged_calendar[key]
            for key in forged_calendar
            if key != "calendar_fingerprint"
        }
    )
    source_path.write_text(json.dumps(forged_source), encoding="utf-8")
    calendar_path.write_text(json.dumps(forged_calendar), encoding="utf-8")
    with pytest.raises(ValueError, match="official calendar source is not pinned"):
        official_trading_session_evidence(
            session=date(2026, 7, 31),
            observed_at=_observed(date(2026, 7, 31)),
            calendar_path=calendar_path,
        )
