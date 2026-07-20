from dataclasses import replace
from datetime import timedelta

import pytest

from chanlun.decision_support.trading_system.lifecycle import (
    advance_lifecycle,
    build_setup,
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


def test_provisional_five_minute_candidate_cannot_reach_triggered() -> None:
    setup = build_setup(
        provisional_point("2buy"),
        neutral_context("30m"),
        eligible_sector(),
    )
    lifecycle = advance_lifecycle(None, setup, None, as_of=AS_OF)

    assert lifecycle.stage == "approaching"
    assert lifecycle.actionable is False


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
