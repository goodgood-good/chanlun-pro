from dataclasses import replace
from datetime import timedelta

import pytest

from chanlun.core.strict_structure.current_events import TerminalSegmentReference
from chanlun.core.strict_structure.models import SourceKind
from chanlun.decision_support.trading_system.lifecycle import (
    advance_lifecycle,
    build_setup,
    five_minute_setup_is_current,
    five_minute_setup_is_executable,
    lifecycle_stage_from_signal,
    match_one_minute_nesting_witness,
)
from tests.trading_system.helpers import (
    AS_OF,
    confirmed_point,
    eligible_sector,
    neutral_context,
    provisional_point,
    supportive_context,
)


def _with_terminal_interval(
    point,
    *,
    market_start,
):
    return replace(
        point,
        terminal_segment=TerminalSegmentReference(
            role="latest_completed",
            structural_level=point.recursive_level,
            unit_id=f"segment:{point.source_frequency}:{point.point_id}",
            source_kind=SourceKind.SEGMENT,
            direction="down" if point.side == "buy" else "up",
            state="locked",
            market_start=market_start,
            market_end=point.anchor_at,
            available_at=point.available_at,
        ),
    )


def _strict_setup(point):
    return _with_terminal_interval(
        point,
        market_start=point.anchor_at - timedelta(minutes=30),
    )


def _strict_witness(point):
    return _with_terminal_interval(
        point,
        market_start=point.anchor_at - timedelta(minutes=1),
    )


def test_formation_period_one_minute_segment_is_a_nesting_witness() -> None:
    five_point = confirmed_point(
        "3buy",
        anchor=10.0,
        stop=9.8,
        center_zg=9.9,
        available_minutes_after=10,
    )
    five_point = _with_terminal_interval(
        five_point,
        market_start=five_point.anchor_at - timedelta(minutes=30),
    )
    setup = build_setup(five_point, neutral_context("30m"), eligible_sector())
    one_point = confirmed_point(
        "1buy",
        frequency="1m",
        anchor=9.9,
        minutes_after=-5,
    )
    one_point = _with_terminal_interval(
        one_point,
        market_start=one_point.anchor_at - timedelta(minutes=1),
    )

    assert one_point.available_at < five_point.available_at
    assert (
        match_one_minute_nesting_witness(
            setup,
            (one_point,),
            as_of=AS_OF,
        )
        == one_point
    )
    assert (
        match_one_minute_nesting_witness(
            setup,
            (one_point,),
            as_of=five_point.available_at - timedelta(seconds=1),
        )
        is None
    )
    future_one = replace(
        one_point,
        available_at=AS_OF + timedelta(minutes=1),
        terminal_segment=replace(
            one_point.terminal_segment,
            available_at=AS_OF + timedelta(minutes=1),
        ),
    )
    assert (
        match_one_minute_nesting_witness(
            setup,
            (future_one,),
            as_of=AS_OF,
        )
        is None
    )


def test_later_same_price_point_outside_terminal_interval_is_not_a_witness() -> None:
    five_point = confirmed_point(
        "3buy",
        anchor=10.0,
        stop=9.8,
        center_zg=9.9,
    )
    five_point = _with_terminal_interval(
        five_point,
        market_start=five_point.anchor_at - timedelta(minutes=30),
    )
    setup = build_setup(five_point, neutral_context("30m"), eligible_sector())
    later = confirmed_point(
        "1buy",
        frequency="1m",
        anchor=9.9,
        minutes_after=5,
    )
    later = _with_terminal_interval(
        later,
        market_start=later.anchor_at - timedelta(minutes=1),
    )

    assert later.structure_anchor_price == 9.9
    assert later.terminal_segment is not None
    assert five_point.terminal_segment is not None
    assert later.terminal_segment.market_start > five_point.terminal_segment.market_end
    assert match_one_minute_nesting_witness(setup, (later,), as_of=AS_OF) is None


def test_nesting_requires_the_full_one_minute_terminal_interval() -> None:
    five_point = confirmed_point("3buy", anchor=10.0, stop=9.8, center_zg=9.9)
    five_start = five_point.anchor_at - timedelta(minutes=30)
    five_point = _with_terminal_interval(
        five_point,
        market_start=five_start,
    )
    setup = build_setup(five_point, neutral_context("30m"), eligible_sector())
    partial = confirmed_point(
        "1buy",
        frequency="1m",
        anchor=9.9,
        minutes_after=-29,
    )
    partial = _with_terminal_interval(
        partial,
        market_start=five_start - timedelta(minutes=5),
    )

    assert five_start <= partial.anchor_at <= five_point.anchor_at
    assert partial.terminal_segment is not None
    assert partial.terminal_segment.market_start < five_start
    assert match_one_minute_nesting_witness(setup, (partial,), as_of=AS_OF) is None


