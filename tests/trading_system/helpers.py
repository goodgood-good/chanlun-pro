"""交易系统当前契约使用的测试夹具。

这些工具只构造严格的公开决策模型，不再适配已经删除的旧买卖点、中枢或递归分支对象。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
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
from chanlun.decision_support.trading_system.selection import (
    SelectionResearchSnapshot,
)


CN = ZoneInfo("Asia/Shanghai")
POINT_AT = datetime(2026, 7, 20, 10, 0, tzinfo=CN)
AS_OF = datetime(2026, 7, 20, 15, 0, tzinfo=CN)


def confirmed_point(
    point_type: str,
    *,
    code: str = "SZ.000001",
    frequency: str = "5m",
    tower: str = "formal",
    level: int = 0,
    anchor: float = 10.0,
    stop: float | None = None,
    center_id: str | None = "center-a",
    center_zd: float | None = 9.0,
    center_zg: float | None = 9.8,
    center_ordinal: int | None = None,
    variant: str | None = None,
    minutes_after: int = 0,
    available_minutes_after: int = 0,
    price_basis_revision: str = "test-raw",
) -> StructuralPoint:
    typed_point = cast(PointType, point_type)
    typed_tower = cast(StructureTower, tower)
    effective_variant = (
        variant
        if variant is not None
        else "strict"
        if point_type in {"2buy", "2sell"}
        else "standard"
    )
    typed_variant = cast(PointVariant, effective_variant)
    anchor_at = POINT_AT + timedelta(minutes=minutes_after)
    side = "buy" if point_type.endswith("buy") else "sell"
    invalidation = (
        stop
        if stop is not None
        else anchor - 0.2
        if side == "buy"
        else anchor + 0.2
    )
    effective_center_ordinal = (
        1 if point_type in {"3buy", "3sell"} and center_ordinal is None
        else center_ordinal
    )
    parent_point_id = (
        build_point_id(
            code=code,
            price_basis_revision=price_basis_revision,
            point_type=cast(PointType, f"1{side}"),
            source_frequency=frequency,
            tower=typed_tower,
            recursive_level=level,
            anchor_at=anchor_at - timedelta(minutes=5),
            center_id=center_id,
            parent_point_id=None,
        )
        if point_type in {"2buy", "2sell"}
        else None
    )
    point_id = build_point_id(
        code=code,
        price_basis_revision=price_basis_revision,
        point_type=typed_point,
        source_frequency=frequency,
        tower=typed_tower,
        recursive_level=level,
        anchor_at=anchor_at,
        center_id=center_id,
        parent_point_id=parent_point_id,
    )
    return StructuralPoint(
        point_id=point_id,
        code=code,
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
        center_ordinal=effective_center_ordinal,
        divergence_kind="trend" if point_type.startswith("1") else None,
        parent_point_id=parent_point_id,
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
    code: str = "SZ.000001",
    frequency: str = "5m",
    tower: str = "formal",
    level: int = 0,
    anchor: float = 10.0,
) -> ProvisionalCandidate:
    side = "buy" if point_type.endswith("buy") else "sell"
    parent_point_id = (
        f"parent:{code}:{side}:{tower}:{level}"
        if point_type in {"2buy", "2sell"}
        else None
    )
    return ProvisionalCandidate(
        candidate_id=f"candidate:{code}:{point_type}:{tower}:{level}",
        code=code,
        point_type=cast(PointType, point_type),
        side=cast(PointSide, side),
        status="provisional",
        source_frequency=frequency,
        tower=cast(StructureTower, tower),
        recursive_level=level,
        observed_at=POINT_AT,
        anchor_price=anchor,
        invalidation_price=(anchor - 0.1 if side == "buy" else anchor + 0.1),
        price_basis_revision="test-raw",
        variant=cast(PointVariant, "standard"),
        center_id=(f"center:{tower}:{level}" if point_type in {"3buy", "3sell"} else None),
        center_zd=(anchor - 0.1 if point_type in {"3buy", "3sell"} else None),
        center_zg=(anchor + 0.1 if point_type in {"3buy", "3sell"} else None),
        center_ordinal=(1 if point_type in {"3buy", "3sell"} else None),
        divergence_kind=None,
        missing_conditions=("terminal_unit_locked",),
        evidence_codes=("test_fixture",),
        parent_point_id=parent_point_id,
    )


def setup_for(point: StructuralPoint | ProvisionalCandidate):
    return build_setup(point, neutral_context("30m"), eligible_sector())


def valid_selection_research() -> SelectionResearchSnapshot:
    """构造一份在测试决策时刻可见的正式个股三程序快照。"""

    return SelectionResearchSnapshot(
        snapshot_id="research:SZ.000001:formal",
        symbol="SZ.000001",
        path="INDIVIDUAL_THREE_PROGRAM",
        effective_at=AS_OF - timedelta(days=1),
        known_at=AS_OF - timedelta(days=2),
        valid_until=AS_OF + timedelta(days=30),
        reviewer="test-reviewer",
        signature="signed:test-selection-research",
        official_evidence_ids=("evidence:test-three-program",),
        industry_opportunity_status="PASS",
        fundamental_role="LEADER",
        relative_value_status="FAIR",
        point_in_time_total_market_cap=Decimal("1000000000"),
        peer_set_id="peer-set:test-sector",
    )


def deterministic_bundle() -> SymbolStructureBundle:
    """构造一份供决策边界测试共用的当前严格结构包。"""

    return SymbolStructureBundle(
        code="SZ.000001",
        as_of=AS_OF,
        sector=eligible_sector(),
        thirty_direction="neutral",
        thirty_points=(),
        # 默认夹具表达“刚刚出现、仍在 10 分钟新鲜窗口内”的当前信号。
        # 过期信号由专门用例显式构造，避免所有正常决策测试都在暗中追旧点。
        five_points=(confirmed_point("2buy", minutes_after=295),),
        one_points=(
            confirmed_point("1buy", frequency="1m", minutes_after=296),
        ),
        opposite_points=(),
        physical_timeframe_recursive=True,
        selection_sources=("QMT_SECTOR_TRIGGER",),
        selection_research=valid_selection_research(),
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
    "valid_selection_research",
)
