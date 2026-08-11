"""Five physical roles at L0 and three core trends plus external entry recursively."""

from decimal import Decimal

from chanlun.core.strict_structure.center_machine import (
    advance_center,
    calculate_centers,
    establish_center,
)
from chanlun.core.strict_structure.models import (
    CenterLevelResult,
    CenterState,
    SourceKind,
    StrictLevelResult,
    StrictStructureResult,
)
from chanlun.core.strict_structure.signals import StrictSignalEngine
from tests.core.strict_structure.helpers import (
    TEST_PRICE_BASIS,
    unit,
    valid_five_up_exit,
)


def _trend_units():
    return (
        unit(
            0,
            "up",
            80,
            120,
            source_kind=SourceKind.TREND_TYPE,
            structural_level=1,
        ),
        unit(
            1,
            "down",
            120,
            100,
            source_kind=SourceKind.TREND_TYPE,
            structural_level=1,
        ),
        unit(
            2,
            "up",
            100,
            115,
            source_kind=SourceKind.TREND_TYPE,
            structural_level=1,
        ),
        unit(
            3,
            "down",
            115,
            105,
            source_kind=SourceKind.TREND_TYPE,
            structural_level=1,
        ),
        # Departure above the [105, 115] core.
        unit(
            4,
            "up",
            105,
            130,
            source_kind=SourceKind.TREND_TYPE,
            structural_level=1,
        ),
        # First return holds the upper boundary (equality is valid).
        unit(
            5,
            "down",
            130,
            115,
            source_kind=SourceKind.TREND_TYPE,
            structural_level=1,
        ),
    )


def test_three_locked_trends_establish_recursive_center() -> None:
    values = _trend_units()

    center = establish_center(
        values[1:4],
        1,
        SourceKind.TREND_TYPE,
        entry_unit=values[0],
    )

    assert center is not None
    assert center.state is CenterState.ONGOING
    assert center.entry_unit is values[0]
    assert center.initial_units == values[1:4]
    assert center.core_units == values[1:4]
    assert (center.zd_tick, center.zg_tick) == (105, 115)
    assert center.pending_leave_unit is None


def test_level_zero_uses_five_segment_gate_and_middle_three_core() -> None:
    values = valid_five_up_exit()

    center = establish_center(
        values[1:], 0, SourceKind.SEGMENT, entry_unit=values[0]
    )
    assert center is not None
    assert center.initial_units == values[1:4]
    assert center.extension_units == ()
    assert center.initial_exit_unit is values[4]
    assert center.establishment_units == values
    assert center.entry_unit is values[0]


def test_recursive_center_completes_only_after_leave_and_return() -> None:
    values = _trend_units()
    center = establish_center(
        values[1:4],
        1,
        SourceKind.TREND_TYPE,
        entry_unit=values[0],
    )
    assert center is not None

    center, _watch = advance_center(center, values[4])
    assert center.state is CenterState.ONGOING
    assert center.pending_leave_unit is values[4]

    center, _complete = advance_center(center, values[5])
    assert center.state is CenterState.COMPLETED
    assert center.completion_leave_unit is values[4]
    assert center.completion_return_unit is values[5]
    assert center.completed_at == values[5].confirmed_at


def test_recursive_scan_emits_first_center_third_buy() -> None:
    values = _trend_units()
    centers = calculate_centers(values, 1, SourceKind.TREND_TYPE)
    assert len(centers.centers) == 1
    assert centers.centers[0].state is CenterState.COMPLETED

    empty_level = StrictLevelResult(
        structural_level=0,
        units=(),
        center_result=CenterLevelResult(
            structural_level=0,
            price_basis_revision=TEST_PRICE_BASIS,
            centers=(),
            previews=(),
            events=(),
            locked_unit_count=0,
            replay_from=0,
        ),
        trend_types=(),
        completed_trends=(),
    )
    recursive_level = StrictLevelResult(
        structural_level=1,
        units=values,
        center_result=centers,
        trend_types=(),
        completed_trends=(),
    )
    structure = StrictStructureResult(
        schema="chanlun-structure",
        price_basis_revision=TEST_PRICE_BASIS,
        levels=(empty_level, recursive_level),
    )

    points = StrictSignalEngine(
        structure=structure,
        price_quantum=Decimal("1"),
    ).third_class_points()

    assert len(points) == 1
    assert points[0].point_type == "3buy"
    assert points[0].structural_level == 1
    assert points[0].center_ordinal == 1


def test_recursive_center_identity_is_prefix_stable() -> None:
    values = _trend_units()
    forming = calculate_centers(values[:5], 1, SourceKind.TREND_TYPE)
    completed = calculate_centers(values, 1, SourceKind.TREND_TYPE)

    assert forming.centers[0].center_id == completed.centers[0].center_id
    assert forming.centers[0].state is CenterState.ONGOING
    assert completed.centers[0].state is CenterState.COMPLETED
