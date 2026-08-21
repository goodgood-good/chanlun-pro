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
from chanlun.core.strict_structure.recursive_engine import StrictRecursiveEngine
from chanlun.core.strict_structure.signals import StrictSignalEngine
from chanlun.core.strict_structure.strength import StrengthSnapshot
from chanlun.decision_support.trading_system.structure_adapter import (
    extract_confirmed_points,
    extract_one_minute_segment_difference_points,
)
from chanlun.decision_support.trading_system.provisional import (
    extract_provisional_candidates,
)
from tests.core.strict_structure.signal_helpers import confirmed_point
from tests.core.strict_structure.helpers import (
    completed_down_center,
    completed_up_center,
    engine_for,
    structure_for,
    unit,
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


SMALL_TO_LARGE_SPECS = (
    ("down", 114, 78),
    ("up", 78, 105),
    ("down", 105, 82),
    ("up", 82, 98),
    ("down", 98, 69),
    ("up", 69, 82),
    ("down", 82, 39),
    ("up", 39, 51),
    ("down", 51, 24),
    ("up", 24, 30),
    ("down", 30, 6),
    ("up", 6, 46),
    ("down", 46, -4),
    ("up", -4, 8),
    ("down", 8, -10),
    ("up", -10, 34),
    ("down", 34, -3),
    ("up", -3, 45),
    ("down", 45, 31),
    ("up", 31, 71),
    ("down", 71, 50),
    ("up", 50, 61),
    ("down", 61, 56),
    ("up", 56, 69),
    ("down", 69, 64),
    ("up", 64, 112),
    ("down", 112, 92),
    ("up", 92, 138),
    ("down", 138, 102),
    ("up", 102, 145),
    ("down", 145, 99),
    ("up", 99, 111),
    ("down", 111, 106),
    ("up", 106, 152),
    ("down", 152, 146),
    ("up", 146, 194),
    ("down", 194, 159),
    ("up", 159, 203),
    ("down", 203, 198),
    ("up", 198, 210),
    ("down", 210, 193),
    ("up", 193, 219),
    ("down", 219, 205),
    ("up", 205, 215),
    ("down", 215, 207),
    ("up", 207, 213),
    ("down", 213, 185),
    ("up", 185, 195),
    ("down", 195, 187),
    ("up", 187, 193),
    ("down", 193, 189),
    ("up", 189, 210),
    ("down", 210, 200),
    ("up", 200, 208),
    ("down", 208, 202),
    ("up", 202, 225),
    ("down", 225, 215),
    ("up", 215, 222),
    ("down", 222, 217),
    ("up", 217, 240),
    ("down", 240, 223),
    ("up", 223, 235),
    ("down", 235, 227),
    ("up", 227, 233),
    ("down", 233, 200),
    ("up", 200, 210),
)


class SmallToLargeFixtureStrength:
    def snapshot(self, value):
        # 只有开头的下行走势发生背驰；上行力度保持不衰减，使反弹延续到真实
        # 的中枢关系边界。
        magnitude = (
            100_000_000.0
            if value.direction == "up"
            else max(
                1.0,
                100_000_000.0 - value.market_end.timestamp() / 300,
            )
        )
        signed = magnitude if value.direction == "up" else -magnitude
        return StrengthSnapshot(
            unit_id=value.unit_id,
            direction=value.direction,
            histogram_area=magnitude,
            histogram_peak=signed,
            dif_extreme=signed,
            source="macd",
            available_at=value.available_at,
        )


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
    strength = SmallToLargeFixtureStrength()
    units = tuple(
        unit(index, direction, start_tick + 1_000, end_tick + 1_000)
        for index, (direction, start_tick, end_tick) in enumerate(SMALL_TO_LARGE_SPECS)
    )
    structure = StrictRecursiveEngine(max_levels=3).calculate(
        units,
        strength=strength,
    )
    engine = StrictSignalEngine(
        structure=structure,
        strength=strength,
        price_quantum=Decimal("0.01"),
    )
    first_points = engine.first_class_points()
    second_points = engine.second_class_points(first_points)
    reverse_points = engine.third_class_points()
    second = next(
        point
        for point in second_points
        if point.structural_level == 1
        and point.point_type == "2buy"
        and "small_to_large_reversal" in point.evidence_codes
    )
    assert second.related_point_ids == (second.parent_point_id,)
    all_points_by_id = {}
    for point in (*first_points, *second_points, *reverse_points):
        previous = all_points_by_id.setdefault(point.point_id, point)
        assert previous == point
    all_points = tuple(
        sorted(
            all_points_by_id.values(),
            key=lambda point: (
                point.available_at,
                point.structural_level,
                point.point_type,
                point.point_id,
            ),
        )
    )
    converted = extract_confirmed_points(
        _evidence(all_points, structure=structure),
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


def test_small_to_large_second_is_visible_before_lock_and_then_confirms() -> None:
    strength = SmallToLargeFixtureStrength()
    all_units = tuple(
        unit(index, direction, start_tick + 1_000, end_tick + 1_000)
        for index, (direction, start_tick, end_tick) in enumerate(
            SMALL_TO_LARGE_SPECS
        )
    )

    live_units = list(all_units[:54])
    live_units[-1] = replace(
        live_units[-1],
        locked=False,
        confirmed_at=None,
    )
    live_structure = StrictRecursiveEngine(max_levels=3).calculate(
        tuple(live_units),
        strength=strength,
    )
    live_engine = StrictSignalEngine(
        structure=live_structure,
        strength=strength,
        price_quantum=Decimal("0.01"),
    )
    live = next(
        point
        for point in live_engine.approaching_points(live_units[-1].available_at)
        if point.point_type == "2buy"
        and point.structural_level == 1
        and "small_to_large_reversal" in point.evidence_codes
    )

    extended_live_units = list(all_units[:57])
    extended_live_units[-1] = replace(
        extended_live_units[-1],
        locked=False,
        confirmed_at=None,
    )
    extended_structure = StrictRecursiveEngine(max_levels=3).calculate(
        tuple(extended_live_units),
        strength=strength,
    )
    extended_engine = StrictSignalEngine(
        structure=extended_structure,
        strength=strength,
        price_quantum=Decimal("0.01"),
    )
    extended_live = next(
        point
        for point in extended_engine.approaching_points(
            extended_live_units[-1].available_at
        )
        if point.point_type == "2buy"
        and point.structural_level == 1
        and "small_to_large_reversal" in point.evidence_codes
    )

    locked_units = list(all_units[:58])
    locked_units[-1] = replace(
        locked_units[-1],
        locked=False,
        confirmed_at=None,
    )
    locked_structure = StrictRecursiveEngine(max_levels=3).calculate(
        tuple(locked_units),
        strength=strength,
    )
    locked_engine = StrictSignalEngine(
        structure=locked_structure,
        strength=strength,
        price_quantum=Decimal("0.01"),
    )
    confirmed = next(
        point
        for point in locked_engine.confirmed_points()
        if point.point_type == "2buy"
        and point.structural_level == 1
        and "small_to_large_reversal" in point.evidence_codes
    )

    assert live.anchor_unit_id == confirmed.anchor_unit_id
    assert extended_live.anchor_unit_id == confirmed.anchor_unit_id
    assert live.parent_point_id == confirmed.parent_point_id
    assert live.small_to_large_carrier_unit_ids == (
        confirmed.small_to_large_carrier_unit_ids
    )
    assert live.confirmed_at is None
    assert live.missing_conditions == ("terminal_unit_locked",)

    live_confirmed = live_engine.confirmed_points()
    candidate = extract_provisional_candidates(
        _evidence(
            live_confirmed,
            approaching_points=(live,),
            structure=live_structure,
        ),
        code="SZ.000001",
        source_frequency="1m",
        as_of=AS_OF,
    )[0]
    converted_parent = next(
        point
        for point in extract_confirmed_points(
            _evidence(live_confirmed, structure=live_structure),
            code="SZ.000001",
            source_frequency="1m",
            as_of=AS_OF,
        )
        if point.parent_point_id is None
        and point.point_id == candidate.parent_point_id
    )
    assert candidate.related_point_ids == (converted_parent.point_id,)
    assert candidate.small_to_large_carrier_unit_ids == (
        live.small_to_large_carrier_unit_ids
    )
