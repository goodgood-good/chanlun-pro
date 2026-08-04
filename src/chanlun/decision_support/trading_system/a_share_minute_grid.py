"""One authoritative A-share completed-1m session and entry-TTL contract."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo

from chanlun.decision_support.fingerprints import normalize_datetime


_CN = ZoneInfo("Asia/Shanghai")


def a_share_completed_one_minute_closes(
    session: date,
    *,
    timezone: tzinfo | None = _CN,
) -> tuple[datetime, ...]:
    """Return the 240 QMT end labels for one A-share continuous session."""

    morning = datetime.combine(session, time(9, 31), tzinfo=timezone)
    afternoon = datetime.combine(session, time(13, 1), tzinfo=timezone)
    return (
        *tuple(morning + timedelta(minutes=index) for index in range(120)),
        *tuple(afternoon + timedelta(minutes=index) for index in range(120)),
    )


def a_share_completed_one_minute_prefix_closes(
    not_after: datetime,
) -> tuple[datetime, ...]:
    """Return the exact completed continuous-auction prefix at a cutoff.

    This is the causal reference grid for an intraday screening observation:
    lunch, the opening auction and seconds inside a minute never manufacture a
    bar, while an after-close cutoff resolves to the full 240-bar session.
    """

    cutoff = normalize_datetime(not_after, "not_after")
    return tuple(
        value
        for value in a_share_completed_one_minute_closes(cutoff.date())
        if value <= cutoff
    )


def validate_a_share_completed_one_minute_interval(
    opened_at: datetime,
    closed_at: datetime,
) -> None:
    """Require one exact exchange-aligned completed continuous-auction minute."""

    opened = normalize_datetime(opened_at, "opened_at")
    closed = normalize_datetime(closed_at, "closed_at")
    if closed - opened != timedelta(minutes=1):
        raise ValueError("paper execution evidence must span exactly one minute")
    if (
        opened.second
        or opened.microsecond
        or closed.second
        or closed.microsecond
        or opened.date() != closed.date()
    ):
        raise ValueError("paper execution minute must align to exchange minutes")
    clock = closed.timetz().replace(tzinfo=None)
    if not (
        time(9, 31) <= clock <= time(11, 30)
        or time(13, 1) <= clock <= time(15, 0)
    ):
        raise ValueError(
            "paper execution minute is outside A-share continuous auction"
        )


def validate_a_share_complete_session_closes(
    closes: Sequence[datetime],
    *,
    session: date,
) -> None:
    """Require the chronological, gap-free 240-close grid for one session."""

    normalized = tuple(
        normalize_datetime(value, "closed_at") for value in closes
    )
    expected = a_share_completed_one_minute_closes(session)
    if len(normalized) != len(expected):
        raise ValueError("execution evidence does not contain 240 completed 1m bars")
    if normalized != expected:
        raise ValueError("execution evidence completed 1m session grid has gaps")


def validate_a_share_completed_one_minute_prefix_closes(
    closes: Sequence[datetime],
    *,
    not_after: datetime,
) -> None:
    """Require every and only the completed 1m closes knowable at a cutoff."""

    normalized = tuple(
        normalize_datetime(value, "closed_at") for value in closes
    )
    expected = a_share_completed_one_minute_prefix_closes(not_after)
    if normalized != expected:
        raise ValueError(
            "causal reference evidence completed 1m prefix grid has gaps"
        )


def a_share_optional_entry_valid_until(
    confirmation_bar_closed_at: datetime,
) -> datetime:
    """Return the exact one-locator-bar TTL without crossing lunch or close."""

    closed_at = normalize_datetime(
        confirmation_bar_closed_at,
        "confirmation_bar_closed_at",
    )
    validate_a_share_completed_one_minute_interval(
        closed_at - timedelta(minutes=1),
        closed_at,
    )
    clock = closed_at.timetz().replace(tzinfo=None)
    auction_end = closed_at.replace(
        hour=11 if clock <= time(11, 30) else 15,
        minute=30 if clock <= time(11, 30) else 0,
        second=0,
        microsecond=0,
    )
    return min(closed_at + timedelta(minutes=1), auction_end)


__all__ = (
    "a_share_completed_one_minute_closes",
    "a_share_completed_one_minute_prefix_closes",
    "a_share_optional_entry_valid_until",
    "validate_a_share_complete_session_closes",
    "validate_a_share_completed_one_minute_interval",
    "validate_a_share_completed_one_minute_prefix_closes",
)
