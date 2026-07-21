from dataclasses import replace
from datetime import timedelta

import pytest

from chanlun.core.strict_structure.center_machine import calculate_centers
from chanlun.core.strict_structure.models import (
    CenterEventKind,
    CenterPreviewState,
    CenterState,
    SourceKind,
)
from tests.core.strict_structure.helpers import (
    TEST_PRICE_BASIS,
    unit,
    valid_five_up_exit,
)


def _two_completed_centers():
    return valid_five_up_exit() + (
        unit(5, "down", 130, 120),
        unit(6, "up", 120, 140),
        unit(7, "down", 140, 125),
        unit(8, "up", 125, 135),
        unit(9, "down", 135, 110),
        unit(10, "up", 110, 120),
    )


def test_scan_with_four_locked_units_has_no_formal_center():
    result = calculate_centers(
        valid_five_up_exit()[:4],
        0,
        SourceKind.SEGMENT,
    )
    assert result.centers == ()
    assert result.locked_unit_count == 4
    assert result.previews
    assert all(not hasattr(item, "center_id") for item in result.previews)


def test_scan_stops_formal_input_at_first_unlocked_unit():
    initial = valid_five_up_exit()
    values = initial + (
        replace(unit(5, "down", 130, 120), locked=False, confirmed_at=None),
    )
    result = calculate_centers(values, 0, SourceKind.SEGMENT)
    assert len(result.centers) == 1
    assert result.centers[0].state is CenterState.ONGOING
    assert result.centers[0].pending_leave_unit is initial[-1]
    assert result.locked_unit_count == 5
    assert result.previews
    assert all(item.locked for item in result.centers[0].body_units)


def test_completion_return_can_start_next_initial_five_without_body_duplication():
    values = _two_completed_centers()
    result = calculate_centers(values, 0, SourceKind.SEGMENT)
    assert len(result.centers) == 2
    first, second = result.centers
    assert first.state is CenterState.COMPLETED
    assert second.state is CenterState.COMPLETED
    assert first.completion_return_unit is second.entry_unit
    assert first.completion_return_unit not in first.body_units
    assert not set(item.unit_id for item in first.body_units) & set(
        item.unit_id for item in second.body_units
    )


def test_scan_emits_establish_extend_watch_complete_events_in_order():
    values = valid_five_up_exit() + (
        unit(5, "down", 130, 110),
        unit(6, "up", 110, 135),
        unit(7, "down", 135, 120),
    )
    result = calculate_centers(values, 0, SourceKind.SEGMENT)
    assert [item.kind for item in result.events] == [
        CenterEventKind.ESTABLISHED,
        CenterEventKind.EXTENDED,
        CenterEventKind.BREAKOUT_WATCH_UP,
        CenterEventKind.COMPLETED_UP,
    ]
    assert result.centers[0].state is CenterState.COMPLETED


def test_scan_preserves_ongoing_center_when_later_geometry_cannot_extend():
    values = valid_five_up_exit() + (
        unit(5, "down", 130, 95),
        unit(6, "up", 95, 100),
    )
    result = calculate_centers(values, 0, SourceKind.SEGMENT)
    assert len(result.centers) == 1
    assert result.centers[0].state is CenterState.ONGOING
    assert result.centers[0].extension_units == (values[5],)
    assert values[6] not in result.centers[0].body_units


def test_scan_can_find_new_center_after_an_abandoned_ongoing_center():
    values = valid_five_up_exit() + (
        unit(5, "down", 130, 95),
        unit(6, "up", 95, 100),
        unit(7, "down", 100, 96),
        unit(8, "up", 96, 99),
        unit(9, "down", 99, 97),
        unit(10, "up", 97, 105),
        unit(11, "down", 105, 101),
    )
    result = calculate_centers(values, 0, SourceKind.SEGMENT)
    assert [item.state for item in result.centers] == [
        CenterState.ONGOING,
        CenterState.COMPLETED,
    ]
    assert result.centers[1].entry_unit is values[6]


def test_zero_width_middle_core_is_touch_only_not_formal_center():
    values = (
        unit(0, "up", 90, 120),
        unit(1, "down", 120, 100),
        unit(2, "up", 100, 130),
        unit(3, "down", 130, 120),
        unit(4, "up", 120, 140),
    )
    result = calculate_centers(values, 0, SourceKind.SEGMENT)
    assert result.centers == ()
    touch = [
        item
        for item in result.previews
        if item.state is CenterPreviewState.TOUCH_ONLY
    ]
    assert len(touch) == 1
    assert touch[0].zd_tick == touch[0].zg_tick == 120


def test_scan_rejects_locked_unit_after_preview_tail():
    values = list(valid_five_up_exit())
    values[3] = replace(values[3], locked=False, confirmed_at=None)
    with pytest.raises(ValueError, match="locked units must form a prefix"):
        calculate_centers(tuple(values), 0, SourceKind.SEGMENT)


def _invalid_sequences():
    base = valid_five_up_exit()
    duplicate = (
        base[0],
        replace(base[1], unit_id=base[0].unit_id),
        *base[2:],
    )
    same_direction = (
        base[0],
        replace(base[1], direction="up", end_tick=base[1].start_tick),
        *base[2:],
    )
    disconnected = (
        base[0],
        replace(
            base[1],
            start_tick=base[1].start_tick - 1,
            high_tick=base[1].high_tick + 1,
        ),
        *base[2:],
    )
    overlapping = (
        base[0],
        replace(base[1], market_start=base[0].market_end - timedelta(minutes=1)),
        *base[2:],
    )
    wrong_level = (
        base[0],
        replace(base[1], structural_level=1),
        *base[2:],
    )
    mixed_basis = (
        *base[:-1],
        replace(base[-1], price_basis_revision="post-action-v2"),
    )
    return (
        (duplicate, "unit ids must be unique"),
        (same_direction, "unit directions must alternate"),
        (disconnected, "adjacent unit prices must connect"),
        (overlapping, "unit market intervals must not overlap"),
        (wrong_level, "unit level/source mismatch"),
        (mixed_basis, "unit price basis mismatch"),
    )


@pytest.mark.parametrize("values,message", _invalid_sequences())
def test_scan_rejects_non_continuous_constituent_sequence(values, message):
    with pytest.raises(ValueError, match=message):
        calculate_centers(values, 0, SourceKind.SEGMENT)


def test_level_result_carries_the_single_input_price_basis():
    result = calculate_centers(valid_five_up_exit(), 0, SourceKind.SEGMENT)
    assert result.price_basis_revision == TEST_PRICE_BASIS
