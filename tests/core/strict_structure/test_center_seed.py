"""Source-specific center maturity and frozen-core seed contracts."""

from dataclasses import replace

import pytest

from chanlun.core.strict_structure.center_machine import (
    calculate_centers,
    establish_center,
    establish_center_preview,
    forming_preview,
)
from chanlun.core.strict_structure.models import (
    CenterPreviewState,
    CenterState,
    SourceKind,
)
from tests.core.strict_structure.helpers import (
    TEST_PRICE_BASIS,
    unit,
    valid_five_up_exit,
)


def _establish(values, source_kind=SourceKind.SEGMENT):
    return establish_center(
        values[1:],
        0,
        source_kind,
        entry_unit=values[0],
    )


def test_five_segment_window_preserves_middle_three_core():
    values = valid_five_up_exit()
    center = _establish(values)

    assert center is not None
    assert center.state is CenterState.ONGOING
    assert center.entry_unit is values[0]
    assert center.initial_units == values[1:4]
    assert center.body_units == values[1:4]
    assert center.core_units == values[1:4]
    assert center.pending_leave_unit is values[4]
    assert center.initial_exit_unit is values[4]
    assert (center.zd_tick, center.zg_tick) == (105, 115)
    assert center.established_at == values[4].confirmed_at
    assert center.price_basis_revision == TEST_PRICE_BASIS


def test_four_segments_are_not_internal_center_and_five_establish():
    values = valid_five_up_exit()
    internal = establish_center(
        values[1:4], 0, SourceKind.SEGMENT, entry_unit=values[0]
    )
    mature = establish_center(values, 0, SourceKind.SEGMENT)
    assert internal is None
    assert mature is not None and mature.has_minimum_physical_roles is True


def test_entry_without_positive_core_overlap_is_rejected():
    values = (
        unit(-1, "up", 80, 80),
        unit(0, "down", 80, 60),
        unit(1, "up", 60, 75),
        unit(2, "down", 75, 65),
        unit(3, "up", 65, 90),
    )
    center = _establish(values)

    assert center is None


@pytest.mark.parametrize("bad_role", ("entry", "leave"))
def test_entry_and_leave_must_positively_overlap_frozen_core(bad_role):
    values = list(valid_five_up_exit())
    if bad_role == "entry":
        values[0] = replace(
            values[0], start_tick=80, end_tick=100, low_tick=80, high_tick=100
        )
        values[1] = replace(values[1], start_tick=100)
    else:
        values[3] = replace(values[3], end_tick=115)
        values[4] = replace(
            values[4], start_tick=115, low_tick=115
        )
    assert _establish(tuple(values)) is None


def test_unlocked_later_extension_is_preview_beside_existing_center():
    values = valid_five_up_exit()
    active = values[:-1] + (
        replace(values[-1], locked=False, confirmed_at=None),
    )

    assert _establish(active) is None
    preview = establish_center_preview(
        active[1:],
        0,
        SourceKind.SEGMENT,
        entry_unit=active[0],
    )
    assert preview is not None
    assert preview.state is CenterPreviewState.FORMING
    assert preview.entry_unit_id == active[0].unit_id
    assert preview.unit_ids == tuple(item.unit_id for item in active[1:4])
    assert preview.pending_leave_unit_id == active[4].unit_id
    result = calculate_centers(active, 0, SourceKind.SEGMENT)
    assert result.centers == ()
    assert preview in result.previews


def test_three_body_touch_is_observation_never_formal_line_center():
    values = (
        unit(-1, "down", 130, 120),
        replace(unit(0, "up", 120, 120), high_tick=130),
        unit(1, "down", 120, 100),
        unit(2, "up", 100, 120),
        unit(3, "down", 120, 110),
    )
    preview = forming_preview(
        values,
        0,
        SourceKind.SEGMENT,
    )
    assert preview is not None
    assert preview.state is CenterPreviewState.TOUCH_ONLY
    assert preview.zd_tick == preview.zg_tick == 120


def test_center_identity_includes_price_basis_revision():
    values = valid_five_up_exit()
    original = _establish(values)
    rebased_values = tuple(
        replace(item, price_basis_revision="post-action-v2") for item in values
    )
    rebased = _establish(rebased_values)
    assert original is not None and rebased is not None
    assert original.center_id != rebased.center_id


def test_seed_rejects_mixed_basis_in_entry_or_body():
    values = valid_five_up_exit()
    mixed = values[:-1] + (
        replace(values[-1], price_basis_revision="post-action-v2"),
    )
    with pytest.raises(ValueError, match="seed price basis mismatch"):
        _establish(mixed)
    with pytest.raises(ValueError, match="seed price basis mismatch"):
        forming_preview(
            mixed[1:], 0, SourceKind.SEGMENT, entry_unit=mixed[0]
        )
