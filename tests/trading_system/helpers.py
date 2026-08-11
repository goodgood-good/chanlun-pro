"""Current-contract fixtures for trading-system tests.

These helpers construct only the strict public decision models. They do not
adapt removed core buy/sell-point, center, or recursive-branch objects.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import cast
from zoneinfo import ZoneInfo

from chanlun.decision_support.trading_system.lifecycle import build_setup
from chanlun.decision_support.trading_system.engine import SymbolStructureBundle
from chanlun.decision_support.trading_system.models import (
    PointType,
    PointVariant,
    PointSide,
    SectorAssessment,
    StructuralPoint,
    StructureTower,
    TimeframeContext,
    build_point_id,
)
from chanlun.decision_support.trading_system.provisional import (
    ProvisionalCandidate,
)


CN = ZoneInfo("Asia/Shanghai")
POINT_AT = datetime(2026, 7, 20, 10, 0, tzinfo=CN)
AS_OF = datetime(2026, 7, 20, 15, 0, tzinfo=CN)


def confirmed_point(
    point_type: str,
    *,
    frequency: str = "5m",
    tower: str = "formal",
    level: int = 0,
    anchor: float = 10.0,
    stop: float | None = None,
    center_id: str | None = "center-a",
    center_zd: float | None = 9.0,
    center_zg: float | None = 9.8,
    center_ordinal: int | None = 1,
    variant: str = "standard",
    minutes_after: int = 0,
    available_minutes_after: int = 0,
    price_basis_revision: str = "test-raw",
) -> StructuralPoint:
    typed_point = cast(PointType, point_type)
    typed_tower = cast(StructureTower, tower)
    typed_variant = cast(PointVariant, variant)
    anchor_at = POINT_AT + timedelta(minutes=minutes_after)
    side = "buy" if point_type.endswith("buy") else "sell"
    invalidation = (
        stop
        if stop is not None
        else anchor - 0.2
        if side == "buy"
        else anchor + 0.2
    )
    point_id = build_point_id(
        code="SZ.000001",
        price_basis_revision=price_basis_revision,
        point_type=typed_point,
        source_frequency=frequency,
        tower=typed_tower,
        recursive_level=level,
        anchor_at=anchor_at,
        center_id=center_id,
        parent_point_id=None,
    )
    return StructuralPoint(
        point_id=point_id,
        code="SZ.000001",
        point_type=typed_point,
        side=cast(PointSide, side),
        status="confirmed",
        variant=typed_variant,
        source_frequency=frequency,
        price_basis_revision=price_basis_revision,
        tower=typed_tower,
        recursive_level=level,
        anchor_at=anchor_at,
        confirmed_at=anchor_at,
        available_at=anchor_at + timedelta(minutes=available_minutes_after),
        structure_anchor_price=anchor,
        structure_invalidation_price=invalidation,
        center_id=center_id,
        center_zd=center_zd,
        center_zg=center_zg,
        center_ordinal=center_ordinal,
        divergence_kind="qs" if point_type.startswith("1") else None,
        parent_point_id=None,
        evidence_codes=("test_fixture",),
    )


def neutral_context(frequency: str) -> TimeframeContext:
    return TimeframeContext(
        frequency=frequency,
        direction="neutral",
        disposition="neutral",
        hard_block=False,
        dominant_point_id=None,
        dominant_point_type=None,
        reason_codes=("test_neutral",),
        observed_at=AS_OF,
    )


def supportive_context(frequency: str) -> TimeframeContext:
    point = confirmed_point("1buy", frequency=frequency)
    return TimeframeContext(
        frequency=frequency,
        direction="up",
        disposition="supportive",
        hard_block=False,
        dominant_point_id=point.point_id,
        dominant_point_type=point.point_type,
        reason_codes=("test_supportive",),
        observed_at=AS_OF,
    )


def hostile_context(frequency: str) -> TimeframeContext:
    point = confirmed_point("1sell", frequency=frequency)
    return TimeframeContext(
        frequency=frequency,
        direction="down",
        disposition="hostile",
        hard_block=True,
        dominant_point_id=point.point_id,
        dominant_point_type=point.point_type,
        reason_codes=("test_hostile",),
        observed_at=AS_OF,
    )


def eligible_sector() -> SectorAssessment:
    return SectorAssessment(
        sector_id="qmt-gics3:test-sector",
        sector_name="测试行业",
        eligible=True,
        hard_block=False,
        regime="neutral",
        rank_components=(("neutral_access", 5),),
        reason_codes=("test_eligible",),
    )


def hostile_sector() -> SectorAssessment:
    return SectorAssessment(
        sector_id="qmt-gics3:test-sector",
        sector_name="测试行业",
        eligible=False,
        hard_block=True,
        regime="hostile",
        rank_components=(),
        reason_codes=("test_hostile",),
    )


def provisional_point(
    point_type: str,
    *,
    frequency: str = "5m",
    tower: str = "formal",
    level: int = 0,
    anchor: float = 10.0,
) -> ProvisionalCandidate:
    side = "buy" if point_type.endswith("buy") else "sell"
    return ProvisionalCandidate(
        candidate_id=f"candidate:{point_type}:{tower}:{level}",
        code="SZ.000001",
        point_type=cast(PointType, point_type),
        side=cast(PointSide, side),
        status="provisional",
        source_frequency=frequency,
        tower=cast(StructureTower, tower),
        recursive_level=level,
        observed_at=POINT_AT,
        anchor_price=anchor,
        missing_conditions=("terminal_unit_locked",),
        evidence_codes=("test_fixture",),
    )


def setup_for(point: StructuralPoint | ProvisionalCandidate):
    return build_setup(point, neutral_context("30m"), eligible_sector())


def deterministic_bundle() -> SymbolStructureBundle:
    """Build one current strict bundle shared by decision-surface tests."""

    return SymbolStructureBundle(
        code="SZ.000001",
        as_of=AS_OF,
        sector=eligible_sector(),
        thirty_direction="neutral",
        thirty_points=(),
        five_points=(confirmed_point("2buy"),),
        one_points=(
            confirmed_point("1buy", frequency="1m", minutes_after=1),
        ),
        opposite_points=(),
        physical_timeframe_recursive=True,
        selection_sources=("QMT_SECTOR_TRIGGER",),
    )


__all__ = (
    "AS_OF",
    "CN",
    "POINT_AT",
    "confirmed_point",
    "deterministic_bundle",
    "eligible_sector",
    "hostile_context",
    "hostile_sector",
    "neutral_context",
    "provisional_point",
    "setup_for",
    "supportive_context",
)
