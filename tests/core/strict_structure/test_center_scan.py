"""Batch scanner invariants for the three-unit center lifecycle."""

from dataclasses import replace
from datetime import timedelta
import random

import pytest

from chanlun.core.strict_structure.center_machine import calculate_centers
from chanlun.core.strict_structure.models import (
    CenterEvidence,
    CenterEventKind,
    CenterPreviewState,
    CenterState,
    SourceKind,
    TrendState,
)
from chanlun.core.strict_structure.trend_assembler import assemble_trend_types
from tests.core.strict_structure.helpers import (
    TEST_PRICE_BASIS,
    unit,
    valid_up_center_lifecycle,
)


def _recursive_pending_leave_then_disjoint_successor():
    """A recursive stream with an outside consolidation before the new core."""

    def recursive_unit(index, direction, start_tick, end_tick):
        return unit(
            index,
            direction,
            start_tick,
            end_tick,
            source_kind=SourceKind.TREND_TYPE,
            structural_level=1,
        )

    return (
        recursive_unit(0, "up", 100, 130),
        recursive_unit(1, "down", 130, 110),
        recursive_unit(2, "up", 110, 150),
        recursive_unit(3, "down", 150, 90),
        recursive_unit(4, "down", 90, 80),
        recursive_unit(5, "up", 80, 105),
        recursive_unit(6, "down", 105, 90),
    )


def _two_completed_centers():
    return valid_up_center_lifecycle() + (
        unit(5, "up", 120, 140),
        unit(6, "down", 140, 125),
        unit(7, "up", 125, 145),
        unit(8, "down", 145, 135),
    )


def _disjoint_successor_centers():
    """Two cores where the fourth unit is detached above the first core."""

    return (
        unit(0, "up", 100, 130),
        unit(1, "down", 130, 110),
        unit(2, "up", 110, 150),
        unit(3, "down", 150, 140),
        unit(4, "up", 140, 160),
        unit(5, "down", 160, 145),
    )


def _sliding_disjoint_successor_centers():
    """The first detached three-unit window fails; the next one is formal."""

    return (
        unit(0, "up", 100, 130),
        unit(1, "down", 130, 110),
        unit(2, "up", 110, 150),
        unit(3, "down", 150, 140),
        unit(4, "up", 140, 200),
        unit(5, "down", 200, 180),
        unit(6, "up", 180, 190),
    )


def test_scanner_establishes_on_third_and_completes_on_fifth_locked_unit() -> None:
    values = valid_up_center_lifecycle()
    results = tuple(
        calculate_centers(values[:end], 0, SourceKind.SEGMENT)
        for end in range(1, len(values) + 1)
    )

    assert [len(result.centers) for result in results] == [0, 0, 1, 1, 1]
    assert results[2].centers[0].pending_leave_unit is None
    assert results[3].centers[0].pending_leave_unit is values[3]
    assert results[4].centers[0].state is CenterState.COMPLETED


def test_empty_stream_returns_empty_level_without_tail_indexing() -> None:
    result = calculate_centers((), 0, SourceKind.SEGMENT)

    assert result.centers == ()
    assert result.previews == ()
    assert result.events == ()
    assert result.locked_unit_count == 0
    assert result.price_basis_revision is None


def test_disjoint_successor_waits_for_all_three_locked_units() -> None:
    values = _disjoint_successor_centers()

    three = calculate_centers(values[:3], 0, SourceKind.SEGMENT)
    four = calculate_centers(values[:4], 0, SourceKind.SEGMENT)
    five = calculate_centers(values[:5], 0, SourceKind.SEGMENT)
    six = calculate_centers(values, 0, SourceKind.SEGMENT)

    assert [center.state for center in three.centers] == [CenterState.ONGOING]
    assert [center.state for center in four.centers] == [CenterState.ONGOING]
    assert [center.state for center in five.centers] == [CenterState.ONGOING]
    assert [center.state for center in six.centers] == [
        CenterState.SUPERSEDED,
        CenterState.ONGOING,
    ]
    assert three.centers[0].center_id == four.centers[0].center_id
    assert four.centers[0].center_id == five.centers[0].center_id
    assert five.centers[0].center_id == six.centers[0].center_id


