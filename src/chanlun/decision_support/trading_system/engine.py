from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from chanlun.decision_support.fingerprints import normalize_datetime
from chanlun.decision_support.trading_system.conflicts import resolve_conflict
from chanlun.decision_support.trading_system.context import classify_context
from chanlun.decision_support.trading_system.execution_policy import (
    evaluate_entry_policy,
    evaluate_exit_policy,
)
from chanlun.decision_support.trading_system.lifecycle import (
    advance_lifecycle,
    build_setup,
    match_one_minute_trigger,
)
from chanlun.decision_support.trading_system.models import (
    ConflictDecision,
    ContextDirection,
    EntryDecision,
    ExitDecision,
    SectorAssessment,
    SignalLifecycle,
    StructuralPoint,
    StructureTower,
    TradeSetup,
    TradingPolicy,
)
from chanlun.decision_support.trading_system.provisional import ProvisionalCandidate


@dataclass(frozen=True, slots=True)
class SymbolStructureBundle:
    code: str
    as_of: datetime
    sector: SectorAssessment
    thirty_direction: ContextDirection
    thirty_points: tuple[StructuralPoint, ...]
    five_points: tuple[StructuralPoint | ProvisionalCandidate, ...]
    one_points: tuple[StructuralPoint, ...]
    opposite_points: tuple[StructuralPoint, ...]
    held_tower: StructureTower | None = None
    held_level: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", normalize_datetime(self.as_of, "as_of"))
        if self.held_level is not None and self.held_level < 0:
            raise ValueError("held_level cannot be negative")


@dataclass(frozen=True, slots=True)
class EvaluatedSignal:
    setup: TradeSetup
    trigger: StructuralPoint | None
    lifecycle: SignalLifecycle
    conflict: ConflictDecision
    entry: EntryDecision | None
    exit: ExitDecision | None


def _point_time(point: StructuralPoint | ProvisionalCandidate) -> datetime:
    if isinstance(point, ProvisionalCandidate):
        return point.observed_at
    return point.confirmed_at or point.anchor_at


def _current_five_minute_points(
    points: tuple[StructuralPoint | ProvisionalCandidate, ...],
    *,
    as_of: datetime,
    policy: TradingPolicy,
) -> tuple[StructuralPoint | ProvisionalCandidate, ...]:
    cutoff = as_of - timedelta(
        seconds=policy.max_five_minute_setup_age_seconds
    )
    current: dict[
        tuple[str, StructureTower, int],
        tuple[datetime, list[StructuralPoint | ProvisionalCandidate]],
    ] = {}
    for point in points:
        if point.source_frequency != "5m":
            raise ValueError("trade setup requires a 5m point")
        if isinstance(point, StructuralPoint) and not point.confirmed:
            continue
        observed_at = _point_time(point)
        if observed_at > as_of:
            raise ValueError("five-minute point cannot be after as_of")
        if observed_at < cutoff:
            continue
        lane = (point.point_type, point.tower, point.recursive_level)
        previous = current.get(lane)
        if previous is None or observed_at > previous[0]:
            current[lane] = (observed_at, [point])
        elif observed_at == previous[0]:
            previous[1].append(point)
    return tuple(
        point
        for _observed_at, lane_points in current.values()
        for point in lane_points
    )


class TradingEngine:
    def __init__(
        self,
        trading_policy: TradingPolicy = TradingPolicy(),
    ) -> None:
        self._policy = trading_policy

    def evaluate_symbol(
        self,
        bundle: SymbolStructureBundle,
    ) -> tuple[EvaluatedSignal, ...]:
        context = classify_context(
            frequency="30m",
            current_direction=bundle.thirty_direction,
            points=bundle.thirty_points,
            as_of=bundle.as_of,
        )
        output: list[EvaluatedSignal] = []
        ordered_points = sorted(
            _current_five_minute_points(
                bundle.five_points,
                as_of=bundle.as_of,
                policy=self._policy,
            ),
            key=lambda point: (
                (
                    point.confirmed_at or point.anchor_at
                    if isinstance(point, StructuralPoint)
                    else point.observed_at
                ),
                point.tower,
                point.recursive_level,
                point.point_type,
                (
                    point.point_id
                    if isinstance(point, StructuralPoint)
                    else point.candidate_id
                ),
            ),
        )
        for point in ordered_points:
            setup = build_setup(point, context, bundle.sector)
            trigger = match_one_minute_trigger(
                setup,
                bundle.one_points,
                as_of=bundle.as_of,
            )
            lifecycle = advance_lifecycle(
                None,
                setup,
                trigger,
                as_of=bundle.as_of,
            )
            if point.side == "sell":
                output.append(
                    EvaluatedSignal(
                        setup=setup,
                        trigger=trigger,
                        lifecycle=lifecycle,
                        conflict=ConflictDecision(False, (), (), ()),
                        entry=None,
                        exit=evaluate_exit_policy(
                            lifecycle,
                            setup,
                            trigger,
                        held_tower=bundle.held_tower,
                        held_level=bundle.held_level,
                        policy=self._policy,
                    ),
                    )
                )
                continue
            conflict = resolve_conflict(setup, bundle.opposite_points)
            entry = evaluate_entry_policy(
                lifecycle,
                setup,
                trigger,
                conflict,
                self._policy,
            )
            output.append(
                EvaluatedSignal(
                    setup,
                    trigger,
                    lifecycle,
                    conflict,
                    entry,
                    None,
                )
            )
        return tuple(output)


def evaluate_symbol(
    bundle: SymbolStructureBundle,
    policy: TradingPolicy = TradingPolicy(),
) -> tuple[EvaluatedSignal, ...]:
    return TradingEngine(policy).evaluate_symbol(bundle)


__all__ = [
    "EvaluatedSignal",
    "SymbolStructureBundle",
    "TradingEngine",
    "evaluate_symbol",
]
