from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from chanlun.core.strict_structure.center_machine import advance_center, establish_center
from chanlun.core.strict_structure.models import (
    CenterState,
    SourceKind,
    StrictLevelResult,
    StrictStructureResult,
)
from chanlun.core.strict_structure.upgrade_evidence import (
    UpgradeEvidenceKind,
    UpgradeEvidenceStatus,
    collect_recursive_upgrade_evidence,
)
from tests.core.strict_structure.helpers import (
    completed_up_center,
    ongoing_center,
    structure_for,
    unit,
)


def nine_touch_center(*, structural_level: int = 0):
    source_kind = (
        SourceKind.SEGMENT
        if structural_level == 0
        else SourceKind.TREND_TYPE
    )
    if structural_level == 0:
        center = ongoing_center(structural_level=structural_level)
    else:
        initial = (
            unit(
                0,
                "up",
                90,
                120,
                structural_level=structural_level,
                source_kind=source_kind,
            ),
            unit(
                1,
                "down",
                120,
                100,
                structural_level=structural_level,
                source_kind=source_kind,
            ),
            unit(
                2,
                "up",
                100,
                115,
                structural_level=structural_level,
                source_kind=source_kind,
            ),
            unit(
                3,
                "down",
                115,
                105,
                structural_level=structural_level,
                source_kind=source_kind,
            ),
        )
        center = establish_center(
            initial[1:],
            structural_level,
            source_kind,
            entry_unit=initial[0],
        )
        assert center is not None
        center, _ = advance_center(
            center,
            unit(
                4,
                "up",
                105,
                130,
                structural_level=structural_level,
                source_kind=source_kind,
            ),
        )
    # u4 是成立窗口的第五段离开；u5 回到核心后，u4/u5 才成为
    # 延伸体。再追加四段核心内震荡，正好形成九个真实触核 body units。
    additions = (
        unit(5, "down", 130, 110, structural_level=structural_level, source_kind=source_kind),
        unit(6, "up", 110, 114, structural_level=structural_level, source_kind=source_kind),
        unit(7, "down", 114, 106, structural_level=structural_level, source_kind=source_kind),
        unit(8, "up", 106, 114, structural_level=structural_level, source_kind=source_kind),
        unit(9, "down", 114, 110, structural_level=structural_level, source_kind=source_kind),
    )
    for item in additions:
        center, _ = advance_center(center, item)
    completed, _ = advance_center(
        center,
        unit(10, "up", 110, 130, structural_level=structural_level, source_kind=source_kind),
    )
    completed, _ = advance_center(
        completed,
        unit(11, "down", 130, 116, structural_level=structural_level, source_kind=source_kind),
    )
    assert completed.state is CenterState.COMPLETED
    assert len(completed.body_units) == 9
    return completed


def test_nine_touching_units_derive_one_higher_context() -> None:
    center = nine_touch_center()
    structure = structure_for(center)

    result = collect_recursive_upgrade_evidence(structure)

    assert len(result) == 1
    evidence = result[0]
    assert evidence.kind is UpgradeEvidenceKind.NINE_SEGMENT_DERIVATION
    assert evidence.status is UpgradeEvidenceStatus.CONFIRMED_DERIVED_CENTER
    assert evidence.source_level == 0 and evidence.target_level == 1
    assert evidence.source_center_ids == (center.center_id,)
    assert len(evidence.source_unit_ids) == 9
    assert evidence.extension_unit_ids == ()
    assert evidence.available_at >= center.available_at
    assert evidence.signal_eligible is False


def test_fewer_than_nine_touching_units_do_not_upgrade() -> None:
    center = completed_up_center()
    assert len(center.body_units) < 9
    assert collect_recursive_upgrade_evidence(structure_for(center)) == ()


def test_expansion_is_reclassification_not_completed_signal_center() -> None:
    first = completed_up_center(0, zd_tick=105, zg_tick=115)
    second = completed_up_center(20, zd_tick=116, zg_tick=126)
    structure = structure_for(first, second)

    result = collect_recursive_upgrade_evidence(structure)

    expansion = [
        item for item in result if item.kind is UpgradeEvidenceKind.CENTER_EXPANSION
    ]
    assert len(expansion) == 1
    evidence = expansion[0]
    assert evidence.status is UpgradeEvidenceStatus.EXPANSION_RECLASSIFYING
    assert evidence.source_center_ids == (first.center_id, second.center_id)
    assert evidence.signal_eligible is False


