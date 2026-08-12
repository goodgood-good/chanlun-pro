from __future__ import annotations

import random
from decimal import Decimal

from chanlun.core.strict_structure.divergence import (
    collect_formal_divergence_ledger,
)
from chanlun.core.strict_structure.formal_state import resolve_formal_direction
from chanlun.core.strict_structure.identity import (
    build_strict_evidence_revision,
    stable_structure_id,
)
from chanlun.core.strict_structure.models import (
    CenterLevelResult,
    DivergenceEvidence,
    SourceKind,
    StrictEvidenceResult,
    StrictLevelResult,
    StrictPointEvidence,
    StrictPointStatus,
    StrictPointVariant,
    StrictStructureResult,
    build_strict_point_id,
)
from chanlun.core.strict_structure.recursive_engine import (
    calculate_level_with_divergence_boundaries,
)
from chanlun.core.strict_structure.signals import StrictSignalEngine
from tests.core.strict_structure.helpers import TEST_PRICE_BASIS, unit


def _reversal_values(end: int):
    rng = random.Random(39)
    count = rng.randint(35, 90)
    price = 100
    direction = "up"
    values = []
    for index in range(count):
        step = rng.randint(5, 35)
        terminal = price + step if direction == "up" else max(1, price - step)
        values.append(unit(index, direction, price, terminal))
        price = terminal
        direction = "down" if direction == "up" else "up"
    return tuple(values[:end])


def _structure(end: int) -> StrictStructureResult:
    values = _reversal_values(end)
    center_result, assembly = calculate_level_with_divergence_boundaries(
        values,
        0,
        SourceKind.SEGMENT,
    )
    return StrictStructureResult(
        schema="chanlun-structure",
        price_basis_revision=TEST_PRICE_BASIS,
        levels=(
            StrictLevelResult(
                structural_level=0,
                units=values,
                center_result=center_result,
                trend_types=assembly.current_trends,
                completed_trends=assembly.completed_trends,
                decomposition_boundaries=assembly.decomposition_boundaries,
            ),
        ),
    )


def _net_displacement_mismatch_structure() -> StrictStructureResult:
    rng = random.Random(45)
    price = rng.randint(80, 180)
    direction = "up"
    values = []
    for index in range(rng.randint(25, 90)):
        step = rng.randint(3, 45)
        terminal = price + step if direction == "up" else max(1, price - step)
        values.append(unit(index, direction, price, terminal))
        price = terminal
        direction = "down" if direction == "up" else "up"
    selected = tuple(values[:62])
    center_result, assembly = calculate_level_with_divergence_boundaries(
        selected,
        0,
        SourceKind.SEGMENT,
    )
    return StrictStructureResult(
        schema="chanlun-structure",
        price_basis_revision=TEST_PRICE_BASIS,
        levels=(
            StrictLevelResult(
                structural_level=0,
                units=selected,
                center_result=center_result,
                trend_types=assembly.current_trends,
                completed_trends=assembly.completed_trends,
                decomposition_boundaries=assembly.decomposition_boundaries,
            ),
        ),
    )


def _bundle(structure, additional_points=()) -> StrictEvidenceResult:
    points = (
        *StrictSignalEngine(
            structure=structure,
            strength=None,
            price_quantum=Decimal("0.01"),
        ).confirmed_points(),
        *additional_points,
    )
    points = tuple(
        sorted(
            points,
            key=lambda point: (
                point.available_at,
                point.structural_level,
                point.point_type,
                point.point_id,
            ),
        )
    )
    divergences = collect_formal_divergence_ledger(structure, points)
    source_closed_at = max(unit.available_at for unit in structure.levels[0].units)
    revision = build_strict_evidence_revision(
        symbol="TEST",
        source_frequency="1m",
        price_basis_revision=TEST_PRICE_BASIS,
        strict_config_revision="strict-config",
        structure=structure,
        confirmed_points=points,
        divergences=divergences,
    )
    observations = CenterLevelResult(
        structural_level=0,
        price_basis_revision=TEST_PRICE_BASIS,
        centers=(),
        previews=(),
        events=(),
        locked_unit_count=0,
        replay_from=0,
    )
    return StrictEvidenceResult(
        symbol="TEST",
        source_frequency="1m",
        source_closed_at=source_closed_at,
        price_basis_revision=TEST_PRICE_BASIS,
        structure_price_quantum=Decimal("0.01"),
        strict_config_revision="strict-config",
        structure_revision=revision,
        structure=structure,
        stroke_center_observations=observations,
        confirmed_points=points,
        approaching_points=(),
        divergences=divergences,
    )


