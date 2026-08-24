"""Every recursive layer establishes a center from three completed units."""

from dataclasses import replace
from decimal import Decimal

from chanlun.core.strict_structure.center_machine import (
    advance_center,
    calculate_centers,
    establish_center,
    establish_center_preview,
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


def test_recursive_center_needs_only_three_completed_lower_trends() -> None:
    trends = (
        unit(
            20,
            "up",
            120,
            140,
            source_kind=SourceKind.TREND_TYPE,
            structural_level=1,
        ),
        unit(
            21,
            "down",
            140,
            125,
            source_kind=SourceKind.TREND_TYPE,
            structural_level=1,
        ),
        unit(
            22,
            "up",
            125,
            135,
            source_kind=SourceKind.TREND_TYPE,
            structural_level=1,
        ),
    )

    center = establish_center(trends, 1, SourceKind.TREND_TYPE)

    assert center is not None
    assert center.entry_unit is None
    assert center.initial_units == trends
    assert center.core_units == trends
    assert center.pending_leave_unit is None


def test_recursive_touching_trends_form_closed_interval_center() -> None:
    values = (
        unit(
            30,
            "up",
            90,
            110,
            source_kind=SourceKind.TREND_TYPE,
            structural_level=1,
        ),
        unit(
            31,
            "up",
            110,
            120,
            source_kind=SourceKind.TREND_TYPE,
            structural_level=1,
        ),
        unit(
            32,
            "down",
            120,
            110,
            source_kind=SourceKind.TREND_TYPE,
            structural_level=1,
        ),
    )

    result = calculate_centers(
        values,
        1,
        SourceKind.TREND_TYPE,
        oscillatory_ids=frozenset({values[1].unit_id}),
    )

    assert len(result.centers) == 1
    assert result.centers[0].initial_units == values
    assert (result.centers[0].zd_tick, result.centers[0].zg_tick) == (110, 110)


def test_recursive_center_identity_ignores_optional_prefix_entry() -> None:
    values = _trend_units()
    core = values[1:4]

    without_entry = establish_center(core, 1, SourceKind.TREND_TYPE)
    with_entry = establish_center(
        core,
        1,
        SourceKind.TREND_TYPE,
        entry_unit=values[0],
    )

    assert without_entry is not None and with_entry is not None
    assert without_entry.entry_unit is None
    assert with_entry.entry_unit is values[0]
    assert with_entry.center_id == without_entry.center_id
    reconstructed = replace(without_entry, entry_unit=values[0])
    assert reconstructed.entry_unit is values[0]
    assert reconstructed.center_id == without_entry.center_id


def test_recursive_scan_keeps_core_identity_after_leading_prefix_is_added() -> None:
    values = _trend_units()
    core = values[1:4]
    non_center_prefix = replace(
        values[0],
        start_tick=119,
        end_tick=120,
        low_tick=119,
        high_tick=120,
    )

    without_prefix = calculate_centers(core, 1, SourceKind.TREND_TYPE)
    with_prefix = calculate_centers(
        (non_center_prefix, *core),
        1,
        SourceKind.TREND_TYPE,
    )

    assert len(without_prefix.centers) == len(with_prefix.centers) == 1
    assert without_prefix.centers[0].entry_unit is None
    assert with_prefix.centers[0].entry_unit is non_center_prefix
    assert with_prefix.centers[0].center_id == without_prefix.centers[0].center_id


def test_recursive_preview_identity_ignores_optional_prefix_entry() -> None:
    values = _trend_units()
    core = values[1:3] + (
        replace(values[3], locked=False, confirmed_at=None),
    )

    without_entry = establish_center_preview(core, 1, SourceKind.TREND_TYPE)
    with_entry = establish_center_preview(
        core,
        1,
        SourceKind.TREND_TYPE,
        entry_unit=values[0],
    )

    assert without_entry is not None and with_entry is not None
    assert without_entry.entry_unit_id is None
    assert with_entry.entry_unit_id == values[0].unit_id
    assert with_entry.formal_center_id == without_entry.formal_center_id


def test_level_zero_uses_five_physical_roles_and_middle_three_core() -> None:
    values = valid_five_up_exit()

    center = establish_center(values, 0, SourceKind.SEGMENT)
    assert center is not None
    assert center.initial_units == values[1:4]
    assert center.extension_units == ()
    assert center.entry_unit is values[0]
    assert center.establishment_leave_unit is values[4]
    assert center.pending_leave_unit is values[4]

    outside_return = unit(5, "down", values[4].end_tick, 120)
    completed, _event = advance_center(center, outside_return)

    assert completed.completion_leave_unit is values[4]
    assert completed.completion_return_unit is outside_return


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
