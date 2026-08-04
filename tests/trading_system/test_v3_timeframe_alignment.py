from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

from chanlun.decision_support.trading_system.v3_timeframe_alignment import (
    CompletedL1TrendFact,
    align_independent_entry_chains,
    independent_alignment_contract,
)
from tests.trading_system.helpers import POINT_AT, confirmed_point


def trend(
    trend_id: str,
    direction: str,
    *,
    start_minute: int,
    end_minute: int,
    available_minute: int,
    start_price: str,
    end_price: str,
    low_price: str,
    high_price: str,
) -> CompletedL1TrendFact:
    terminal_end = POINT_AT + timedelta(minutes=end_minute)
    return CompletedL1TrendFact(
        trend_id=trend_id,
        source_frequency="5m",
        recursive_level=0,
        price_basis_revision="test-raw-v1",
        direction=direction,
        market_start=POINT_AT + timedelta(minutes=start_minute),
        market_end=terminal_end,
        confirmed_at=POINT_AT + timedelta(minutes=available_minute),
        available_at=POINT_AT + timedelta(minutes=available_minute),
        start_price=Decimal(start_price),
        end_price=Decimal(end_price),
        low_price=Decimal(low_price),
        high_price=Decimal(high_price),
        terminal_start=terminal_end - timedelta(minutes=5),
        terminal_end=terminal_end,
        evidence_unit_ids=(f"{trend_id}-a", f"{trend_id}-b"),
    )


def setup():
    l0 = confirmed_point(
        "3buy",
        frequency="30m",
        center_zd=9.0,
        center_zg=9.8,
        available_minutes_after=120,
    )
    departure = trend(
        "up-departure",
        "up",
        start_minute=5,
        end_minute=30,
        available_minute=35,
        start_price="9.5",
        end_price="10.2",
        low_price="9.4",
        high_price="10.3",
    )
    first_return = trend(
        "first-return",
        "down",
        start_minute=30,
        end_minute=60,
        available_minute=65,
        start_price="10.2",
        end_price="9.9",
        low_price="9.8",
        high_price="10.2",
    )
    locator = confirmed_point(
        "1buy",
        frequency="1m",
        minutes_after=58,
        available_minutes_after=7,
    )
    return l0, departure, first_return, locator


def test_alignment_contract_is_hashed_and_forbids_stale_reuse() -> None:
    contract = independent_alignment_contract()

    assert contract.parameter_set_id.startswith("sha256:")
    assert contract.window_boundaries == "INCLUSIVE"
    assert contract.stale_point_reuse_allowed is False


def test_equal_return_boundary_and_terminal_locator_pass() -> None:
    l0, departure, first_return, locator = setup()

    decisions = align_independent_entry_chains(
        l0_points=(l0,),
        l1_trends=(departure, first_return),
        l2_points=(locator,),
    )

    assert decisions[0].status == "PASS"
    assert decisions[0].chain is not None
    assert decisions[0].chain.return_low == Decimal("9.8")
    assert decisions[0].chain.l2_locator_point_id == locator.point_id


def test_first_return_below_zg_rejects_without_using_later_return() -> None:
    l0, departure, first_return, locator = setup()
    failed_first = replace(
        first_return,
        trend_id="failed-first-return",
        end_price=Decimal("9.7"),
        low_price=Decimal("9.7"),
    )
    later_valid = replace(
        first_return,
        trend_id="later-valid-return",
        market_start=POINT_AT + timedelta(minutes=70),
        market_end=POINT_AT + timedelta(minutes=90),
        terminal_start=POINT_AT + timedelta(minutes=85),
        terminal_end=POINT_AT + timedelta(minutes=90),
        confirmed_at=POINT_AT + timedelta(minutes=95),
        available_at=POINT_AT + timedelta(minutes=95),
        evidence_unit_ids=("later-a", "later-b"),
    )

    decision = align_independent_entry_chains(
        l0_points=(l0,),
        l1_trends=(departure, failed_first, later_valid),
        l2_points=(locator,),
    )[0]

    assert decision.status == "REJECT"
    assert decision.reason_codes == ("FIRST_L1_RETURN_LOW_BELOW_L0_ZG",)


def test_old_locator_and_unevidenced_second_buy_are_rejected() -> None:
    l0, departure, first_return, locator = setup()
    old = replace(
        locator,
        anchor_at=POINT_AT - timedelta(minutes=1),
        confirmed_at=POINT_AT - timedelta(minutes=1),
        available_at=POINT_AT - timedelta(minutes=1),
    )
    second = confirmed_point(
        "2buy",
        frequency="1m",
        minutes_after=58,
        available_minutes_after=7,
    )

    rejected = align_independent_entry_chains(
        l0_points=(l0,),
        l1_trends=(departure, first_return),
        l2_points=(old, second),
    )[0]
    accepted = align_independent_entry_chains(
        l0_points=(l0,),
        l1_trends=(departure, first_return),
        l2_points=(old, second),
        allowed_l2_second_buy_ids=(second.point_id,),
    )[0]

    assert rejected.reason_codes == ("NO_L2_LOCATOR_AT_FIRST_L1_RETURN_TERMINAL",)
    assert accepted.status == "PASS"


def test_appending_future_facts_cannot_change_historical_alignment() -> None:
    l0, departure, first_return, locator = setup()
    prefix = align_independent_entry_chains(
        l0_points=(l0,),
        l1_trends=(departure, first_return),
        l2_points=(locator,),
    )
    future_trend = replace(
        first_return,
        trend_id="future-return",
        market_start=POINT_AT + timedelta(minutes=180),
        market_end=POINT_AT + timedelta(minutes=210),
        terminal_start=POINT_AT + timedelta(minutes=205),
        terminal_end=POINT_AT + timedelta(minutes=210),
        confirmed_at=POINT_AT + timedelta(minutes=215),
        available_at=POINT_AT + timedelta(minutes=215),
        evidence_unit_ids=("future-a", "future-b"),
    )
    future_point = confirmed_point(
        "1buy",
        frequency="1m",
        minutes_after=210,
        available_minutes_after=5,
        center_id="future-center",
    )

    full = align_independent_entry_chains(
        l0_points=(l0,),
        l1_trends=(departure, first_return, future_trend),
        l2_points=(locator, future_point),
    )

    assert full == prefix
