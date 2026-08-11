from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from chanlun.core.strict_structure.center_machine import (
    advance_center,
    calculate_centers,
    establish_center,
)
from chanlun.core.strict_structure.divergence import merge_formal_divergence_ledger
from chanlun.core.strict_structure.identity import (
    build_strict_evidence_revision,
    build_trend_id,
)
from chanlun.core.strict_structure.models import (
    CenterLevelResult,
    CenterState,
    ConstituentUnit,
    SourceKind,
    StrictEvidenceResult,
    StrictLevelResult,
    StrictStructureResult,
    TrendCenter,
    TrendKind,
    TrendState,
    TrendType,
    build_strict_point_id,
)
from chanlun.core.strict_structure.same_level_decomposition import (
    combine_same_level_trends,
)
from chanlun.core.strict_structure.signals import StrictSignalEngine
from chanlun.core.strict_structure.unit_adapter import trend_type_to_unit
from chanlun.decision_support.trading_system.direct_recursive_structure import (
    build_direct_recursive_structure_path,
)
from tests.core.strict_structure.helpers import TEST_PRICE_BASIS, unit
from tests.core.strict_structure.signal_helpers import confirmed_point


CODE = "SZ.000001"


def _center_result(
    level: int,
    units: tuple[ConstituentUnit, ...],
    centers: tuple[TrendCenter, ...],
) -> CenterLevelResult:
    return CenterLevelResult(
        structural_level=level,
        price_basis_revision=TEST_PRICE_BASIS,
        centers=centers,
        previews=(),
        events=(),
        locked_unit_count=len(units),
        replay_from=0,
    )


def _center_geometry(
    direction: str,
    start: int,
    end: int,
) -> tuple[tuple[str, int, int, int, int], ...]:
    """Return entry/body/leave/return geometry with a stable net endpoint.

    The zero-length return is intentional: it completes the center while the
    next locked trend can still start at the previous trend's terminal price.
    A zero-displacement trend uses a wick to keep the physical entry inside
    the nested center without changing its recursive endpoint.
    """

    if start == end:
        if direction == "up":
            return (
                ("up", start, start, start - 15, start),
                ("down", start, start - 20, start - 20, start),
                ("up", start - 20, start - 10, start - 20, start - 10),
                ("down", start - 10, start - 15, start - 15, start - 10),
                ("up", start - 15, start, start - 15, start),
                ("down", start, start, start, start),
            )
        return (
            ("down", start, start, start, start + 15),
            ("up", start, start + 20, start, start + 20),
            ("down", start + 20, start + 10, start + 10, start + 20),
            ("up", start + 10, start + 15, start + 10, start + 15),
            ("down", start + 15, start, start, start + 15),
            ("up", start, start, start, start),
        )
    if direction == "up":
        assert end - start >= 4
        return (
            ("up", start, end, start, end),
            ("down", end, start, start, end),
            ("up", start, end - 2, start, end - 2),
            ("down", end - 2, start + 1, start + 1, end - 2),
            ("up", start + 1, end, start + 1, end),
            ("down", end, end, end, end),
        )
    assert start - end >= 4
    return (
        ("down", start, end, end, start),
        ("up", end, start, end, start),
        ("down", start, end + 2, end + 2, start),
        ("up", end + 2, start - 1, end + 2, start - 1),
        ("down", start - 1, end, end, start - 1),
        ("up", end, end, end, end),
    )


def _physical_center_geometry(
    direction: str,
    start: int,
    end: int,
) -> tuple[tuple[str, int, int, int, int], ...]:
    if start == end:
        return _center_geometry(direction, start, end)
    if direction == "up":
        assert end - start >= 4
        return (
            ("up", start, start + 3, start, start + 3),
            ("down", start + 3, start, start, start + 3),
            ("up", start, start + 2, start, start + 2),
            ("down", start + 2, start + 1, start + 1, start + 2),
            ("up", start + 1, end, start + 1, end),
            ("down", end, end, end, end),
        )
    assert start - end >= 4
    return (
        ("down", start, start - 3, start - 3, start),
        ("up", start - 3, start, start - 3, start),
        ("down", start, start - 2, start - 2, start),
        ("up", start - 2, start - 1, start - 2, start - 1),
        ("down", start - 1, end, end, start - 1),
        ("up", end, end, end, end),
    )


