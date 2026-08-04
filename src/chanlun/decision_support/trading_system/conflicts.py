from __future__ import annotations

from chanlun.decision_support.trading_system.models import (
    ConflictDecision,
    StructuralPoint,
    TradeSetup,
)


def resolve_conflict(
    setup: TradeSetup,
    opposite_points: tuple[StructuralPoint, ...],
    *,
    physical_timeframes: bool = False,
) -> ConflictDecision:
    point = setup.point
    if not isinstance(point, StructuralPoint):
        return ConflictDecision(False, (), (), ("setup_not_confirmed",))
    candidates = tuple(
        candidate
        for candidate in opposite_points
        if candidate.confirmed
        and candidate.side != point.side
        and candidate.available_at <= setup.context.observed_at
    )
    timeframe_rank = {"1m": 0, "5m": 1, "30m": 2, "d": 3}

    def blocks(candidate: StructuralPoint) -> bool:
        if physical_timeframes:
            candidate_rank = timeframe_rank.get(candidate.source_frequency)
            point_rank = timeframe_rank.get(point.source_frequency)
            if candidate_rank is None or point_rank is None:
                raise ValueError("unsupported physical conflict frequency")
            return candidate_rank > point_rank or (
                candidate_rank == point_rank
                and candidate.tower == point.tower
                and candidate.recursive_level == point.recursive_level == 0
                and candidate.center_id == point.center_id
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
