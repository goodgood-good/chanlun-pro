"""Verified local A-share trading calendar used for QMT history coverage."""

from __future__ import annotations

from datetime import date, datetime, timedelta
import json
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo

from chanlun.persistence.fingerprints import normalize_datetime, sha256_json


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
    "SSE-ANNOUNCEMENT-2023-47": (
        "sha256:fbc33262e717ecaa005b7665a29ea1918924a4828e8d5b14be1a75019d078c8e"
    ),
    "SSE-ANNOUNCEMENT-2024-38": (
        "sha256:2ff9de672a37cc3ec9a3bc0edf9f2ac064dd064a0f9a34b647e80c7125f3db30"
    ),
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
    calendar_path: str | Path | None = None,
) -> dict[str, object] | None:
    """Return pinned SSE evidence when the target is causally covered.

    ``None`` means the annual artifact was either not published at the
    observation time or does not cover the target year; callers may then use a
    provider history download.  A missing or corrupt configured artifact
    raises instead of silently downgrading to an unrelated source.
    """

    if isinstance(session, datetime) or not isinstance(session, date):
        raise TypeError("session must be a date")
    if calendar_path is None:
        path = DEFAULT_OFFICIAL_TRADING_CALENDAR_PATH.with_name(f"a_share_trading_calendar_{session.year}.json")
        if not path.is_file():
            return None
    else:
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


__all__ = (
    "DEFAULT_OFFICIAL_TRADING_CALENDAR_PATH",
    "OFFICIAL_CALENDAR_SOURCE_SCHEMA",
    "OFFICIAL_TRADING_SESSION_EVIDENCE_SCHEMA",
    "official_trading_session_evidence",
)
