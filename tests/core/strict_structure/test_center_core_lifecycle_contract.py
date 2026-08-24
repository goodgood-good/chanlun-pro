"""Frozen three-unit core and post-establishment lifecycle rules."""

from dataclasses import replace

from chanlun.core.strict_structure.center_machine import (
    advance_center,
    calculate_centers,
    establish_center,
)
from chanlun.core.strict_structure.models import (
    CenterEventKind,
    CenterEvidence,
    CenterState,
    SourceKind,
)
from chanlun.core.strict_structure.recursive_engine import StrictRecursiveEngine
from tests.core.strict_structure.helpers import unit, valid_up_center_lifecycle


def test_optional_external_entry_is_not_core_body_or_identity_owner() -> None:
    core = valid_up_center_lifecycle()[:3]
    entry = unit(-1, "up", 90, 120)

    without_entry = establish_center(core, 0, SourceKind.SEGMENT)
    with_entry = establish_center(
        core,
        0,
        SourceKind.SEGMENT,
        entry_unit=entry,
    )

    assert without_entry is not None
    assert with_entry is not None
    assert without_entry.entry_unit is None
    assert with_entry.entry_unit is entry
    assert with_entry.entry_unit not in with_entry.body_units
    assert with_entry.initial_units == core
    assert with_entry.body_units == core
    assert with_entry.core_units == core
    assert with_entry.center_id == without_entry.center_id


def test_fourth_departure_stays_outside_center_body() -> None:
    values = valid_up_center_lifecycle()
    center = establish_center(values[:3], 0, SourceKind.SEGMENT)
    assert center is not None

    watched, event = advance_center(center, values[3])

    assert event.kind is CenterEventKind.BREAKOUT_WATCH_UP
    assert watched.state is CenterState.ONGOING
    assert watched.pending_leave_unit is values[3]
    assert watched.body_units == values[:3]
    assert values[3] not in watched.body_units


def test_fifth_outside_return_completes_without_joining_body() -> None:
    values = valid_up_center_lifecycle()
    center = establish_center(values[:3], 0, SourceKind.SEGMENT)
    assert center is not None
    watched, _ = advance_center(center, values[3])

    completed, event = advance_center(watched, values[4])

    assert event.kind is CenterEventKind.COMPLETED_UP
    assert completed.state is CenterState.COMPLETED
    assert completed.body_units == values[:3]
    assert completed.completion_leave_unit is values[3]
    assert completed.completion_return_unit is values[4]
    assert values[3] not in completed.body_units
    assert values[4] not in completed.body_units


def test_boundary_touch_departure_and_outside_return_complete_lifecycle() -> None:
    core = (
        unit(0, "down", 115, 100),
        unit(1, "up", 100, 115),
        replace(unit(2, "down", 115, 115), low_tick=105),
    )
    leave = unit(3, "up", 115, 130)
    ret = unit(4, "down", 130, 120)
    center = establish_center(core, 0, SourceKind.SEGMENT)
    assert center is not None
    assert (center.zd_tick, center.zg_tick) == (105, 115)

    watched, watch = advance_center(center, leave)
    completed, completion = advance_center(watched, ret)

    assert watch.kind is CenterEventKind.BREAKOUT_WATCH_UP
    assert watched.pending_leave_unit is leave
    assert leave.low_tick == center.zg_tick
    assert completion.kind is CenterEventKind.COMPLETED_UP
    assert completed.completion_leave_unit is leave
    assert completed.completion_return_unit is ret


