from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from chanlun.core.strict_structure.center_machine import calculate_centers
from chanlun.core.strict_structure.models import (
    CenterLevelResult,
    SourceKind,
    StrictEvidenceResult,
    StrictLevelResult,
    StrictStructureResult,
    build_strict_point_id,
)
from chanlun.core.strict_structure.signals import StrictSignalEngine
from chanlun.decision_support.trading_system.structure_adapter import (
    extract_confirmed_points,
)
from chanlun.decision_support.trading_system.v3_direct_recursive_structure import (
    build_v3_direct_recursive_structure_path,
)
from tests.core.strict_structure.helpers import TEST_PRICE_BASIS, unit
from tests.core.strict_structure.signal_helpers import confirmed_point


CODE = "SZ.000001"


def _empty_centers(level: int) -> CenterLevelResult:
    return CenterLevelResult(
        structural_level=level,
        price_basis_revision=TEST_PRICE_BASIS,
        centers=(),
        previews=(),
        events=(),
        locked_unit_count=5,
        replay_from=0,
    )


def _recursive_units() -> tuple[tuple, tuple, tuple]:
    geometry = (
        # External entry, three overlapping core trends, same-direction
        # departure, and the first return that confirms a third buy.
        ("up", 80, 120),
        ("down", 120, 100),
        ("up", 100, 115),
        ("down", 115, 105),
        ("up", 105, 130),
        ("down", 130, 115),
    )
    level_zero = tuple(
        replace(
            unit(index, direction, start, end),
            unit_id=f"l0-{index}",
        )
        for index, (direction, start, end) in enumerate(geometry)
    )
    level_one = tuple(
        replace(
            unit(
                index,
                direction,
                start,
                end,
                source_kind=SourceKind.TREND_TYPE,
                structural_level=1,
            ),
            unit_id=f"l1-{index}",
            child_ids=(level_zero[index].unit_id,),
        )
        for index, (direction, start, end) in enumerate(geometry)
    )
    level_two = tuple(
        replace(
            unit(
                index,
                direction,
                start,
                end,
                source_kind=SourceKind.TREND_TYPE,
                structural_level=2,
            ),
            unit_id=f"l2-{index}",
            child_ids=(level_one[index].unit_id,),
        )
        for index, (direction, start, end) in enumerate(geometry)
    )
    return level_zero, level_one, level_two


def _evidence(
    *,
    locator_type: str = "1buy",
    locator_in_first_return: bool = True,
) -> StrictEvidenceResult:
    level_zero, level_one, level_two = _recursive_units()
    level_two_centers = calculate_centers(
        level_two,
        2,
        SourceKind.TREND_TYPE,
    )
    levels = (
        StrictLevelResult(0, level_zero, _empty_centers(0), (), ()),
        StrictLevelResult(1, level_one, _empty_centers(1), (), ()),
        StrictLevelResult(2, level_two, level_two_centers, (), ()),
    )
    structure = StrictStructureResult(
        schema_version="chanlun-structure/v3",
        price_basis_revision=TEST_PRICE_BASIS,
        levels=levels,
    )
    strategic = StrictSignalEngine(
        structure=structure,
        price_quantum=Decimal("1"),
    ).third_class_points()
    assert len(strategic) == 1

    anchor = level_zero[5 if locator_in_first_return else 0]
    raw_locator = confirmed_point(
        point_type=locator_type,
        price_basis_revision=TEST_PRICE_BASIS,
    )
    locator = replace(
        raw_locator,
        point_id=build_strict_point_id(
            price_basis_revision=TEST_PRICE_BASIS,
            point_type=raw_locator.point_type,
            structural_level=0,
            anchor_unit_id=anchor.unit_id,
            center_id=raw_locator.center_id,
            parent_point_id=raw_locator.parent_point_id,
        ),
        structural_level=0,
        anchor_unit_id=anchor.unit_id,
        anchor_at=anchor.market_end,
        confirmed_at=anchor.confirmed_at,
        available_at=anchor.available_at,
        divergence=(
            None
            if raw_locator.divergence is None
            else replace(
                raw_locator.divergence,
                anchor_at=anchor.market_end,
                confirmed_at=anchor.confirmed_at,
                available_at=anchor.available_at,
            )
        ),
    )
    closed_at = max(strategic[0].available_at, locator.available_at)
    return StrictEvidenceResult(
        symbol=CODE,
        source_frequency="1m",
        source_closed_at=closed_at,
        price_basis_revision=TEST_PRICE_BASIS,
        structure_price_quantum=Decimal("1"),
        strict_config_revision="strict-test-v1",
        structure_revision="direct-recursive-test-v1",
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
        confirmed_points=(locator, strategic[0]),
        approaching_points=(),
    )


