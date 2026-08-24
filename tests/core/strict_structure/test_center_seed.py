"""Three-unit center seeds and their causal lifecycle contracts."""

from dataclasses import replace

import pytest

from chanlun.core.strict_structure.center_machine import (
    advance_center,
    calculate_centers,
    establish_center,
    establish_center_preview,
    forming_preview,
)
from chanlun.core.strict_structure.models import (
    CenterEventKind,
    CenterPreviewState,
    CenterState,
    SourceKind,
)
from tests.core.strict_structure.helpers import (
    TEST_PRICE_BASIS,
    unit,
    valid_up_center_lifecycle,
)


def test_three_locked_segments_establish_frozen_core() -> None:
    values = valid_up_center_lifecycle()

    center = establish_center(values[:3], 0, SourceKind.SEGMENT)

    assert center is not None
    assert center.state is CenterState.ONGOING
    assert center.entry_unit is None
    assert center.initial_units == values[:3]
    assert center.body_units == values[:3]
    assert center.core_units == values[:3]
    assert center.pending_leave_unit is None
    assert (center.zd_tick, center.zg_tick) == (105, 115)
    assert center.established_at == values[2].confirmed_at
    assert center.price_basis_revision == TEST_PRICE_BASIS


def test_fourth_segment_is_departure_and_fifth_confirms_third_class() -> None:
    values = valid_up_center_lifecycle()
    center = establish_center(values[:3], 0, SourceKind.SEGMENT)
    assert center is not None

    leaving, watch = advance_center(center, values[3])
    completed, event = advance_center(leaving, values[4])

    assert watch.kind is CenterEventKind.BREAKOUT_WATCH_UP
    assert leaving.pending_leave_unit is values[3]
    assert leaving.body_units == values[:3]
    assert event.kind is CenterEventKind.COMPLETED_UP
    assert completed.state is CenterState.COMPLETED
    assert completed.completion_leave_unit is values[3]
    assert completed.completion_return_unit is values[4]
    assert completed.core_units == values[:3]


def test_external_entry_is_optional_divergence_evidence() -> None:
    values = valid_up_center_lifecycle()
    entry = unit(-1, "up", 90, values[0].start_tick)

    without_entry = establish_center(values[:3], 0, SourceKind.SEGMENT)
    with_entry = establish_center(
        values[:3],
        0,
        SourceKind.SEGMENT,
        entry_unit=entry,
    )

    assert without_entry is not None and without_entry.entry_unit is None
    assert with_entry is not None and with_entry.entry_unit is entry
    assert with_entry.core_units == without_entry.core_units
    assert with_entry.entry_unit not in with_entry.body_units


def test_three_segments_without_common_overlap_are_rejected() -> None:
    values = (
        unit(0, "down", 130, 100),
        unit(1, "up", 100, 150),
        unit(2, "down", 150, 140),
    )

    assert establish_center(values, 0, SourceKind.SEGMENT) is None


def test_unlocked_third_segment_remains_forming_preview() -> None:
    values = valid_up_center_lifecycle()
    active = values[:2] + (
        replace(values[2], locked=False, confirmed_at=None),
    )

    assert establish_center(active, 0, SourceKind.SEGMENT) is None
    preview = establish_center_preview(active, 0, SourceKind.SEGMENT)

    assert preview is not None
    assert preview.state is CenterPreviewState.FORMING
    assert preview.entry_unit_id is None
    assert preview.unit_ids == tuple(item.unit_id for item in active)
    result = calculate_centers(active, 0, SourceKind.SEGMENT)
    assert result.centers == ()
    assert preview in result.previews


def test_unlocked_fourth_departure_projects_beside_formal_center() -> None:
    values = valid_up_center_lifecycle()
    active = values[:3] + (
        replace(values[3], locked=False, confirmed_at=None),
    )

    result = calculate_centers(active, 0, SourceKind.SEGMENT)

    assert len(result.centers) == 1
    assert result.centers[0].core_units == values[:3]
    assert any(
        preview.pending_leave_unit_id == active[3].unit_id
        for preview in result.previews
    )


def test_three_segment_touch_is_observation_never_formal_line_center() -> None:
    values = (
        unit(0, "down", 130, 120),
        unit(1, "up", 120, 140),
        unit(2, "down", 140, 130),
    )

    preview = forming_preview(values, 0, SourceKind.SEGMENT)

    assert preview is not None
    assert preview.state is CenterPreviewState.TOUCH_ONLY
    assert preview.zd_tick == preview.zg_tick == 130
    assert establish_center(values, 0, SourceKind.SEGMENT) is None


def test_center_identity_includes_price_basis_revision() -> None:
    values = valid_up_center_lifecycle()[:3]
    original = establish_center(values, 0, SourceKind.SEGMENT)
    rebased_values = tuple(
        replace(item, price_basis_revision="post-action") for item in values
    )
    rebased = establish_center(rebased_values, 0, SourceKind.SEGMENT)

    assert original is not None and rebased is not None
    assert original.center_id != rebased.center_id


def test_seed_rejects_mixed_price_basis() -> None:
    values = valid_up_center_lifecycle()[:3]
    mixed = values[:-1] + (
        replace(values[-1], price_basis_revision="post-action"),
    )

    with pytest.raises(ValueError, match="seed price basis mismatch"):
        establish_center(mixed, 0, SourceKind.SEGMENT)
    with pytest.raises(ValueError, match="seed price basis mismatch"):
        forming_preview(mixed, 0, SourceKind.SEGMENT)
