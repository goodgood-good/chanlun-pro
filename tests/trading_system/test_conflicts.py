from dataclasses import replace

import pytest

from chanlun.decision_support.trading_system.conflicts import resolve_conflict
from tests.trading_system.helpers import confirmed_point, provisional_point, setup_for


def test_geometric_candidate_conflict_reason_preserves_pending_confirmation() -> None:
    candidate = replace(
        provisional_point("3sell"),
        evidence_codes=(
            "unfinished_segment_participates",
            "provisional_center_completion",
            "core_boundary_held",
        ),
    )

    decision = resolve_conflict(setup_for(candidate), ())

    assert decision.reason_codes == (
        "five_minute_geometry_candidate_awaiting_confirmation",
    )


def test_recursive_five_minute_point_cannot_become_trade_setup() -> None:
    with pytest.raises(ValueError, match="physical 5m level-0"):
        setup_for(confirmed_point("2buy", tower="formal", level=1))


def test_same_formal_level_and_center_sell_cancels_setup() -> None:
    buy = confirmed_point("2buy", tower="formal", level=0, center_id="same")
    sell = confirmed_point("1sell", tower="formal", level=0, center_id="same")
    decision = resolve_conflict(setup_for(buy), (sell,))

    assert decision.hard_block is True
    assert decision.blocking_point_ids == (sell.point_id,)


def test_unrelated_center_without_parent_binding_is_not_global_veto() -> None:
    buy = confirmed_point("2buy", tower="formal", level=0, center_id="buy-center")
    sell = confirmed_point("1sell", tower="formal", level=0, center_id="sell-center")

    decision = resolve_conflict(setup_for(buy), (sell,))

    assert decision.hard_block is False
    assert decision.risk_only_point_ids == (sell.point_id,)


def test_physical_frequency_higher_recursive_sell_blocks_lower_setup() -> None:
    buy = confirmed_point("2buy", frequency="5m", level=0)
    sell = confirmed_point("1sell", frequency="5m", level=1)

    decision = resolve_conflict(
        setup_for(buy),
        (sell,),
        physical_timeframes=True,
    )

    assert decision.hard_block is True
    assert decision.blocking_point_ids == (sell.point_id,)


def test_physical_frequency_lower_recursive_sell_is_risk_only() -> None:
    buy = confirmed_point("2buy", frequency="5m", level=0)
    sell = confirmed_point("1sell", frequency="1m", level=0)

    decision = resolve_conflict(
        setup_for(buy),
        (sell,),
        physical_timeframes=True,
    )

    assert decision.hard_block is False
    assert decision.risk_only_point_ids == (sell.point_id,)


def test_recursive_level_does_not_override_lower_physical_source() -> None:
    buy = confirmed_point("2buy", frequency="5m", level=0)
    daily_sell_from_one_minute = confirmed_point(
        "1sell",
        frequency="1m",
        level=3,
    )

    decision = resolve_conflict(
        setup_for(buy),
        (daily_sell_from_one_minute,),
        physical_timeframes=True,
    )

    assert decision.hard_block is False
    assert decision.risk_only_point_ids == (daily_sell_from_one_minute.point_id,)


def test_higher_recursive_context_cannot_be_promoted_to_trade_setup() -> None:
    with pytest.raises(ValueError, match="physical 5m level-0"):
        setup_for(confirmed_point("2buy", frequency="5m", level=2))


def test_recursive_one_minute_context_cannot_impersonate_physical_five_minute() -> None:
    buy = confirmed_point("2buy", frequency="5m", level=0)
    reversal = confirmed_point("1sell", frequency="1m", level=1)
    continuation = confirmed_point(
        "3sell",
        frequency="1m",
        level=1,
        center_id="continuation",
        center_zd=10.1,
        center_zg=10.2,
    )

    reversal_decision = resolve_conflict(
        setup_for(buy),
        (reversal,),
        physical_timeframes=True,
    )
    continuation_decision = resolve_conflict(
        setup_for(buy),
        (continuation,),
        physical_timeframes=True,
    )

    assert reversal_decision.hard_block is False
    assert reversal_decision.risk_only_point_ids == (reversal.point_id,)
    assert continuation_decision.hard_block is False
    assert continuation_decision.risk_only_point_ids == (continuation.point_id,)


def test_older_opposite_point_cannot_veto_later_setup() -> None:
    sell = confirmed_point(
        "1sell",
        tower="formal",
        level=0,
        center_id="same",
        minutes_after=-5,
    )
    buy = confirmed_point(
        "2buy",
        tower="formal",
        level=0,
        center_id="same",
    )

    decision = resolve_conflict(setup_for(buy), (sell,))

    assert decision.hard_block is False
    assert decision.blocking_point_ids == ()
    assert decision.risk_only_point_ids == ()


def test_later_opposite_point_can_veto_earlier_setup() -> None:
    buy = confirmed_point(
        "2buy",
        tower="formal",
        level=0,
        center_id="same",
    )
    sell = confirmed_point(
        "1sell",
        tower="formal",
        level=0,
        center_id="same",
        minutes_after=5,
    )

    decision = resolve_conflict(setup_for(buy), (sell,))

    assert decision.hard_block is True
    assert decision.blocking_point_ids == (sell.point_id,)