@pytest.mark.parametrize("missing", ("five", "one"))
def test_nesting_witness_requires_both_terminal_lineages(missing: str) -> None:
    five_point = confirmed_point("3buy", anchor=10.0, stop=9.8, center_zg=9.9)
    five_point = _with_terminal_interval(
        five_point,
        market_start=five_point.anchor_at - timedelta(minutes=30),
    )
    one_point = confirmed_point(
        "1buy",
        frequency="1m",
        anchor=9.9,
        minutes_after=-5,
    )
    one_point = _with_terminal_interval(
        one_point,
        market_start=one_point.anchor_at - timedelta(minutes=1),
    )
    if missing == "five":
        five_point = replace(five_point, terminal_segment=None)
    else:
        one_point = replace(one_point, terminal_segment=None)
    setup = build_setup(five_point, neutral_context("30m"), eligible_sector())

    assert match_one_minute_nesting_witness(setup, (one_point,), as_of=AS_OF) is None


def test_five_minute_three_buy_accepts_nested_one_minute_first_buy() -> None:
    setup_point = _strict_setup(
        confirmed_point(
            "3buy",
            frequency="5m",
            anchor=10.50,
            stop=10.00,
            center_zg=10.00,
        )
    )
    setup = build_setup(
        setup_point,
        supportive_context("30m"),
        eligible_sector(),
    )
    one_point = _strict_witness(
        confirmed_point(
            "1buy",
            frequency="1m",
            anchor=10.20,
            stop=10.10,
            minutes_after=-1,
            available_minutes_after=2,
        )
    )
    witness = match_one_minute_nesting_witness(
        setup,
        (one_point,),
        as_of=AS_OF,
    )

    assert witness == one_point
    assert witness.point_type != setup.point.point_type


def test_setup_identity_survives_converged_internal_graph_rebuild() -> None:
    first = confirmed_point("1buy", center_id="left-boundary-center-a")
    rebuilt = confirmed_point(
        "1buy",
        center_id="left-boundary-center-b",
        available_minutes_after=5,
    )

    first_setup = build_setup(
        first,
        supportive_context("30m"),
        eligible_sector(),
    )
    rebuilt_setup = build_setup(
        rebuilt,
        supportive_context("30m"),
        eligible_sector(),
    )

    assert first.point_id != rebuilt.point_id
    assert first.available_at != rebuilt.available_at
    assert first_setup.setup_id == rebuilt_setup.setup_id


def test_setup_identity_changes_when_same_anchor_is_repriced() -> None:
    first = confirmed_point("2sell", anchor=11.05, stop=11.05)
    repriced = confirmed_point("2sell", anchor=11.85, stop=11.85)

    first_setup = build_setup(
        first,
        supportive_context("30m"),
        eligible_sector(),
    )
    repriced_setup = build_setup(
        repriced,
        supportive_context("30m"),
        eligible_sector(),
    )

    assert first.anchor_at == repriced.anchor_at
    assert first.point_id != repriced.point_id
    assert first_setup.setup_id != repriced_setup.setup_id


@pytest.mark.parametrize("point_type", ("1buy", "2buy"))
def test_buy_setup_accepts_nested_one_or_two_buy_witness(
    point_type: str,
) -> None:
    setup_point = _strict_setup(
        confirmed_point("3buy", anchor=10.0, stop=9.8, center_zg=9.9)
    )
    setup = build_setup(
        setup_point,
        supportive_context("30m"),
        eligible_sector(),
    )
    witness = _strict_witness(
        confirmed_point(
            point_type,
            frequency="1m",
            anchor=9.9,
            minutes_after=-1,
            available_minutes_after=2,
        )
    )

    assert match_one_minute_nesting_witness(setup, (witness,), as_of=AS_OF) == witness


@pytest.mark.parametrize("point_type", ("1sell", "2sell"))
def test_sell_setup_accepts_nested_one_or_two_sell_witness(
    point_type: str,
) -> None:
    setup_point = _strict_setup(
        confirmed_point(
            "3sell",
            anchor=10.0,
            stop=10.2,
            center_zd=10.1,
            center_zg=10.3,
        )
    )
    setup = build_setup(
        setup_point,
        neutral_context("30m"),
        eligible_sector(),
    )
    witness = _strict_witness(
        confirmed_point(
            point_type,
            frequency="1m",
            anchor=10.1,
            stop=10.2,
            minutes_after=-1,
            available_minutes_after=2,
        )
    )

    assert match_one_minute_nesting_witness(setup, (witness,), as_of=AS_OF) == witness


