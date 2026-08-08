"""Physical five-role maturity and recursive three-trend contracts."""

from dataclasses import replace

from chanlun.core.strict_structure.center_machine import (
    calculate_centers,
    establish_center,
)
from chanlun.core.strict_structure.models import (
    CenterPreviewState,
    CenterState,
    SourceKind,
)
from tests.core.strict_structure.helpers import unit, valid_five_up_exit


def _extended_center(values):
    center = establish_center(values, 0, SourceKind.SEGMENT)
    assert center is not None
    return center


def test_segment_center_requires_exactly_five_establishment_segments() -> None:
    values = valid_five_up_exit()
    internal = establish_center(values[:4], 0, SourceKind.SEGMENT)

    assert internal is None
    compatible = establish_center(values, 0, SourceKind.SEGMENT)
    assert compatible is not None
    assert compatible.initial_units == values[1:4]
    assert compatible.extension_units == ()
    assert compatible.initial_exit_unit is values[4]
    assert compatible.pending_leave_unit is values[4]
    assert compatible.has_minimum_physical_roles is True
    assert compatible.establishment_units == values


def test_previous_leave_is_earliest_possible_next_entry() -> None:
    values = valid_five_up_exit() + (
        unit(5, "down", 130, 120),
        unit(6, "up", 120, 140),
        unit(7, "down", 140, 125),
        unit(8, "up", 125, 135),
        unit(9, "down", 135, 132),
    )

    result = calculate_centers(values, 0, SourceKind.SEGMENT)

    assert len(result.centers) == 2
    first_center, second_center = result.centers
    assert first_center.completion_leave_unit is values[4]
    assert first_center.completion_return_unit is values[5]
    assert second_center.entry_unit is first_center.completion_leave_unit
    assert second_center.entry_unit is values[4]
    assert second_center.core_units == values[5:8]
    assert second_center.completion_leave_unit is values[8]
    assert second_center.completion_return_unit is values[9]
    assert second_center.entry_unit.market_end == second_center.core_units[0].market_start
    assert second_center.entry_unit not in second_center.body_units
    assert first_center.completion_leave_unit not in second_center.body_units


def test_shared_leave_entry_survives_opposite_side_departure() -> None:
    """Regression: a reversal exit must not erase the shared-boundary center."""

    values = valid_five_up_exit() + (
        unit(5, "down", 130, 120),
        unit(6, "up", 120, 128),
        unit(7, "down", 128, 122),
        unit(8, "up", 122, 135),
        unit(9, "down", 135, 125),
        unit(10, "up", 125, 132),
        unit(11, "down", 132, 118),
        unit(12, "up", 118, 120),
    )

    result = calculate_centers(values, 0, SourceKind.SEGMENT)

    assert len(result.centers) == 2
    first, second = result.centers
    assert first.completion_leave_unit is values[4]
    assert second.entry_unit is first.completion_leave_unit
    assert second.entry_unit is values[4]
    assert second.core_units == values[5:8]
    assert second.completion_leave_unit is values[11]
    assert second.completion_leave_unit.direction == "down"
    assert second.entry_unit.direction == "up"
    assert second.completion_return_unit is values[12]
    assert second.state is CenterState.COMPLETED


def test_shared_leave_entry_is_not_replaced_by_later_faster_candidate() -> None:
    values = (
        unit(0, "down", 120, 90),
        unit(1, "up", 90, 110),
        unit(2, "down", 110, 100),
        unit(3, "up", 100, 110),
        unit(4, "down", 110, 105),
        unit(5, "up", 105, 140),
        unit(6, "down", 140, 120),
        unit(7, "up", 120, 150),
        unit(8, "down", 150, 115),
        unit(9, "up", 115, 150),
        unit(10, "down", 150, 110),
        unit(11, "up", 110, 145),
        unit(12, "down", 145, 135),
        unit(13, "up", 135, 160),
        unit(14, "down", 160, 100),
        unit(15, "up", 100, 130),
        unit(16, "down", 130, 115),
        unit(17, "up", 115, 119),
    )

    result = calculate_centers(values, 0, SourceKind.SEGMENT)

    assert len(result.centers) == 2
    first, second = result.centers
    assert first.completion_leave_unit is values[5]
    assert first.completion_return_unit is values[6]
    assert second.entry_unit is first.completion_leave_unit
    assert second.entry_unit is values[5]
    assert second.completion_leave_unit is values[16]
    assert second.completion_return_unit is values[17]
    assert all(center.entry_unit is not values[9] for center in result.centers)


