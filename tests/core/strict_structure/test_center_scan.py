"""Batch scanner invariants for five-role physical centers."""

from dataclasses import replace
from datetime import timedelta
import random

import pytest

from chanlun.core.strict_structure.center_machine import (
    calculate_centers,
    establish_center,
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
    valid_five_up_exit,
)


def _first_completed_then_second_body():
    return (
        *valid_five_up_exit(),
        unit(5, "down", 130, 120),
        unit(6, "up", 120, 175),
        unit(7, "down", 175, 155),
        unit(8, "up", 155, 175),
        unit(9, "down", 175, 160),
        unit(10, "up", 160, 190),
    )


def _two_completed_centers():
    return _first_completed_then_second_body() + (
        unit(11, "down", 190, 177),
    )


def _sh000001_causal_prefix_units():
    endpoints = (
        (412991, 416189),
        (416189, 415153),
        (415153, 416615),
        (416615, 414356),
        (414356, 417227),
        (417227, 416412),
        (416412, 417830),
        (417830, 417327),
        (417327, 418021),
        (418021, 415425),
        (415425, 418306),
        (418306, 417449),
        (417449, 422025),
        (422025, 420230),
        (420230, 423018),
        (423018, 419934),
        (419934, 421606),
    )
    return tuple(
        unit(index, "up" if index % 2 == 0 else "down", start, end)
        for index, (start, end) in enumerate(endpoints)
    )


def test_scanner_needs_five_locked_lifecycle_roles():
    values = valid_five_up_exit()
    counts = [
        len(calculate_centers(values[:end], 0, SourceKind.SEGMENT).centers)
        for end in range(1, len(values) + 1)
    ]
    assert counts == [0, 0, 0, 0, 1]


def test_empty_stream_returns_empty_level_without_tail_indexing():
    result = calculate_centers((), 0, SourceKind.SEGMENT)

    assert result.centers == ()
    assert result.previews == ()
    assert result.events == ()
    assert result.locked_unit_count == 0
    assert result.price_basis_revision is None


def test_invalid_five_role_window_keeps_final_entry_and_three_core_units():
    prices = (140, 100, 130, 110, 125, 115)
    values = tuple(unit(i, "up" if end > start else "down", start, end)
                   for i, (start, end) in enumerate(zip(prices, prices[1:])))
    result = calculate_centers(values, 0, SourceKind.SEGMENT)
    assert result.centers == ()
    assert len(result.previews) == 1
    preview = result.previews[0]
    assert preview.entry_unit_id == values[1].unit_id
    assert preview.unit_ids == tuple(value.unit_id for value in values[2:])
    assert preview.establishment_leave_unit_id is None
    assert (preview.zd_tick, preview.zg_tick) == (115, 125)


def test_unlocked_fifth_segment_remains_preview_only():
    values = valid_five_up_exit()
    active = values[:-1] + (
        replace(values[-1], locked=False, confirmed_at=None),
    )
    result = calculate_centers(active, 0, SourceKind.SEGMENT)

    assert result.locked_unit_count == 4
    assert result.centers == ()
    assert len(result.previews) == 1
    preview = result.previews[0]
    assert preview.state is CenterPreviewState.FORMING
    assert preview.entry_unit_id == active[0].unit_id
    assert preview.unit_ids == tuple(item.unit_id for item in active[1:4])
    assert preview.pending_leave_unit_id == active[4].unit_id


@pytest.mark.parametrize('source_kind', [SourceKind.SEGMENT, SourceKind.STROKE_OBSERVATION, SourceKind.TREND_TYPE])
@pytest.mark.parametrize('mirror', [False, True])
def test_locking_unchanged_geometry_preserves_the_first_selected_core(source_kind, mirror):
    prices = (100, 104, 101, 105, 102, 106, 103, 107)
    if mirror:
        prices = tuple(220 - value for value in prices)
    level = 1 if source_kind is SourceKind.TREND_TYPE else 0
    values = tuple(unit(i, 'up' if end > start else 'down', start, end,
                        locked=i < 2, source_kind=source_kind, structural_level=level)
                   for i, (start, end) in enumerate(zip(prices, prices[1:])))
    live = calculate_centers(values, level, source_kind)
    locked = tuple(replace(value, locked=True, confirmed_at=value.available_at) for value in values)
    confirmed = calculate_centers(locked, level, source_kind)
    assert not live.centers and len(live.previews) == 1
    assert live.previews[0].formal_center_id == confirmed.centers[0].center_id
    assert live.previews[0].unit_ids[:3] == tuple(u.unit_id for u in confirmed.centers[0].initial_units)
    assert (live.previews[0].zd_tick, live.previews[0].zg_tick) == (
        confirmed.centers[0].zd_tick, confirmed.centers[0].zg_tick)