@pytest.mark.parametrize("point_type", ("3buy", "3sell"))
def test_nested_boundary_touch_third_class_is_not_a_segment_difference(
    point_type: str,
) -> None:
    side = "buy" if point_type == "3buy" else "sell"
    setup_point = _strict_setup(
        confirmed_point(
            f"2{side}",
            anchor=10.0,
            stop=9.8 if side == "buy" else 10.2,
            center_zg=10.1,
            center_zd=9.9,
        )
    )
    setup = build_setup(
        setup_point,
        neutral_context("30m"),
        eligible_sector(),
    )
    third_class = _strict_witness(
        confirmed_point(
            point_type,
            frequency="1m",
            anchor=10.0,
            stop=9.8 if side == "buy" else 10.2,
            center_zd=9.8 if side == "buy" else 10.0,
            center_zg=10.0 if side == "buy" else 10.2,
            variant="boundary_touch",
            minutes_after=-1,
            available_minutes_after=2,
        )
    )

    assert match_one_minute_nesting_witness(setup, (third_class,), as_of=AS_OF) is None


@pytest.mark.parametrize("side", ("buy", "sell"))
def test_standard_nested_third_class_can_be_a_segment_difference(
    side: str,
) -> None:
    setup_point = _strict_setup(
        confirmed_point(
            f"2{side}",
            anchor=10.0,
            stop=9.8 if side == "buy" else 10.2,
            center_zd=9.9,
            center_zg=10.1,
        )
    )
    setup = build_setup(
        setup_point,
        neutral_context("30m"),
        eligible_sector(),
    )
    continuation = _strict_witness(
        confirmed_point(
            f"3{side}",
            frequency="1m",
            anchor=9.95 if side == "buy" else 10.05,
            stop=9.8 if side == "buy" else 10.2,
            center_zd=9.90 if side == "buy" else 10.10,
            center_zg=9.93 if side == "buy" else 10.15,
            variant="standard",
            minutes_after=-1,
            available_minutes_after=2,
        )
    )

    assert (
        match_one_minute_nesting_witness(
            setup,
            (continuation,),
            as_of=AS_OF,
        )
        == continuation
    )


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


def test_inner_interval_before_setup_terminal_start_is_rejected() -> None:
    setup_point = _strict_setup(confirmed_point("2buy", minutes_after=10))
    setup = build_setup(
        setup_point,
        neutral_context("30m"),
        eligible_sector(),
    )
    early = _strict_witness(confirmed_point("1buy", frequency="1m", minutes_after=-24))

    assert match_one_minute_nesting_witness(setup, (early,), as_of=AS_OF) is None


def test_segment_difference_cannot_cross_symbol_boundary() -> None:
    setup_point = _strict_setup(
        confirmed_point("2buy", code="SZ.000001", anchor=10.0, stop=9.8)
    )
    setup = build_setup(
        setup_point,
        neutral_context("30m"),
        eligible_sector(),
    )
    other_symbol = _strict_witness(
        confirmed_point(
            "1buy",
            code="SH.600000",
            frequency="1m",
            anchor=9.9,
            minutes_after=-1,
            available_minutes_after=2,
        )
    )

    assert match_one_minute_nesting_witness(setup, (other_symbol,), as_of=AS_OF) is None


