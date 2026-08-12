"""Auditable A-share trading-session evidence for forward paper delivery.

The primary forward source is a pinned, self-hashed annual SSE closure
announcement.  Its trading-day list is rebuilt from the published closure
intervals and the fixed weekend rule, and is causally available only from the
day after publication because the source provides no intraday publication
timestamp.  QMT remains a strict fallback outside that artifact's coverage.

The QMT client available to this project exposes completed historical trading
dates, but its future-calendar APIs are not available in every deployment.
Consequently an empty response for today must never be interpreted as a
holiday.  Its fallback distinction stays explicit:

* a returned target date proves a trading session;
* absence is conclusive only after QMT has published a later trading date;
* otherwise the requirement remains unresolved and delivery auditing fails
  closed without inventing a weekday calendar.

No Tick, account or order API participates in either source.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta
import json
from pathlib import Path
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo

from chanlun.decision_support.fingerprints import normalize_datetime, sha256_json


TRADING_SESSION_EVIDENCE_SCHEMA = (
    "chanlun-qmt-trading-session-evidence"
)
OFFICIAL_TRADING_SESSION_EVIDENCE_SCHEMA = (
    "chanlun-official-trading-session-evidence"
)
OFFICIAL_CALENDAR_SOURCE_SCHEMA = (
    "chanlun-official-a-share-calendar-source"
)
DEFAULT_OFFICIAL_TRADING_CALENDAR_PATH = (
    Path(__file__).resolve().parent
    / "data"
    / "a_share_trading_calendar_2026.json"
)
_CN = ZoneInfo("Asia/Shanghai")
_CLASSIFICATIONS = {
    "TRADING_SESSION",
    "NON_TRADING_SESSION",
    "UNRESOLVED",
}
_QMT_REASON_CODES = {
    "QMT_TRADING_SESSION_CONFIRMED",
    "QMT_NON_TRADING_SESSION_CONFIRMED",
    "A_SHARE_WEEKEND_NON_TRADING_SESSION",
    "FUTURE_TRADING_SESSION_UNPUBLISHED",
    "QMT_TRADING_CALENDAR_NOT_PUBLISHED_THROUGH_SESSION",
    "QMT_TRADING_CALENDAR_UNAVAILABLE",
}
_QMT_FIELDS = {
    "schema",
    "market",
    "timezone",
    "session",
    "observed_at",
    "classification",
    "reason_code",
    "source_method",
    "query_attempted",
    "query_succeeded",
    "returned_sessions",
    "published_through",
    "source_response_sha256",
    "minimum_market_data_frequency",
    "tick_data_used",
    "real_account_accessed",
    "real_order_transport_enabled",
    "live_status",
    "content_sha256",
}
_OFFICIAL_REASON_CODES = {
    "SSE_TRADING_SESSION_CONFIRMED",
    "SSE_NON_TRADING_SESSION_CONFIRMED",
}
_OFFICIAL_EVIDENCE_FIELDS = {
    "schema",
    "market",
    "timezone",
    "session",
    "observed_at",
    "classification",
    "reason_code",
    "source_method",
    "source_document",
    "calendar_document",
    "minimum_market_data_frequency",
    "tick_data_used",
    "real_account_accessed",
    "real_order_transport_enabled",
    "live_status",
    "content_sha256",
}
_OFFICIAL_SOURCE_FIELDS = {
    "schema",
    "authority",
    "market",
    "timezone",
    "source_id",
    "announcement_id",
    "published_on",
    "source_url",
    "coverage_start",
    "coverage_end",
    "weekends_closed",
    "closure_ranges",
    "content_sha256",
}
_OFFICIAL_CALENDAR_FIELDS = {
    "schema",
    "market",
    "timezone",
    "source_id",
    "source_fingerprint",
    "coverage_start",
    "coverage_end",
    "trading_days",
    "calendar_fingerprint",
}
_OFFICIAL_CLOSURE_FIELDS = {"label", "start", "end"}
_PINNED_OFFICIAL_SOURCE_FINGERPRINTS = {
    "SSE-ANNOUNCEMENT-2025-45": (
        "sha256:8f5ea6d9f7e5e253a3d3b8920da690c5f3a7e73934bc2e2011babc9dd6e32777"
    ),
}


def _strict_date(value: object, field_name: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{field_name} must be a canonical ISO date")
    return parsed


def _read_json_mapping(path: Path, field_name: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{field_name} is unavailable or invalid") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be a JSON object")
    return value


def _validated_official_documents(
    calendar_document: Mapping[str, object],
    source_document: Mapping[str, object],
) -> tuple[
    dict[str, object],
    dict[str, object],
    date,
    date,
    date,
    tuple[date, ...],
]:
    """Rebuild the pinned SSE annual calendar from its closure intervals."""

    if set(source_document) != _OFFICIAL_SOURCE_FIELDS:
        raise ValueError("official calendar source schema is invalid")
    source = dict(source_document)
    if (
        source.get("schema") != OFFICIAL_CALENDAR_SOURCE_SCHEMA
        or source.get("authority") != "Shanghai Stock Exchange"
        or source.get("market") != "a"
        or source.get("timezone") != "Asia/Shanghai"
        or source.get("weekends_closed") is not True
    ):
        raise ValueError("official calendar source schema is invalid")
    source_stable = {
        key: source[key] for key in source if key != "content_sha256"
    }
    if source.get("content_sha256") != sha256_json(source_stable):
        raise ValueError("official calendar source hash is invalid")
    source_id = source.get("source_id")
    if (
        not isinstance(source_id, str)
        or _PINNED_OFFICIAL_SOURCE_FINGERPRINTS.get(source_id)
        != source.get("content_sha256")
    ):
        raise ValueError("official calendar source is not pinned")
    if source.get("announcement_id") != source_id:
        raise ValueError("official calendar announcement identity is invalid")
    source_url = source.get("source_url")
    if not isinstance(source_url, str) or not source_url.startswith(
        "https://www.sse.com.cn/"
    ):
        raise ValueError("official calendar source URL is invalid")
    published_on = _strict_date(source.get("published_on"), "published_on")
    coverage_start = _strict_date(
        source.get("coverage_start"), "coverage_start"
    )
    coverage_end = _strict_date(source.get("coverage_end"), "coverage_end")
    if coverage_start > coverage_end:
        raise ValueError("official calendar coverage is invalid")
    raw_ranges = source.get("closure_ranges")
    if not isinstance(raw_ranges, list) or not raw_ranges:
        raise ValueError("official calendar closure ranges are invalid")
    ranges: list[tuple[date, date]] = []
    labels: set[str] = set()
    for raw in raw_ranges:
        if not isinstance(raw, Mapping) or set(raw) != _OFFICIAL_CLOSURE_FIELDS:
            raise ValueError("official calendar closure range is invalid")
        label = raw.get("label")
        if not isinstance(label, str) or not label or label in labels:
            raise ValueError("official calendar closure label is invalid")
        labels.add(label)
        start = _strict_date(raw.get("start"), "closure.start")
        end = _strict_date(raw.get("end"), "closure.end")
        if start > end or start < coverage_start or end > coverage_end:
            raise ValueError("official calendar closure range is invalid")
        ranges.append((start, end))
    if ranges != sorted(ranges) or any(
        previous_end >= current_start
        for (_, previous_end), (current_start, _) in zip(ranges, ranges[1:])
    ):
        raise ValueError("official calendar closure ranges overlap or are unordered")

    expected: list[date] = []
    current = coverage_start
    while current <= coverage_end:
        if current.weekday() < 5 and not any(
            start <= current <= end for start, end in ranges
        ):
            expected.append(current)
        current += timedelta(days=1)

    if set(calendar_document) != _OFFICIAL_CALENDAR_FIELDS:
        raise ValueError("official calendar schema is invalid")
    calendar = dict(calendar_document)
    if (
        calendar.get("schema") != "current"
        or calendar.get("market") != "a"
        or calendar.get("timezone") != "Asia/Shanghai"
        or calendar.get("source_id") != source_id
        or calendar.get("source_fingerprint") != source.get("content_sha256")
        or calendar.get("coverage_start") != coverage_start.isoformat()
        or calendar.get("coverage_end") != coverage_end.isoformat()
    ):
        raise ValueError("official calendar identity is invalid")
    calendar_stable = {
        key: calendar[key] for key in calendar if key != "calendar_fingerprint"
    }
    if calendar.get("calendar_fingerprint") != sha256_json(calendar_stable):
        raise ValueError("official calendar hash is invalid")
    raw_days = calendar.get("trading_days")
    if not isinstance(raw_days, list):
        raise ValueError("official calendar trading days are invalid")
    trading_days = tuple(
        _strict_date(value, "trading_days") for value in raw_days
    )
    if trading_days != tuple(expected):
        raise ValueError("official calendar trading days do not match source")
    return (
        calendar,
        source,
        published_on,
        coverage_start,
        coverage_end,
        trading_days,
    )


def _dates(values: Sequence[date]) -> tuple[date, ...]:
    if isinstance(values, (str, bytes)):
        raise TypeError("returned_sessions must be a sequence of dates")
    result = tuple(values)
    if (
        any(
            not isinstance(value, date) or isinstance(value, datetime)
            for value in result
        )
        or tuple(sorted(result)) != result
        or len(result) != len(set(result))
    ):
        raise ValueError("returned_sessions must be unique sorted dates")
    return result


def _classification(
    *,
    session: date,
    observed_day: date,
    returned_sessions: tuple[date, ...],
    published_through: date | None,
    query_attempted: bool,
    query_succeeded: bool,
) -> tuple[str, str, str]:
    if session.weekday() >= 5:
        return (
            "NON_TRADING_SESSION",
            "A_SHARE_WEEKEND_NON_TRADING_SESSION",
            "A_SHARE_WEEKEND_RULE",
        )
    if session > observed_day:
        return (
            "UNRESOLVED",
            "FUTURE_TRADING_SESSION_UNPUBLISHED",
            "QMT_GET_TRADING_DATES",
        )
    if not query_attempted or not query_succeeded:
        return (
            "UNRESOLVED",
            "QMT_TRADING_CALENDAR_UNAVAILABLE",
            "QMT_GET_TRADING_DATES",
        )
    if session in returned_sessions:
        return (
            "TRADING_SESSION",
            "QMT_TRADING_SESSION_CONFIRMED",
            "QMT_GET_TRADING_DATES",
        )
    if published_through is not None and published_through > session:
        return (
            "NON_TRADING_SESSION",
            "QMT_NON_TRADING_SESSION_CONFIRMED",
            "QMT_GET_TRADING_DATES",
        )
    return (
        "UNRESOLVED",
        "QMT_TRADING_CALENDAR_NOT_PUBLISHED_THROUGH_SESSION",
        "QMT_GET_TRADING_DATES",
    )


def build_trading_session_evidence(
    *,
    session: date,
    observed_at: datetime,
    returned_sessions: Sequence[date] = (),
    published_through: date | None = None,
    query_attempted: bool,
    query_succeeded: bool,
) -> dict[str, object]:
    """Build one immutable, self-hashed trading-session observation."""

    if isinstance(session, datetime) or not isinstance(session, date):
        raise TypeError("session must be a date")
    observed = normalize_datetime(observed_at, "observed_at").astimezone(_CN)
    sessions = _dates(returned_sessions)
    if published_through is not None and (
        isinstance(published_through, datetime)
        or not isinstance(published_through, date)
    ):
        raise TypeError("published_through must be a date or None")
    if type(query_attempted) is not bool or type(query_succeeded) is not bool:
        raise TypeError("query flags must be booleans")
    if query_succeeded and not query_attempted:
        raise ValueError("a successful query must have been attempted")
    if any(value != session for value in sessions):
        raise ValueError("QMT target query returned a date outside the target session")
    if sessions and (
        published_through is None or published_through < sessions[-1]
    ):
        raise ValueError("published_through must cover returned sessions")
    if session.weekday() >= 5 and (
        sessions
        or published_through is not None
        or query_attempted
        or query_succeeded
    ):
        raise ValueError("weekend evidence must use only the fixed market rule")

    classification, reason_code, source_method = _classification(
        session=session,
        observed_day=observed.date(),
        returned_sessions=sessions,
        published_through=published_through,
        query_attempted=query_attempted,
        query_succeeded=query_succeeded,
    )
    source_payload = {
        "market": "SH",
        "target_session": session.isoformat(),
        "returned_sessions": [value.isoformat() for value in sessions],
        "published_through": (
            None if published_through is None else published_through.isoformat()
        ),
        "query_attempted": query_attempted,
        "query_succeeded": query_succeeded,
    }
    stable: dict[str, object] = {
        "schema": TRADING_SESSION_EVIDENCE_SCHEMA,
        "market": "SH",
        "timezone": "Asia/Shanghai",
        "session": session.isoformat(),
        "observed_at": observed.isoformat(),
        "classification": classification,
        "reason_code": reason_code,
        "source_method": source_method,
        "query_attempted": query_attempted,
        "query_succeeded": query_succeeded,
        "returned_sessions": source_payload["returned_sessions"],
        "published_through": source_payload["published_through"],
        "source_response_sha256": sha256_json(source_payload),
        "minimum_market_data_frequency": "1m",
        "tick_data_used": False,
        "real_account_accessed": False,
        "real_order_transport_enabled": False,
        "live_status": "LIVE_DISABLED",
    }
    return {**stable, "content_sha256": sha256_json(stable)}


def _build_official_trading_session_evidence(
    *,
    session: date,
    observed_at: datetime,
    calendar_document: Mapping[str, object],
    source_document: Mapping[str, object],
) -> dict[str, object] | None:
    observed = normalize_datetime(observed_at, "observed_at").astimezone(_CN)
    (
        calendar,
        source,
        published_on,
        coverage_start,
        coverage_end,
        trading_days,
    ) = _validated_official_documents(calendar_document, source_document)
    # 年度公告发布后可以界定未来交易日，但证据只能证明发布日期，不能证明日内时刻。
    # 因此从次日才视为因果可用，确保发布日期 00:01 的观测绝不会借用稍后发布的公告。
    if observed.date() <= published_on or not (
        coverage_start <= session <= coverage_end
    ):
        return None
    is_trading = session in frozenset(trading_days)
    stable: dict[str, object] = {
        "schema": OFFICIAL_TRADING_SESSION_EVIDENCE_SCHEMA,
        "market": "SH",
        "timezone": "Asia/Shanghai",
        "session": session.isoformat(),
        "observed_at": observed.isoformat(),
        "classification": (
            "TRADING_SESSION" if is_trading else "NON_TRADING_SESSION"
        ),
        "reason_code": (
            "SSE_TRADING_SESSION_CONFIRMED"
            if is_trading
            else "SSE_NON_TRADING_SESSION_CONFIRMED"
        ),
        "source_method": "SSE_OFFICIAL_ANNUAL_CALENDAR",
        "source_document": source,
        "calendar_document": calendar,
        "minimum_market_data_frequency": "1m",
        "tick_data_used": False,
        "real_account_accessed": False,
        "real_order_transport_enabled": False,
        "live_status": "LIVE_DISABLED",
    }
    return {**stable, "content_sha256": sha256_json(stable)}


def official_trading_session_evidence(
    *,
    session: date,
    observed_at: datetime,
    calendar_path: str | Path = DEFAULT_OFFICIAL_TRADING_CALENDAR_PATH,
) -> dict[str, object] | None:
    """Return pinned SSE evidence when the target is causally covered.

    ``None`` means the annual artifact was either not published at the
    observation time or does not cover the target year; callers may then use a
    separately audited QMT fallback.  A missing or corrupt configured artifact
    raises instead of silently downgrading to an unrelated source.
    """

    if isinstance(session, datetime) or not isinstance(session, date):
        raise TypeError("session must be a date")
    path = Path(calendar_path).expanduser().absolute()
    calendar = _read_json_mapping(path, "official calendar")
    source = _read_json_mapping(
        path.with_suffix(".source.json"),
        "official calendar source",
    )
    return _build_official_trading_session_evidence(
        session=session,
        observed_at=observed_at,
        calendar_document=calendar,
        source_document=source,
    )


def authoritative_trading_session_evidence(
    *,
    session: date,
    observed_at: datetime,
    fallback_provider: Callable[..., Mapping[str, object]],
    calendar_path: str | Path = DEFAULT_OFFICIAL_TRADING_CALENDAR_PATH,
) -> dict[str, object]:
    """Prefer the pinned exchange calendar, with strict QMT fallback."""

    official = official_trading_session_evidence(
        session=session,
        observed_at=observed_at,
        calendar_path=calendar_path,
    )
    if official is not None:
        return official
    fallback = fallback_provider(session=session, observed_at=observed_at)
    if not isinstance(fallback, Mapping):
        raise TypeError("trading session fallback returned an invalid document")
    return validate_trading_session_evidence(
        fallback,
        session=session,
        observed_at=observed_at,
    )


def _validate_official_trading_session_evidence(
    evidence: Mapping[str, object],
    *,
    session: date,
    observed_at: datetime,
) -> dict[str, object]:
    if set(evidence) != _OFFICIAL_EVIDENCE_FIELDS:
        raise ValueError("official trading session evidence schema is invalid")
    if (
        evidence.get("schema") != OFFICIAL_TRADING_SESSION_EVIDENCE_SCHEMA
        or evidence.get("market") != "SH"
        or evidence.get("timezone") != "Asia/Shanghai"
        or evidence.get("session") != session.isoformat()
        or evidence.get("source_method") != "SSE_OFFICIAL_ANNUAL_CALENDAR"
        or evidence.get("minimum_market_data_frequency") != "1m"
        or evidence.get("tick_data_used") is not False
        or evidence.get("real_account_accessed") is not False
        or evidence.get("real_order_transport_enabled") is not False
        or evidence.get("live_status") != "LIVE_DISABLED"
    ):
        raise ValueError("official trading session evidence identity is invalid")
    stable = {key: evidence[key] for key in evidence if key != "content_sha256"}
    if evidence.get("content_sha256") != sha256_json(stable):
        raise ValueError("official trading session evidence content hash is invalid")
    try:
        evidence_observed = datetime.fromisoformat(str(evidence["observed_at"]))
    except ValueError as exc:
        raise ValueError("official trading session observation is invalid") from exc
    audit_observed = normalize_datetime(observed_at, "observed_at").astimezone(_CN)
    evidence_observed = normalize_datetime(
        evidence_observed,
        "evidence.observed_at",
    ).astimezone(_CN)
    if evidence_observed > audit_observed:
        raise ValueError("official trading session evidence comes from the future")
    calendar = evidence.get("calendar_document")
    source = evidence.get("source_document")
    if not isinstance(calendar, Mapping) or not isinstance(source, Mapping):
        raise ValueError("official trading session documents are invalid")
    rebuilt = _build_official_trading_session_evidence(
        session=session,
        observed_at=evidence_observed,
        calendar_document=calendar,
        source_document=source,
    )
    if rebuilt is None or dict(evidence) != rebuilt:
        raise ValueError("official trading session classification is invalid")
    if rebuilt.get("classification") not in _CLASSIFICATIONS or (
        rebuilt.get("reason_code") not in _OFFICIAL_REASON_CODES
    ):
        raise ValueError("official trading session verdict is invalid")
    return rebuilt


def validate_trading_session_evidence(
    evidence: Mapping[str, object],
    *,
    session: date,
    observed_at: datetime,
) -> dict[str, object]:
    """Validate provenance and recompute the claimed session classification."""

    if not isinstance(evidence, Mapping):
        raise ValueError("trading session evidence schema is invalid")
    if evidence.get("schema") == OFFICIAL_TRADING_SESSION_EVIDENCE_SCHEMA:
        return _validate_official_trading_session_evidence(
            evidence,
            session=session,
            observed_at=observed_at,
        )
    if set(evidence) != _QMT_FIELDS:
        raise ValueError("trading session evidence schema is invalid")
    if evidence.get("schema") != TRADING_SESSION_EVIDENCE_SCHEMA:
        raise ValueError("trading session evidence schema is invalid")
    if evidence.get("market") != "SH" or evidence.get("timezone") != "Asia/Shanghai":
        raise ValueError("trading session evidence market is invalid")
    if evidence.get("session") != session.isoformat():
        raise ValueError("trading session evidence target changed")
    stable = {key: evidence[key] for key in evidence if key != "content_sha256"}
    if evidence.get("content_sha256") != sha256_json(stable):
        raise ValueError("trading session evidence content hash is invalid")
    try:
        evidence_observed = datetime.fromisoformat(str(evidence["observed_at"]))
        parsed_sessions = tuple(
            date.fromisoformat(str(value))
            for value in evidence["returned_sessions"]  # type: ignore[union-attr]
        )
        published = (
            None
            if evidence["published_through"] is None
            else date.fromisoformat(str(evidence["published_through"]))
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("trading session evidence values are invalid") from exc
    audit_observed = normalize_datetime(observed_at, "observed_at").astimezone(_CN)
    evidence_observed = normalize_datetime(
        evidence_observed,
        "evidence.observed_at",
    ).astimezone(_CN)
    if evidence_observed > audit_observed:
        raise ValueError("trading session evidence comes from the future")
    query_attempted = evidence.get("query_attempted")
    query_succeeded = evidence.get("query_succeeded")
    if type(query_attempted) is not bool or type(query_succeeded) is not bool:
        raise ValueError("trading session evidence query flags are invalid")
    rebuilt = build_trading_session_evidence(
        session=session,
        observed_at=evidence_observed,
        returned_sessions=parsed_sessions,
        published_through=published,
        query_attempted=query_attempted,
        query_succeeded=query_succeeded,
    )
    if dict(evidence) != rebuilt:
        raise ValueError("trading session evidence classification is invalid")
    if evidence.get("classification") not in _CLASSIFICATIONS or (
        evidence.get("reason_code") not in _QMT_REASON_CODES
    ):
        raise ValueError("trading session evidence verdict is invalid")
    return rebuilt


def resolve_trading_session_requirement(
    evidence: Mapping[str, object] | None,
    *,
    session: date,
    observed_at: datetime,
) -> dict[str, object]:
    """Return a tri-state delivery requirement from audited evidence."""

    if evidence is None:
        return {
            "required": None,
            "requirement_resolved": False,
            "trading_session_status": "UNRESOLVED",
            "trading_session_reason_code": "TRADING_SESSION_EVIDENCE_MISSING",
            "trading_session_evidence_proven": False,
            "trading_session_evidence": None,
        }
    try:
        validated = validate_trading_session_evidence(
            evidence,
            session=session,
            observed_at=observed_at,
        )
    except (TypeError, ValueError):
        return {
            "required": None,
            "requirement_resolved": False,
            "trading_session_status": "UNRESOLVED",
            "trading_session_reason_code": "TRADING_SESSION_EVIDENCE_INVALID",
            "trading_session_evidence_proven": False,
            "trading_session_evidence": None,
        }
    classification = str(validated["classification"])
    required = True if classification == "TRADING_SESSION" else (
        False if classification == "NON_TRADING_SESSION" else None
    )
    return {
        "required": required,
        "requirement_resolved": required is not None,
        "trading_session_status": classification,
        "trading_session_reason_code": str(validated["reason_code"]),
        "trading_session_evidence_proven": required is not None,
        "trading_session_evidence": validated,
    }


__all__ = (
    "DEFAULT_OFFICIAL_TRADING_CALENDAR_PATH",
    "OFFICIAL_CALENDAR_SOURCE_SCHEMA",
    "OFFICIAL_TRADING_SESSION_EVIDENCE_SCHEMA",
    "TRADING_SESSION_EVIDENCE_SCHEMA",
    "authoritative_trading_session_evidence",
    "build_trading_session_evidence",
    "official_trading_session_evidence",
    "resolve_trading_session_requirement",
    "validate_trading_session_evidence",
)