def test_direct_recursive_chain_maps_one_graph_to_30m_5m_1m() -> None:
    evidence = _evidence()

    path = build_v3_direct_recursive_structure_path(
        evidence=evidence,
        code=CODE,
    )

    assert path.aligned_entry_count == 1
    assert path.grade == "RESEARCH_ONLY"
    assert path.live_status == "LIVE_DISABLED"
    decision = path.decisions[0]
    assert decision.status == "PASS"
    assert decision.chain is not None
    assert decision.chain.l1_return_unit_id == "l2-5"
    assert "l0-5" in decision.chain.provenance_unit_ids
    entry = path.technical_entries[0]
    assert (
        entry.l0_source_frequency,
        entry.l1_source_frequency,
        entry.l2_source_frequency,
    ) == ("30m", "5m", "1m")
    assert entry.direct_recursive_levels_unique is True
    assert entry.level_relation_mode == "DIRECT_RECURSIVE"


def test_locator_outside_exact_first_return_is_rejected() -> None:
    path = build_v3_direct_recursive_structure_path(
        evidence=_evidence(locator_in_first_return=False),
        code=CODE,
    )

    assert path.aligned_entry_count == 0
    assert path.decisions[0].reason_codes == (
        "NO_L2_1M_LOCATOR_IN_DIRECT_FIRST_RETURN",
    )


def test_second_buy_requires_explicit_signed_point_identity() -> None:
    evidence = _evidence(locator_type="2buy")
    rejected = build_v3_direct_recursive_structure_path(
        evidence=evidence,
        code=CODE,
    )
    assert rejected.decisions[0].reason_codes == (
        "L2_1M_SECOND_BUY_REQUIRES_SIGNED_EVIDENCE",
    )

    points = extract_confirmed_points(
        evidence,
        code=CODE,
        source_frequency="1m",
        as_of=evidence.source_closed_at,
    )
    second_id = next(point.point_id for point in points if point.point_type == "2buy")
    accepted = build_v3_direct_recursive_structure_path(
        evidence=evidence,
        code=CODE,
        allowed_l2_second_buy_ids=(second_id,),
    )
    assert accepted.aligned_entry_count == 1
    assert accepted.technical_entries[0].l2_locator == (
        "L2_SECOND_BUY_AFTER_SMALL_TO_LARGE_REVERSAL"
    )


def test_unknown_signed_second_buy_is_rejected_at_contract_boundary() -> None:
    evidence = _evidence(locator_type="2buy")

    try:
        build_v3_direct_recursive_structure_path(
            evidence=evidence,
            code=CODE,
            allowed_l2_second_buy_ids=("sha256:unknown",),
        )
    except ValueError as exc:
        assert str(exc) == "allowed direct-recursive second buy is unknown"
    else:  # pragma: no cover - explicit contract guard
        raise AssertionError("unknown signed identity must be rejected")


def test_less_than_three_recursive_levels_fails_closed() -> None:
    evidence = _evidence()
    shortened = replace(
        evidence,
        structure=replace(evidence.structure, levels=evidence.structure.levels[:2]),
        confirmed_points=tuple(
            point for point in evidence.confirmed_points if point.point_type != "3buy"
        ),
    )

    path = build_v3_direct_recursive_structure_path(
        evidence=shortened,
        code=CODE,
    )

    assert path.grade == "UNRESOLVED"
    assert path.aligned_entry_count == 0
    assert path.rejection_counts == (("LESS_THAN_THREE_RECURSIVE_LEVELS", 1),)