@pytest.mark.parametrize('source_kind', [SourceKind.SEGMENT, SourceKind.STROKE_OBSERVATION, SourceKind.TREND_TYPE])
@pytest.mark.parametrize('mirror', [False, True])
def test_unattachable_last_unit_does_not_erase_valid_owner_extensions(source_kind, mirror):
    prices = (130, 105, 115, 100, 120, 104, 121, 90, 112, 100, 104, 90)
    level = 1 if source_kind is SourceKind.TREND_TYPE else 0
    values = tuple(unit(i, 'up' if end > start else 'down', start, end,
                        locked=i < 6, source_kind=source_kind, structural_level=level)
                   for i, (start, end) in enumerate(zip(prices, prices[1:])))
    # The return's internal price extreme reaches the core even though its endpoint
    # is below ZD. The following down unit is wholly outside that same core.
    values = (*values[:9], replace(values[9], high_tick=106), values[10])
    if mirror:
        values = tuple(replace(value, direction='down' if value.direction == 'up' else 'up',
                              start_tick=300-value.start_tick, end_tick=300-value.end_tick,
                              low_tick=300-value.high_tick, high_tick=300-value.low_tick) for value in values)
    before = calculate_centers(values[:-1], level, source_kind)
    after = calculate_centers(values, level, source_kind)
    assert len(before.previews) == len(after.previews) == 1
    assert after.centers == before.centers
    assert after.previews[0].formal_center_id == before.previews[0].formal_center_id
    assert after.previews[0].unit_ids == before.previews[0].unit_ids
    assert after.previews[0].failed_departure_unit_ids == before.previews[0].failed_departure_unit_ids
    assert after.previews[0].completion_return_unit_id is None
    assert values[-1].unit_id not in after.previews[0].unit_ids
    # When those valid extensions have already locked, the same outside tail
    # supplies no new evidence and must not create a phantom owner projection.
    locked_prefix = tuple(replace(value, locked=True, confirmed_at=value.available_at) for value in values[:-1])
    no_change = calculate_centers((*locked_prefix, values[-1]), level, source_kind)
    assert no_change.previews == ()


def test_locked_leave_and_unlocked_outside_return_make_one_completed_preview():
    values = valid_five_up_exit() + (
        replace(unit(5, "down", 130, 120), locked=False, confirmed_at=None),
    )
    result = calculate_centers(values, 0, SourceKind.SEGMENT)

    assert len(result.centers) == 1
    assert result.centers[0].state is CenterState.ONGOING
    assert result.centers[0].pending_leave_unit is values[4]
    completed = [
        item
        for item in result.previews
        if item.state is CenterPreviewState.COMPLETED
    ]
    assert len(completed) == 1
    assert completed[0].unit_ids == tuple(item.unit_id for item in values[1:4])
    assert completed[0].completion_leave_unit_id == values[4].unit_id
    assert completed[0].completion_return_unit_id == values[5].unit_id


def test_completed_preview_leave_seeds_four_component_terminal_preview():
    values = valid_five_up_exit() + (
        replace(unit(5, "down", 130, 120), locked=False, confirmed_at=None),
        replace(unit(6, "up", 120, 125), locked=False, confirmed_at=None),
        replace(unit(7, "down", 125, 121), locked=False, confirmed_at=None),
    )

    result = calculate_centers(values, 0, SourceKind.SEGMENT)

    assert [preview.state for preview in result.previews] == [
        CenterPreviewState.COMPLETED,
        CenterPreviewState.FORMING,
    ]
    completed, terminal = result.previews
    assert completed.completion_leave_unit_id == values[4].unit_id
    assert completed.completion_return_unit_id == values[5].unit_id
    assert terminal.entry_unit_id == values[4].unit_id
    assert terminal.unit_ids == tuple(item.unit_id for item in values[5:8])
    assert terminal.establishment_leave_unit_id is None
    assert (terminal.zd_tick, terminal.zg_tick) == (121, 125)


def test_invalid_shared_leave_seed_slides_forward_without_body_duplication():
    values = _two_completed_centers()
    result = calculate_centers(values, 0, SourceKind.SEGMENT)

    assert [item.state for item in result.centers] == [
        CenterState.COMPLETED,
        CenterState.COMPLETED,
    ]
    first, second = result.centers
    assert first.completion_return_unit is values[5]
    assert second.entry_unit is values[6]
    assert first.completion_leave_unit is values[4]
    assert second.entry_unit is not first.completion_leave_unit
    assert first.completion_return_unit not in first.body_units
    assert not {item.unit_id for item in first.body_units} & {
        item.unit_id for item in second.body_units
    }


