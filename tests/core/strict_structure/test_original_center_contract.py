"""Original-text center establishment and third-class lifecycle contract.

The unique V3 specification defines a center from the overlap of the first
three consecutive lower-level completed structures.  A later structure is a
departure, and the first completed return outside the frozen core confirms a
third-class point.  These tests deliberately use the shortest causal witness
so an extra pre-core entry role cannot hide an otherwise valid center.
"""

from __future__ import annotations

from chanlun.core.strict_structure.center_machine import calculate_centers
from chanlun.core.strict_structure.models import CenterState, SourceKind

from .helpers import unit


def _completed_three_sell_witness():
    # First three segments establish [100, 110].  The fourth leaves downward;
    # the fifth is the first upward return and remains below ZD=100.
    return (
        unit(0, "up", 90, 110),
        unit(1, "down", 110, 100),
        unit(2, "up", 100, 115),
        unit(3, "down", 115, 90),
        unit(4, "up", 90, 99),
    )


def test_first_three_completed_segments_establish_l0_center() -> None:
    values = _completed_three_sell_witness()[:3]

    result = calculate_centers(values, 0, SourceKind.SEGMENT)

    assert len(result.centers) == 1
    center = result.centers[0]
    assert center.state is CenterState.ONGOING
    assert center.initial_units == values
    assert center.core_units == values
    assert (center.zd_tick, center.zg_tick) == (100, 110)
    assert center.pending_leave_unit is None


def test_fourth_leave_and_first_outside_return_complete_three_sell() -> None:
    values = _completed_three_sell_witness()

    result = calculate_centers(values, 0, SourceKind.SEGMENT)

    assert len(result.centers) == 1
    center = result.centers[0]
    assert center.state is CenterState.COMPLETED
    assert center.completion_leave_unit is values[3]
    assert center.completion_return_unit is values[4]
    assert (center.zd_tick, center.zg_tick) == (100, 110)


def test_touching_first_three_trends_form_closed_interval_center() -> None:
    # The three connected trend types share exactly one price tick (110).
    # V3 §5.4 explicitly states center_exists <=> ZD <= ZG.
    values = (
        unit(0, "up", 90, 110, source_kind=SourceKind.TREND_TYPE),
        unit(1, "up", 110, 120, source_kind=SourceKind.TREND_TYPE),
        unit(2, "down", 120, 110, source_kind=SourceKind.TREND_TYPE),
    )

    result = calculate_centers(
        values,
        0,
        SourceKind.TREND_TYPE,
        oscillatory_ids=frozenset({values[1].unit_id}),
    )

    assert len(result.centers) == 1
    assert (result.centers[0].zd_tick, result.centers[0].zg_tick) == (110, 110)


def test_completed_center_is_prefix_invariant_after_future_units() -> None:
    prefix = _completed_three_sell_witness()
    future = (
        unit(5, "down", 99, 80),
        unit(6, "up", 80, 95),
    )

    before = calculate_centers(prefix, 0, SourceKind.SEGMENT).centers[0]
    after = calculate_centers(prefix + future, 0, SourceKind.SEGMENT).centers[0]

    assert after == before