def _locked_consolidation(
    *,
    level: int,
    direction: str,
    center: TrendCenter,
    constituents: tuple[ConstituentUnit, ...],
) -> TrendType:
    confirmed_at = center.completed_at
    assert confirmed_at is not None
    return TrendType(
        trend_id=build_trend_id(
            price_basis_revision=TEST_PRICE_BASIS,
            structural_level=level,
            center_ids=(center.center_id,),
            constituent_unit_ids=tuple(item.unit_id for item in constituents),
            direction=direction,
        ),
        structural_level=level,
        price_basis_revision=TEST_PRICE_BASIS,
        kind=TrendKind.CONSOLIDATION,
        direction=direction,
        state=TrendState.LOCKED,
        centers=(center,),
        constituent_units=constituents,
        start_tick=constituents[0].start_tick,
        end_tick=constituents[-1].end_tick,
        low_tick=min(item.low_tick for item in constituents),
        high_tick=max(item.high_tick for item in constituents),
        market_start=constituents[0].market_start,
        market_end=constituents[-1].market_end,
        confirmed_at=confirmed_at,
        available_at=max(
            center.available_at,
            *(item.available_at for item in constituents),
        ),
    )


def _recursive_fixture() -> tuple[
    tuple[StrictLevelResult, StrictLevelResult, StrictLevelResult],
    ConstituentUnit,
    ConstituentUnit,
]:
    raw_units: list[ConstituentUnit] = []
    level_zero_centers: list[TrendCenter] = []
    level_zero_trends: list[TrendType] = []
    raw_leaf_by_carrier: dict[str, ConstituentUnit] = {}
    next_index = 0

    def physical_carrier(
        direction: str,
        start: int,
        end: int,
    ) -> ConstituentUnit:
        nonlocal next_index
        values = []
        for (
            unit_direction,
            unit_start,
            unit_end,
            low,
            high,
        ) in _physical_center_geometry(
            direction,
            start,
            end,
        ):
            value = replace(
                unit(next_index, unit_direction, unit_start, unit_end),
                low_tick=low,
                high_tick=high,
            )
            next_index += 1
            values.append(value)
        center = establish_center(values[:5], 0, SourceKind.SEGMENT)
        assert center is not None
        center, _ = advance_center(center, values[5])
        assert center.state is CenterState.COMPLETED
        constituents = tuple(values[:5])
        trend = _locked_consolidation(
            level=0,
            direction=direction,
            center=center,
            constituents=constituents,
        )
        carrier = trend_type_to_unit(trend)
        raw_units.extend(values)
        level_zero_centers.append(center)
        level_zero_trends.append(trend)
        raw_leaf_by_carrier[carrier.unit_id] = values[0]
        return carrier

    macro_geometry = (
        ("up", 80, 120),
        ("down", 120, 100),
        ("up", 100, 115),
        ("down", 115, 105),
        ("up", 105, 130),
        ("down", 130, 115),
        ("up", 115, 145),
    )
    level_one_units: list[ConstituentUnit] = []
    level_one_centers: list[TrendCenter] = []
    level_one_trends: list[TrendType] = []
    for direction, start, end in macro_geometry:
        block = tuple(
            physical_carrier(item_direction, item_start, item_end)
            for item_direction, item_start, item_end, _low, _high in _center_geometry(
                direction,
                start,
                end,
            )
        )
        center = establish_center(
            block[1:4],
            1,
            SourceKind.TREND_TYPE,
            entry_unit=block[0],
        )
        assert center is not None
        center, _ = advance_center(center, block[4])
        center, _ = advance_center(center, block[5])
        assert center.state is CenterState.COMPLETED
        trend = _locked_consolidation(
            level=1,
            direction=direction,
            center=center,
            constituents=block[:5],
        )
        level_one_units.extend(block)
        level_one_centers.append(center)
        level_one_trends.append(trend)

    level_zero = tuple(raw_units)
    level_one = tuple(level_one_units)
    level_two = combine_same_level_trends(
        tuple(trend_type_to_unit(trend) for trend in level_one_trends),
        frozenset(trend.trend_id for trend in level_one_trends),
    ).units
    level_two_centers = calculate_centers(level_two, 2, SourceKind.TREND_TYPE)
    levels = (
        StrictLevelResult(
            0,
            level_zero,
            _center_result(0, level_zero, tuple(level_zero_centers)),
            tuple(level_zero_trends),
            (),
        ),
        StrictLevelResult(
            1,
            level_one,
            _center_result(1, level_one, tuple(level_one_centers)),
            tuple(level_one_trends),
            (),
        ),
        StrictLevelResult(2, level_two, level_two_centers, (), ()),
    )
    inside = raw_leaf_by_carrier[level_two[5].child_ids[0]]
    outside = raw_leaf_by_carrier[level_two[0].child_ids[0]]
    return levels, inside, outside


