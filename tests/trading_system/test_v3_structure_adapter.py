from datetime import timedelta
from decimal import Decimal

import pytest

from chanlun.decision_support.trading_system.v3_structure_adapter import (
    build_v3_independent_technical_entry_snapshot,
    build_v3_technical_entry_snapshot,
)
from chanlun.decision_support.trading_system.v3_timeframe_alignment import (
    AlignedEntryChain,
    CompletedL1TrendFact,
)
from tests.trading_system.helpers import POINT_AT, confirmed_point


def test_v3_adapter_only_copies_confirmed_frozen_points() -> None:
    l0 = confirmed_point(
        "3buy",
        frequency="30m",
        anchor=10.0,
        center_zg=10.0,
        center_ordinal=1,
    )
    l2 = confirmed_point(
        "1buy",
        frequency="1m",
        anchor=10.2,
        center_id="l2-center",
        minutes_after=1,
    )
    snapshot = build_v3_technical_entry_snapshot(
        structure_snapshot_id="strict-evidence:v1",
        observed_at=POINT_AT + timedelta(minutes=2),
        l0_three_buy=l0,
        l2_locator=l2,
        l1_departure_completed=True,
        l1_first_return_completed=True,
        first_return_low=Decimal("10.0"),
        direct_recursive_levels_unique=True,
        all_components_completed=True,
    )
    assert snapshot.pen_definition_mode == "ORIGINAL_OLD_PEN"
    assert snapshot.l0_point_id == l0.point_id
    assert snapshot.l2_point_id == l2.point_id
    assert snapshot.first_return_low == snapshot.l0_zg


def test_v3_adapter_rejects_wrong_frequency_or_future_point() -> None:
    l0 = confirmed_point("3buy", frequency="5m")
    l2 = confirmed_point("1buy", frequency="1m")
    with pytest.raises(ValueError, match="30m"):
        build_v3_technical_entry_snapshot(
            structure_snapshot_id="strict-evidence:v1",
            observed_at=POINT_AT + timedelta(minutes=2),
            l0_three_buy=l0,
            l2_locator=l2,
            l1_departure_completed=True,
            l1_first_return_completed=True,
            first_return_low=Decimal("10"),
            direct_recursive_levels_unique=True,
            all_components_completed=True,
        )


def test_independent_adapter_copies_only_the_certified_chain() -> None:
    l0 = confirmed_point(
        "3buy",
        frequency="30m",
        center_zd=9.0,
        center_zg=9.8,
        available_minutes_after=20,
    )
    l2 = confirmed_point(
        "1buy",
        frequency="1m",
        minutes_after=9,
        available_minutes_after=2,
    )

    def trend(trend_id, direction, start, end, available, start_price, end_price, low):
        return CompletedL1TrendFact(
            trend_id=trend_id,
            source_frequency="5m",
            recursive_level=0,
            price_basis_revision="test-raw-v1",
            direction=direction,
            market_start=POINT_AT + timedelta(minutes=start),
            market_end=POINT_AT + timedelta(minutes=end),
            confirmed_at=POINT_AT + timedelta(minutes=available),
            available_at=POINT_AT + timedelta(minutes=available),
            start_price=Decimal(start_price),
            end_price=Decimal(end_price),
            low_price=Decimal(low),
            high_price=Decimal("10.2"),
            terminal_start=POINT_AT + timedelta(minutes=end - 1),
            terminal_end=POINT_AT + timedelta(minutes=end),
            evidence_unit_ids=(f"{trend_id}-a", f"{trend_id}-b"),
        )

    departure = trend("up", "up", 0, 5, 6, "9.5", "10.2", "9.4")
    first_return = trend("down", "down", 5, 10, 11, "10.2", "9.9", "9.8")
    chain = AlignedEntryChain(
        l0_point_id=l0.point_id,
        l1_departure_trend_id=departure.trend_id,
        l1_return_trend_id=first_return.trend_id,
        l2_locator_point_id=l2.point_id,
        decision_at=POINT_AT + timedelta(minutes=20),
        return_low=Decimal("9.8"),
        l0_zg=Decimal("9.8"),
    )

    snapshot = build_v3_independent_technical_entry_snapshot(
        structure_snapshot_id="independent-chain:v1",
        observed_at=POINT_AT + timedelta(minutes=20),
        chain=chain,
        l0_three_buy=l0,
        l1_departure=departure,
        l1_first_return=first_return,
        l2_locator=l2,
    )

    assert snapshot.level_relation_mode == "USER_OVERRIDE_INDEPENDENT_TIMEFRAMES"
    assert snapshot.direct_recursive_levels_unique is False
    assert snapshot.first_return_low == snapshot.l0_zg
