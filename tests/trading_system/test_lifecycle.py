from dataclasses import replace
from datetime import timedelta

import pytest

from chanlun.core.strict_structure.current_events import TerminalSegmentReference
from chanlun.core.strict_structure.models import SourceKind
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
def test_buy_setup_accepts_one_or_two_buy_as_reversal_trigger(
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
def test_sell_setup_accepts_one_or_two_sell_as_reversal_trigger(
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
def test_boundary_touch_third_class_point_cannot_be_continuation_trigger(
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


@pytest.mark.parametrize("side", ("buy", "sell"))
def test_standard_third_class_point_can_be_one_minute_continuation_trigger(
    side: str,
) -> None:
    setup = build_setup(
        confirmed_point(
            f"2{side}",
            anchor=10.0,
            stop=9.8 if side == "buy" else 10.2,
            center_zd=9.9,
            center_zg=10.1,
        ),
        neutral_context("30m"),
        eligible_sector(),
    )
    continuation = confirmed_point(
        f"3{side}",
        frequency="1m",
        anchor=9.95 if side == "buy" else 10.05,
        stop=9.8 if side == "buy" else 10.2,
        center_zd=9.90 if side == "buy" else 10.10,
        center_zg=9.93 if side == "buy" else 10.15,
        variant="standard",
        minutes_after=1,
    )

    assert match_one_minute_trigger(
        setup,
        (continuation,),
        as_of=AS_OF,
    ) == continuation


def test_provisional_five_minute_candidate_cannot_reach_triggered() -> None:
    setup = build_setup(
        provisional_point("2buy"),
        neutral_context("30m"),
        eligible_sector(),
    )
    lifecycle = advance_lifecycle(None, setup, None, as_of=AS_OF)

    assert lifecycle.stage == "approaching"
    assert lifecycle.actionable is False


def test_geometrically_ready_third_class_candidate_awaits_confirmation() -> None:
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
    assert lifecycle.reason_codes == (
        "five_minute_geometric_candidate_awaiting_confirmation",
    )
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


def test_segment_difference_cannot_cross_symbol_boundary() -> None:
    setup = build_setup(
        confirmed_point("2buy", code="SZ.000001", anchor=10.0, stop=9.8),
        neutral_context("30m"),
        eligible_sector(),
    )
    other_symbol = confirmed_point(
        "1buy",
        code="SH.600000",
        frequency="1m",
        anchor=9.9,
        minutes_after=1,
    )

    assert match_one_minute_trigger(setup, (other_symbol,), as_of=AS_OF) is None


def test_segment_difference_during_five_minute_formation_is_retained() -> None:
    setup = build_setup(
        confirmed_point(
            "2buy",
            anchor=10.0,
            stop=9.8,
            available_minutes_after=10,
        ),
        neutral_context("30m"),
        eligible_sector(),
    )
    segment = confirmed_point(
        "1buy",
        frequency="1m",
        anchor=9.9,
        minutes_after=5,
    )

    assert segment.available_at < setup.point.available_at
    assert match_one_minute_trigger(setup, (segment,), as_of=AS_OF) == segment


@pytest.mark.parametrize(
    ("side", "anchor", "stop", "center_zd", "center_zg", "segment_anchor"),
    (
        ("buy", 10.0, 9.8, 9.7, 9.9, 9.9),
        ("sell", 10.0, 10.2, 10.1, 10.3, 10.1),
    ),
)
def test_segment_difference_inside_real_terminal_segment_precedes_point_anchor(
    side: str,
    anchor: float,
    stop: float,
    center_zd: float,
    center_zg: float,
    segment_anchor: float,
) -> None:
    point = confirmed_point(
        f"3{side}",
        anchor=anchor,
        stop=stop,
        center_zd=center_zd,
        center_zg=center_zg,
    )
    point = replace(
        point,
        terminal_segment=TerminalSegmentReference(
            role="latest_completed",
            structural_level=0,
            unit_id=f"segment:terminal:{side}",
            source_kind=SourceKind.SEGMENT,
            direction="down" if side == "buy" else "up",
            state="locked",
            market_start=point.anchor_at - timedelta(minutes=30),
            market_end=point.anchor_at,
            available_at=point.available_at,
        ),
    )
    setup = build_setup(
        point,
        neutral_context("30m"),
        eligible_sector(),
    )
    segment = confirmed_point(
        f"1{side}",
        frequency="1m",
        anchor=segment_anchor,
        stop=stop,
        minutes_after=-5,
    )

    assert segment.available_at < point.anchor_at
    assert match_one_minute_trigger(setup, (segment,), as_of=AS_OF) == segment


def test_segment_difference_before_terminal_segment_start_is_rejected() -> None:
    point = confirmed_point("3buy", anchor=10.0, stop=9.8, center_zg=9.9)
    point = replace(
        point,
        terminal_segment=TerminalSegmentReference(
            role="latest_completed",
            structural_level=0,
            unit_id="segment:terminal:buy",
            source_kind=SourceKind.SEGMENT,
            direction="down",
            state="locked",
            market_start=point.anchor_at - timedelta(minutes=30),
            market_end=point.anchor_at,
            available_at=point.available_at,
        ),
    )
    setup = build_setup(point, neutral_context("30m"), eligible_sector())
    too_early = confirmed_point(
        "1buy",
        frequency="1m",
        anchor=9.9,
        minutes_after=-31,
    )

    assert match_one_minute_trigger(setup, (too_early,), as_of=AS_OF) is None


def test_segment_difference_anchored_before_terminal_segment_is_rejected_when_seen_late(
) -> None:
    point = confirmed_point("3buy", anchor=10.0, stop=9.8, center_zg=9.9)
    terminal_start = point.anchor_at - timedelta(minutes=30)
    point = replace(
        point,
        terminal_segment=TerminalSegmentReference(
            role="latest_completed",
            structural_level=0,
            unit_id="segment:terminal:late-seen-buy",
            source_kind=SourceKind.SEGMENT,
            direction="down",
            state="locked",
            market_start=terminal_start,
            market_end=point.anchor_at,
            available_at=point.available_at,
        ),
    )
    setup = build_setup(point, neutral_context("30m"), eligible_sector())
    old_segment_seen_late = confirmed_point(
        "1buy",
        frequency="1m",
        anchor=9.9,
        minutes_after=-31,
        available_minutes_after=32,
    )

    assert old_segment_seen_late.anchor_at < terminal_start
    assert old_segment_seen_late.available_at > terminal_start
    assert (
        match_one_minute_trigger(setup, (old_segment_seen_late,), as_of=AS_OF)
        is None
    )


def test_one_minute_chart_recursive_l1_is_not_a_subordinate_segment_point() -> None:
    setup = build_setup(
        confirmed_point("2buy", anchor=10.0, stop=9.8),
        neutral_context("30m"),
        eligible_sector(),
    )
    effective_five_minute = confirmed_point(
        "1buy",
        frequency="1m",
        level=1,
        anchor=9.9,
        minutes_after=1,
    )

    assert (
        match_one_minute_trigger(
            setup,
            (effective_five_minute,),
            as_of=AS_OF,
        )
        is None
    )


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
    assert repeated.stage == first.stage == "triggered"


@pytest.mark.parametrize(
    ("point_type", "stop", "current_price"),
    (
        ("2buy", 9.8, 9.79),
        ("2sell", 10.2, 10.21),
    ),
)
def test_structure_stop_crossing_invalidates_setup_immediately(
    point_type: str,
    stop: float,
    current_price: float,
) -> None:
    setup = build_setup(
        confirmed_point(point_type, anchor=10.0, stop=stop),
        neutral_context("30m"),
        eligible_sector(),
    )

    lifecycle = advance_lifecycle(
        None,
        setup,
        None,
        as_of=AS_OF,
        current_price=current_price,
    )

    assert lifecycle.stage == "invalidated"
    assert lifecycle.reason_codes == ("structure_invalidated",)
    assert lifecycle.actionable is False


@pytest.mark.parametrize(
    ("point_type", "stop"),
    (
        ("2buy", 9.8),
        ("2sell", 10.2),
    ),
)
def test_touching_structure_boundary_does_not_invalidate_setup(
    point_type: str,
    stop: float,
) -> None:
    setup = build_setup(
        confirmed_point(point_type, anchor=10.0, stop=stop),
        neutral_context("30m"),
        eligible_sector(),
    )

    lifecycle = advance_lifecycle(
        None,
        setup,
        None,
        as_of=AS_OF,
        current_price=stop,
    )

    assert lifecycle.stage == "triggered"
    assert lifecycle.reason_codes == ("five_minute_trade_signal_confirmed",)


def test_price_on_valid_side_keeps_five_minute_signal_triggered() -> None:
    setup = build_setup(
        confirmed_point("2buy", anchor=10.0, stop=9.8),
        neutral_context("30m"),
        eligible_sector(),
    )

    lifecycle = advance_lifecycle(
        None,
        setup,
        None,
        as_of=AS_OF,
        current_price=9.81,
    )

    assert lifecycle.stage == "triggered"
