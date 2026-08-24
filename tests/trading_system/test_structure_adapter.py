from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

import chanlun.decision_support.trading_system.structure_adapter as adapter_module
from chanlun.core.strict_structure.identity import build_strict_evidence_revision
from chanlun.core.strict_structure.divergence import collect_formal_divergence_ledger
from chanlun.core.strict_structure.models import (
    CenterLevelResult,
    ConstituentUnit,
    SourceKind,
    StrictEvidenceResult,
    StrictLevelResult,
    StrictStructureResult,
    build_strict_point_id,
)
from chanlun.decision_support.trading_system.structure_adapter import (
    convert_confirmed_point_evidence,
    convert_current_confirmed_point_evidence,
    extract_confirmed_points,
    extract_one_minute_segment_difference_points,
)
from tests.core.strict_structure.signal_helpers import confirmed_point
from tests.core.strict_structure.helpers import (
    completed_down_center,
    completed_up_center,
    engine_for,
    structure_for,
)
from tests.trading_system.helpers import confirmed_point as trading_point


CN = ZoneInfo("Asia/Shanghai")
AS_OF = datetime(2026, 7, 20, 15, 0, tzinfo=CN)
SIX_POINT_TYPES = ("1buy", "2buy", "3buy", "1sell", "2sell", "3sell")


def test_one_minute_segment_difference_ledger_merges_history_and_current_tail(
    monkeypatch,
) -> None:
    historical_only = trading_point(
        "1sell",
        frequency="1m",
        minutes_after=0,
    )
    historical_overlap = trading_point(
        "1buy",
        frequency="1m",
        minutes_after=1,
    )
    current_overlap = replace(
        historical_overlap,
        evidence_codes=("current_tail",),
    )
    current_only = trading_point(
        "3buy",
        frequency="1m",
        minutes_after=2,
    )
    monkeypatch.setattr(
        adapter_module,
        "extract_confirmed_points",
        lambda *_args, **_kwargs: (historical_only, historical_overlap),
    )
    monkeypatch.setattr(
        adapter_module,
        "extract_current_confirmed_points",
        lambda *_args, **_kwargs: (current_overlap, current_only),
    )

    points = extract_one_minute_segment_difference_points(
        object(),
        code="SZ.000001",
        source_frequency="1m",
        as_of=AS_OF,
    )

    assert points == (historical_only, current_overlap, current_only)


def test_segment_difference_ledger_rejects_non_one_minute_evidence() -> None:
    with pytest.raises(ValueError, match="require 1m evidence"):
        extract_one_minute_segment_difference_points(
            object(),
            code="SZ.000001",
            source_frequency="5m",
            as_of=AS_OF,
        )


def test_segment_difference_ledger_recovers_historical_terminal_lineage(
    monkeypatch,
) -> None:
    raw = _aware_point("1sell")
    evidence = _evidence((raw,))
    monkeypatch.setattr(
        adapter_module,
        "extract_current_confirmed_points",
        lambda *_args, **_kwargs: (),
    )

    points = extract_one_minute_segment_difference_points(
        evidence,
        code="SZ.000001",
        source_frequency="1m",
        as_of=AS_OF,
    )

    assert len(points) == 1
    reference = points[0].terminal_segment
    assert reference is not None
    assert reference.unit_id == raw.anchor_unit_id
    assert reference.market_end == raw.anchor_at
    assert reference.direction == "up"
    assert reference.state == "locked"


def _anchor_unit(point) -> ConstituentUnit:
    source = (
        SourceKind.SEGMENT if point.structural_level == 0 else SourceKind.TREND_TYPE
    )
    return ConstituentUnit(
        unit_id=point.anchor_unit_id,
        structural_level=point.structural_level,
        source_kind=source,
        price_basis_revision=point.price_basis_revision,
        direction="up",
        start_tick=point.anchor_tick,
        end_tick=point.anchor_tick,
        low_tick=point.anchor_tick,
        high_tick=point.anchor_tick,
        market_start=point.anchor_at,
        market_end=point.anchor_at,
        confirmed_at=point.anchor_at,
        available_at=point.anchor_at,
        locked=True,
        child_ids=(),
    )


