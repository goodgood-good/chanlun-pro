from chanlun.decision_support.trading_system.conflicts import resolve_conflict
from tests.trading_system.helpers import confirmed_point, setup_for


def test_lower_level_sell_does_not_cancel_higher_level_buy_setup() -> None:
    decision = resolve_conflict(
        setup_for(confirmed_point("2buy", tower="xd", level=1)),
        (confirmed_point("1sell", tower="xd", level=0),),
    )

    assert decision.hard_block is False
    assert decision.risk_only_point_ids


def test_same_tower_and_level_sell_cancels_setup() -> None:
    buy = confirmed_point("2buy", tower="xd", level=1, center_id="same")
    sell = confirmed_point("1sell", tower="xd", level=1, center_id="same")
    decision = resolve_conflict(setup_for(buy), (sell,))

    assert decision.hard_block is True
    assert decision.blocking_point_ids == (sell.point_id,)


def test_different_tower_without_parent_binding_is_not_global_veto() -> None:
    buy = confirmed_point("2buy", tower="xd", level=1, center_id="same")
    sell = confirmed_point("1sell", tower="bi", level=2, center_id="same")

    decision = resolve_conflict(setup_for(buy), (sell,))

    assert decision.hard_block is False
    assert decision.risk_only_point_ids == (sell.point_id,)
