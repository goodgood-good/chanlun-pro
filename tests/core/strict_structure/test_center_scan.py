from dataclasses import replace
from datetime import timedelta
import random

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


def _direction_flip_then_later_center():
    return valid_five_up_exit() + (
        unit(5, "down", 130, 95),
        unit(6, "up", 95, 100),
        unit(7, "down", 100, 96),
        unit(8, "up", 96, 99),
        unit(9, "down", 99, 97),
        unit(10, "up", 97, 105),
        unit(11, "down", 105, 101),
    )


def _uptrend_linked_live_preview():
    """Mirror SH.000001: old center leave plus three provisional segments."""

    return valid_five_up_exit() + (
        replace(unit(5, "down", 130, 120), locked=False, confirmed_at=None),
        replace(unit(6, "up", 120, 128), locked=False, confirmed_at=None),
        replace(unit(7, "down", 128, 122), locked=False, confirmed_at=None),
    )


def _downtrend_linked_live_preview():
    initial = (
        unit(0, "down", 120, 90),
        unit(1, "up", 90, 110),
        unit(2, "down", 110, 95),
        unit(3, "up", 95, 105),
        unit(4, "down", 105, 80),
    )
    return initial + (
        replace(unit(5, "up", 80, 90), locked=False, confirmed_at=None),
        replace(unit(6, "down", 90, 82), locked=False, confirmed_at=None),
        replace(unit(7, "up", 82, 88), locked=False, confirmed_at=None),
    )


def _sh000001_causal_prefix_units():
    """Real 1m segment prices around the historical repaint regression."""

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


def test_scan_with_four_locked_units_has_one_formal_first_three_center():
    result = calculate_centers(
        valid_five_up_exit()[:4],
        0,
        SourceKind.SEGMENT,
    )
    assert len(result.centers) == 1
    assert result.centers[0].state is CenterState.ONGOING
    assert result.centers[0].initial_units == valid_five_up_exit()[:3]
    assert result.centers[0].extension_units == valid_five_up_exit()[3:4]
    assert result.locked_unit_count == 4
    assert result.previews == ()


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
    assert len(result.previews) == 1
    preview = result.previews[0]
    assert preview.state is CenterPreviewState.COMPLETED
    assert preview.unit_ids == tuple(item.unit_id for item in initial)
    assert preview.completion_return_unit_id == values[-1].unit_id
    assert all(item.locked for item in result.centers[0].body_units)


def test_completion_return_can_start_next_center_without_body_overlap():
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
        CenterEventKind.EXTENDED,
        CenterEventKind.BREAKOUT_WATCH_UP,
        CenterEventKind.COMPLETED_UP,
    ]
    assert result.centers[0].state is CenterState.COMPLETED


def test_cross_core_return_extends_then_completes_opposite_side():
    values = valid_five_up_exit() + (
        unit(5, "down", 130, 95),
        unit(6, "up", 95, 100),
    )
    result = calculate_centers(values, 0, SourceKind.SEGMENT)
    assert len(result.centers) == 1
    center = result.centers[0]
    assert center.state is CenterState.COMPLETED
    assert center.entry_unit is values[0]
    assert center.completion_direction == "down"
    assert center.completion_leave_unit is values[5]
    assert center.completion_return_unit is values[6]
    assert center.completion_leave_unit is center.body_units[-1]


def test_scan_reuses_completion_return_as_next_center_boundary():
    values = _direction_flip_then_later_center()
    result = calculate_centers(values, 0, SourceKind.SEGMENT)
    assert [item.state for item in result.centers] == [
        CenterState.COMPLETED,
        CenterState.COMPLETED,
    ]
    assert result.centers[0].completion_return_unit is values[6]
    assert result.centers[1].entry_unit is values[6]
    assert result.centers[0].completion_return_unit is values[6]


def test_scan_keeps_one_completed_preview_with_multiple_unlocked_units():
    values = list(_direction_flip_then_later_center())
    for index in range(8, len(values)):
        values[index] = replace(values[index], locked=False, confirmed_at=None)
    values = tuple(values)

    result = calculate_centers(values, 0, SourceKind.SEGMENT)

    completed = [
        preview
        for preview in result.previews
        if preview.state is CenterPreviewState.COMPLETED
    ]
    assert len(completed) == 1
    assert completed[0].unit_ids == tuple(item.unit_id for item in values[7:11])
    assert completed[0].completion_return_unit_id == values[11].unit_id
    assert len(result.previews) == 1