def _with_point_anchors(structure, points) -> StrictStructureResult:
    """只附加适配器测试夹具所需的精确正式锚点事实。"""

    values = tuple(points)
    max_level = max(
        (point.structural_level for point in values),
        default=len(structure.levels) - 1,
    )
    levels = list(structure.levels)
    while len(levels) <= max_level:
        level = len(levels)
        levels.append(
            StrictLevelResult(
                structural_level=level,
                units=(),
                center_result=CenterLevelResult(
                    structural_level=level,
                    price_basis_revision="test-raw",
                    centers=(),
                    previews=(),
                    events=(),
                    locked_unit_count=0,
                    replay_from=0,
                ),
                trend_types=(),
                completed_trends=(),
            )
        )
    for level_number, level in enumerate(levels):
        by_id = {unit.unit_id: unit for unit in level.units}
        for point in values:
            if point.structural_level != level_number:
                continue
            by_id.setdefault(point.anchor_unit_id, _anchor_unit(point))
        level_units = tuple(
            sorted(
                by_id.values(), key=lambda unit: (unit.market_start, unit.unit_id)
            )
        )
        levels[level_number] = replace(
            level,
            units=level_units,
            center_result=replace(
                level.center_result,
                locked_unit_count=sum(unit.locked for unit in level_units),
            ),
        )
    return replace(structure, levels=tuple(levels))


def _aware_point(point_type: str, *, parent=None):
    anchor_at = AS_OF - timedelta(minutes=30)
    confirmed_at = AS_OF - timedelta(minutes=20)
    available_at = AS_OF - timedelta(minutes=10)
    raw = confirmed_point(point_type=point_type)
    divergence = raw.divergence
    if divergence is not None:
        divergence = replace(
            divergence,
            anchor_at=anchor_at,
            anchor_tick=raw.anchor_tick,
            confirmed_at=confirmed_at,
            available_at=available_at,
        )
    parent_point_id = None if parent is None else parent.point_id
    return replace(
        raw,
        point_id=build_strict_point_id(
            price_basis_revision=raw.price_basis_revision,
            point_type=raw.point_type,
            structural_level=raw.structural_level,
            anchor_unit_id=raw.anchor_unit_id,
            center_id=raw.center_id,
            parent_point_id=parent_point_id,
        ),
        anchor_at=anchor_at,
        confirmed_at=confirmed_at,
        available_at=available_at,
        divergence=divergence,
        parent_point_id=parent_point_id,
    )


def _evidence(
    points=(),
    *,
    approaching_points=(),
    symbol: str = "SZ.000001",
    source_frequency: str = "1m",
    structure=None,
) -> StrictEvidenceResult:
    structure = _with_point_anchors(
        structure
        or StrictStructureResult(
            schema="chanlun-structure",
            price_basis_revision="test-raw",
            levels=(),
        ),
        (*points, *approaching_points),
    )
    observations = CenterLevelResult(
        structural_level=0,
        price_basis_revision="test-raw",
        centers=(),
        previews=(),
        events=(),
        locked_unit_count=0,
        replay_from=0,
    )
    divergences = collect_formal_divergence_ledger(structure, points)
    revision = build_strict_evidence_revision(
        symbol=symbol,
        source_frequency=source_frequency,
        price_basis_revision="test-raw",
        strict_config_revision="strict-config",
        structure=structure,
        confirmed_points=points,
        divergences=divergences,
    )
    return StrictEvidenceResult(
        symbol=symbol,
        source_frequency=source_frequency,
        source_closed_at=AS_OF,
        price_basis_revision="test-raw",
        structure_price_quantum=Decimal("0.01"),
        strict_config_revision="strict-config",
        structure_revision=revision,
        structure=structure,
        stroke_center_observations=observations,
        confirmed_points=points,
        approaching_points=approaching_points,
        divergences=divergences,
    )


def test_adapter_reads_only_strict_points() -> None:
    raw = _aware_point("1buy")
    points = extract_confirmed_points(
        _evidence((raw,)),
        code="SZ.000001",
        source_frequency="1m",
        as_of=AS_OF,
    )

    assert len(points) == 1
    assert points[0].tower == "formal"
    assert points[0].available_at == raw.available_at
    assert points[0].price_basis_revision == raw.price_basis_revision
    assert points[0].structure_anchor_price == float(raw.structure_anchor_price)


