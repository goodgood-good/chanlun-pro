"""Source-specific center maturity and lifecycle contract.

The line layer deliberately applies the production maturity gate selected by
the user: five consecutive source lines must overlap the frozen core; an
external entry line is not counted.  Recursive input is different because each
unit is already a completed lower-level trend type, so the original three-trend
closed-interval definition remains intact.
"""

from __future__ import annotations

from chanlun.core.strict_structure.center_machine import calculate_centers
from chanlun.core.strict_structure.models import CenterState, SourceKind

from .helpers import unit


def _completed_five_sell_witness():
    # U0 is entry, U1-U3 freeze [100, 110], U4 is the overlapping leave,
    # and U5 is the first return below ZD=100.
    return (
        unit(0, "down", 120, 90),
        unit(1, "up", 90, 110),
        unit(2, "down", 110, 100),
        unit(3, "up", 100, 110),
        unit(4, "down", 110, 90),
        unit(5, "up", 90, 99),
    )


def test_entry_middle_three_and_leave_establish_l0_center() -> None:
    values = _completed_five_sell_witness()[:5]

    result = calculate_centers(values, 0, SourceKind.SEGMENT)

    assert len(result.centers) == 1
    center = result.centers[0]
    assert center.state is CenterState.ONGOING
    assert center.entry_unit is values[0]
    assert center.initial_units == values[1:4]
    assert center.extension_units == ()
    assert center.core_units == values[1:4]
    assert (center.zd_tick, center.zg_tick) == (100, 110)
    assert center.pending_leave_unit is values[4]


def test_later_leave_and_first_outside_return_complete_three_sell() -> None:
    values = _completed_five_sell_witness()

    result = calculate_centers(values, 0, SourceKind.SEGMENT)

    assert len(result.centers) == 1
    center = result.centers[0]
    assert center.state is CenterState.COMPLETED
    assert center.completion_leave_unit is values[4]
    assert center.completion_return_unit is values[5]
    assert (center.zd_tick, center.zg_tick) == (100, 110)


def test_touching_first_three_trends_form_closed_interval_center() -> None:
    values = (
        unit(-1, "down", 120, 90, source_kind=SourceKind.TREND_TYPE),
        unit(0, "up", 90, 110, source_kind=SourceKind.TREND_TYPE),
        unit(1, "up", 110, 120, source_kind=SourceKind.TREND_TYPE),
        unit(2, "down", 120, 110, source_kind=SourceKind.TREND_TYPE),
    )

    result = calculate_centers(
        values,
        0,
        SourceKind.TREND_TYPE,
        oscillatory_ids=frozenset({values[2].unit_id}),
    )

    assert len(result.centers) == 1
    assert (result.centers[0].zd_tick, result.centers[0].zg_tick) == (110, 110)


def test_completed_center_is_prefix_invariant_after_future_units() -> None:
    prefix = _completed_five_sell_witness()
    future = (
        unit(6, "down", 99, 80),
        unit(7, "up", 80, 95),
    )

    before = calculate_centers(prefix, 0, SourceKind.SEGMENT).centers[0]
    after = calculate_centers(prefix + future, 0, SourceKind.SEGMENT).centers[0]

    assert after == before
