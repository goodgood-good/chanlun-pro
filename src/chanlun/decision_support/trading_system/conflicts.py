from __future__ import annotations

from chanlun.core.strict_structure.level_catalog import effective_frequency_rank
from chanlun.decision_support.trading_system.models import (
    ConflictDecision,
    REVERSAL_SUPPORT_POINT_TYPES,
    StructuralPoint,
    TradeSetup,
)
from chanlun.decision_support.trading_system.five_minute_setup_state import (
    setup_state_for_point,
    unconfirmed_setup_reason_code,
)


def resolve_conflict(
    setup: TradeSetup,
    opposite_points: tuple[StructuralPoint, ...],
    *,
    physical_timeframes: bool = False,
) -> ConflictDecision:
    point = setup.point
    if not isinstance(point, StructuralPoint):
        return ConflictDecision(
            False,
            (),
            (),
            (
                unconfirmed_setup_reason_code(
                    setup_state_for_point(point).formation_state,
                    forming_reason_code="setup_not_confirmed",
                ),
            ),
        )
    candidates = tuple(
        candidate
        for candidate in opposite_points
        if candidate.confirmed
        and candidate.side != point.side
        and candidate.available_at >= point.available_at
        and candidate.available_at <= setup.context.observed_at
    )

    def blocks(candidate: StructuralPoint) -> bool:
        if physical_timeframes:
            candidate_rank = effective_frequency_rank(
                candidate.source_frequency,
                candidate.recursive_level,
            )
            point_rank = effective_frequency_rank(
                point.source_frequency,
                point.recursive_level,
            )
            if candidate_rank > point_rank:
                return True
            if candidate_rank < point_rank:
                return False
            if candidate.source_frequency != point.source_frequency:
                # 两个独立物理图的中枢编号不可直接比较；同一有效级别上，只有
                # 一、二类反转证据能够否定另一图中的反向设置，三类延续点只计风险。
                return candidate.point_type in REVERSAL_SUPPORT_POINT_TYPES
            return (
                candidate.tower == point.tower
                and candidate.recursive_level >= point.recursive_level
                and (
                    candidate.center_id == point.center_id
                    or candidate.recursive_level > point.recursive_level
                )
            )
        return (
            candidate.tower == point.tower
            and candidate.recursive_level >= point.recursive_level
            and (
                candidate.center_id == point.center_id
                or candidate.recursive_level > point.recursive_level
            )
        )

    blockers = tuple(
        sorted(
            {
                candidate.point_id
                for candidate in candidates
                if blocks(candidate)
            }
        )
    )
    risks = tuple(
        sorted(
            {
                candidate.point_id
                for candidate in candidates
                if candidate.point_id not in blockers
            }
        )
    )
    return ConflictDecision(
        hard_block=bool(blockers),
        blocking_point_ids=blockers,
        risk_only_point_ids=risks,
        reason_codes=(
            ("same_or_higher_structure_conflict",)
            if blockers
            else ("lower_or_unrelated_structure_risk",)
            if risks
            else ()
        ),
    )
