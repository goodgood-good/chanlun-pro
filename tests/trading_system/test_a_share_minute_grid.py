"""Single-source contract for A-share completed 1m execution evidence."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from chanlun.decision_support.trading_system.a_share_minute_grid import (
    a_share_completed_one_minute_closes,
    a_share_completed_one_minute_prefix_closes,
    a_share_optional_entry_valid_until,
    validate_a_share_complete_session_closes,
    validate_a_share_completed_one_minute_interval,
    validate_a_share_completed_one_minute_prefix_closes,
)
from chanlun.decision_support.trading_system.models import EntryExecutionBoundary


CN = ZoneInfo("Asia/Shanghai")
SESSION = date(2026, 7, 30)


def _at(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 7, 30, hour, minute, second, tzinfo=CN)


def _boundary(closed_at: datetime) -> EntryExecutionBoundary:
    return EntryExecutionBoundary(
        symbol="SH.600000",
        setup_occurrence_id="setup-occurrence:test",
        point_id="sha256:" + "1" * 64,
        source_frequency="1m",
        confirmation_bar_closed_at=closed_at,
        raw_open=Decimal("10.00"),
        raw_high=Decimal("10.05"),
        raw_low=Decimal("9.98"),
        raw_close=Decimal("10.03"),
        raw_volume=Decimal("10000"),
        entry_valid_until=a_share_optional_entry_valid_until(closed_at),
        raw_price_basis_revision="qmt-none-test",
    )


def test_completed_session_is_exactly_240_exchange_aligned_closes() -> None:
    closes = a_share_completed_one_minute_closes(SESSION)

    assert len(closes) == 240
    assert closes[0] == _at(9, 31)
    assert closes[119] == _at(11, 30)
    assert closes[120] == _at(13, 1)
    assert closes[-1] == _at(15, 0)
    validate_a_share_complete_session_closes(closes, session=SESSION)


@pytest.mark.parametrize(
    ("opened_at", "closed_at"),
    (
        (_at(9, 30), _at(9, 31)),
        (_at(11, 29), _at(11, 30)),
        (_at(13, 0), _at(13, 1)),
        (_at(14, 59), _at(15, 0)),
    ),
)
def test_completed_minute_accepts_session_boundaries(
    opened_at: datetime,
    closed_at: datetime,
) -> None:
    validate_a_share_completed_one_minute_interval(opened_at, closed_at)


@pytest.mark.parametrize(
    ("opened_at", "closed_at"),
    (
        (_at(9, 30), _at(9, 30, 30)),
        (_at(9, 30), _at(9, 35)),
        (_at(9, 29), _at(9, 30)),
        (_at(11, 30), _at(11, 31)),
        (_at(12, 59), _at(13, 0)),
        (_at(15, 0), _at(15, 1)),
    ),
)
def test_completed_minute_rejects_partial_aggregate_and_closed_periods(
    opened_at: datetime,
    closed_at: datetime,
) -> None:
    with pytest.raises(ValueError):
        validate_a_share_completed_one_minute_interval(opened_at, closed_at)


def test_complete_session_rejects_gap_reorder_and_wrong_session() -> None:
    closes = a_share_completed_one_minute_closes(SESSION)

    with pytest.raises(ValueError, match="240 completed"):
        validate_a_share_complete_session_closes(closes[:-1], session=SESSION)
    with pytest.raises(ValueError, match="has gaps"):
        validate_a_share_complete_session_closes(
            (*closes[:20], closes[21], closes[20], *closes[22:]),
            session=SESSION,
        )
    with pytest.raises(ValueError, match="has gaps"):
        validate_a_share_complete_session_closes(
            closes,
            session=date(2026, 7, 29),
        )


@pytest.mark.parametrize(
    ("cutoff", "expected_count", "expected_last"),
    (
        (_at(9, 30), 0, None),
        (_at(10, 0, 30), 30, _at(10, 0)),
        (_at(12, 30), 120, _at(11, 30)),
        (_at(15, 30), 240, _at(15, 0)),
    ),
)
def test_causal_completed_minute_prefix_respects_session_boundaries(
    cutoff: datetime,
    expected_count: int,
    expected_last: datetime | None,
) -> None:
    closes = a_share_completed_one_minute_prefix_closes(cutoff)

    assert len(closes) == expected_count
    assert (None if not closes else closes[-1]) == expected_last
    validate_a_share_completed_one_minute_prefix_closes(
        closes,
        not_after=cutoff,
    )


def test_causal_completed_minute_prefix_rejects_stale_or_gapped_evidence() -> None:
    cutoff = _at(10, 0)
    closes = a_share_completed_one_minute_prefix_closes(cutoff)

    with pytest.raises(ValueError, match="prefix grid has gaps"):
        validate_a_share_completed_one_minute_prefix_closes(
            closes[:-1],
            not_after=cutoff,
        )
    with pytest.raises(ValueError, match="prefix grid has gaps"):
        validate_a_share_completed_one_minute_prefix_closes(
            (*closes[:10], *closes[11:]),
            not_after=cutoff,
        )
    with pytest.raises(ValueError, match="prefix grid has gaps"):
        validate_a_share_completed_one_minute_prefix_closes(
            (closes[-1],),
            not_after=cutoff,
        )


@pytest.mark.parametrize(
    ("confirmed", "expected"),
    (
        (_at(9, 31), _at(9, 32)),
        (_at(11, 29), _at(11, 30)),
        (_at(11, 30), _at(11, 30)),
        (_at(13, 1), _at(13, 2)),
        (_at(14, 59), _at(15, 0)),
        (_at(15, 0), _at(15, 0)),
    ),
)
def test_optional_entry_ttl_is_one_nesting_decision_bar_or_auction_end(
    confirmed: datetime,
    expected: datetime,
) -> None:
    assert a_share_optional_entry_valid_until(confirmed) == expected
    assert _boundary(confirmed).entry_valid_until == expected


def test_boundary_rejects_second_level_confirmation_and_flexible_ttl() -> None:
    with pytest.raises(ValueError, match="align to exchange minutes"):
        _boundary(_at(10, 4, 30))

    boundary = _boundary(_at(10, 4))
    with pytest.raises(ValueError, match="frozen A-share nesting-decision TTL"):
        replace(
            boundary,
            entry_valid_until=boundary.confirmation_bar_closed_at
            + timedelta(seconds=30),
        )
    with pytest.raises(ValueError, match="frozen A-share nesting-decision TTL"):
        replace(
            boundary,
            entry_valid_until=boundary.confirmation_bar_closed_at,
        )
