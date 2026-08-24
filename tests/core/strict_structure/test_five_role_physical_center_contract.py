"""物理中枢必须具备进入、三段核心和独立离开五个角色。"""

from dataclasses import replace

import pytest

from chanlun.cl_utils.strict_chart import strict_center_to_chart_dict
from chanlun.core.strict_structure.center_machine import (
    advance_center,
    calculate_centers,
    establish_center,
)
from chanlun.core.strict_structure.models import (
    CenterEventKind,
    CenterState,
    SourceKind,
)
from tests.core.strict_structure.helpers import unit, valid_five_up_exit


def _five_role_center():
    return (
        unit(0, "up", 90, 120),
        unit(1, "down", 120, 100),
        unit(2, "up", 100, 115),
        unit(3, "down", 115, 105),
        unit(4, "up", 105, 130),
    )


def test_three_or_four_segments_never_publish_a_physical_center() -> None:
    values = _five_role_center()

    assert calculate_centers(values[:3], 0, SourceKind.SEGMENT).centers == ()
    assert calculate_centers(values[:4], 0, SourceKind.SEGMENT).centers == ()


def test_five_overlapping_roles_publish_entry_core_and_leave() -> None:
    values = _five_role_center()

    result = calculate_centers(values, 0, SourceKind.SEGMENT)

    assert len(result.centers) == 1
    center = result.centers[0]
    assert center.entry_unit is values[0]
    assert center.core_units == values[1:4]
    assert center.body_units == values[1:4]
    assert center.pending_leave_unit is values[4]
    assert center.establishment_units == values
    assert center.established_at == values[4].confirmed_at
    assert (center.zd_tick, center.zg_tick) == (105, 115)
    assert all(
        max(item.low_tick, center.zd_tick)
        < min(item.high_tick, center.zg_tick)
        for item in values
    )


def test_physical_center_identity_includes_entry_and_establishment_leave() -> None:
    values = _five_role_center()
    original = establish_center(values, 0, SourceKind.SEGMENT)
    changed_entry = establish_center(
        (replace(values[0], unit_id="other-entry"), *values[1:]),
        0,
        SourceKind.SEGMENT,
    )
    changed_leave = establish_center(
        (*values[:4], replace(values[4], unit_id="other-leave")),
        0,
        SourceKind.SEGMENT,
    )

    assert original is not None
    assert changed_entry is not None
    assert changed_leave is not None
    assert len({original.center_id, changed_entry.center_id, changed_leave.center_id}) == 3


def test_establishment_leave_cannot_be_detached_from_lifecycle_ownership() -> None:
    center = calculate_centers(
        _five_role_center(), 0, SourceKind.SEGMENT
    ).centers[0]

    with pytest.raises(ValueError, match="exactly one lifecycle role"):
        replace(center, pending_leave_unit=None)


def test_fifth_role_must_be_an_independent_departure() -> None:
    values = _five_role_center()
    inside = replace(
        values[4],
        end_tick=110,
        high_tick=110,
    )

    assert calculate_centers(
        values[:4] + (inside,), 0, SourceKind.SEGMENT
    ).centers == ()


def test_entry_and_leave_must_both_overlap_the_middle_core() -> None:
    values = _five_role_center()
    disjoint_entry = replace(
        values[0],
        start_tick=80,
        end_tick=100,
        low_tick=80,
        high_tick=100,
    )
    connected_first_core = replace(values[1], start_tick=100)

    assert calculate_centers(
        (disjoint_entry, connected_first_core, *values[2:]),
        0,
        SourceKind.SEGMENT,
    ).centers == ()


def test_chart_contract_exposes_five_roles_and_middle_three_core() -> None:
    values = _five_role_center()
    center = calculate_centers(values, 0, SourceKind.SEGMENT).centers[0]

    payload = strict_center_to_chart_dict(center)

    assert payload["minimum_lifecycle_role_count"] == 5
    assert payload["lifecycle_role_count"] == 5
    assert payload["overlap_component_count"] == 5
    assert payload["establishment_component_count"] == 5
    assert payload["establishment_segment_ids"] == [
        item.unit_id for item in values
    ]
    assert payload["entry_unit_id"] == values[0].unit_id
    assert payload["establishment_leave_unit_id"] == values[4].unit_id
    assert payload["core_unit_ids"] == [item.unit_id for item in values[1:4]]
    assert payload["display_range"] == {
        "start_role": "middle_three_first_start",
        "end_role": "middle_three_last_end",
        "includes_entry": False,
        "includes_leave": False,
        "price_core_source": "middle_three_intersection",
    }


def _two_completed_centers():
    return valid_five_up_exit() + (
        unit(5, "down", 130, 120),
        unit(6, "up", 120, 140),
        unit(7, "down", 140, 125),
        unit(8, "up", 125, 135),
        unit(9, "down", 135, 132),
    )