def _evidence(
    *,
    locator_type: str = "1buy",
    locator_in_first_return: bool = True,
) -> StrictEvidenceResult:
    levels, inside_anchor, outside_anchor = _recursive_fixture()
    structure = StrictStructureResult(
        schema="chanlun-structure",
        price_basis_revision=TEST_PRICE_BASIS,
        levels=levels,
    )
    all_thirds = StrictSignalEngine(
        structure=structure,
        price_quantum=Decimal("1"),
    ).third_class_points()
    strategic = tuple(point for point in all_thirds if point.structural_level == 2)
    assert len(strategic) == 1

    anchor = inside_anchor if locator_in_first_return else outside_anchor
    raw_locator = confirmed_point(
        point_type=locator_type,
        price_basis_revision=TEST_PRICE_BASIS,
    )
    parent = None
    if locator_type == "2buy":
        parent_anchor = levels[0].units[3]
        raw_parent = confirmed_point(
            point_type="1buy",
            price_basis_revision=TEST_PRICE_BASIS,
        )
        parent_divergence = replace(
            raw_parent.divergence,
            anchor_at=parent_anchor.market_end,
            anchor_tick=parent_anchor.low_tick,
            confirmed_at=parent_anchor.confirmed_at,
            available_at=parent_anchor.available_at,
        )
        parent = replace(
            raw_parent,
            point_id=build_strict_point_id(
                price_basis_revision=TEST_PRICE_BASIS,
                point_type="1buy",
                structural_level=0,
                anchor_unit_id=parent_anchor.unit_id,
                center_id=None,
                parent_point_id=None,
            ),
            anchor_unit_id=parent_anchor.unit_id,
            anchor_at=parent_anchor.market_end,
            confirmed_at=parent_anchor.confirmed_at,
            available_at=parent_anchor.available_at,
            anchor_tick=parent_anchor.low_tick,
            invalidation_tick=parent_anchor.low_tick,
            divergence=parent_divergence,
        )
    parent_point_id = None if parent is None else parent.point_id
    locator = replace(
        raw_locator,
        point_id=build_strict_point_id(
            price_basis_revision=TEST_PRICE_BASIS,
            point_type=raw_locator.point_type,
            structural_level=0,
            anchor_unit_id=anchor.unit_id,
            center_id=raw_locator.center_id,
            parent_point_id=parent_point_id,
        ),
        structural_level=0,
        anchor_unit_id=anchor.unit_id,
        anchor_at=anchor.market_end,
        confirmed_at=anchor.confirmed_at,
        available_at=anchor.available_at,
        anchor_tick=(
            anchor.low_tick if raw_locator.side == "buy" else anchor.high_tick
        ),
        invalidation_tick=(anchor.low_tick if parent is None else parent.anchor_tick),
        divergence=(
            None
            if raw_locator.divergence is None
            else replace(
                raw_locator.divergence,
                anchor_at=anchor.market_end,
                anchor_tick=(
                    anchor.low_tick if raw_locator.side == "buy" else anchor.high_tick
                ),
                confirmed_at=anchor.confirmed_at,
                available_at=anchor.available_at,
            )
        ),
        parent_point_id=parent_point_id,
    )
    closed_at = max(
        locator.available_at,
        *(point.available_at for point in all_thirds),
    )
    confirmed_points = (
        *((parent,) if parent is not None else ()),
        locator,
        *all_thirds,
    )
    divergences = merge_formal_divergence_ledger(structure, confirmed_points)
    revision = build_strict_evidence_revision(
        symbol=CODE,
        source_frequency="1m",
        price_basis_revision=TEST_PRICE_BASIS,
        strict_config_revision="strict-test",
        structure=structure,
        confirmed_points=confirmed_points,
        divergences=divergences,
    )
    return StrictEvidenceResult(
        symbol=CODE,
        source_frequency="1m",
        source_closed_at=closed_at,
        price_basis_revision=TEST_PRICE_BASIS,
        structure_price_quantum=Decimal("1"),
        strict_config_revision="strict-test",
        structure_revision=revision,
        structure=structure,
        stroke_center_observations=CenterLevelResult(
            structural_level=0,
            price_basis_revision=TEST_PRICE_BASIS,
            centers=(),
            previews=(),
            events=(),
            locked_unit_count=0,
            replay_from=0,
        ),
        confirmed_points=confirmed_points,
        approaching_points=(),
        divergences=divergences,
    )


