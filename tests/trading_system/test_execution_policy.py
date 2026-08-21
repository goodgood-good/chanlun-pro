from dataclasses import replace
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


@pytest.mark.parametrize(
    "field_name",
    (
        "minimum_tick",
        "first_buy_risk_multiplier",
        "second_buy_risk_multiplier",
        "third_buy_risk_multiplier",
    ),
)
def test_trading_policy_rejects_non_finite_risk_values(field_name: str) -> None:
    with pytest.raises(ValueError, match="must be finite"):
        TradingPolicy(**{field_name: Decimal("NaN")})


def test_trading_policy_requires_typed_contract_values() -> None:
    with pytest.raises(TypeError, match="must be Decimals"):
        TradingPolicy(minimum_tick=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="flags must be booleans"):
        TradingPolicy(require_confirmed_five_minute=1)  # type: ignore[arg-type]


def test_three_buy_requires_one_tick_clearance_and_first_center() -> None:
    touching = valid_entry_inputs(
        "3buy",
        anchor=9.8,
        variant="boundary_touch",
        center_ordinal=1,
    )
    later = valid_entry_inputs("3buy", center_ordinal=2)

    assert evaluate_entry_policy(*touching, policy=TradingPolicy()).allowed is False
    later_decision = evaluate_entry_policy(*later, policy=TradingPolicy())
    assert later_decision.allowed is False
    assert "three_buy_not_first_center" in later_decision.reason_codes


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


def test_geometric_five_minute_candidate_reports_pending_confirmation() -> None:
    formed_buy = replace(
        provisional_point("3buy"),
        evidence_codes=(
            "unfinished_segment_participates",
            "provisional_center_completion",
            "core_boundary_held",
        ),
    )
    buy_setup = build_setup(
        formed_buy,
        neutral_context("30m"),
        eligible_sector(),
    )
    buy_lifecycle = advance_lifecycle(None, buy_setup, None, as_of=AS_OF)

    buy = evaluate_entry_policy(
        buy_lifecycle,
        buy_setup,
        None,
        ConflictDecision(False, (), (), ()),
        TradingPolicy(),
    )

    formed_sell = replace(
        provisional_point("3sell"),
        evidence_codes=formed_buy.evidence_codes,
    )
    sell_setup = build_setup(
        formed_sell,
        neutral_context("30m"),
        eligible_sector(),
    )
    sell_lifecycle = advance_lifecycle(None, sell_setup, None, as_of=AS_OF)
    sell = evaluate_exit_policy(
        sell_lifecycle,
        sell_setup,
        None,
        held_tower="formal",
        held_level=0,
    )

    assert buy_lifecycle.stage == "formed"
    assert buy.allowed is False
    assert buy.reason_codes == (
        "five_minute_geometry_candidate_awaiting_confirmation",
        "lifecycle_not_actionable",
    )
    assert sell_lifecycle.stage == "formed"
    assert sell.allowed is False
    assert sell.reason_codes == (
        "five_minute_geometry_candidate_awaiting_confirmation",
    )


def test_sell_exit_is_not_blocked_by_sector_state() -> None:
    five_sell = confirmed_point("2sell", frequency="5m", tower="formal", level=0)
    setup = build_setup(five_sell, supportive_context("30m"), hostile_sector())
    one_sell = confirmed_point("1sell", frequency="1m", minutes_after=1)
    lifecycle = advance_lifecycle(None, setup, one_sell, as_of=AS_OF)

    decision = evaluate_exit_policy(
        lifecycle,
        setup,
        one_sell,
        held_tower="formal",
        held_level=0,
    )

    assert decision.allowed is True
    assert decision.action == "exit_full"


def test_default_sell_exit_is_based_on_confirmed_five_minute_point() -> None:
    five_sell = confirmed_point("2sell", frequency="5m", tower="formal", level=0)
    setup = build_setup(five_sell, supportive_context("30m"), hostile_sector())
    armed = advance_lifecycle(None, setup, None, as_of=AS_OF)
    without_segment = evaluate_exit_policy(
        armed,
        setup,
        None,
        held_tower="formal",
        held_level=0,
    )
    one_sell = confirmed_point("1sell", frequency="1m", minutes_after=1)
    triggered = advance_lifecycle(None, setup, one_sell, as_of=AS_OF)
    accepted = evaluate_exit_policy(
        triggered,
        setup,
        one_sell,
        held_tower="formal",
        held_level=0,
    )

    assert without_segment.allowed is True
    assert without_segment.action == "exit_full"
    assert accepted.action == "exit_full"


def test_sell_without_reference_structure_requires_relation_review() -> None:
    five_sell = confirmed_point("2sell", frequency="5m", tower="formal", level=0)
    setup = build_setup(five_sell, supportive_context("30m"), hostile_sector())
    lifecycle = advance_lifecycle(None, setup, None, as_of=AS_OF)

    decision = evaluate_exit_policy(
        lifecycle,
        setup,
        None,
        held_tower=None,
        held_level=None,
    )

    assert decision.allowed is False
    assert decision.action == "none"
    assert decision.reason_codes == (
        "sell_structure_relation_requires_manual_review",
    )


def test_legacy_one_minute_gate_rejects_invalid_segment_when_enabled() -> None:
    five_sell = confirmed_point("2sell", frequency="5m", tower="formal", level=0)
    setup = build_setup(five_sell, supportive_context("30m"), hostile_sector())
    third_sell = confirmed_point(
        "3sell",
        frequency="1m",
        anchor=10.0,
        stop=10.2,
        center_zd=10.0,
        center_zg=10.2,
        variant="boundary_touch",
        minutes_after=1,
    )
    # 构造一条遭篡改的旧版段差生命周期，证明读取旧参数档案时执行层仍会
    # 关闭失败，不能只依赖上游生命周期匹配器。
    valid_first_sell = confirmed_point("1sell", frequency="1m", minutes_after=1)
    lifecycle = advance_lifecycle(None, setup, valid_first_sell, as_of=AS_OF)
    lifecycle = replace(lifecycle, trigger_point_id=third_sell.point_id)

    decision = evaluate_exit_policy(
        lifecycle,
        setup,
        third_sell,
        held_tower="formal",
        held_level=0,
        policy=TradingPolicy(require_confirmed_one_minute=True),
    )

    assert decision.allowed is False
    assert decision.reason_codes == ("one_minute_sell_not_confirmed",)


def test_environment_and_one_minute_are_advisory_under_production_policy() -> None:
    five_buy = confirmed_point("3buy", frequency="5m", center_ordinal=1)
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
        ),
    )

    assert default.allowed is True
    assert ablated.allowed is True


def test_legacy_one_minute_exit_gate_remains_readable_for_old_archives() -> None:
    five_sell = confirmed_point("2sell", frequency="5m", tower="formal", level=0)
    setup = build_setup(five_sell, supportive_context("30m"), hostile_sector())
    armed = advance_lifecycle(None, setup, None, as_of=AS_OF)

    decision = evaluate_exit_policy(
        armed,
        setup,
        None,
        held_tower="formal",
        held_level=0,
        policy=TradingPolicy(require_confirmed_one_minute=True),
    )

    assert decision.allowed is False
    assert decision.reason_codes == ("one_minute_sell_not_confirmed",)
