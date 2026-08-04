from __future__ import annotations

from datetime import date, datetime, timedelta
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from chanlun import config
from chanlun.decision_support.fingerprints import sha256_json
from chanlun.decision_support.trading_system.v3_forward_paper import (
    audit_forward_paper_session_delivery,
)
from chanlun.decision_support.trading_system.v3_trading_session import (
    DEFAULT_OFFICIAL_TRADING_CALENDAR_PATH,
    authoritative_trading_session_evidence,
    build_trading_session_evidence,
    official_trading_session_evidence,
    resolve_trading_session_requirement,
    validate_trading_session_evidence,
)


CN = ZoneInfo("Asia/Shanghai")


def _observed(day: date, hour: int = 16) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=CN)


def test_qmt_returned_target_proves_trading_session() -> None:
    session = date(2026, 7, 30)
    observed = _observed(session)
    evidence = build_trading_session_evidence(
        session=session,
        observed_at=observed,
        returned_sessions=(session,),
        published_through=session,
        query_attempted=True,
        query_succeeded=True,
    )

    validated = validate_trading_session_evidence(
        evidence,
        session=session,
        observed_at=observed,
    )
    requirement = resolve_trading_session_requirement(
        evidence,
        session=session,
        observed_at=observed,
    )

    assert validated["classification"] == "TRADING_SESSION"
    assert validated["reason_code"] == "QMT_TRADING_SESSION_CONFIRMED"
    assert requirement["required"] is True
    assert requirement["requirement_resolved"] is True
    assert requirement["trading_session_evidence_proven"] is True


def test_weekday_holiday_requires_a_later_published_qmt_session() -> None:
    holiday = date(2026, 7, 29)
    observed = _observed(date(2026, 7, 30))
    evidence = build_trading_session_evidence(
        session=holiday,
        observed_at=observed,
        returned_sessions=(),
        published_through=date(2026, 7, 30),
        query_attempted=True,
        query_succeeded=True,
    )

    delivery = audit_forward_paper_session_delivery(
        (),
        session=holiday,
        observed_at=observed,
        trading_session_evidence=evidence,
    )

    assert delivery["required"] is False
    assert delivery["requirement_resolved"] is True
    assert delivery["status"] == "not_due"
    assert delivery["reason_code"] == "NON_TRADING_SESSION_NOT_DUE"
    assert delivery["trading_session_reason_code"] == (
        "QMT_NON_TRADING_SESSION_CONFIRMED"
    )


def test_same_day_empty_qmt_response_is_unresolved_not_a_holiday() -> None:
    session = date(2026, 7, 31)
    observed = _observed(session, 1)
    evidence = build_trading_session_evidence(
        session=session,
        observed_at=observed,
        returned_sessions=(),
        published_through=date(2026, 7, 30),
        query_attempted=True,
        query_succeeded=True,
    )

    delivery = audit_forward_paper_session_delivery(
        (),
        session=session,
        observed_at=observed,
        trading_session_evidence=evidence,
    )

    assert delivery["required"] is None
    assert delivery["requirement_resolved"] is False
    assert delivery["status"] == "unresolved"
    assert delivery["reason_code"] == "TRADING_SESSION_EVIDENCE_UNAVAILABLE"
    assert delivery["trading_session_reason_code"] == (
        "QMT_TRADING_CALENDAR_NOT_PUBLISHED_THROUGH_SESSION"
    )


def test_future_weekday_and_missing_provider_never_use_weekday_inference() -> None:
    observed = _observed(date(2026, 7, 31), 1)
    future = date(2026, 8, 3)
    evidence = build_trading_session_evidence(
        session=future,
        observed_at=observed,
        query_attempted=False,
        query_succeeded=False,
    )
    future_requirement = resolve_trading_session_requirement(
        evidence,
        session=future,
        observed_at=observed,
    )
    missing_requirement = resolve_trading_session_requirement(
        None,
        session=date(2026, 7, 31),
        observed_at=observed,
    )

    assert future_requirement["required"] is None
    assert future_requirement["trading_session_reason_code"] == (
        "FUTURE_TRADING_SESSION_UNPUBLISHED"
    )
    assert missing_requirement["required"] is None
    assert missing_requirement["trading_session_reason_code"] == (
        "TRADING_SESSION_EVIDENCE_MISSING"
    )


def test_weekend_is_resolved_by_fixed_a_share_market_rule() -> None:
    weekend = date(2026, 8, 1)
    observed = _observed(weekend)
    evidence = build_trading_session_evidence(
        session=weekend,
        observed_at=observed,
        query_attempted=False,
        query_succeeded=False,
    )
    requirement = resolve_trading_session_requirement(
        evidence,
        session=weekend,
        observed_at=observed,
    )

    assert requirement["required"] is False
    assert requirement["trading_session_status"] == "NON_TRADING_SESSION"
    assert requirement["trading_session_reason_code"] == (
        "A_SHARE_WEEKEND_NON_TRADING_SESSION"
    )


