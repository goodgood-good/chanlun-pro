from __future__ import annotations

import random
from dataclasses import replace
from decimal import Decimal

from chanlun.core.strict_structure.divergence import (
    collect_formal_divergence_ledger,
)
from chanlun.core.strict_structure.formal_state import (
    resolve_formal_direction,
    resolve_formal_direction_from_components,
    resolve_level_formal_direction,
    semantic_trend_direction,
)
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
    TrendKind,
    TrendState,
    build_strict_point_id,
)
from chanlun.core.strict_structure.recursive_engine import (
    calculate_level_with_divergence_boundaries,
)
from chanlun.core.strict_structure.signals import StrictSignalEngine
from tests.core.strict_structure.helpers import TEST_PRICE_BASIS, unit


def _alternating_values(*, seed: int, count: int):
    rng = random.Random(seed)
    price = rng.randint(80, 180)
    direction = "up"
    values = []
    for index in range(count):
        step = rng.randint(3, 45)
        terminal = price + step if direction == "up" else max(1, price - step)
        values.append(unit(index, direction, price, terminal))
        price = terminal
        direction = "down" if direction == "up" else "up"
    return tuple(values)


def _structure(*, seed: int, count: int) -> StrictStructureResult:
    values = _alternating_values(seed=seed, count=count)
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


