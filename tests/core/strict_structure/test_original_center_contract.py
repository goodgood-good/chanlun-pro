"""Original three-unit center and third-class lifecycle contract."""

from __future__ import annotations

from chanlun.core.strict_structure.center_machine import calculate_centers
from chanlun.core.strict_structure.models import CenterState, SourceKind

from .helpers import unit, valid_up_center_lifecycle


def test_three_locked_same_level_units_immediately_establish_center() -> None:
    values = valid_up_center_lifecycle()

    result = calculate_centers(values[:3], 0, SourceKind.SEGMENT)

    assert len(result.centers) == 1
    center = result.centers[0]
    assert center.state is CenterState.ONGOING
    assert center.entry_unit is None
    assert center.initial_units == values[:3]
    assert center.core_units == values[:3]
    assert center.extension_units == ()
    assert center.pending_leave_unit is None
    assert (center.zd_tick, center.zg_tick) == (105, 115)


def test_fourth_unit_only_opens_departure_watch() -> None:
    values = valid_up_center_lifecycle()

    center = calculate_centers(
        values[:4],
        0,
        SourceKind.SEGMENT,
    ).centers[0]

    assert center.state is CenterState.ONGOING
    assert center.initial_units == values[:3]
    assert center.pending_leave_unit is values[3]
    assert center.completion_leave_unit is None
    assert center.completion_return_unit is None


def test_fifth_outside_return_completes_third_class_lifecycle() -> None:
    values = valid_up_center_lifecycle()

    center = calculate_centers(
        values,
        0,
        SourceKind.SEGMENT,
    ).centers[0]

    assert center.state is CenterState.COMPLETED
    assert center.initial_units == values[:3]
    assert center.completion_leave_unit is values[3]
    assert center.completion_return_unit is values[4]
    assert (center.zd_tick, center.zg_tick) == (105, 115)


def test_touching_three_trend_types_form_closed_interval_center() -> None:
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
    assert result.centers[0].initial_units == values
    assert (result.centers[0].zd_tick, result.centers[0].zg_tick) == (110, 110)


def test_completed_center_is_prefix_invariant_after_future_units() -> None:
    prefix = valid_up_center_lifecycle()
    future = (
        unit(5, "up", 120, 145),
        unit(6, "down", 145, 125),
    )

    before = calculate_centers(prefix, 0, SourceKind.SEGMENT).centers[0]
    after = calculate_centers(
        prefix + future,
        0,
        SourceKind.SEGMENT,
    ).centers[0]

    assert after == before