def test_disjoint_successor_does_not_fabricate_third_class_completion() -> None:
    values = _disjoint_successor_centers()

    result = calculate_centers(values, 0, SourceKind.SEGMENT)

    first, second = result.centers
    assert first.initial_units == values[:3]
    assert first.state is CenterState.SUPERSEDED
    assert first.pending_leave_unit is None
    assert first.completion_leave_unit is None
    assert first.completion_return_unit is None
    assert first.completed_at is None
    assert first.physically_completed is False
    assert first.superseded_by_center_id == second.center_id
    assert first.superseded_at == second.established_at
    assert second.initial_units == values[3:]
    assert second.entry_unit is values[2]
    assert values[2] not in second.body_units
    assert (first.zd_tick, first.zg_tick) == (110, 130)
    assert (second.zd_tick, second.zg_tick) == (145, 150)
    assert [event.kind for event in result.events] == [
        CenterEventKind.ESTABLISHED,
        CenterEventKind.SUPERSEDED,
        CenterEventKind.ESTABLISHED,
    ]
    assert not any(
        event.kind
        in (CenterEventKind.COMPLETED_UP, CenterEventKind.COMPLETED_DOWN)
        for event in result.events
    )


def test_sliding_successor_preserves_non_center_bridge_context() -> None:
    values = _sliding_disjoint_successor_centers()

    before = calculate_centers(values[:6], 0, SourceKind.SEGMENT)
    after = calculate_centers(values, 0, SourceKind.SEGMENT)

    assert [center.state for center in before.centers] == [CenterState.ONGOING]
    first, successor = after.centers
    assert first.state is CenterState.SUPERSEDED
    assert first.supersession_bridge_units == (values[3],)
    assert values[3] not in first.body_units
    assert first.completion_leave_unit is None
    assert first.completion_return_unit is None
    assert successor.initial_units == values[4:7]
    assert successor.entry_unit is values[3]

    evidence = CenterEvidence.from_center(first)
    assert evidence.state is CenterState.SUPERSEDED
    assert evidence.superseded_by_center_id == successor.center_id
    assert evidence.superseded_at == successor.established_at
    assert evidence.supersession_bridge_unit_ids == (values[3].unit_id,)
    assert evidence.completion_leave_unit_id is None
    assert evidence.completion_return_unit_id is None


def test_recursive_successor_supersedes_unresolved_departure_without_fake_return() -> None:
    values = _recursive_pending_leave_then_disjoint_successor()
    oscillatory_ids = frozenset({values[3].unit_id})

    before = tuple(
        calculate_centers(
            values[:end],
            1,
            SourceKind.TREND_TYPE,
            oscillatory_ids,
        )
        for end in range(3, 7)
    )
    after = calculate_centers(
        values,
        1,
        SourceKind.TREND_TYPE,
        oscillatory_ids,
    )

    assert all(result.centers[0].state is CenterState.ONGOING for result in before)
    assert before[1].centers[0].pending_leave_unit is values[3]
    first, successor = after.centers
    assert first.state is CenterState.SUPERSEDED
    assert first.pending_leave_unit is None
    assert first.completion_leave_unit is None
    assert first.completion_return_unit is None
    assert first.completed_at is None
    assert first.supersession_bridge_units == (values[3],)
    assert first.superseded_by_center_id == successor.center_id
    assert successor.entry_unit is values[3]
    assert successor.initial_units == values[4:]
    assert not set(first_unit.unit_id for first_unit in first.body_units).intersection(
        successor_unit.unit_id for successor_unit in successor.body_units
    )
    assert not any(
        event.kind in (CenterEventKind.COMPLETED_UP, CenterEventKind.COMPLETED_DOWN)
        for event in after.events
    )

    assembly = assemble_trend_types(
        after.centers,
        values,
        1,
        oscillatory_ids,
    )
    left, right = assembly.current_trends
    assert (left.state, right.state) == (TrendState.COMPLETE, TrendState.FORMING)
    assert left.constituent_units == values[:4]
    assert right.constituent_units == values[4:]
    assert left.end_tick == right.start_tick