def test_scanner_emits_establish_watch_extend_watch_complete_in_order():
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


def test_shared_boundary_center_identity_is_prefix_stable():
    values = _two_completed_centers()
    first_complete = calculate_centers(values[:6], 0, SourceKind.SEGMENT).centers
    second_ongoing = calculate_centers(values[:11], 0, SourceKind.SEGMENT).centers
    both_complete = calculate_centers(values, 0, SourceKind.SEGMENT).centers

    assert len(first_complete) == 1
    assert len(second_ongoing) == len(both_complete) == 2
    assert first_complete[0] == second_ongoing[0] == both_complete[0]
    assert second_ongoing[1].state is CenterState.ONGOING
    assert both_complete[1].state is CenterState.COMPLETED
    assert second_ongoing[1].center_id == both_complete[1].center_id


def test_active_owner_suppresses_shifted_forming_preview():
    values = valid_five_up_exit() + (
        replace(unit(5, "down", 130, 110), locked=False, confirmed_at=None),
        replace(unit(6, "up", 110, 125), locked=False, confirmed_at=None),
        replace(unit(7, "down", 125, 110), locked=False, confirmed_at=None),
    )
    result = calculate_centers(values, 0, SourceKind.SEGMENT)

    assert len(result.centers) == 1
    assert result.centers[0].state is CenterState.ONGOING
    assert sum(
        item.state is CenterPreviewState.FORMING for item in result.previews
    ) <= 1
    for preview in result.previews:
        assert preview.entry_unit_id == result.centers[0].entry_unit.unit_id


def test_active_owner_suppresses_shifted_completed_preview_inside_its_core():
    """A sliding live-tail window cannot manufacture a second completed center.

    This is the reduced SH.601059/5m production sequence from 2026-08-31.  The
    shifted candidate core (1637, 1657) overlaps the active center core
    (1640, 1679), while every unit after the shared entry is still unlocked.
    Those units extend the active center; they do not complete a new center.
    """

    endpoints = (
        (1724, 1640),
        (1640, 1685),
        (1685, 1634),
        (1634, 1679),
        (1679, 1608),
        (1608, 1688),
        (1688, 1653),
        (1653, 1687),
        (1687, 1621),
        (1621, 1657),
        (1657, 1637),
        (1637, 1698),
        (1698, 1662),
    )
    values = []
    for index, (start_tick, end_tick) in enumerate(endpoints):
        value = unit(
            index,
            "up" if end_tick > start_tick else "down",
            start_tick,
            end_tick,
        )
        if index >= 8:
            value = replace(
                value,
                locked=False,
                confirmed_at=None,
                forming=index == len(endpoints) - 1,
            )
        values.append(value)

    result = calculate_centers(tuple(values), 0, SourceKind.SEGMENT)

    assert len(result.centers) == 1
    active = result.centers[0]
    assert active.state is CenterState.ONGOING
    assert (active.zd_tick, active.zg_tick) == (1640, 1679)
    assert len(result.previews) == 1
    projected = result.previews[0]
    assert projected.state is CenterPreviewState.FORMING
    assert projected.formal_center_id == active.center_id
    assert (projected.zd_tick, projected.zg_tick) == (
        active.zd_tick,
        active.zg_tick,
    )
    assert not any(
        preview.state is CenterPreviewState.COMPLETED
        for preview in result.previews
    )


def test_at_most_one_ongoing_center_and_one_forming_preview_per_prefix():
    values = _two_completed_centers() + (
        replace(unit(12, "up", 177, 200), locked=False, confirmed_at=None),
        replace(unit(13, "down", 200, 160), locked=False, confirmed_at=None),
    )
    for end in range(1, len(values) + 1):
        result = calculate_centers(values[:end], 0, SourceKind.SEGMENT)
        assert sum(c.state is CenterState.ONGOING for c in result.centers) <= 1
        assert sum(
            p.state is CenterPreviewState.FORMING for p in result.previews
        ) <= 1


def test_completed_centers_are_not_rewritten_by_future_units():
    values = _sh000001_causal_prefix_units()
    earlier = calculate_centers(values[:14], 0, SourceKind.SEGMENT)
    later = calculate_centers(values, 0, SourceKind.SEGMENT)
    earlier_ids = tuple(
        item.center_id
        for item in earlier.centers
        if item.state is CenterState.COMPLETED
    )
    later_ids = tuple(
        item.center_id
        for item in later.centers
        if item.state is CenterState.COMPLETED
    )
    assert later_ids[: len(earlier_ids)] == earlier_ids