def test_failed_leave_folds_only_after_return_reenters_core() -> None:
    values = valid_up_center_lifecycle(return_low_tick=110)
    center = establish_center(values[:3], 0, SourceKind.SEGMENT)
    assert center is not None
    watched, watch = advance_center(center, values[3])
    assert watch.kind is CenterEventKind.BREAKOUT_WATCH_UP

    extended, event = advance_center(watched, values[4])

    assert event.kind is CenterEventKind.EXTENDED
    assert extended.state is CenterState.ONGOING
    assert extended.initial_units == values[:3]
    assert extended.failed_departure_units == (values[3],)
    assert extended.extension_units == (values[4],)
    assert extended.body_units == (*values[:3], values[4])
    assert extended.pending_leave_unit is None
    assert extended.center_id == center.center_id


def test_boundary_departure_failure_keeps_only_return_in_center_body() -> None:
    core = (
        unit(0, "down", 115, 100),
        unit(1, "up", 100, 115),
        replace(unit(2, "down", 115, 115), low_tick=105),
    )
    leave = unit(3, "up", 115, 130)
    ret = unit(4, "down", 130, 110)
    center = establish_center(core, 0, SourceKind.SEGMENT)
    assert center is not None

    watched, _ = advance_center(center, leave)
    extended, event = advance_center(watched, ret)

    assert event.kind is CenterEventKind.EXTENDED
    assert extended.failed_departure_units == (leave,)
    assert extended.extension_units == (ret,)
    assert extended.body_units == (*core, ret)
    assert all(
        max(item.low_tick, center.zd_tick)
        < min(item.high_tick, center.zg_tick)
        for item in extended.body_units
    )
    level = StrictRecursiveEngine(max_levels=1).calculate((*core, leave, ret)).levels[0]
    assert level.trend_types[0].constituent_units == (*core, leave, ret)
    evidence = CenterEvidence.from_center(extended)
    assert evidence.failed_departure_unit_ids == (leave.unit_id,)


def test_boundary_departure_crossing_becomes_new_pending_leave() -> None:
    core = (
        unit(0, "down", 115, 100),
        unit(1, "up", 100, 115),
        replace(unit(2, "down", 115, 115), low_tick=105),
    )
    leave = unit(3, "up", 115, 130)
    crossed = unit(4, "down", 130, 100)
    center = establish_center(core, 0, SourceKind.SEGMENT)
    assert center is not None

    watched, _ = advance_center(center, leave)
    pending, event = advance_center(watched, crossed)

    assert event.kind is CenterEventKind.BREAKOUT_WATCH_DOWN
    assert pending.failed_departure_units == (leave,)
    assert pending.body_units == core
    assert pending.extension_units == ()
    assert pending.pending_leave_unit is crossed
    assert pending.completion_leave_unit is None
    assert pending.completion_return_unit is None
    assert pending.physically_completed is False


def test_live_preview_keeps_failed_boundary_departure_outside_body() -> None:
    core = (
        unit(0, "down", 115, 100),
        unit(1, "up", 100, 115),
        replace(unit(2, "down", 115, 115), low_tick=105),
    )
    leave = replace(
        unit(3, "up", 115, 130),
        locked=False,
        confirmed_at=None,
    )
    ret = replace(
        unit(4, "down", 130, 110),
        locked=False,
        confirmed_at=None,
    )

    result = calculate_centers((*core, leave, ret), 0, SourceKind.SEGMENT)

    assert len(result.previews) == 1
    preview = result.previews[0]
    assert preview.failed_departure_unit_ids == (leave.unit_id,)
    assert preview.unit_ids == (*tuple(item.unit_id for item in core), ret.unit_id)
    assert preview.pending_leave_unit_id is None


def test_recursive_center_uses_the_same_three_completed_unit_core() -> None:
    body = (
        unit(0, "down", 120, 100, source_kind=SourceKind.TREND_TYPE),
        unit(1, "up", 100, 115, source_kind=SourceKind.TREND_TYPE),
        unit(2, "down", 115, 105, source_kind=SourceKind.TREND_TYPE),
    )

    center = establish_center(body, 0, SourceKind.TREND_TYPE)

    assert center is not None
    assert center.entry_unit is None
    assert center.initial_units == body
    assert center.core_units == body
