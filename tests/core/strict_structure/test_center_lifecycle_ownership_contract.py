"""Causal center ownership across establishment, completion, and restart."""

from dataclasses import replace

import pytest

from chanlun.core.strict_structure.center_machine import calculate_centers, establish_center
from chanlun.core.strict_structure.models import CenterState, SourceKind
from tests.core.strict_structure.helpers import unit, valid_up_center_lifecycle


def _two_completed_centers():
    return valid_up_center_lifecycle() + (
        unit(5, "up", 120, 140),
        unit(6, "down", 140, 125),
        unit(7, "up", 125, 145),
        unit(8, "down", 145, 135),
    )


@pytest.mark.parametrize(
    "source_kind",
    (SourceKind.SEGMENT, SourceKind.STROKE_OBSERVATION),
)
def test_every_source_layer_establishes_from_three_locked_units(source_kind) -> None:
    values = tuple(
        replace(item, source_kind=source_kind)
        for item in valid_up_center_lifecycle()[:3]
    )

    center = establish_center(values, 0, source_kind)

    assert center is not None
    assert center.entry_unit is None
    assert center.initial_units == values
    assert center.established_market_time == values[2].market_end


def test_previous_leave_is_only_optional_entry_for_next_core() -> None:
    values = _two_completed_centers()

    first, second = calculate_centers(
        values,
        0,
        SourceKind.SEGMENT,
    ).centers

    assert first.completion_leave_unit is values[3]
    assert first.completion_return_unit is values[4]
    assert second.entry_unit is first.completion_leave_unit
    assert second.initial_units == values[4:7]
    assert second.entry_unit not in second.body_units


def test_optional_restart_entry_does_not_change_second_center_identity() -> None:
    values = _two_completed_centers()
    scanned = calculate_centers(values, 0, SourceKind.SEGMENT).centers[1]
    without_entry = establish_center(values[4:7], 0, SourceKind.SEGMENT)
    with_entry = establish_center(
        values[4:7],
        0,
        SourceKind.SEGMENT,
        entry_unit=values[3],
    )

    assert without_entry is not None
    assert with_entry is not None
    assert scanned.center_id == without_entry.center_id == with_entry.center_id
    assert scanned.initial_units == without_entry.initial_units == values[4:7]


def test_second_center_identity_is_stable_from_three_core_to_completion() -> None:
    values = _two_completed_centers()

    established = calculate_centers(
        values[:7],
        0,
        SourceKind.SEGMENT,
    ).centers
    completed = calculate_centers(
        values,
        0,
        SourceKind.SEGMENT,
    ).centers

    assert len(established) == len(completed) == 2
    assert established[0] == completed[0]
    assert established[1].state is CenterState.ONGOING
    assert completed[1].state is CenterState.COMPLETED
    assert established[1].center_id == completed[1].center_id
    assert established[1].initial_units == completed[1].initial_units == values[4:7]


def test_earliest_established_core_owns_later_overlapping_extensions() -> None:
    values = valid_up_center_lifecycle()[:3] + (
        unit(3, "up", 105, 110),
        unit(4, "down", 110, 106),
        unit(5, "up", 106, 112),
    )
    seed = establish_center(values[:3], 0, SourceKind.SEGMENT)
    assert seed is not None

    result = calculate_centers(values, 0, SourceKind.SEGMENT)

    assert len(result.centers) == 1
    center = result.centers[0]
    assert center.center_id == seed.center_id
    assert center.initial_units == values[:3]
    assert center.extension_units == values[3:]
    assert center.body_units == values


def test_first_center_snapshot_is_not_rewritten_by_second_center() -> None:
    values = _two_completed_centers()

    first_complete = calculate_centers(
        values[:5],
        0,
        SourceKind.SEGMENT,
    ).centers[0]
    later = calculate_centers(values, 0, SourceKind.SEGMENT).centers

    assert later[0] == first_complete
    assert later[1].entry_unit is first_complete.completion_leave_unit
    assert not {
        item.unit_id for item in later[0].body_units
    }.intersection(item.unit_id for item in later[1].body_units)


def test_unlocked_fourth_unit_cannot_replace_formal_three_unit_owner() -> None:
    values = valid_up_center_lifecycle()
    active = values[:3] + (
        replace(values[3], locked=False, confirmed_at=None),
    )

    result = calculate_centers(active, 0, SourceKind.SEGMENT)

    assert len(result.centers) == 1
    assert result.centers[0].initial_units == values[:3]
    assert result.centers[0].pending_leave_unit is None
    assert len(result.previews) == 1
    assert result.previews[0].unit_ids == tuple(item.unit_id for item in values[:3])
    assert result.previews[0].pending_leave_unit_id == values[3].unit_id