def _first_sell_support(structure: StrictStructureResult) -> StrictPointEvidence:
    level = structure.levels[0]
    previous = level.trend_types[-2]
    compare = level.units[16]
    signal = level.units[22]
    assert previous.direction == "up"
    assert compare.direction == signal.direction == "up"
    assert signal.confirmed_at is not None
    divergence = DivergenceEvidence(
        divergence_id=stable_structure_id(
            "chanlun-strict-divergence",
            TEST_PRICE_BASIS,
            0,
            SourceKind.SEGMENT.value,
            "trend",
            "up",
            (compare.unit_id,),
            (signal.unit_id,),
        ),
        structural_level=0,
        source_kind=SourceKind.SEGMENT,
        price_basis_revision=TEST_PRICE_BASIS,
        kind="trend",
        direction="up",
        compare_unit_id=compare.unit_id,
        signal_unit_id=signal.unit_id,
        anchor_at=signal.market_end,
        anchor_tick=signal.high_tick,
        confirmed_at=signal.confirmed_at,
        available_at=signal.available_at,
        price_extreme_confirmed=True,
        histogram_area_decayed=True,
        histogram_peak_decayed=False,
        dif_extreme_decayed=False,
        strength_source="macd",
    )
    center = previous.centers[-1]
    return StrictPointEvidence(
        point_id=build_strict_point_id(
            price_basis_revision=TEST_PRICE_BASIS,
            point_type="1sell",
            structural_level=0,
            anchor_unit_id=signal.unit_id,
            center_id=center.center_id,
            parent_point_id=None,
        ),
        point_type="1sell",
        side="sell",
        status=StrictPointStatus.CONFIRMED,
        variant=StrictPointVariant.STANDARD,
        structural_level=0,
        source_kind=SourceKind.SEGMENT,
        price_basis_revision=TEST_PRICE_BASIS,
        anchor_unit_id=signal.unit_id,
        anchor_at=signal.market_end,
        confirmed_at=signal.confirmed_at,
        available_at=signal.available_at,
        price_quantum=Decimal("0.01"),
        anchor_tick=signal.high_tick,
        invalidation_tick=signal.high_tick,
        center_id=center.center_id,
        center_zd_tick=center.zd_tick,
        center_zg_tick=center.zg_tick,
        center_ordinal=None,
        divergence=divergence,
        parent_point_id=None,
        evidence_codes=("formal_trend", "trend_divergence"),
    )


def test_current_directional_trend_is_published():
    state = resolve_formal_direction(_bundle(_structure(23)))

    assert state.direction == "up"
    assert state.reason_codes == ("current_directional_trend",)


def test_consolidation_net_displacement_is_never_published_as_direction():
    state = resolve_formal_direction(_bundle(_structure(30)))

    assert state.direction == "neutral"
    assert state.reason_codes == ("current_formal_movement_is_consolidation",)


def test_direction_change_without_first_or_second_point_stays_neutral():
    structure = _structure(38)
    state = resolve_formal_direction(_bundle(structure))

    assert state.direction == "neutral"
    assert state.reason_codes == (
        "direction_change_lacks_first_or_second_point",
    )


def test_direction_change_with_first_point_is_published_and_auditable():
    structure = _structure(38)
    support = _first_sell_support(structure)
    state = resolve_formal_direction(_bundle(structure, (support,)))

    assert state.direction == "down"
    assert state.support_point_id == support.point_id
    assert state.reason_codes == (
        "current_directional_trend",
        "direction_change_supported_by_first_or_second_point",
    )


def test_formal_direction_uses_center_relation_not_net_displacement():
    structure = _net_displacement_mismatch_structure()
    level = structure.levels[0]
    current = level.trend_types[-1]
    assert current.direction == "up"
    state = resolve_formal_direction(_bundle(structure))

    assert state.direction == "down"
    assert state.reason_codes == ("current_directional_trend",)
