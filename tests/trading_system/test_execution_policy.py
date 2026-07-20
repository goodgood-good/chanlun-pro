from decimal import Decimal

import pytest

from chanlun.decision_support.trading_system.execution_policy import (
    TradingPolicy,
    evaluate_entry_policy,
    evaluate_exit_policy,
)
from chanlun.decision_support.trading_system.lifecycle import (
    advance_lifecycle,
    build_setup,
)
from chanlun.decision_support.trading_system.models import ConflictDecision
from tests.trading_system.helpers import (
    AS_OF,
    confirmed_point,
    eligible_sector,
    hostile_context,
    hostile_sector,
    neutral_context,
    provisional_point,
    supportive_context,
)


def valid_entry_inputs(
    point_type: str,
    **point_overrides: object,
):
    five_point = confirmed_point(
        point_type,
        frequency="5m",
        **point_overrides,
    )
    setup = build_setup(five_point, neutral_context("30m"), eligible_sector())
    trigger = confirmed_point(
        "1buy",
        frequency="1m",
        anchor=setup.price_low,
        minutes_after=1,
    )
    lifecycle = advance_lifecycle(None, setup, trigger, as_of=AS_OF)
    return lifecycle, setup, trigger, ConflictDecision(False, (), (), ())


@pytest.mark.parametrize(
    ("point_type", "expected_multiplier"),
    (
        ("1buy", Decimal("0.50")),
        ("2buy", Decimal("1.00")),
        ("3buy", Decimal("0.75")),
    ),
)
def test_each_buy_class_has_an_independent_risk_lane(
    point_type: str,
    expected_multiplier: Decimal,
) -> None:
    lifecycle, setup, trigger, conflict = valid_entry_inputs(point_type)
    decision = evaluate_entry_policy(
        lifecycle,
        setup,
        trigger,
        conflict,
        TradingPolicy(),
    )

    assert decision.allowed is True
    assert decision.risk_multiplier == expected_multiplier


def test_three_buy_requires_first_center_and_one_tick_clearance() -> None:
    touching = valid_entry_inputs(
        "3buy",
        variant="boundary_touch",
        center_ordinal=1,
    )
    later = valid_entry_inputs("3buy", center_ordinal=2)

    assert evaluate_entry_policy(*touching, policy=TradingPolicy()).allowed is False
    assert evaluate_entry_policy(*later, policy=TradingPolicy()).allowed is False


def test_forming_five_minute_is_never_executable() -> None:
    setup = build_setup(
        provisional_point("2buy"),
        neutral_context("30m"),
        eligible_sector(),
    )
    lifecycle = advance_lifecycle(None, setup, None, as_of=AS_OF)

    decision = evaluate_entry_policy(
        lifecycle,
        setup,
        None,
        ConflictDecision(False, (), (), ()),
        TradingPolicy(),
    )

    assert decision.allowed is False
    assert "five_minute_not_confirmed" in decision.reason_codes


def test_sell_exit_is_not_blocked_by_sector_state() -> None:
    five_sell = confirmed_point("2sell", frequency="5m", tower="xd", level=1)
    setup = build_setup(five_sell, supportive_context("30m"), hostile_sector())
    one_sell = confirmed_point("1sell", frequency="1m", minutes_after=1)
    lifecycle = advance_lifecycle(None, setup, one_sell, as_of=AS_OF)

    decision = evaluate_exit_policy(
        lifecycle,
        setup,
        one_sell,
        held_tower="xd",
        held_level=1,
    )

    assert decision.allowed is True
    assert decision.action == "exit_full"


def test_sell_exit_requires_confirmed_one_minute_trigger() -> None:
    five_sell = confirmed_point("2sell", frequency="5m", tower="xd", level=1)
    setup = build_setup(five_sell, supportive_context("30m"), hostile_sector())
    armed = advance_lifecycle(None, setup, None, as_of=AS_OF)
    rejected = evaluate_exit_policy(
        armed,
        setup,
        None,
        held_tower="xd",
        held_level=1,
    )
    one_sell = confirmed_point("1sell", frequency="1m", minutes_after=1)
    triggered = advance_lifecycle(None, setup, one_sell, as_of=AS_OF)
    accepted = evaluate_exit_policy(
        triggered,
        setup,
        one_sell,
        held_tower="xd",
        held_level=1,
    )

    assert rejected.allowed is False
    assert accepted.action == "exit_full"


def test_ablation_policy_can_disable_entry_layers_without_changing_defaults() -> None:
    five_buy = confirmed_point("3buy", frequency="5m", center_ordinal=2)
    setup = build_setup(five_buy, hostile_context("30m"), hostile_sector())
    observed = advance_lifecycle(None, setup, None, as_of=AS_OF)

    default = evaluate_entry_policy(
        observed,
        setup,
        None,
        ConflictDecision(False, (), (), ()),
        TradingPolicy(),
    )
    ablated = evaluate_entry_policy(
        observed,
        setup,
        None,
        ConflictDecision(False, (), (), ()),
        TradingPolicy(
            require_confirmed_one_minute=False,
            require_sector_eligibility=False,
            require_thirty_minute_context=False,
            first_center_three_buy_only=False,
        ),
    )

    assert default.allowed is False
    assert ablated.allowed is True


def test_one_minute_exit_filter_can_be_disabled_only_for_ablation() -> None:
    five_sell = confirmed_point("2sell", frequency="5m", tower="xd", level=1)
    setup = build_setup(five_sell, supportive_context("30m"), hostile_sector())
    armed = advance_lifecycle(None, setup, None, as_of=AS_OF)

    decision = evaluate_exit_policy(
        armed,
        setup,
        None,
        held_tower="xd",
        held_level=1,
        policy=TradingPolicy(require_confirmed_one_minute=False),
    )

    assert decision.allowed is True
    assert decision.action == "exit_full"