def test_first_mature_center_after_third_class_point_is_not_replaced() -> None:
    """A faster internal completion cannot rewrite the post-third-point seed."""

    endpoints = (
        (388532, 373334),
        (373334, 389274),
        (389274, 385597),
        (385597, 387669),
        (387669, 384895),
        (384895, 389996),
        (389996, 377453),
        (377453, 386611),
        (386611, 380954),
        (380954, 393658),
        (393658, 380011),
        (380011, 391844),
        (391844, 385112),
        (385112, 393105),
        (393105, 383537),
        (383537, 391932),
        (391932, 387753),
        (387753, 402571),
        (402571, 393704),
        (393704, 398588),
        (398588, 392258),
        (392258, 402494),
        (402494, 398068),
        (398068, 403408),
        (403408, 392659),
        (392659, 396464),
        (396464, 381658),
        (381658, 391446),
    )
    values = tuple(
        unit(
            index,
            "up" if end > start else "down",
            start,
            end,
        )
        for index, (start, end) in enumerate(endpoints)
    )

    first_mature = calculate_centers(
        values[:22],
        0,
        SourceKind.SEGMENT,
    )
    result = calculate_centers(values, 0, SourceKind.SEGMENT)

    assert len(result.centers) == 2
    first, second = result.centers
    assert first.completion_leave_unit is values[15]
    assert first.completion_return_unit is values[16]
    # Starts 15 and 16 are invalid; 17 is the first valid five-unit seed in
    # the suffix. The old look-ahead replaced it with start 19 because that
    # narrower candidate completed two units sooner.
    assert second.entry_unit is values[17]
    assert second.core_units == values[18:21]
    assert second.completion_leave_unit is values[26]
    assert second.completion_return_unit is values[27]
    assert first_mature.centers[1].entry_unit is values[17]
    assert first_mature.centers[1].state is CenterState.ONGOING
    assert first_mature.centers[1].center_id == second.center_id

    live_values = tuple(
        item
        if index < 21
        else replace(item, locked=False, confirmed_at=None)
        for index, item in enumerate(values[:25])
    )
    live = calculate_centers(live_values, 0, SourceKind.SEGMENT)
    forming = [
        preview
        for preview in live.previews
        if preview.state is CenterPreviewState.FORMING
    ]
    assert len(forming) == 1
    assert forming[0].entry_unit_id == values[17].unit_id


def test_shared_leave_entry_center_identity_is_prefix_stable() -> None:
    values = valid_five_up_exit() + (
        unit(5, "down", 130, 120),
        unit(6, "up", 120, 140),
        unit(7, "down", 140, 125),
        unit(8, "up", 125, 135),
        unit(9, "down", 135, 132),
    )

    ongoing = calculate_centers(values[:9], 0, SourceKind.SEGMENT).centers
    completed = calculate_centers(values, 0, SourceKind.SEGMENT).centers

    assert len(ongoing) == len(completed) == 2
    assert ongoing[1].entry_unit is ongoing[0].completion_leave_unit
    assert ongoing[1].center_id == completed[1].center_id
    assert ongoing[1].state is CenterState.ONGOING
    assert completed[1].state is CenterState.COMPLETED


def test_recursive_center_also_reuses_previous_leave_as_next_entry() -> None:
    values = tuple(
        replace(item, source_kind=SourceKind.TREND_TYPE)
        for item in (
            unit(0, "up", 90, 120),
            unit(1, "down", 120, 100),
            unit(2, "up", 100, 115),
            unit(3, "down", 115, 105),
            unit(4, "up", 105, 130),
            unit(5, "down", 130, 120),
            unit(6, "up", 120, 140),
            unit(7, "down", 140, 125),
            unit(8, "up", 125, 135),
            unit(9, "down", 135, 132),
        )
    )

    result = calculate_centers(values, 0, SourceKind.TREND_TYPE)

    assert len(result.centers) == 2
    first_center, second_center = result.centers
    assert first_center.completion_leave_unit is values[4]
    assert first_center.completion_return_unit is values[5]
    assert second_center.entry_unit is first_center.completion_leave_unit
    assert second_center.core_units == values[5:8]
    assert second_center.completion_leave_unit is values[8]
    assert second_center.completion_return_unit is values[9]


def test_first_three_body_units_alone_freeze_core() -> None:
    values = valid_five_up_exit()
    center = _extended_center(values)

    assert center.core_units == values[1:4]
    assert center.initial_units == values[1:4]
    assert center.body_units == values[1:4]
    assert center.entry_unit is values[0]
    assert center.established_market_time == values[4].market_end


def test_unlocked_fifth_role_is_not_a_public_physical_center() -> None:
    values = valid_five_up_exit()
    active = values[:4] + (
        replace(values[4], locked=False, confirmed_at=None),
    )

    result = calculate_centers(active, 0, SourceKind.SEGMENT)
    assert result.centers == ()


def test_stroke_observation_uses_the_same_five_role_gate() -> None:
    values = tuple(
        replace(item, source_kind=SourceKind.STROKE_OBSERVATION)
        for item in valid_five_up_exit()
    )
    internal = establish_center(values[:4], 0, SourceKind.STROKE_OBSERVATION)

    assert internal is None
    mature = establish_center(values, 0, SourceKind.STROKE_OBSERVATION)
    assert mature is not None
    assert mature.has_minimum_physical_roles is True


def test_recursive_center_still_uses_three_completed_trend_types() -> None:
    entry = unit(-1, "down", 130, 90, source_kind=SourceKind.TREND_TYPE)
    body = (
        unit(0, "up", 90, 120, source_kind=SourceKind.TREND_TYPE),
        unit(1, "down", 120, 100, source_kind=SourceKind.TREND_TYPE),
        unit(2, "up", 100, 115, source_kind=SourceKind.TREND_TYPE),
    )

    center = establish_center(body, 0, SourceKind.TREND_TYPE, entry_unit=entry)
    assert center is not None
    assert center.entry_unit is entry
    assert center.initial_units == body
    assert center.core_units == body


def test_segment_center_first_appears_on_fifth_locked_stream_unit() -> None:
    values = valid_five_up_exit()
    prefix_counts = [
        len(calculate_centers(values[:end], 0, SourceKind.SEGMENT).centers)
        for end in range(1, len(values) + 1)
    ]
    assert prefix_counts == [0, 0, 0, 0, 1]