def test_current_adapter_keeps_geometry_time_after_later_audit_lock() -> None:
    raw = _aware_point("1sell")
    evidence = _evidence((raw,))
    level = evidence.structure.levels[0]
    anchor_unit = next(
        unit for unit in level.units if unit.unit_id == raw.anchor_unit_id
    )
    formed_at = anchor_unit.available_at
    structure = replace(
        evidence.structure,
        levels=(
            replace(
                level,
                units=tuple(
                    replace(unit, formed_at=formed_at)
                    if unit.unit_id == raw.anchor_unit_id
                    else unit
                    for unit in level.units
                ),
            ),
            *evidence.structure.levels[1:],
        ),
    )

    points = convert_current_confirmed_point_evidence(
        structure,
        confirmed_points=evidence.confirmed_points,
        approaching_points=evidence.approaching_points,
        code="SZ.000001",
        source_frequency="1m",
        as_of=AS_OF,
    )

    assert len(points) == 1
    assert points[0].confirmed_at == formed_at
    assert points[0].available_at == formed_at
    assert "geometry_confirmed_before_audit_lock" in points[0].evidence_codes


def test_adapter_rejects_point_available_after_as_of() -> None:
    raw = _aware_point("1buy")
    evidence = _evidence((raw,))
    object.__setattr__(raw, "available_at", AS_OF + timedelta(minutes=1))

    with pytest.raises(ValueError, match="available after as_of"):
        extract_confirmed_points(
            evidence,
            code="SZ.000001",
            source_frequency="1m",
            as_of=AS_OF,
        )


def test_all_six_types_survive_adapter_without_category_collapse() -> None:
    up = completed_up_center(0)
    down = completed_down_center(10)
    structure = structure_for(up, down)
    third_points = engine_for(up, down).third_class_points()
    first_buy = _aware_point("1buy")
    first_sell = _aware_point("1sell")
    raw_points = (
        first_buy,
        _aware_point("2buy", parent=first_buy),
        first_sell,
        _aware_point("2sell", parent=first_sell),
        *third_points,
    )

    points = extract_confirmed_points(
        _evidence(raw_points, structure=structure),
        code="SZ.000001",
        source_frequency="1m",
        as_of=AS_OF,
    )

    assert {point.point_type for point in points} == set(SIX_POINT_TYPES)
    assert len(points) == 6
    assert (
        next(point for point in points if point.point_type == "3buy").center_ordinal
        == 1
    )


def test_adapter_rejects_mismatched_or_future_snapshot_context() -> None:
    evidence = _evidence((_aware_point("1buy"),))

    with pytest.raises(ValueError, match="strict evidence context mismatch"):
        extract_confirmed_points(
            evidence,
            code="SH.600000",
            source_frequency="1m",
            as_of=AS_OF,
        )
    with pytest.raises(ValueError, match="snapshot is after as_of"):
        extract_confirmed_points(
            evidence,
            code="SZ.000001",
            source_frequency="1m",
            as_of=AS_OF - timedelta(minutes=1),
        )


def test_small_to_large_parent_link_survives_id_conversion() -> None:
    parent = _aware_point("1buy")
    raw_child = _aware_point("2buy", parent=parent)
    child = replace(
        raw_child,
        point_id=build_strict_point_id(
            price_basis_revision=raw_child.price_basis_revision,
            point_type=raw_child.point_type,
            structural_level=1,
            anchor_unit_id=raw_child.anchor_unit_id,
            center_id=None,
            parent_point_id=parent.point_id,
        ),
        structural_level=1,
        source_kind=SourceKind.TREND_TYPE,
        center_id=None,
        center_zd_tick=None,
        center_zg_tick=None,
        center_ordinal=None,
        evidence_codes=(
            "confirmed_lower_level_first_class_parent",
            "small_to_large_reversal",
            "complete_adjacent_rebound",
            "complete_first_pullback",
            "prior_extreme_held",
        ),
        related_point_ids=(parent.point_id,),
        small_to_large_carrier_unit_ids=(
            "l1-signal",
            "l1-rebound",
            raw_child.anchor_unit_id,
        ),
    )

    converted = convert_confirmed_point_evidence(
        (parent, child),
        code="SZ.000001",
        source_frequency="1m",
        as_of=AS_OF,
    )
    converted_second = next(
        point
        for point in converted
        if "small_to_large_reversal" in point.evidence_codes
    )
    converted_parent = next(
        point
        for point in converted
        if point.point_id == converted_second.parent_point_id
    )
    assert converted_second.parent_point_id == converted_parent.point_id
    assert converted_second.related_point_ids == (converted_parent.point_id,)
    assert converted_second.small_to_large_carrier_unit_ids == (
        "l1-signal",
        "l1-rebound",
        raw_child.anchor_unit_id,
    )