def test_previous_leave_is_reused_as_next_physical_entry() -> None:
    values = _two_completed_centers()

    first, second = calculate_centers(values, 0, SourceKind.SEGMENT).centers

    assert first.completion_leave_unit is values[4]
    assert first.completion_return_unit is values[5]
    assert second.entry_unit is first.completion_leave_unit
    assert second.core_units == values[5:8]
    assert second.completion_leave_unit is values[8]
    assert second.completion_return_unit is values[9]


def test_second_center_identity_is_stable_from_five_roles_to_completion() -> None:
    values = _two_completed_centers()

    ongoing = calculate_centers(values[:9], 0, SourceKind.SEGMENT).centers
    completed = calculate_centers(values, 0, SourceKind.SEGMENT).centers

    assert len(ongoing) == len(completed) == 2
    assert ongoing[1].state is CenterState.ONGOING
    assert completed[1].state is CenterState.COMPLETED
    assert ongoing[1].center_id == completed[1].center_id
    assert ongoing[1].establishment_units == values[4:9]


def test_unlocked_fifth_role_stays_preview_only() -> None:
    values = valid_five_up_exit()
    live = values[:4] + (
        replace(values[4], locked=False, confirmed_at=None),
    )

    result = calculate_centers(live, 0, SourceKind.SEGMENT)

    assert result.centers == ()
    assert len(result.previews) == 1
    assert result.previews[0].entry_unit_id == values[0].unit_id
    assert result.previews[0].formal_center_id is not None


def test_stroke_observation_uses_same_five_role_gate() -> None:
    values = tuple(
        replace(item, source_kind=SourceKind.STROKE_OBSERVATION)
        for item in valid_five_up_exit()
    )

    assert establish_center(
        values[:4], 0, SourceKind.STROKE_OBSERVATION
    ) is None
    assert establish_center(values, 0, SourceKind.STROKE_OBSERVATION) is not None


def test_recursive_center_still_uses_three_completed_trend_types() -> None:
    entry = unit(-1, "down", 130, 90, source_kind=SourceKind.TREND_TYPE)
    body = (
        unit(0, "up", 90, 120, source_kind=SourceKind.TREND_TYPE),
        unit(1, "down", 120, 100, source_kind=SourceKind.TREND_TYPE),
        unit(2, "up", 100, 115, source_kind=SourceKind.TREND_TYPE),
    )

    center = establish_center(
        body, 0, SourceKind.TREND_TYPE, entry_unit=entry
    )
    without_entry = establish_center(body, 0, SourceKind.TREND_TYPE)

    assert center is not None and without_entry is not None
    assert center.entry_unit is entry
    assert center.core_units == body
    assert center.establishment_leave_unit is None
    assert center.center_id == without_entry.center_id


def test_sixth_outside_return_completes_without_joining_body() -> None:
    values = valid_five_up_exit()
    center = establish_center(values, 0, SourceKind.SEGMENT)
    assert center is not None
    outside_return = unit(5, "down", values[4].end_tick, 120)

    completed, event = advance_center(center, outside_return)

    assert event.kind is CenterEventKind.COMPLETED_UP
    assert completed.completion_leave_unit is values[4]
    assert completed.completion_return_unit is outside_return
    assert completed.body_units == values[1:4]


def test_failed_establishment_leave_is_retained_as_history() -> None:
    values = valid_five_up_exit()
    center = establish_center(values, 0, SourceKind.SEGMENT)
    assert center is not None
    reentry = unit(5, "down", values[4].end_tick, 110)

    extended, event = advance_center(center, reentry)

    assert event.kind is CenterEventKind.EXTENDED
    assert extended.establishment_leave_unit is values[4]
    assert extended.failed_departure_units == (values[4],)
    assert extended.extension_units == (reentry,)
    assert extended.pending_leave_unit is None


def test_failed_leave_crossing_opposite_boundary_rearms_departure() -> None:
    values = valid_five_up_exit()
    center = establish_center(values, 0, SourceKind.SEGMENT)
    assert center is not None
    crossing_return = unit(5, "down", values[4].end_tick, 100)

    rearmed, event = advance_center(center, crossing_return)

    assert event.kind is CenterEventKind.BREAKOUT_WATCH_DOWN
    assert rearmed.failed_departure_units == (values[4],)
    assert rearmed.pending_leave_unit is crossing_return
    assert rearmed.body_units == values[1:4]


def test_live_preview_retains_failed_establishment_leave_outside_body() -> None:
    values = valid_five_up_exit()
    live_leave = replace(values[4], locked=False, confirmed_at=None)
    live_return = replace(
        unit(5, "down", live_leave.end_tick, 110),
        locked=False,
        confirmed_at=None,
    )

    result = calculate_centers(
        (*values[:4], live_leave, live_return), 0, SourceKind.SEGMENT
    )

    assert result.centers == ()
    assert len(result.previews) == 1
    preview = result.previews[0]
    assert preview.failed_departure_unit_ids == (live_leave.unit_id,)
    assert preview.unit_ids == tuple(
        item.unit_id for item in (*values[1:4], live_return)
    )