def test_core_overlap_is_extension_family_not_expansion() -> None:
    first = completed_up_center(0, zd_tick=105, zg_tick=115)
    second = completed_up_center(20, zd_tick=110, zg_tick=120)
    result = collect_recursive_upgrade_evidence(structure_for(first, second))
    assert all(item.kind is not UpgradeEvidenceKind.CENTER_EXPANSION for item in result)


def test_only_tail_pair_can_remain_expansion_reclassifying() -> None:
    first = completed_up_center(0, zd_tick=105, zg_tick=115)
    second = completed_up_center(20, zd_tick=116, zg_tick=126)
    third = completed_up_center(40, zd_tick=300, zg_tick=310)

    result = collect_recursive_upgrade_evidence(structure_for(first, second, third))

    # first-second was an expansion state in an earlier prefix, but the final
    # snapshot has a later, wave-separated center.  It must not remain active.
    assert all(item.kind is not UpgradeEvidenceKind.CENTER_EXPANSION for item in result)


def test_nine_segment_evidence_links_existing_standard_target_center() -> None:
    source = nine_touch_center()
    target = nine_touch_center(structural_level=1)
    level_zero = structure_for(source).levels[0]
    target_units = (
        target.entry_unit,
        *target.body_units,
        target.completion_leave_unit,
        target.completion_return_unit,
    )
    level_one = StrictLevelResult(
        structural_level=1,
        units=target_units,
        center_result=replace(
            level_zero.center_result,
            structural_level=1,
            centers=(target,),
            price_basis_revision=target.price_basis_revision,
            locked_unit_count=len(target_units),
        ),
        trend_types=(),
        completed_trends=(),
    )
    structure = StrictStructureResult(
        schema_version="chanlun-structure/v3",
        price_basis_revision=source.price_basis_revision,
        levels=(level_zero, level_one),
    )

    evidence = collect_recursive_upgrade_evidence(structure)[0]
    assert evidence.resolved_by_standard_center_id == target.center_id


def test_future_standard_target_does_not_rewrite_nine_segment_evidence() -> None:
    source = nine_touch_center()
    target = nine_touch_center(structural_level=1)
    future = source.available_at + timedelta(days=1)
    future_return = replace(
        target.completion_return_unit,
        confirmed_at=future,
        available_at=future,
    )
    target = replace(
        target,
        completion_return_unit=future_return,
        completed_at=future,
        available_at=future,
    )
    level_zero = structure_for(source).levels[0]
    target_units = (
        target.entry_unit,
        *target.body_units,
        target.completion_leave_unit,
        target.completion_return_unit,
    )
    level_one = StrictLevelResult(
        structural_level=1,
        units=target_units,
        center_result=replace(
            level_zero.center_result,
            structural_level=1,
            centers=(target,),
            price_basis_revision=target.price_basis_revision,
            locked_unit_count=len(target_units),
        ),
        trend_types=(),
        completed_trends=(),
    )
    structure = StrictStructureResult(
        schema_version="chanlun-structure/v3",
        price_basis_revision=source.price_basis_revision,
        levels=(level_zero, level_one),
    )

    without_future_target = collect_recursive_upgrade_evidence(
        StrictStructureResult(
            schema_version="chanlun-structure/v3",
            price_basis_revision=source.price_basis_revision,
            levels=(level_zero,),
        )
    )[0]
    with_future_target = collect_recursive_upgrade_evidence(structure)[0]

    assert with_future_target == without_future_target
    assert with_future_target.resolved_by_standard_center_id is None


def test_as_of_cannot_see_future_nine_segment_or_expansion_evidence() -> None:
    long_center = nine_touch_center()
    first = completed_up_center(20, zd_tick=200, zg_tick=210)
    second = completed_up_center(40, zd_tick=211, zg_tick=221)
    structure = structure_for(long_center, first, second)

    before = min(long_center.available_at, first.available_at, second.available_at) - timedelta(
        seconds=1
    )
    assert collect_recursive_upgrade_evidence(structure, as_of=before) == ()

    at_long_center = long_center.available_at
    visible = collect_recursive_upgrade_evidence(structure, as_of=at_long_center)
    assert [item.kind for item in visible] == [
        UpgradeEvidenceKind.NINE_SEGMENT_DERIVATION
    ]


def test_as_of_requires_timezone_awareness() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        collect_recursive_upgrade_evidence(
            structure_for(nine_touch_center()),
            as_of=nine_touch_center().available_at.replace(tzinfo=None),
        )