def test_rehashed_forgery_and_future_observation_are_rejected() -> None:
    session = date(2026, 7, 30)
    observed = _observed(session)
    evidence = build_trading_session_evidence(
        session=session,
        observed_at=observed,
        returned_sessions=(session,),
        published_through=session,
        query_attempted=True,
        query_succeeded=True,
    )
    forged = dict(evidence)
    forged["classification"] = "NON_TRADING_SESSION"
    forged["reason_code"] = "QMT_NON_TRADING_SESSION_CONFIRMED"
    stable = {key: forged[key] for key in forged if key != "content_sha256"}
    forged["content_sha256"] = sha256_json(stable)

    requirement = resolve_trading_session_requirement(
        forged,
        session=session,
        observed_at=observed,
    )
    from_future = resolve_trading_session_requirement(
        build_trading_session_evidence(
            session=session,
            observed_at=observed + timedelta(minutes=1),
            returned_sessions=(session,),
            published_through=session,
            query_attempted=True,
            query_succeeded=True,
        ),
        session=session,
        observed_at=observed,
    )

    assert requirement["required"] is None
    assert requirement["trading_session_reason_code"] == (
        "TRADING_SESSION_EVIDENCE_INVALID"
    )
    assert from_future["required"] is None
    assert from_future["trading_session_reason_code"] == (
        "TRADING_SESSION_EVIDENCE_INVALID"
    )


def test_pinned_sse_calendar_resolves_current_future_and_weekday_holiday() -> None:
    observed = _observed(date(2026, 7, 31), 1)
    configured = Path(
        config.DECISION_SUPPORT_PAPER_BOOTSTRAP["paper_calendar_provider"]
    ).resolve()

    assert configured == DEFAULT_OFFICIAL_TRADING_CALENDAR_PATH.resolve()

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
    requirement = resolve_trading_session_requirement(
        current,
        session=date(2026, 7, 31),
        observed_at=observed,
    )
    assert requirement["required"] is True
    assert requirement["trading_session_evidence_proven"] is True


def test_official_calendar_is_not_used_before_publication_or_outside_coverage() -> None:
    calls: list[date] = []

    def fallback(*, session: date, observed_at: datetime):
        calls.append(session)
        return build_trading_session_evidence(
            session=session,
            observed_at=observed_at,
            query_attempted=False,
            query_succeeded=False,
        )

    before_publication = authoritative_trading_session_evidence(
        session=date(2026, 1, 5),
        observed_at=_observed(date(2025, 12, 21)),
        calendar_path=DEFAULT_OFFICIAL_TRADING_CALENDAR_PATH,
        fallback_provider=fallback,
    )
    publication_day_without_time = authoritative_trading_session_evidence(
        session=date(2026, 1, 5),
        observed_at=_observed(date(2025, 12, 22), 23),
        calendar_path=DEFAULT_OFFICIAL_TRADING_CALENDAR_PATH,
        fallback_provider=fallback,
    )
    outside_coverage = authoritative_trading_session_evidence(
        session=date(2027, 1, 4),
        observed_at=_observed(date(2026, 12, 31)),
        calendar_path=DEFAULT_OFFICIAL_TRADING_CALENDAR_PATH,
        fallback_provider=fallback,
    )

    assert before_publication["classification"] == "UNRESOLVED"
    assert publication_day_without_time["classification"] == "UNRESOLVED"
    assert outside_coverage["classification"] == "UNRESOLVED"
    assert calls == [
        date(2026, 1, 5),
        date(2026, 1, 5),
        date(2027, 1, 4),
    ]


def test_official_calendar_wins_without_calling_qmt_when_covered() -> None:
    def forbidden_fallback(**_kwargs):
        raise AssertionError("covered official session must not call QMT")

    evidence = authoritative_trading_session_evidence(
        session=date(2026, 7, 31),
        observed_at=_observed(date(2026, 7, 31), 1),
        calendar_path=DEFAULT_OFFICIAL_TRADING_CALENDAR_PATH,
        fallback_provider=forbidden_fallback,
    )

    assert evidence["source_method"] == "SSE_OFFICIAL_ANNUAL_CALENDAR"
    assert evidence["reason_code"] == "SSE_TRADING_SESSION_CONFIRMED"


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


def test_rehashed_official_verdict_forgery_is_rejected() -> None:
    session = date(2026, 7, 31)
    observed = _observed(session, 1)
    evidence = official_trading_session_evidence(
        session=session,
        observed_at=observed,
        calendar_path=DEFAULT_OFFICIAL_TRADING_CALENDAR_PATH,
    )
    assert evidence is not None
    forged = dict(evidence)
    forged["classification"] = "NON_TRADING_SESSION"
    forged["reason_code"] = "SSE_NON_TRADING_SESSION_CONFIRMED"
    forged["content_sha256"] = sha256_json(
        {key: forged[key] for key in forged if key != "content_sha256"}
    )

    requirement = resolve_trading_session_requirement(
        forged,
        session=session,
        observed_at=observed,
    )

    assert requirement["required"] is None
    assert requirement["trading_session_reason_code"] == (
        "TRADING_SESSION_EVIDENCE_INVALID"
    )