def test_detached_recursive_continuation_does_not_become_a_fake_leave() -> None:
    values = (
        unit(
            0,
            "up",
            100,
            130,
            source_kind=SourceKind.TREND_TYPE,
            structural_level=1,
        ),
        unit(
            1,
            "down",
            130,
            110,
            source_kind=SourceKind.TREND_TYPE,
            structural_level=1,
        ),
        unit(
            2,
            "up",
            110,
            150,
            source_kind=SourceKind.TREND_TYPE,
            structural_level=1,
        ),
        unit(
            3,
            "up",
            150,
            170,
            source_kind=SourceKind.TREND_TYPE,
            structural_level=1,
        ),
    )

    result = calculate_centers(
        values,
        1,
        SourceKind.TREND_TYPE,
        frozenset({values[2].unit_id}),
    )

    assert len(result.centers) == 1
    assert result.centers[0].state is CenterState.ONGOING
    assert result.centers[0].pending_leave_unit is None
    assert [event.kind for event in result.events] == [CenterEventKind.ESTABLISHED]


def test_unlocked_third_unit_remains_preview_only() -> None:
    values = valid_up_center_lifecycle()
    active = values[:2] + (
        replace(values[2], locked=False, confirmed_at=None),
    )

    result = calculate_centers(active, 0, SourceKind.SEGMENT)

    assert result.locked_unit_count == 2
    assert result.centers == ()
    assert len(result.previews) == 1
    preview = result.previews[0]
    assert preview.state is CenterPreviewState.FORMING
    assert preview.entry_unit_id is None
    assert preview.unit_ids == tuple(item.unit_id for item in active)
    assert preview.pending_leave_unit_id is None


def test_unlocked_fourth_unit_projects_formal_center_departure_watch() -> None:
    values = valid_up_center_lifecycle()
    active = values[:3] + (
        replace(values[3], locked=False, confirmed_at=None),
    )

    result = calculate_centers(active, 0, SourceKind.SEGMENT)

    assert result.locked_unit_count == 3
    assert len(result.centers) == 1
    assert result.centers[0].initial_units == values[:3]
    assert result.centers[0].pending_leave_unit is None
    assert len(result.previews) == 1
    preview = result.previews[0]
    assert preview.unit_ids == tuple(item.unit_id for item in values[:3])
    assert preview.pending_leave_unit_id == values[3].unit_id


def test_locked_leave_and_unlocked_return_make_completed_preview() -> None:
    values = valid_up_center_lifecycle()
    active = values[:4] + (
        replace(values[4], locked=False, confirmed_at=None),
    )

    result = calculate_centers(active, 0, SourceKind.SEGMENT)

    assert len(result.centers) == 1
    assert result.centers[0].state is CenterState.ONGOING
    assert result.centers[0].pending_leave_unit is values[3]
    assert len(result.previews) == 1
    preview = result.previews[0]
    assert preview.state is CenterPreviewState.COMPLETED
    assert preview.unit_ids == tuple(item.unit_id for item in values[:3])
    assert preview.completion_leave_unit_id == values[3].unit_id
    assert preview.completion_return_unit_id == values[4].unit_id


