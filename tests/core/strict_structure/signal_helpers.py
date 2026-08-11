from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from chanlun.core.strict_structure.models import (
    DivergenceEvidence,
    SourceKind,
    StrictPointEvidence,
    StrictPointStatus,
    StrictPointVariant,
    build_strict_point_id,
)
from chanlun.core.strict_structure.identity import stable_structure_id


BASE = datetime(2026, 1, 5, 10, 0, tzinfo=timezone.utc)


def _trend_divergence(
    point_type: str,
    available_at: datetime,
    price_basis_revision: str,
) -> DivergenceEvidence:
    direction = "down" if point_type == "1buy" else "up"
    return DivergenceEvidence(
        divergence_id=stable_structure_id(
            "chanlun-strict-divergence",
            price_basis_revision,
            0,
            SourceKind.SEGMENT.value,
            "trend",
            direction,
            ("compare-unit",),
            ("anchor-unit",),
        ),
        structural_level=0,
        source_kind=SourceKind.SEGMENT,
        price_basis_revision=price_basis_revision,
        kind="trend",
        direction=direction,
        compare_unit_id="compare-unit",
        signal_unit_id="anchor-unit",
        anchor_at=BASE,
        anchor_tick=100,
        confirmed_at=BASE + timedelta(minutes=5),
        available_at=available_at,
        price_extreme_confirmed=True,
        histogram_area_decayed=True,
        histogram_peak_decayed=True,
        dif_extreme_decayed=True,
        strength_source="macd_htf",
    )


def confirmed_point(
    *,
    point_type: str = "3buy",
    price_basis_revision: str = "test-raw",
) -> StrictPointEvidence:
    side = "buy" if point_type.endswith("buy") else "sell"
    anchor_at = BASE
    confirmed_at = BASE + timedelta(minutes=5)
    available_at = BASE + timedelta(minutes=10)
    anchor_tick = 100
    invalidation_tick = 95 if side == "buy" else 105
    is_first = point_type in {"1buy", "1sell"}
    is_second = point_type in {"2buy", "2sell"}
    is_third = point_type in {"3buy", "3sell"}
    center_id = "center-1" if is_third else None
    parent_point_id = "parent-point" if is_second else None
    return StrictPointEvidence(
        point_id=build_strict_point_id(
            price_basis_revision=price_basis_revision,
            point_type=point_type,
            structural_level=0,
            anchor_unit_id="anchor-unit",
            center_id=center_id,
            parent_point_id=parent_point_id,
        ),
        point_type=point_type,
        side=side,
        status=StrictPointStatus.CONFIRMED,
        variant=(
            StrictPointVariant.STANDARD
            if is_first or is_third
            else StrictPointVariant.STRICT
        ),
        structural_level=0,
        source_kind=SourceKind.SEGMENT,
        price_basis_revision=price_basis_revision,
        anchor_unit_id="anchor-unit",
        anchor_at=anchor_at,
        confirmed_at=confirmed_at,
        available_at=available_at,
        price_quantum=Decimal("0.01"),
        anchor_tick=anchor_tick,
        invalidation_tick=invalidation_tick,
        center_id=center_id,
        center_zd_tick=90 if is_third else None,
        center_zg_tick=110 if is_third else None,
        center_ordinal=1 if is_third else None,
        divergence=(
            _trend_divergence(
                point_type,
                available_at,
                price_basis_revision,
            )
            if is_first
            else None
        ),
        parent_point_id=parent_point_id,
        evidence_codes=("formal_structure",),
    )
