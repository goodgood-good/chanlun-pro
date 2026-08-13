from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from chanlun.core.strict_structure.divergence import collect_formal_divergence_ledger
from chanlun.core.strict_structure.identity import (
    build_strict_evidence_revision,
    stable_structure_id,
)
from chanlun.core.strict_structure.models import (
    CenterLevelResult,
    ConstituentUnit,
    SourceKind,
    StrictEvidenceResult,
    StrictLevelResult,
    StrictPointStatus,
    StrictStructureResult,
    build_strict_point_id,
)
from tests.core.strict_structure.signal_helpers import (
    confirmed_point as base_confirmed_point,
)
from tests.core.strict_structure.helpers import (
    completed_down_center,
    completed_up_center,
    structure_for,
)


CN = ZoneInfo("Asia/Shanghai")
DEFAULT_CLOSED_AT = datetime(2026, 7, 20, 15, 0, tzinfo=CN)
PRICE_BASIS = "test-raw"


def strict_point(
    point_type: str = "3buy",
    *,
    status: StrictPointStatus = StrictPointStatus.CONFIRMED,
    available_at: datetime = DEFAULT_CLOSED_AT,
    structural_level: int = 0,
):
    raw = base_confirmed_point(
        point_type=point_type,
        price_basis_revision=PRICE_BASIS,
    )
    anchor_at = available_at - timedelta(minutes=10)
    confirmed_at = available_at - timedelta(minutes=5)
    divergence = raw.divergence
    source_kind = SourceKind.SEGMENT if structural_level == 0 else SourceKind.TREND_TYPE
    if divergence is not None:
        divergence = replace(
            divergence,
            divergence_id=stable_structure_id(
                "chanlun-strict-divergence",
                PRICE_BASIS,
                structural_level,
                source_kind.value,
                divergence.kind,
                divergence.direction,
                divergence.compare_leg_unit_ids,
                divergence.signal_leg_unit_ids,
            ),
            structural_level=structural_level,
            source_kind=source_kind,
            anchor_at=anchor_at,
            anchor_tick=raw.anchor_tick,
            confirmed_at=confirmed_at,
            available_at=available_at,
        )
    point_id = build_strict_point_id(
        price_basis_revision=PRICE_BASIS,
        point_type=raw.point_type,
        structural_level=structural_level,
        anchor_unit_id=raw.anchor_unit_id,
        center_id=raw.center_id,
        parent_point_id=raw.parent_point_id,
    )
    if status is StrictPointStatus.APPROACHING:
        point_id = "approaching:" + point_id
    return replace(
        raw,
        point_id=point_id,
        status=status,
        structural_level=structural_level,
        source_kind=source_kind,
        anchor_at=anchor_at,
        confirmed_at=(confirmed_at if status is StrictPointStatus.CONFIRMED else None),
        available_at=available_at,
        divergence=divergence,
        missing_conditions=(
            () if status is StrictPointStatus.CONFIRMED else ("terminal_unit_locked",)
        ),
    )