def test_completed_center_return_can_be_next_center_entry():
    """Adjacent centers use the return as a unique non-overlapping boundary."""

    values = valid_five_up_exit() + (
        unit(5, "down", 130, 120),
        unit(6, "up", 120, 140),
        unit(7, "down", 140, 125),
        unit(8, "up", 125, 150),
        unit(9, "down", 150, 135),
    )

    result = calculate_centers(values, 0, SourceKind.SEGMENT)

    assert [center.state for center in result.centers] == [
        CenterState.COMPLETED,
        CenterState.COMPLETED,
    ]
    first, second = result.centers
    assert first.completion_leave_unit is values[4]
    assert first.completion_return_unit is values[5]
    assert second.entry_unit is values[5]
    assert second.completion_leave_unit is values[8]
    assert second.completion_return_unit is values[9]
    assert not set(item.unit_id for item in first.body_units) & set(
        item.unit_id for item in second.body_units
    )


def test_shared_boundary_center_identity_is_prefix_stable():
    values = valid_five_up_exit() + (
        unit(5, "down", 130, 120),
        unit(6, "up", 120, 140),
        unit(7, "down", 140, 125),
        unit(8, "up", 125, 150),
        unit(9, "down", 150, 135),
    )

    first_complete = calculate_centers(
        values[:6], 0, SourceKind.SEGMENT
    ).centers
    second_ongoing = calculate_centers(
        values[:9], 0, SourceKind.SEGMENT
    ).centers
    both_complete = calculate_centers(
        values, 0, SourceKind.SEGMENT
    ).centers

    assert len(first_complete) == 1
    assert len(second_ongoing) == len(both_complete) == 2
    assert first_complete[0] == second_ongoing[0] == both_complete[0]
    assert second_ongoing[1].state is CenterState.ONGOING
    assert both_complete[1].state is CenterState.COMPLETED
    assert second_ongoing[1].center_id == both_complete[1].center_id


def test_later_completed_seed_supersedes_broad_ongoing_seed_causally():
    values = (
        unit(0, "up", 90, 120),
        unit(1, "down", 120, 100),
        unit(2, "up", 100, 115),
        unit(3, "down", 115, 105),
        unit(4, "up", 105, 130),
        unit(5, "down", 130, 108),
        unit(6, "up", 108, 120),
        unit(7, "down", 120, 100),
        unit(8, "up", 100, 107),
        unit(9, "down", 107, 90),
        unit(10, "up", 90, 100),
    )

    at_completion = calculate_centers(
        values[:9], 0, SourceKind.SEGMENT
    ).centers
    after_broad_seed_breaks = calculate_centers(
        values, 0, SourceKind.SEGMENT
    ).centers

    assert len(at_completion) == 1
    assert len(after_broad_seed_breaks) == 2
    assert at_completion[0].state is CenterState.COMPLETED
    assert at_completion[0].entry_unit is values[3]
    assert at_completion[0].completion_leave_unit is values[7]
    assert at_completion[0].completion_return_unit is values[8]
    assert at_completion[0] == after_broad_seed_breaks[0]
    assert after_broad_seed_breaks[1].state is CenterState.ONGOING


@pytest.mark.parametrize(
    "values",
    (_uptrend_linked_live_preview(), _downtrend_linked_live_preview()),
)
def test_scan_does_not_shift_active_leave_into_later_center_core(values):
    """The active leave is only the next center's entering boundary.

    Three provisional units after that shared boundary can provide the next
    center's three core legs, but there is no fifth/leaving leg yet.  Reusing
    the active center's penultimate body unit would shift every role one unit
    early and incorrectly place the old leave inside the new core.
    """

    result = calculate_centers(values, 0, SourceKind.SEGMENT)

    assert len(result.centers) == 1
    active = result.centers[0]
    assert active.state is CenterState.ONGOING
    assert active.pending_leave_unit is values[4]
    assert not any(
        preview.state is CenterPreviewState.FORMING
        and len(preview.unit_ids) == 5
        for preview in result.previews
    )


def test_active_center_owns_preview_even_after_shifted_seed_exists():
    """An unlocked shifted seed cannot coexist with its formal owner."""

    values = valid_five_up_exit() + (
        replace(unit(5, "down", 130, 120), locked=False, confirmed_at=None),
        replace(unit(6, "up", 120, 140), locked=False, confirmed_at=None),
        replace(unit(7, "down", 140, 125), locked=False, confirmed_at=None),
        replace(unit(8, "up", 125, 150), locked=False, confirmed_at=None),
    )

    result = calculate_centers(values, 0, SourceKind.SEGMENT)

    assert len(result.previews) == 1
    preview = result.previews[0]
    assert preview.state is CenterPreviewState.COMPLETED
    assert preview.unit_ids[:3] == tuple(
        item.unit_id for item in result.centers[-1].initial_units
    )
    assert preview.completion_return_unit_id == values[5].unit_id