def test_completed_preview_seeds_next_three_unit_forming_preview() -> None:
    lifecycle = valid_up_center_lifecycle()
    values = lifecycle[:4] + tuple(
        replace(item, locked=False, confirmed_at=None)
        for item in (
            lifecycle[4],
            unit(5, "up", 120, 140),
            unit(6, "down", 140, 125),
        )
    )

    result = calculate_centers(values, 0, SourceKind.SEGMENT)

    assert [preview.state for preview in result.previews] == [
        CenterPreviewState.COMPLETED,
        CenterPreviewState.FORMING,
    ]
    completed, successor = result.previews
    assert completed.completion_leave_unit_id == values[3].unit_id
    assert completed.completion_return_unit_id == values[4].unit_id
    assert successor.entry_unit_id == values[3].unit_id
    assert successor.unit_ids == tuple(item.unit_id for item in values[4:7])
    assert (successor.zd_tick, successor.zg_tick) == (125, 130)


def test_completed_centers_do_not_duplicate_body_ownership() -> None:
    values = _two_completed_centers()

    result = calculate_centers(values, 0, SourceKind.SEGMENT)

    assert [center.state for center in result.centers] == [
        CenterState.COMPLETED,
        CenterState.COMPLETED,
    ]
    first, second = result.centers
    assert first.completion_return_unit is values[4]
    assert second.entry_unit is first.completion_leave_unit
    assert second.initial_units == values[4:7]
    assert not {
        item.unit_id for item in first.body_units
    }.intersection(item.unit_id for item in second.body_units)


def test_scanner_emits_establish_extend_watch_complete_in_order() -> None:
    values = valid_up_center_lifecycle()[:3] + (
        unit(3, "up", 105, 110),
        unit(4, "down", 110, 100),
        unit(5, "up", 100, 104),
    )

    result = calculate_centers(values, 0, SourceKind.SEGMENT)

    assert [event.kind for event in result.events] == [
        CenterEventKind.ESTABLISHED,
        CenterEventKind.EXTENDED,
        CenterEventKind.BREAKOUT_WATCH_DOWN,
        CenterEventKind.COMPLETED_DOWN,
    ]
    assert result.centers[0].state is CenterState.COMPLETED


def test_center_identity_is_prefix_stable_across_second_lifecycle() -> None:
    values = _two_completed_centers()
    first_complete = calculate_centers(
        values[:5], 0, SourceKind.SEGMENT
    ).centers
    second_established = calculate_centers(
        values[:7], 0, SourceKind.SEGMENT
    ).centers
    both_complete = calculate_centers(values, 0, SourceKind.SEGMENT).centers

    assert len(first_complete) == 1
    assert len(second_established) == len(both_complete) == 2
    assert first_complete[0] == second_established[0] == both_complete[0]
    assert second_established[1].center_id == both_complete[1].center_id
    assert second_established[1].initial_units == both_complete[1].initial_units


def test_active_owner_suppresses_shifted_forming_preview() -> None:
    values = valid_up_center_lifecycle()[:3] + (
        replace(unit(3, "up", 105, 110), locked=False, confirmed_at=None),
        replace(unit(4, "down", 110, 100), locked=False, confirmed_at=None),
    )

    result = calculate_centers(values, 0, SourceKind.SEGMENT)

    assert len(result.centers) == 1
    assert result.centers[0].initial_units == values[:3]
    assert len(result.previews) == 1
    assert result.previews[0].entry_unit_id is None
    assert result.previews[0].unit_ids[:3] == tuple(
        item.unit_id for item in values[:3]
    )


def test_at_most_one_active_center_and_forming_preview_per_prefix() -> None:
    values = _two_completed_centers() + (
        replace(unit(9, "up", 135, 160), locked=False, confirmed_at=None),
        replace(unit(10, "down", 160, 140), locked=False, confirmed_at=None),
    )

    for end in range(1, len(values) + 1):
        result = calculate_centers(values[:end], 0, SourceKind.SEGMENT)
        assert sum(
            center.state is CenterState.ONGOING for center in result.centers
        ) <= 1
        assert sum(
            preview.state is CenterPreviewState.FORMING
            for preview in result.previews
        ) <= 1