def strict_evidence_result(
    *,
    code: str = "SZ.000001",
    source_frequency: str = "5m",
    source_closed_at: datetime = DEFAULT_CLOSED_AT,
    confirmed_points=(),
    approaching_points=(),
) -> StrictEvidenceResult:
    normalized_confirmed = []
    completed_centers = []
    for ordinal, point in enumerate(tuple(confirmed_points)):
        if point.point_type not in {"3buy", "3sell"}:
            normalized_confirmed.append(point)
            continue
        center_factory = (
            completed_up_center if point.point_type == "3buy" else completed_down_center
        )
        center = center_factory(
            ordinal * 10,
            structural_level=point.structural_level,
            zd_tick=point.center_zd_tick,
            zg_tick=point.center_zg_tick,
        )
        center_id = center.center_id
        return_unit = center.completion_return_unit
        assert return_unit is not None
        completed_centers.append(center)
        normalized_confirmed.append(
            replace(
                point,
                point_id=build_strict_point_id(
                    price_basis_revision=point.price_basis_revision,
                    point_type=point.point_type,
                    structural_level=point.structural_level,
                    anchor_unit_id=return_unit.unit_id,
                    center_id=center_id,
                    parent_point_id=point.parent_point_id,
                ),
                anchor_unit_id=return_unit.unit_id,
                anchor_at=return_unit.market_end,
                confirmed_at=center.completed_at,
                anchor_tick=(
                    return_unit.low_tick
                    if point.side == "buy"
                    else return_unit.high_tick
                ),
                invalidation_tick=(
                    center.zg_tick if point.side == "buy" else center.zd_tick
                ),
                center_id=center_id,
                center_zd_tick=center.zd_tick,
                center_zg_tick=center.zg_tick,
            )
        )
    first_by_side = {
        point.side: point
        for point in normalized_confirmed
        if point.point_type in {"1buy", "1sell"}
    }
    rebound_confirmed = []
    for point in normalized_confirmed:
        if point.point_type not in {"2buy", "2sell"}:
            rebound_confirmed.append(point)
            continue
        parent = first_by_side.get(point.side)
        if parent is None:
            raise ValueError("strict test second-class point requires its first point")
        rebound_confirmed.append(
            replace(
                point,
                point_id=build_strict_point_id(
                    price_basis_revision=point.price_basis_revision,
                    point_type=point.point_type,
                    structural_level=point.structural_level,
                    anchor_unit_id=point.anchor_unit_id,
                    center_id=point.center_id,
                    parent_point_id=parent.point_id,
                ),
                parent_point_id=parent.point_id,
            )
        )
    confirmed_points = tuple(rebound_confirmed)
    structure = (
        structure_for(*completed_centers)
        if completed_centers
        else StrictStructureResult(
            schema="chanlun-structure",
            price_basis_revision=PRICE_BASIS,
            levels=(),
        )
    )
    all_points = confirmed_points + tuple(approaching_points)
    if all_points:
        if not structure.levels:
            empty_centers = CenterLevelResult(
                structural_level=0,
                price_basis_revision=PRICE_BASIS,
                centers=(),
                previews=(),
                events=(),
                locked_unit_count=0,
                replay_from=0,
            )
            structure = StrictStructureResult(
                schema="chanlun-structure",
                price_basis_revision=PRICE_BASIS,
                levels=(
                    StrictLevelResult(
                        structural_level=0,
                        units=(),
                        center_result=empty_centers,
                        trend_types=(),
                        completed_trends=(),
                    ),
                ),
            )
        level = structure.levels[0]
        units_by_id = {value.unit_id: value for value in level.units}
        for point in all_points:
            if point.structural_level != 0:
                raise ValueError("strict test helper only supports raw level zero")
            if point.anchor_unit_id in units_by_id:
                continue
            units_by_id[point.anchor_unit_id] = ConstituentUnit(
                unit_id=point.anchor_unit_id,
                structural_level=0,
                source_kind=SourceKind.SEGMENT,
                price_basis_revision=PRICE_BASIS,
                direction="up",
                start_tick=point.anchor_tick,
                end_tick=point.anchor_tick,
                low_tick=point.anchor_tick,
                high_tick=point.anchor_tick,
                market_start=point.anchor_at - timedelta(minutes=1),
                market_end=point.anchor_at,
                confirmed_at=point.anchor_at,
                available_at=point.anchor_at,
                locked=True,
                child_ids=(),
            )
        level_units = tuple(
            sorted(
                units_by_id.values(),
                key=lambda value: (value.market_start, value.unit_id),
            )
        )
        level = replace(
            level,
            units=level_units,
            center_result=replace(
                level.center_result,
                locked_unit_count=len(level_units),
            ),
        )
        structure = replace(structure, levels=(level,))
    observations = CenterLevelResult(
        structural_level=0,
        price_basis_revision=PRICE_BASIS,
        centers=(),
        previews=(),
        events=(),
        locked_unit_count=0,
        replay_from=0,
    )
    divergences = collect_formal_divergence_ledger(
        structure,
        confirmed_points,
    )
    revision = build_strict_evidence_revision(
        symbol=code,
        source_frequency=source_frequency,
        price_basis_revision=PRICE_BASIS,
        strict_config_revision="strict-config",
        structure=structure,
        confirmed_points=confirmed_points,
        divergences=divergences,
    )
    return StrictEvidenceResult(
        symbol=code,
        source_frequency=source_frequency,
        source_closed_at=source_closed_at,
        price_basis_revision=PRICE_BASIS,
        structure_price_quantum=Decimal("0.01"),
        strict_config_revision="strict-config",
        structure_revision=revision,
        structure=structure,
        stroke_center_observations=observations,
        confirmed_points=confirmed_points,
        approaching_points=approaching_points,
        divergences=divergences,
    )


class StrictOnlyCL:
    def __init__(self, evidence: StrictEvidenceResult) -> None:
        self.evidence = evidence
        self.evidence_calls = 0
        self.process_calls = 0

    def process_klines(self, _frame) -> None:
        self.process_calls += 1

    def get_xds(self):
        return ()

    def release_strict_evidence_cache(self) -> None:
        return None

    def get_strict_evidence(self) -> StrictEvidenceResult:
        self.evidence_calls += 1
        if self.evidence_calls > 1:
            raise AssertionError("strict evidence must be read exactly once")
        return self.evidence