def test_active_center_forming_extension_suppresses_shifted_live_seed():
    """SH.513100 1m: an unresolved extension is still the same center.

    The locked fifth segment leaves below the center.  Every later segment is
    provisional and its first return enters the original core, so there is no
    third sell and therefore no boundary at which a second same-level center
    may start.  A sliding five-unit window exists geometrically in the tail,
    but it must not replace the active center's own extension projection.
    """

    values = (
        unit(0, "down", 2192, 2103),
        unit(1, "up", 2103, 2118),
        unit(2, "down", 2118, 2094),
        unit(3, "up", 2094, 2153),
        unit(4, "down", 2153, 2095),
        replace(unit(5, "up", 2095, 2122), locked=False, confirmed_at=None),
        replace(unit(6, "down", 2122, 2035), locked=False, confirmed_at=None),
        replace(unit(7, "up", 2035, 2186), locked=False, confirmed_at=None),
    )

    result = calculate_centers(values, 0, SourceKind.SEGMENT)

    assert len(result.centers) == 1
    assert result.centers[0].state is CenterState.ONGOING
    forming = [
        preview
        for preview in result.previews
        if preview.state is CenterPreviewState.FORMING
    ]
    assert len(forming) == 1
    assert forming[0].unit_ids == tuple(item.unit_id for item in values)
    assert forming[0].unit_ids[:5] == tuple(item.unit_id for item in values[:5])
    assert (forming[0].zd_tick, forming[0].zg_tick) == (2103, 2118)

    shifted = calculate_centers(values[3:], 0, SourceKind.SEGMENT).previews[-1]
    with pytest.raises(
        ValueError,
        match="shifted forming preview cannot displace",
    ):
        replace(result, previews=(shifted,))


def test_shifted_forming_seed_cannot_bypass_rejected_active_projection():
    """An unlocked incompatible tail cannot create a second active center.

    This is the reduced SZ.000001 daily sequence from the full cache audit.
    The locked center is ongoing, while its first provisional successor sits
    wholly outside the old core and makes the active projection stop.  A later
    five-unit sliding window exists, but it is only forming and must wait for
    locked evidence instead of coexisting with the formal center.
    """

    values = (
        unit(0, "down", 1061, 707),
        unit(1, "up", 707, 982),
        unit(2, "down", 982, 841),
        unit(3, "up", 841, 1106),
        unit(4, "down", 1106, 952),
        unit(5, "up", 952, 1273),
        replace(unit(6, "down", 1273, 1067), locked=False, confirmed_at=None),
        replace(unit(7, "up", 1067, 1163), locked=False, confirmed_at=None),
        replace(unit(8, "down", 1163, 999), locked=False, confirmed_at=None),
    )

    result = calculate_centers(values, 0, SourceKind.SEGMENT)

    assert len(result.centers) == 1
    assert result.centers[0].state is CenterState.ONGOING
    assert not any(
        preview.state is CenterPreviewState.FORMING
        for preview in result.previews
    )


def test_deterministic_live_tail_fuzz_preserves_single_active_ownership():
    """Exercise the ownership invariant over many connected live tails."""

    generator = random.Random(20260805)
    seeds = (
        (
            unit(0, "up", 900, 1200),
            unit(1, "down", 1200, 1000),
            unit(2, "up", 1000, 1150),
            unit(3, "down", 1150, 1050),
            unit(4, "up", 1050, 1300),
        ),
        (
            unit(0, "down", 1300, 1000),
            unit(1, "up", 1000, 1200),
            unit(2, "down", 1200, 900),
            unit(3, "up", 900, 1100),
            unit(4, "down", 1100, 800),
        ),
    )
    for case in range(250):
        values = list(seeds[case % 2])
        tail_size = generator.randint(1, 12)
        for index in range(5, 5 + tail_size):
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
            result = calculate_centers(
                tuple(values[:size]),
                0,
                SourceKind.SEGMENT,
            )
            assert sum(
                center.state is CenterState.ONGOING
                for center in result.centers
            ) <= 1
            assert sum(
                preview.state is CenterPreviewState.FORMING
                for preview in result.previews
            ) <= 1


