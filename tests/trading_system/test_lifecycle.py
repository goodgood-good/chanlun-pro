from dataclasses import replace
from datetime import timedelta

import pytest

from chanlun.decision_support.trading_system.lifecycle import (
    advance_lifecycle,
    build_setup,
    lifecycle_stage_from_signal,
    match_one_minute_trigger,
)
from tests.trading_system.helpers import (
    AS_OF,
    confirmed_point,
    eligible_sector,
    neutral_context,
    provisional_point,
    supportive_context,
)


def test_five_minute_three_buy_can_use_one_minute_first_buy_trigger() -> None:
    setup = build_setup(
        confirmed_point(
            "3buy",
            frequency="5m",
            anchor=10.50,
            stop=10.00,
            center_zg=10.00,
        ),
        supportive_context("30m"),
        eligible_sector(),
    )
    trigger = match_one_minute_trigger(
        setup,
        (
            confirmed_point(
                "1buy",
                frequency="1m",
                anchor=10.20,
                stop=10.10,
                minutes_after=5,
            ),
        ),
        as_of=AS_OF,
    )

    assert trigger is not None
    assert trigger.point_type == "1buy"
    assert trigger.point_type != setup.point.point_type


@pytest.mark.parametrize("point_type", ("1buy", "2buy"))
def test_buy_setup_accepts_only_one_or_two_buy_as_reversal_trigger(
    point_type: str,
) -> None:
    setup = build_setup(
        confirmed_point("3buy", anchor=10.0, stop=9.8, center_zg=9.9),
        supportive_context("30m"),
        eligible_sector(),
    )
    trigger = confirmed_point(
        point_type,
        frequency="1m",
        anchor=9.9,
        minutes_after=1,
    )

    assert match_one_minute_trigger(setup, (trigger,), as_of=AS_OF) == trigger


@pytest.mark.parametrize("point_type", ("1sell", "2sell"))
def test_sell_setup_accepts_only_one_or_two_sell_as_reversal_trigger(
    point_type: str,
) -> None:
    setup = build_setup(
        confirmed_point(
            "3sell",
            anchor=10.0,
            stop=10.2,
            center_zd=10.1,
            center_zg=10.3,
        ),
        neutral_context("30m"),
        eligible_sector(),
    )
    trigger = confirmed_point(
        point_type,
        frequency="1m",
        anchor=10.1,
        stop=10.2,
        minutes_after=1,
    )

    assert match_one_minute_trigger(setup, (trigger,), as_of=AS_OF) == trigger


@pytest.mark.parametrize("point_type", ("3buy", "3sell"))
def test_third_class_point_cannot_replace_one_minute_reversal_trigger(
    point_type: str,
) -> None:
    side = "buy" if point_type == "3buy" else "sell"
    setup = build_setup(
        confirmed_point(
            f"2{side}",
            anchor=10.0,
            stop=9.8 if side == "buy" else 10.2,
            center_zg=10.1,
            center_zd=9.9,
        ),
        neutral_context("30m"),
        eligible_sector(),
    )
    third_class = confirmed_point(
        point_type,
        frequency="1m",
        anchor=10.0,
        stop=9.8 if side == "buy" else 10.2,
        center_zd=9.8 if side == "buy" else 10.0,
        center_zg=10.0 if side == "buy" else 10.2,
        variant="boundary_touch",
        minutes_after=1,
    )

    assert match_one_minute_trigger(setup, (third_class,), as_of=AS_OF) is None


def test_provisional_five_minute_candidate_cannot_reach_triggered() -> None:
    setup = build_setup(
        provisional_point("2buy"),
        neutral_context("30m"),
        eligible_sector(),
    )
    lifecycle = advance_lifecycle(None, setup, None, as_of=AS_OF)

    assert lifecycle.stage == "approaching"
    assert lifecycle.actionable is False


def test_geometrically_completed_third_class_candidate_is_formed() -> None:
    point = replace(
        provisional_point("3buy"),
        evidence_codes=(
            "physical_timeframe_recursive_base_level",
            "provisional_center_completion",
            "core_boundary_held",
        ),
    )
    setup = build_setup(
        point,
        neutral_context("30m"),
        eligible_sector(),
    )

    lifecycle = advance_lifecycle(None, setup, None, as_of=AS_OF)

    assert lifecycle.stage == "formed"
    assert lifecycle.reason_codes == ("five_minute_geometric_point_formed",)
    assert lifecycle.actionable is False


def test_formed_evidence_does_not_promote_non_third_class_candidate() -> None:
    signal = {
        "point_type": "2buy",
        "lifecycle_stage": "approaching",
        "setup_5m": {
            "point_type": "2buy",
            "status": "provisional",
            "evidence_codes": [
                "provisional_center_completion",
                "core_boundary_held",
            ],
        },
    }

    assert lifecycle_stage_from_signal(signal) == "approaching"


def test_trigger_before_setup_start_is_rejected() -> None:
    setup = build_setup(
        confirmed_point("2buy", minutes_after=10),
        neutral_context("30m"),
        eligible_sector(),
    )
    early = confirmed_point("1buy", frequency="1m", minutes_after=5)

    assert match_one_minute_trigger(setup, (early,), as_of=AS_OF) is None


def test_trigger_outside_structure_price_interval_is_rejected() -> None:
    setup = build_setup(
        confirmed_point("2buy", anchor=10.0, stop=9.8),
        neutral_context("30m"),
        eligible_sector(),
    )
    outside = confirmed_point(
        "1buy",
        frequency="1m",
        anchor=10.5,
        minutes_after=1,
    )

    assert match_one_minute_trigger(setup, (outside,), as_of=AS_OF) is None


def test_illegal_lifecycle_transition_fails_closed() -> None:
    setup = build_setup(
        confirmed_point("2buy"),
        neutral_context("30m"),
        eligible_sector(),
    )
    armed = advance_lifecycle(
        None,
        setup,
        None,
        as_of=AS_OF - timedelta(minutes=1),
    )
    previous = replace(armed, stage="active", actionable=True)

    with pytest.raises(ValueError, match="illegal lifecycle transition"):
        advance_lifecycle(previous, setup, None, as_of=AS_OF)


def test_signal_identity_survives_repeated_observation() -> None:
    setup = build_setup(
        confirmed_point("2buy"),
        neutral_context("30m"),
        eligible_sector(),
    )
    first = advance_lifecycle(None, setup, None, as_of=AS_OF)
    repeated = advance_lifecycle(
        first,
        setup,
        None,
        as_of=AS_OF + timedelta(minutes=1),
    )

    assert repeated.signal_id == first.signal_id
    assert repeated.stage == first.stage == "armed"
