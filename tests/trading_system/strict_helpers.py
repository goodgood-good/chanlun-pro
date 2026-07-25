from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from chanlun.core.strict_structure.identity import build_strict_evidence_revision
from chanlun.core.strict_structure.models import (
    CenterLevelResult,
    StrictEvidenceResult,
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
PRICE_BASIS = "test-raw-v1"


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
    if divergence is not None:
        divergence = replace(divergence, available_at=available_at)
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
        anchor_at=anchor_at,
        confirmed_at=(
            confirmed_at if status is StrictPointStatus.CONFIRMED else None
        ),
        available_at=available_at,
        divergence=divergence,
        missing_conditions=(
            ()
            if status is StrictPointStatus.CONFIRMED
            else ("terminal_unit_locked",)
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
        center_id = f"test-center:{source_frequency}:{ordinal}:{point.point_type}"
        center_factory = (
            completed_up_center
            if point.point_type == "3buy"
            else completed_down_center
        )
        center = center_factory(
            ordinal * 10,
            structural_level=point.structural_level,
            zd_tick=point.center_zd_tick,
            zg_tick=point.center_zg_tick,
            center_id=center_id,
        )
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
                center_id=center_id,
                center_zd_tick=center.zd_tick,
                center_zg_tick=center.zg_tick,
            )
        )
    confirmed_points = tuple(normalized_confirmed)
    structure = (
        structure_for(*completed_centers)
        if completed_centers
        else StrictStructureResult(
            schema_version="chanlun-structure/v3",
            price_basis_revision=PRICE_BASIS,
            levels=(),
        )
    )
    observations = CenterLevelResult(
        structural_level=0,
        price_basis_revision=PRICE_BASIS,
        centers=(),
        previews=(),
        events=(),
        locked_unit_count=0,
        replay_from=0,
    )
    revision = build_strict_evidence_revision(
        symbol=code,
        source_frequency=source_frequency,
        price_basis_revision=PRICE_BASIS,
        strict_config_revision="strict-config-v1",
        structure=structure,
        confirmed_points=confirmed_points,
    )
    return StrictEvidenceResult(
        symbol=code,
        source_frequency=source_frequency,
        source_closed_at=source_closed_at,
        price_basis_revision=PRICE_BASIS,
        structure_price_quantum=Decimal("0.01"),
        strict_config_revision="strict-config-v1",
        structure_revision=revision,
        structure=structure,
        stroke_center_observations=observations,
        confirmed_points=confirmed_points,
        approaching_points=approaching_points,
    )


class StrictOnlyCL:
    def __init__(self, evidence: StrictEvidenceResult) -> None:
        self.evidence = evidence
        self.evidence_calls = 0
        self.process_calls = 0

    def process_klines(self, _frame) -> None:
        self.process_calls += 1

    def get_strict_evidence(self) -> StrictEvidenceResult:
        self.evidence_calls += 1
        if self.evidence_calls > 1:
            raise AssertionError("strict evidence must be read exactly once")
        return self.evidence

    def _legacy(self, *_args, **_kwargs):
        raise AssertionError("legacy structure method must not be read")

    get_branch_bspoints = _legacy
    get_recursive_branch_levels_for_tower = _legacy
    get_xds = _legacy
    get_bis = _legacy
    get_strict_points = _legacy
    get_strict_approaching_points = _legacy
    get_strict_structure_levels = _legacy
