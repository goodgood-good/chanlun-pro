from chanlun.decision_support.trading_system.conflicts import resolve_conflict
from tests.trading_system.helpers import confirmed_point, setup_for


def test_lower_level_sell_does_not_cancel_higher_level_buy_setup() -> None:
    decision = resolve_conflict(
        setup_for(confirmed_point("2buy", tower="formal", level=1)),
        (confirmed_point("1sell", tower="formal", level=0),),
    )

    assert decision.hard_block is False
    assert decision.risk_only_point_ids


def test_same_formal_level_and_center_sell_cancels_setup() -> None:
    buy = confirmed_point("2buy", tower="formal", level=1, center_id="same")
    sell = confirmed_point("1sell", tower="formal", level=1, center_id="same")
    decision = resolve_conflict(setup_for(buy), (sell,))

    assert decision.hard_block is True
    assert decision.blocking_point_ids == (sell.point_id,)


def test_unrelated_center_without_parent_binding_is_not_global_veto() -> None:
    buy = confirmed_point("2buy", tower="formal", level=1, center_id="buy-center")
    sell = confirmed_point("1sell", tower="formal", level=1, center_id="sell-center")

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
    buy = confirmed_point("2buy", frequency="5m", level=1)
    sell = confirmed_point("1sell", frequency="5m", level=0)

    decision = resolve_conflict(
        setup_for(buy),
        (sell,),
        physical_timeframes=True,
    )

    assert decision.hard_block is False
    assert decision.risk_only_point_ids == (sell.point_id,)


def test_older_opposite_point_cannot_veto_later_setup() -> None:
    sell = confirmed_point(
        "1sell",
        tower="formal",
        level=1,
        center_id="same",
        minutes_after=-5,
    )
    buy = confirmed_point(
        "2buy",
        tower="formal",
        level=1,
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
        level=1,
        center_id="same",
    )
    sell = confirmed_point(
        "1sell",
        tower="formal",
        level=1,
        center_id="same",
        minutes_after=5,
    )

    decision = resolve_conflict(setup_for(buy), (sell,))

    assert decision.hard_block is True
    assert decision.blocking_point_ids == (sell.point_id,)
