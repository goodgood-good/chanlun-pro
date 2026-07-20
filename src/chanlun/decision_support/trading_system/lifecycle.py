from __future__ import annotations

from datetime import datetime

from chanlun.decision_support.fingerprints import normalize_datetime, sha256_json
from chanlun.decision_support.trading_system.models import (
    LifecycleStage,
    SectorAssessment,
    SignalLifecycle,
    StructuralPoint,
    TimeframeContext,
    TradeSetup,
)
from chanlun.decision_support.trading_system.provisional import ProvisionalCandidate


_TRANSITIONS: dict[LifecycleStage | None, set[LifecycleStage]] = {
    None: {"observed", "approaching", "armed"},
    "observed": {"approaching", "armed", "invalidated"},
    "approaching": {"armed", "invalidated"},
    "armed": {"triggered", "invalidated"},
    "triggered": {"executable", "invalidated"},
    "executable": {"active", "invalidated"},
    "active": {"closed"},
    "closed": set(),
    "invalidated": set(),
}


def build_setup(
    point: StructuralPoint | ProvisionalCandidate,
    context: TimeframeContext,
    sector: SectorAssessment,
) -> TradeSetup:
    if point.source_frequency != "5m":
        raise ValueError("trade setup requires a 5m point")
    if isinstance(point, StructuralPoint):
        started_at = point.confirmed_at or point.anchor_at
        prices = [point.invalidation_price, point.anchor_price]
        boundary = point.center_zg if point.side == "buy" else point.center_zd
        if boundary is not None:
            prices.append(boundary)
        point_identity = point.point_id
    else:
        started_at = point.observed_at
        prices = [point.anchor_price]
        point_identity = point.candidate_id
    setup_id = sha256_json(
        {
            "schema": "chanlun-trade-setup/v1",
            "point_id": point_identity,
            "sector_id": sector.sector_id,
        }
    )
    return TradeSetup(
        setup_id=setup_id,
        point=point,
        context=context,
        sector=sector,
        started_at=started_at,
        price_low=float(min(prices)),
        price_high=float(max(prices)),
    )


def match_one_minute_trigger(
    setup: TradeSetup,
    points: tuple[StructuralPoint, ...],
    *,
    as_of: datetime,
) -> StructuralPoint | None:
    if not isinstance(setup.point, StructuralPoint) or not setup.point.confirmed:
        return None
    closed_at = normalize_datetime(as_of, "as_of")
    matches = tuple(
        point
        for point in points
        if point.confirmed
        and point.source_frequency == "1m"
        and point.side == setup.point.side
        and point.point_id != setup.point.point_id
        and point.confirmed_at is not None
        and setup.started_at <= point.confirmed_at <= closed_at
        and setup.price_low <= point.anchor_price <= setup.price_high
    )
    return min(
        matches,
        key=lambda point: (
            point.confirmed_at,
            point.recursive_level,
            point.tower,
            point.point_id,
        ),
        default=None,
    )


def _base_stage(setup: TradeSetup) -> LifecycleStage:
    if isinstance(setup.point, ProvisionalCandidate):
        return "approaching"
    if not setup.point.confirmed:
        return "observed"
    if setup.point.side == "sell":
        return "armed"
    if setup.context.hard_block or setup.sector.hard_block:
        return "observed"
    return "armed"


def _reason_codes(stage: LifecycleStage) -> tuple[str, ...]:
    return {
        "observed": ("context_or_sector_blocked",),
        "approaching": ("five_minute_provisional",),
        "armed": ("waiting_one_minute_trigger",),
        "triggered": ("one_minute_trigger_confirmed",),
        "executable": ("execution_constraints_passed",),
        "active": ("position_active",),
        "closed": ("position_closed",),
        "invalidated": ("structure_invalidated",),
    }[stage]


def _signal_id(setup: TradeSetup) -> str:
    return sha256_json(
        {
            "schema": "chanlun-signal-lifecycle/v1",
            "setup_id": setup.setup_id,
            "side": setup.point.side,
        }
    )


def _require_transition(
    previous: LifecycleStage | None,
    target: LifecycleStage,
) -> None:
    if target not in _TRANSITIONS[previous]:
        raise ValueError(f"illegal lifecycle transition: {previous} -> {target}")


def advance_lifecycle(
    previous: SignalLifecycle | None,
    setup: TradeSetup,
    trigger: StructuralPoint | None,
    *,
    as_of: datetime,
) -> SignalLifecycle:
    observed_at = normalize_datetime(as_of, "as_of")
    signal_id = _signal_id(setup)
    if previous is not None:
        if previous.signal_id != signal_id or previous.setup_id != setup.setup_id:
            raise ValueError("previous lifecycle belongs to another setup")
        if observed_at < previous.observed_at:
            raise ValueError("lifecycle time cannot move backwards")
    base_stage = _base_stage(setup)
    valid_trigger = (
        None
        if trigger is None
        else match_one_minute_trigger(setup, (trigger,), as_of=observed_at)
    )
    target = "triggered" if base_stage == "armed" and valid_trigger else base_stage
    if previous is not None and previous.stage == target:
        return previous
    if previous is None:
        _require_transition(None, base_stage)
        if target == "triggered":
            _require_transition("armed", "triggered")
    else:
        _require_transition(previous.stage, target)
    return SignalLifecycle(
        signal_id=signal_id,
        setup_id=setup.setup_id,
        stage=target,
        observed_at=observed_at,
        trigger_point_id=(
            None if valid_trigger is None else valid_trigger.point_id
        ),
        reason_codes=_reason_codes(target),
        actionable=target in {"triggered", "executable", "active"},
    )
