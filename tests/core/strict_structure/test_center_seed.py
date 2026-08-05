from dataclasses import replace

import pytest

from chanlun.core.strict_structure import center_machine
from chanlun.core.strict_structure.center_machine import (
    advance_center,
    calculate_centers,
    establish_center,
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
    valid_five_up_exit,
    valid_three_center_seed,
)


def test_first_three_locked_units_establish_original_text_center():
    seed = valid_three_center_seed()

    value = establish_center(seed, 0, SourceKind.SEGMENT)

    assert value is not None
    assert value.state is CenterState.ONGOING
    assert value.initial_units == seed
    assert value.core_units == seed
    assert value.entry_unit is seed[0]  # deprecated compatibility alias
    assert value.initial_exit_unit is seed[-1]  # compatibility alias
    assert value.pending_leave_unit is None
    assert (value.zd_tick, value.zg_tick) == (105, 115)
    assert value.established_at == seed[-1].confirmed_at
    assert value.body_units == seed
    assert value.price_basis_revision == TEST_PRICE_BASIS


def test_fourth_can_extend_and_fifth_can_become_departure():
    lifecycle = valid_five_up_exit()
    value = establish_center(lifecycle[:3], 0, SourceKind.SEGMENT)
    assert value is not None

    extended, first = advance_center(value, lifecycle[3])
    pending, event = advance_center(extended, lifecycle[4])

    assert first.kind is CenterEventKind.EXTENDED
    assert pending.state is CenterState.ONGOING
    assert pending.pending_leave_unit is lifecycle[4]
    assert pending.body_units == lifecycle
    assert event.kind is CenterEventKind.BREAKOUT_WATCH_UP


def test_first_locked_return_outside_completes_center():
    lifecycle = valid_five_up_exit()
    ret = unit(5, "down", lifecycle[-1].end_tick, 120)
    result = calculate_centers(lifecycle + (ret,), 0, SourceKind.SEGMENT)

    assert len(result.centers) == 1
    completed = result.centers[0]
    assert completed.state is CenterState.COMPLETED
    assert completed.completion_leave_unit is lifecycle[4]
    assert completed.completion_return_unit is ret
    assert result.events[-1].kind is CenterEventKind.COMPLETED_UP


def test_fewer_than_three_locked_units_never_establish_formal_center():
    seed = valid_three_center_seed()
    assert establish_center(seed[:1], 0, SourceKind.SEGMENT) is None
    assert establish_center(seed[:2], 0, SourceKind.SEGMENT) is None


def test_first_three_reject_when_closed_intersection_is_empty():
    values = (
        unit(0, "up", 90, 120),
        unit(1, "down", 120, 70),
        unit(2, "up", 70, 80),
    )
    assert establish_center(values, 0, SourceKind.SEGMENT) is None


def test_closed_boundary_intersection_is_a_formal_center_not_touch_only():
    values = (
        unit(0, "up", 90, 120),
        unit(1, "down", 120, 100),
        unit(2, "up", 100, 100),
    )

    value = establish_center(values, 0, SourceKind.SEGMENT)

    assert value is not None
    assert value.zd_tick == value.zg_tick == 100
    preview = forming_preview(values, 0, SourceKind.SEGMENT)
    assert preview is not None
    assert preview.state is CenterPreviewState.FORMING


def test_unlocked_third_component_establishes_forming_preview_only():
    seed = valid_three_center_seed()
    active = seed[:-1] + (
        replace(seed[-1], locked=False, confirmed_at=None),
    )
    assert establish_center(active, 0, SourceKind.SEGMENT) is None
    preview_builder = getattr(center_machine, "establish_center_preview", None)
    assert callable(preview_builder), "establish_center_preview is required"

    preview = preview_builder(active, 0, SourceKind.SEGMENT)

    assert preview is not None
    assert preview.state is CenterPreviewState.FORMING
    assert preview.price_basis_revision == TEST_PRICE_BASIS
    assert preview.unit_ids == tuple(item.unit_id for item in active)
    assert (preview.zd_tick, preview.zg_tick) == (105, 115)
    result = calculate_centers(active, 0, SourceKind.SEGMENT)
    assert result.centers == ()
    assert result.previews == (preview,)


def test_locked_seed_remains_formal_when_later_departure_is_unlocked():
    lifecycle = valid_five_up_exit()
    active_leave = replace(
        lifecycle[4], locked=False, confirmed_at=None
    )
    result = calculate_centers(
        lifecycle[:4] + (active_leave,), 0, SourceKind.SEGMENT
    )

    assert len(result.centers) == 1
    assert result.centers[0].state is CenterState.ONGOING
    assert result.centers[0].pending_leave_unit is None
    assert result.locked_unit_count == 4
    assert result.previews
    assert all(not hasattr(item, "center_id") for item in result.previews)


def test_center_identity_namespace_includes_price_basis_revision():
    seed = valid_three_center_seed()
    original = establish_center(seed, 0, SourceKind.SEGMENT)
    rebased_seed = tuple(
        replace(item, price_basis_revision="post-action-v2") for item in seed
    )
    rebased = establish_center(rebased_seed, 0, SourceKind.SEGMENT)
    assert original is not None and rebased is not None
    assert original.center_id != rebased.center_id


def test_initial_units_reject_mixed_basis_instead_of_squeezing_it_into_center():
    seed = valid_three_center_seed()
    mixed = seed[:-1] + (
        replace(seed[-1], price_basis_revision="post-action-v2"),
    )
    with pytest.raises(ValueError, match="seed price basis mismatch"):
        establish_center(mixed, 0, SourceKind.SEGMENT)
    with pytest.raises(ValueError, match="seed price basis mismatch"):
        forming_preview(mixed, 0, SourceKind.SEGMENT)


def test_seed_rejects_disconnected_market_or_price_sequence():
    seed = valid_three_center_seed()
    disconnected = replace(
        seed[1],
        start_tick=seed[1].start_tick - 1,
        low_tick=min(seed[1].low_tick, seed[1].start_tick - 1),
    )
    with pytest.raises(ValueError, match="seed prices must connect"):
        establish_center((seed[0], disconnected, seed[2]), 0, SourceKind.SEGMENT)