def test_adjacent_forming_preview_does_not_erase_active_center_completion():
    """A new live-edge seed must not regress an older geometric completion.

    The first provisional return already stays outside the active center and
    therefore completes its same-level third-class-point geometry.  Three
    later provisional units may establish a new adjacent preview, but they
    cannot make that earlier completion disappear from the same prefix.
    """

    values = _uptrend_linked_live_preview() + (
        replace(unit(8, "up", 122, 140), locked=False, confirmed_at=None),
    )

    result = calculate_centers(values, 0, SourceKind.SEGMENT)

    completed = [
        preview
        for preview in result.previews
        if preview.state is CenterPreviewState.COMPLETED
    ]
    assert len(completed) == 1
    assert completed[0].unit_ids[:5] == tuple(item.unit_id for item in values[:5])
    assert completed[0].completion_return_unit_id == values[5].unit_id
    assert len(result.previews) == 1


def test_scan_does_not_relax_non_overlapping_entry_without_active_center_tail():
    values = _uptrend_linked_live_preview()[3:]

    result = calculate_centers(values, 0, SourceKind.SEGMENT)

    assert result.centers == ()
    assert not any(
        preview.state is CenterPreviewState.FORMING
        and len(preview.unit_ids) == 5
        for preview in result.previews
    )


@pytest.mark.parametrize(
    "values",
    (valid_five_up_exit(), _direction_flip_then_later_center()),
)
def test_scan_has_at_most_one_ongoing_center_and_only_at_locked_tail(values):
    result = calculate_centers(values, 0, SourceKind.SEGMENT)
    ongoing = [
        center for center in result.centers if center.state is CenterState.ONGOING
    ]
    assert len(ongoing) <= 1
    if ongoing:
        assert ongoing[0] is result.centers[-1]
        assert ongoing[0].body_units[-1] is values[result.locked_unit_count - 1]


def test_completed_center_is_not_rewritten_by_later_prefix_units():
    values = _sh000001_causal_prefix_units()
    completed_at_14 = tuple(
        center.center_id
        for center in calculate_centers(
            values[:14],
            0,
            SourceKind.SEGMENT,
        ).centers
        if center.state is CenterState.COMPLETED
    )
    completed_at_17 = tuple(
        center.center_id
        for center in calculate_centers(
            values,
            0,
            SourceKind.SEGMENT,
        ).centers
        if center.state is CenterState.COMPLETED
    )

    assert len(completed_at_14) == 2
    assert completed_at_17[: len(completed_at_14)] == completed_at_14


def test_zero_width_closed_core_is_formal_center():
    values = (
        unit(0, "up", 90, 120),
        unit(1, "down", 120, 100),
        unit(2, "up", 100, 100),
    )
    result = calculate_centers(values, 0, SourceKind.SEGMENT)
    assert len(result.centers) == 1
    assert result.centers[0].zd_tick == result.centers[0].zg_tick == 100
    assert not any(
        item.state is CenterPreviewState.TOUCH_ONLY
        for item in result.previews
    )


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
        replace(
            base[1],
            direction="up",
            start_tick=base[1].end_tick,
            end_tick=base[1].start_tick,
        ),
        *base[2:],
    )
    disconnected = (
        base[0],
        replace(
            base[1],
            start_tick=base[1].start_tick - 1,
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


def test_recursive_preview_accepts_either_direction_as_departure():
    """Three-trend centers have no external entry-direction constraint."""

    values = (
        unit(0, "up", 90, 120, source_kind=SourceKind.TREND_TYPE),
        unit(1, "down", 120, 100, source_kind=SourceKind.TREND_TYPE),
        unit(
            2,
            "up",
            100,
            115,
            source_kind=SourceKind.TREND_TYPE,
            locked=False,
        ),
        unit(
            3,
            "down",
            115,
            90,
            source_kind=SourceKind.TREND_TYPE,
            locked=False,
        ),
        unit(
            4,
            "up",
            90,
            99,
            source_kind=SourceKind.TREND_TYPE,
            locked=False,
        ),
    )

    result = calculate_centers(values, 0, SourceKind.TREND_TYPE)

    completed = [
        preview
        for preview in result.previews
        if preview.state is CenterPreviewState.COMPLETED
    ]
    assert len(completed) == 1
    assert completed[0].unit_ids == tuple(item.unit_id for item in values[:4])
    assert completed[0].completion_return_unit_id == values[4].unit_id