def test_deterministic_prefix_fuzz_never_replaces_established_seed() -> None:
    generator = random.Random(20260824)
    for _case in range(40):
        count = generator.randint(7, 18)
        current = generator.randint(500, 1500)
        direction = "up" if generator.randrange(2) else "down"
        values = []
        for index in range(count):
            distance = generator.randint(5, 180)
            end = current + distance if direction == "up" else current - distance
            values.append(unit(index, direction, current, end))
            current = end
            direction = "down" if direction == "up" else "up"

        final = calculate_centers(tuple(values), 0, SourceKind.SEGMENT)
        final_ids = tuple(center.center_id for center in final.centers)
        for end in range(3, len(values) + 1):
            prefix = calculate_centers(
                tuple(values[:end]),
                0,
                SourceKind.SEGMENT,
            )
            prefix_ids = tuple(center.center_id for center in prefix.centers)
            assert final_ids[: len(prefix_ids)] == prefix_ids


def test_deterministic_live_tail_fuzz_preserves_single_active_ownership() -> None:
    generator = random.Random(20260806)
    for case in range(30):
        values = list(valid_up_center_lifecycle())
        for index in range(len(values), len(values) + generator.randint(1, 6)):
            direction = "down" if values[-1].direction == "up" else "up"
            start = values[-1].end_tick
            distance = generator.randint(5, 120)
            end = start + distance if direction == "up" else start - distance
            values.append(unit(index, direction, start, end))
        locked_count = generator.randint(2, len(values))
        values = [
            value
            if index < locked_count
            else replace(value, locked=False, confirmed_at=None)
            for index, value in enumerate(values)
        ]

        result = calculate_centers(tuple(values), 0, SourceKind.SEGMENT)
        assert sum(
            center.state is CenterState.ONGOING for center in result.centers
        ) <= 1, case
        assert sum(
            preview.state is CenterPreviewState.FORMING
            for preview in result.previews
        ) <= 1, case


def test_zero_width_three_unit_intersection_is_touch_only_not_formal() -> None:
    values = (
        unit(0, "down", 120, 100),
        unit(1, "up", 100, 110),
        unit(2, "down", 110, 110),
    )

    result = calculate_centers(values, 0, SourceKind.SEGMENT)

    assert result.centers == ()
    assert len(result.previews) == 1
    touch = result.previews[0]
    assert touch.state is CenterPreviewState.TOUCH_ONLY
    assert touch.zd_tick == touch.zg_tick == 110


def test_scan_rejects_locked_unit_after_preview_tail() -> None:
    values = list(valid_up_center_lifecycle())
    values[2] = replace(values[2], locked=False, confirmed_at=None)

    with pytest.raises(ValueError, match="locked units must form a prefix"):
        calculate_centers(tuple(values), 0, SourceKind.SEGMENT)


def _invalid_sequences():
    base = valid_up_center_lifecycle()
    duplicate = (
        base[0],
        replace(base[1], unit_id=base[0].unit_id),
        *base[2:],
    )
    same_direction = (
        base[0],
        replace(
            base[1],
            direction="down",
            end_tick=95,
            low_tick=95,
            high_tick=100,
        ),
        *base[2:],
    )
    disconnected = (
        base[0],
        replace(base[1], start_tick=99, low_tick=99),
        *base[2:],
    )
    overlapping = (
        base[0],
        replace(
            base[1],
            market_start=base[0].market_end - timedelta(minutes=1),
        ),
        *base[2:],
    )
    wrong_level = (
        base[0],
        replace(base[1], structural_level=1),
        *base[2:],
    )
    mixed_basis = (
        *base[:-1],
        replace(base[-1], price_basis_revision="post-action"),
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
def test_scan_rejects_non_continuous_constituent_sequence(values, message) -> None:
    with pytest.raises(ValueError, match=message):
        calculate_centers(values, 0, SourceKind.SEGMENT)


def test_level_result_carries_single_input_price_basis() -> None:
    result = calculate_centers(
        valid_up_center_lifecycle()[:3],
        0,
        SourceKind.SEGMENT,
    )

    assert result.price_basis_revision == TEST_PRICE_BASIS
