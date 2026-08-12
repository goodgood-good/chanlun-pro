from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from chanlun.decision_support.trading_system.selection import (
    CompletedDailyClose,
    HigherTimeframeRiskSnapshot,
    SectorMemberHistory,
    advance_top_risk_state,
    build_sector_strength_snapshot,
    completed_ma5_at,
    completed_sma,
    member_ma_strength_category,
    selection_research_by_symbol,
    selection_research_ledger_document,
    selection_research_ledger_from_document,
    visible_selection_research,
)
from tests.trading_system.helpers import valid_selection_research


CN = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 7, 24, 14, 31, tzinfo=CN)


def test_formal_research_ledger_round_trip_and_point_in_time_selection() -> None:
    first = valid_selection_research()
    later = replace(
        first,
        snapshot_id="research:SZ.000001:later",
        effective_at=first.effective_at + timedelta(hours=1),
        known_at=first.known_at + timedelta(hours=1),
    )
    snapshots = (first, later)
    document = selection_research_ledger_document(snapshots)

    restored = selection_research_ledger_from_document(document)

    assert restored == snapshots
    assert selection_research_by_symbol(restored) == {first.symbol: snapshots}
    assert (
        visible_selection_research(
            restored,
            symbol=first.symbol,
            selection_path=first.path,
            decision_time=later.effective_at,
        )
        == later
    )


def test_formal_research_ledger_rejects_duplicate_or_unsorted_snapshots() -> None:
    first = valid_selection_research()
    other = replace(first, snapshot_id="research:AA.000001:earlier")

    with pytest.raises(ValueError, match="身份必须唯一"):
        selection_research_ledger_document((first, first))
    with pytest.raises(ValueError, match="按标的与时间排序"):
        selection_research_ledger_document((first, other))


def test_sector_strength_retains_missing_member_as_unresolved() -> None:
    member = SectorMemberHistory(
        symbol="SH.600000",
        listed_on=date(2000, 1, 1),
        history_status="UNEXPLAINED_GAP",
        closes=(),
    )
    snapshot = build_sector_strength_snapshot(
        snapshot_id="sector:gap",
        sector_id="SW1:bank",
        anchor_session=date(2026, 6, 1),
        decision_time=NOW,
        members=(member,),
        rank=1,
    )
    assert snapshot.resolved is False
    assert snapshot.member_count == 1
    assert snapshot.strength is None


def test_ma_equal_to_close_is_not_counted_as_standing_above() -> None:
    closes = tuple(
        CompletedDailyClose(
            session=date(2026, 6, 1) + timedelta(days=index),
            close=Decimal("10"),
            known_at=NOW - timedelta(days=20 - index),
        )
        for index in range(10)
    )
    member = SectorMemberHistory(
        symbol="SH.600000",
        listed_on=date(2000, 1, 1),
        history_status="COMPLETE",
        closes=closes,
    )
    assert member_ma_strength_category(
        member,
        anchor_session=date(2026, 6, 1),
        decision_time=NOW,
    ) == 1
    assert completed_sma((Decimal("1"),) * 4, 5) is None


def test_top_fractal_risk_state_machine_matches_frozen_transitions() -> None:
    formed = advance_top_risk_state("NONE", "TOP_FRACTAL_MAPPING_UNIQUE")
    red = advance_top_risk_state(formed.current, "CENTER_THIRD_SELL_UNEXTENDED")
    cleared = advance_top_risk_state(
        red.current, "OPPOSITE_FRACTAL_COMPLETES_DOWN_PEN"
    )
    assert (formed.current, red.current, cleared.current) == (
        "FORMED",
        "PEN_RISK_CONFIRMED",
        "NONE",
    )
    with pytest.raises(ValueError, match="unresolved top-risk transition"):
        advance_top_risk_state("NONE", "CENTER_THIRD_BUY")


def test_ma5_uses_only_five_completed_point_in_time_visible_closes() -> None:
    rows = tuple(
        CompletedDailyClose(
            session=date(2026, 7, 14) + timedelta(days=index),
            close=Decimal(index + 1),
            known_at=NOW - timedelta(days=5 - index),
            completed=True,
        )
        for index in range(5)
    ) + (
        CompletedDailyClose(
            session=NOW.date(),
            close=Decimal("100"),
            known_at=NOW + timedelta(minutes=1),
            completed=True,
        ),
    )
    assert completed_ma5_at(rows, decision_time=NOW) == Decimal("3")


def test_known_nonunique_top_mapping_is_explicit_amber_not_unknown() -> None:
    risk = HigherTimeframeRiskSnapshot(
        snapshot_id="risk:known-nonunique",
        observed_at=NOW,
        monthly="NONE",
        weekly="FORMED_UNRESOLVED",
        daily="NONE",
        monthly_ma5=Decimal("10"),
        weekly_ma5=Decimal("10"),
        daily_ma5=Decimal("10"),
        mapping_unique=False,
    )
    assert risk.gate == "AMBER"


def test_confirmed_red_risk_has_priority_over_amber_at_any_lower_period() -> None:
    risk = HigherTimeframeRiskSnapshot(
        snapshot_id="risk:red-over-amber",
        observed_at=NOW,
        monthly="FORMED",
        weekly="NONE",
        daily="PEN_RISK_CONFIRMED",
        monthly_ma5=Decimal("10"),
        weekly_ma5=Decimal("10"),
        daily_ma5=Decimal("10"),
        mapping_unique=True,
    )
    assert risk.gate == "RED"