@pytest.mark.parametrize(
    ("side", "anchor", "stop", "center_zd", "center_zg", "segment_anchor"),
    (
        ("buy", 10.0, 9.8, 9.7, 9.9, 9.9),
        ("sell", 10.0, 10.2, 10.1, 10.3, 10.1),
    ),
)
def test_segment_difference_inside_real_terminal_segment_is_jointly_actionable(
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
    segment = _with_terminal_interval(
        segment,
        market_start=segment.anchor_at - timedelta(minutes=1),
    )

    assert segment.available_at < point.anchor_at
    assert segment.available_at < point.available_at
    assert match_one_minute_nesting_witness(setup, (segment,), as_of=AS_OF) == segment


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
    too_early = _strict_witness(
        confirmed_point(
            "1buy",
            frequency="1m",
            anchor=9.9,
            minutes_after=-35,
        )
    )

    assert match_one_minute_nesting_witness(setup, (too_early,), as_of=AS_OF) is None


def test_segment_difference_anchored_before_terminal_segment_is_rejected_when_seen_late() -> (
    None
):
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
    old_segment_seen_late = _strict_witness(
        confirmed_point(
            "1buy",
            frequency="1m",
            anchor=9.9,
            minutes_after=-35,
            available_minutes_after=36,
        )
    )

    assert old_segment_seen_late.anchor_at < terminal_start
    assert old_segment_seen_late.available_at > terminal_start
    assert (
        match_one_minute_nesting_witness(
            setup,
            (old_segment_seen_late,),
            as_of=AS_OF,
        )
        is None
    )


def test_completed_bar_labels_share_the_same_physical_left_boundary() -> None:
    point = confirmed_point("3buy", anchor=10.0, stop=9.8, center_zg=9.9)
    terminal_start_label = point.anchor_at - timedelta(minutes=30)
    point = _with_terminal_interval(
        point,
        market_start=terminal_start_label,
    )
    setup = build_setup(point, neutral_context("30m"), eligible_sector())
    first_nested_minute = _with_terminal_interval(
        confirmed_point(
            "1buy",
            frequency="1m",
            anchor=9.9,
            minutes_after=-26,
        ),
        market_start=terminal_start_label - timedelta(minutes=4),
    )

    assert (
        match_one_minute_nesting_witness(
            setup,
            (first_nested_minute,),
            as_of=AS_OF,
        )
        == first_nested_minute
    )


def test_one_minute_chart_recursive_l1_is_not_a_subordinate_segment_point() -> None:
    setup_point = _strict_setup(confirmed_point("2buy", anchor=10.0, stop=9.8))
    setup = build_setup(
        setup_point,
        neutral_context("30m"),
        eligible_sector(),
    )
    effective_five_minute = _strict_witness(
        confirmed_point(
            "1buy",
            frequency="1m",
            level=1,
            anchor=9.9,
            minutes_after=-1,
            available_minutes_after=2,
        )
    )

    assert (
        match_one_minute_nesting_witness(
            setup,
            (effective_five_minute,),
            as_of=AS_OF,
        )
        is None
    )


def test_nested_witness_does_not_require_legacy_price_proximity() -> None:
    setup_point = _strict_setup(confirmed_point("2buy", anchor=10.0, stop=9.8))
    setup = build_setup(
        setup_point,
        neutral_context("30m"),
        eligible_sector(),
    )
    outside = _strict_witness(
        confirmed_point(
            "1buy",
            frequency="1m",
            anchor=10.5,
            minutes_after=-1,
            available_minutes_after=2,
        )
    )

    assert match_one_minute_nesting_witness(setup, (outside,), as_of=AS_OF) == outside


def test_cold_restart_at_late_as_of_selects_first_jointly_known_witness() -> None:
    setup_point = _strict_setup(confirmed_point("2buy", anchor=10.0, stop=9.8))
    setup = build_setup(
        setup_point,
        neutral_context("30m"),
        eligible_sector(),
    )
    first = _strict_witness(
        confirmed_point(
            "1buy",
            frequency="1m",
            minutes_after=-2,
            available_minutes_after=3,
        )
    )
    later = _strict_witness(
        confirmed_point(
            "2buy",
            frequency="1m",
            minutes_after=-1,
            available_minutes_after=4,
        )
    )

    assert first.available_at < later.available_at < AS_OF
    assert (
        match_one_minute_nesting_witness(
            setup,
            (later, first),
            as_of=AS_OF,
        )
        == first
    )


def test_preknown_witness_tie_uses_interval_closest_to_outer_endpoint() -> None:
    setup_point = _strict_setup(
        confirmed_point(
            "2buy",
            anchor=10.0,
            stop=9.8,
            available_minutes_after=5,
        )
    )
    setup = build_setup(
        setup_point,
        neutral_context("30m"),
        eligible_sector(),
    )
    older = _strict_witness(confirmed_point("1buy", frequency="1m", minutes_after=-10))
    closest = _strict_witness(confirmed_point("2buy", frequency="1m", minutes_after=-1))

    assert older.available_at < setup_point.available_at
    assert closest.available_at < setup_point.available_at
    assert (
        match_one_minute_nesting_witness(
            setup,
            (older, closest),
            as_of=AS_OF,
        )
        == closest
    )


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


def test_terminal_tail_remains_current_for_display_after_execution_expiry() -> None:
    point = _strict_setup(confirmed_point("2buy"))
    stale_at = point.anchor_at + timedelta(days=5)

    assert five_minute_setup_is_current(point, as_of=stale_at) is True
    assert five_minute_setup_is_executable(point, as_of=stale_at) is False


def test_later_nested_witness_does_not_move_first_execution_boundary() -> None:
    setup_point = _strict_setup(confirmed_point("2buy"))
    setup = build_setup(
        setup_point,
        neutral_context("30m"),
        eligible_sector(),
    )
    first_witness = _strict_witness(
        confirmed_point(
            "1buy",
            frequency="1m",
            minutes_after=-2,
            available_minutes_after=3,
        )
    )
    later_witness = _strict_witness(
        confirmed_point(
            "1buy",
            frequency="1m",
            minutes_after=-1,
            available_minutes_after=3,
        )
    )
    first = advance_lifecycle(
        None,
        setup,
        first_witness,
        as_of=AS_OF,
    )

    repeated = advance_lifecycle(
        first,
        setup,
        later_witness,
        as_of=AS_OF + timedelta(minutes=1),
    )

    assert repeated == first
    assert first.trigger_point_id == first_witness.point_id
    assert repeated.trigger_point_id != later_witness.point_id


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
