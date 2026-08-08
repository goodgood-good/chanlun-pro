"""Five-role center lifecycle and frozen three-component core."""

from chanlun.core.strict_structure.center_machine import advance_center, calculate_centers, establish_center
from chanlun.core.strict_structure.models import CenterEventKind, CenterState, SourceKind
from tests.core.strict_structure.helpers import unit


def _entry_and_core():
    return (
        unit(0, "up", 90, 120),
        unit(1, "down", 120, 100),
        unit(2, "up", 100, 115),
        unit(3, "down", 115, 105),
    )


def test_external_entry_is_not_a_core_or_body_component() -> None:
    values = _entry_and_core() + (unit(4, "up", 105, 130),)
    result = calculate_centers(values, 0, SourceKind.SEGMENT)

    assert len(result.centers) == 1
    center = result.centers[0]
    assert center.entry_unit is values[0]
    assert center.entry_unit not in center.body_units
    assert center.initial_units == values[1:4]
    assert center.body_units == values[1:4]
    assert center.core_units == values[1:4]
    assert center.lifecycle_role_count == 5
    assert (center.zd_tick, center.zg_tick) == (105, 115)


def test_external_leave_can_be_the_fifth_role() -> None:
    values = _entry_and_core()
    leave = unit(4, "up", 105, 130)
    ret = unit(5, "down", 130, 120)
    result = calculate_centers(values + (leave, ret), 0, SourceKind.SEGMENT)

    assert len(result.centers) == 1
    center = result.centers[0]
    assert center.state is CenterState.COMPLETED
    assert center.body_units == values[1:4]
    assert center.completion_leave_unit is leave
    assert center.completion_return_unit is ret
    assert center.lifecycle_role_count == 5
    assert leave not in center.body_units
    assert ret not in center.body_units


def test_failed_leave_is_folded_only_after_return_reenters_core() -> None:
    values = _entry_and_core()
    leave = unit(4, "up", 105, 130)
    center = establish_center(values + (leave,), 0, SourceKind.SEGMENT)
    assert center is not None
    assert center.pending_leave_unit is leave
    assert leave not in center.body_units

    reentry = unit(5, "down", 130, 110)
    extended, event = advance_center(center, reentry)
    assert event.kind is CenterEventKind.EXTENDED
    assert extended.body_units == values[1:4] + (leave, reentry)
    assert extended.pending_leave_unit is None


def test_recursive_center_keeps_three_completed_trend_body() -> None:
    entry = unit(0, "up", 90, 120, source_kind=SourceKind.TREND_TYPE)
    body = (
        unit(1, "down", 120, 100, source_kind=SourceKind.TREND_TYPE),
        unit(2, "up", 100, 115, source_kind=SourceKind.TREND_TYPE),
        unit(3, "down", 115, 105, source_kind=SourceKind.TREND_TYPE),
    )
    center = establish_center(body, 0, SourceKind.TREND_TYPE, entry_unit=entry)
    assert center is not None
    assert center.initial_units == body
    assert center.core_units == body