def test_deterministic_live_tail_fuzz_preserves_single_active_ownership():
    generator = random.Random(20260806)
    seeds = (
        valid_five_up_exit(),
        (
            unit(0, "down", 1300, 900),
            unit(1, "up", 900, 1100),
            unit(2, "down", 1100, 950),
            unit(3, "up", 950, 1050),
            unit(4, "down", 1050, 800),
        ),
    )
    for case in range(150):
        values = list(seeds[case % 2])
        for index in range(len(values), len(values) + generator.randint(1, 10)):
            direction = "down" if values[-1].direction == "up" else "up"
            start = values[-1].end_tick
            distance = generator.randint(5, 180)
            end = start + distance if direction == "up" else start - distance
            values.append(unit(index, direction, start, end))
        locked_count = generator.randint(5, len(values))
        values = [
            value
            if index < locked_count
            else replace(value, locked=False, confirmed_at=None)
            for index, value in enumerate(values)
        ]
        for size in range(5, len(values) + 1):
            result = calculate_centers(tuple(values[:size]), 0, SourceKind.SEGMENT)
            assert sum(c.state is CenterState.ONGOING for c in result.centers) <= 1
            assert sum(
                p.state is CenterPreviewState.FORMING for p in result.previews
            ) <= 1


def test_deterministic_fuzz_retains_first_seed_after_each_third_class_point():
    generator = random.Random(20260806)
    for _case in range(500):
        count = generator.randint(10, 28)
        current = generator.randint(500, 1500)
        direction = "up" if generator.randrange(2) else "down"
        values = []
        for index in range(count):
            distance = generator.randint(5, 300)
            end = (
                current + distance
                if direction == "up"
                else current - distance
            )
            values.append(unit(index, direction, current, end))
            current = end
            direction = "down" if direction == "up" else "up"

        result = calculate_centers(
            tuple(values),
            0,
            SourceKind.SEGMENT,
        )
        index_by_id = {
            item.unit_id: index for index, item in enumerate(values)
        }
        for position, previous in enumerate(result.centers):
            if previous.state is not CenterState.COMPLETED:
                continue
            leave_index = index_by_id[previous.completion_leave_unit.unit_id]
            resume = leave_index
            expected = next(
                (
                    start
                    for start in range(resume, len(values) - 4)
                    if establish_center(
                        values[start : start + 5],
                        0,
                        SourceKind.SEGMENT,
                    )
                    is not None
                ),
                None,
            )
            if expected is None:
                continue
            assert position + 1 < len(result.centers)
            following = result.centers[position + 1]
            assert index_by_id[following.entry_unit.unit_id] == expected
            assert index_by_id[following.core_units[0].unit_id] >= leave_index


def test_zero_width_three_unit_intersection_is_touch_only_not_formal():
    values = (
        unit(0, "down", 130, 120),
        replace(unit(1, "up", 120, 120), high_tick=130),
        unit(2, "down", 120, 100),
        unit(3, "up", 100, 120),
        unit(4, "down", 120, 110),
    )
    result = calculate_centers(values, 0, SourceKind.SEGMENT)
    assert result.centers == ()
    touch = [p for p in result.previews if p.state is CenterPreviewState.TOUCH_ONLY]
    assert len(touch) == 1
    assert touch[0].zd_tick == touch[0].zg_tick == 120
    assert touch[0].formal_center_id is None


def test_scan_rejects_locked_unit_after_preview_tail():
    values = list(valid_five_up_exit())
    values[3] = replace(values[3], locked=False, confirmed_at=None)
    with pytest.raises(ValueError, match="locked units must form a prefix"):
        calculate_centers(tuple(values), 0, SourceKind.SEGMENT)


def _invalid_sequences():
    base = valid_five_up_exit()
    duplicate = (base[0], replace(base[1], unit_id=base[0].unit_id), *base[2:])
    same_direction = (
        base[0],
        replace(base[1], direction="up", end_tick=base[1].start_tick),
        *base[2:],
    )
    disconnected = (
        base[0],
        replace(base[1], start_tick=base[1].start_tick - 1, high_tick=base[1].high_tick + 1),
        *base[2:],
    )
    overlapping = (
        base[0],
        replace(base[1], market_start=base[0].market_end - timedelta(minutes=1)),
        *base[2:],
    )
    wrong_level = (base[0], replace(base[1], structural_level=1), *base[2:])
    mixed_basis = (*base[:-1], replace(base[-1], price_basis_revision="post-action"))
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


def test_level_result_carries_single_input_price_basis():
    result = calculate_centers(valid_five_up_exit(), 0, SourceKind.SEGMENT)
    assert result.price_basis_revision == TEST_PRICE_BASIS