def test_direct_recursive_chain_maps_one_graph_to_30m_5m_1m() -> None:
    evidence = _evidence()

    path = build_direct_recursive_structure_path(
        evidence=evidence,
        code=CODE,
    )

    assert path.aligned_entry_count == 1
    assert path.grade == "RESEARCH_ONLY"
    assert path.live_status == "LIVE_DISABLED"
    decision = path.decisions[0]
    assert decision.status == "PASS"
    assert decision.chain is not None
    return_unit = evidence.structure.levels[2].units[5]
    locator = next(
        point for point in evidence.confirmed_points if point.point_type == "1buy"
    )
    assert decision.chain.l1_return_unit_id == return_unit.unit_id
    assert locator.anchor_unit_id in decision.chain.provenance_unit_ids
    entry = path.technical_entries[0]
    assert (
        entry.l0_source_frequency,
        entry.l1_source_frequency,
        entry.l2_source_frequency,
    ) == ("30m", "5m", "1m")
    assert entry.direct_recursive_levels_unique is True
    assert entry.level_relation_mode == "DIRECT_RECURSIVE"


def test_locator_outside_exact_first_return_is_rejected() -> None:
    path = build_direct_recursive_structure_path(
        evidence=_evidence(locator_in_first_return=False),
        code=CODE,
    )

    assert path.aligned_entry_count == 0
    assert path.decisions[0].reason_codes == (
        "NO_L2_1M_LOCATOR_IN_DIRECT_FIRST_RETURN",
    )


def test_canonical_ordinary_second_buy_is_a_direct_locator() -> None:
    evidence = _evidence(locator_type="2buy")
    path = build_direct_recursive_structure_path(
        evidence=evidence,
        code=CODE,
    )
    assert path.aligned_entry_count == 1
    assert path.decisions[0].status == "PASS"
    assert path.technical_entries[0].l2_locator == "L2_SECOND_BUY"


def test_less_than_three_recursive_levels_fails_closed() -> None:
    evidence = _evidence()
    structure = replace(evidence.structure, levels=evidence.structure.levels[:2])
    confirmed_points = tuple(
        point
        for point in evidence.confirmed_points
        if not (point.point_type == "3buy" and point.structural_level == 2)
    )
    divergences = merge_formal_divergence_ledger(structure, confirmed_points)
    shortened = replace(
        evidence,
        structure=structure,
        confirmed_points=confirmed_points,
        structure_revision=build_strict_evidence_revision(
            symbol=CODE,
            source_frequency="1m",
            price_basis_revision=TEST_PRICE_BASIS,
            strict_config_revision="strict-test",
            structure=structure,
            confirmed_points=confirmed_points,
            divergences=divergences,
        ),
        divergences=divergences,
    )

    path = build_direct_recursive_structure_path(
        evidence=shortened,
        code=CODE,
    )

    assert path.grade == "UNRESOLVED"
    assert path.aligned_entry_count == 0
    assert path.rejection_counts == (("LESS_THAN_THREE_RECURSIVE_LEVELS", 1),)
