from __future__ import annotations

from chanlun.decision_support.trading_system.models import (
    ConflictDecision,
    StructuralPoint,
    TradeSetup,
)


def resolve_conflict(
    setup: TradeSetup,
    opposite_points: tuple[StructuralPoint, ...],
) -> ConflictDecision:
    point = setup.point
    if not isinstance(point, StructuralPoint):
        return ConflictDecision(False, (), (), ("setup_not_confirmed",))
    candidates = tuple(
        candidate
        for candidate in opposite_points
        if candidate.confirmed
        and candidate.side != point.side
        and candidate.confirmed_at is not None
        and candidate.confirmed_at <= setup.context.observed_at
    )
    blockers = tuple(
        sorted(
            {
                candidate.point_id
                for candidate in candidates
                if candidate.tower == point.tower
                and candidate.recursive_level >= point.recursive_level
                and (
                    candidate.center_id == point.center_id
                    or candidate.recursive_level > point.recursive_level
                )
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