def _open_terminal_trend_view(
    structure: StrictStructureResult,
) -> StrictStructureResult:
    """Expose one still-open trend so endpoint displacement cannot pick direction."""

    level = structure.levels[0]
    terminal = level.trend_types[-1]
    assert terminal.kind is TrendKind.TREND
    assert terminal.state is TrendState.COMPLETE
    assert not level.decomposition_boundaries
    forming = replace(
        terminal,
        state=TrendState.FORMING,
        confirmed_at=None,
    )
    return replace(
        structure,
        levels=(replace(level, trend_types=(forming,)),),
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


def _first_class_reversal_support(
    structure: StrictStructureResult,
) -> StrictPointEvidence:
    level = structure.levels[0]
    current = level.trend_types[-1]
    previous = next(
        trend
        for trend in reversed(level.trend_types[:-1])
        if trend.kind is TrendKind.TREND
    )
    current_direction = semantic_trend_direction(current)
    previous_direction = semantic_trend_direction(previous)
    assert current_direction in {"up", "down"}
    assert previous_direction in {"up", "down"}
    assert current_direction != previous_direction

    signal_direction = "down" if current_direction == "up" else "up"
    eligible = tuple(
        item
        for item in current.constituent_units
        if item.locked
        and item.confirmed_at is not None
        and item.direction == signal_direction
        and previous.market_end <= item.market_end <= current.market_end
    )
    assert len(eligible) >= 2
    compare, signal = eligible[-2:]
    assert signal.confirmed_at is not None
    divergence = DivergenceEvidence(
        divergence_id=stable_structure_id(
            "chanlun-strict-divergence",
            TEST_PRICE_BASIS,
            0,
            SourceKind.SEGMENT.value,
            "trend",
            signal_direction,
            (compare.unit_id,),
            (signal.unit_id,),
        ),
        structural_level=0,
        source_kind=SourceKind.SEGMENT,
        price_basis_revision=TEST_PRICE_BASIS,
        kind="trend",
        direction=signal_direction,
        compare_unit_id=compare.unit_id,
        signal_unit_id=signal.unit_id,
        anchor_at=signal.market_end,
        anchor_tick=(
            signal.low_tick if current_direction == "up" else signal.high_tick
        ),
        confirmed_at=signal.confirmed_at,
        available_at=signal.available_at,
        price_extreme_confirmed=True,
        histogram_area_decayed=True,
        histogram_peak_decayed=False,
        dif_extreme_decayed=False,
        strength_source="macd",
    )
    center = current.centers[-1]
    point_type = "1buy" if current_direction == "up" else "1sell"
    side = "buy" if current_direction == "up" else "sell"
    return StrictPointEvidence(
        point_id=build_strict_point_id(
            price_basis_revision=TEST_PRICE_BASIS,
            point_type=point_type,
            structural_level=0,
            anchor_unit_id=signal.unit_id,
            center_id=center.center_id,
            parent_point_id=None,
        ),
        point_type=point_type,
        side=side,
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
        anchor_tick=divergence.anchor_tick,
        invalidation_tick=divergence.anchor_tick,
        center_id=center.center_id,
        center_zd_tick=center.zd_tick,
        center_zg_tick=center.zg_tick,
        center_ordinal=None,
        divergence=divergence,
        parent_point_id=None,
        evidence_codes=("formal_trend", "trend_divergence"),
    )


def test_current_directional_trend_is_published():
    structure = _structure(seed=45, count=62)
    current = structure.levels[0].trend_types[-1]
    assert current.kind is TrendKind.TREND
    assert current.state is TrendState.FORMING
    expected_direction = semantic_trend_direction(current)
    assert expected_direction in {"up", "down"}
    state = resolve_formal_direction(_bundle(structure))

    assert state.direction == expected_direction
    assert state.reason_codes == ("current_directional_trend",)


def test_consolidation_net_displacement_is_never_published_as_direction():
    structure = _structure(seed=39, count=30)
    assert structure.levels[0].trend_types[-1].kind is TrendKind.CONSOLIDATION
    state = resolve_formal_direction(_bundle(structure))

    assert state.direction == "neutral"
    assert state.reason_codes == ("current_formal_movement_is_consolidation",)


def test_direction_change_without_first_or_second_point_stays_neutral():
    structure = _structure(seed=13, count=140)
    trend_types = structure.levels[0].trend_types
    current = trend_types[-1]
    previous = next(
        trend
        for trend in reversed(trend_types[:-1])
        if trend.kind is TrendKind.TREND
    )
    assert semantic_trend_direction(previous) == "down"
    assert semantic_trend_direction(current) == "up"
    state = resolve_formal_direction(_bundle(structure))

    assert state.direction == "neutral"
    assert state.reason_codes == ("direction_change_lacks_first_or_second_point",)


def test_direction_change_with_first_point_is_published_and_auditable():
    structure = _structure(seed=13, count=140)
    support = _first_class_reversal_support(structure)
    state = resolve_formal_direction(_bundle(structure, (support,)))

    assert state.direction == "up"
    assert state.support_point_id == support.point_id
    assert state.reason_codes == (
        "current_directional_trend",
        "direction_change_supported_by_first_or_second_point",
    )


def test_formal_direction_uses_center_relation_not_net_displacement():
    structure = _structure(seed=107, count=60)
    level = structure.levels[0]
    current = level.trend_types[-1]
    assert current.direction == "up"
    assert semantic_trend_direction(current) == "down"
    current_view = _open_terminal_trend_view(structure)
    source_closed_at = max(item.available_at for item in level.units)
    state = resolve_formal_direction_from_components(
        structure=current_view,
        confirmed_points=(),
        source_closed_at=source_closed_at,
    )

    assert state.direction == "down"
    assert state.reason_codes == ("current_directional_trend",)


def test_level_direction_and_global_direction_share_one_resolver():
    evidence = _bundle(_structure(seed=13, count=140))

    assert resolve_level_formal_direction(evidence, 0) == resolve_formal_direction(
        evidence
    )


def test_component_direction_matches_full_revision_hashed_evidence():
    structure = _structure(seed=13, count=140)
    evidence = _bundle(structure, (_first_class_reversal_support(structure),))

    assert resolve_formal_direction_from_components(
        structure=evidence.structure,
        confirmed_points=evidence.confirmed_points,
        source_closed_at=evidence.source_closed_at,
    ) == resolve_formal_direction(evidence)


def test_semantic_direction_is_independent_from_endpoint_displacement():
    structure = _structure(seed=107, count=60)
    current = structure.levels[0].trend_types[-1]

    assert current.direction == "up"
    assert semantic_trend_direction(current) == "down"
