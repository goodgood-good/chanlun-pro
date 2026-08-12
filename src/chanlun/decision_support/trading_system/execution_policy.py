from __future__ import annotations

from decimal import Decimal

from chanlun.decision_support.trading_system.models import (
    ConflictDecision,
    EntryDecision,
    ExitDecision,
    SignalLifecycle,
    StructuralPoint,
    StructureTower,
    TradeSetup,
    TradingPolicy,
)


def _valid_one_minute_trigger(
    lifecycle: SignalLifecycle,
    point: StructuralPoint,
    trigger: StructuralPoint | None,
) -> bool:
    return bool(
        lifecycle.stage == "triggered"
        and trigger is not None
        and trigger.confirmed
        and trigger.side == point.side
        and trigger.source_frequency == "1m"
        and lifecycle.trigger_point_id == trigger.point_id
    )


def evaluate_entry_policy(
    lifecycle: SignalLifecycle,
    setup: TradeSetup,
    trigger: StructuralPoint | None,
    conflict: ConflictDecision,
    policy: TradingPolicy,
) -> EntryDecision:
    reasons: list[str] = []
    point = setup.point
    is_confirmed_buy = (
        isinstance(point, StructuralPoint)
        and point.confirmed
        and point.side == "buy"
        and point.source_frequency == "5m"
    )
    if policy.require_confirmed_five_minute and not is_confirmed_buy:
        reasons.append("five_minute_not_confirmed")
    if policy.require_confirmed_one_minute and not (
        isinstance(point, StructuralPoint)
        and _valid_one_minute_trigger(lifecycle, point, trigger)
    ):
        reasons.append("one_minute_not_confirmed")
    if policy.require_sector_eligibility and setup.sector.hard_block:
        reasons.append("sector_hostile")
    if policy.require_thirty_minute_context and setup.context.hard_block:
        reasons.append("thirty_minute_hostile")
    if conflict.hard_block:
        reasons.append("structure_conflict")
    if isinstance(point, StructuralPoint) and point.point_type == "3buy":
        clearance = (
            None
            if point.center_zg is None
            else Decimal(str(point.structure_anchor_price))
            - Decimal(str(point.center_zg))
        )
        if (
            point.variant == "boundary_touch"
            or clearance is None
            or clearance < policy.minimum_tick
        ):
            reasons.append("three_buy_lacks_tick_clearance")
    multiplier = {
        "1buy": policy.first_buy_risk_multiplier,
        "2buy": policy.second_buy_risk_multiplier,
        "3buy": policy.third_buy_risk_multiplier,
    }.get(
        point.point_type if isinstance(point, StructuralPoint) else "",
        Decimal("0"),
    )
    structural_stop = (
        Decimal(str(point.structure_invalidation_price))
        if isinstance(point, StructuralPoint)
        else None
    )
    unique_reasons = tuple(dict.fromkeys(reasons))
    return EntryDecision(
        allowed=not unique_reasons,
        signal_id=lifecycle.signal_id,
        risk_multiplier=multiplier,
        structural_stop=structural_stop,
        reason_codes=unique_reasons,
    )


def evaluate_exit_policy(
    lifecycle: SignalLifecycle,
    setup: TradeSetup,
    trigger: StructuralPoint | None,
    *,
    held_tower: StructureTower | None,
    held_level: int | None,
    policy: TradingPolicy = TradingPolicy(),
) -> ExitDecision:
    point = setup.point
    if (
        not isinstance(point, StructuralPoint)
        or point.side != "sell"
        or not point.confirmed
        or point.source_frequency != "5m"
    ):
        return ExitDecision(
            False,
            lifecycle.signal_id,
            "none",
            ("sell_not_confirmed",),
        )
    if policy.require_confirmed_one_minute and not _valid_one_minute_trigger(
        lifecycle,
        point,
        trigger,
    ):
        return ExitDecision(
            False,
            lifecycle.signal_id,
            "none",
            ("one_minute_sell_not_confirmed",),
        )
    if held_tower is None or held_level is None:
        return ExitDecision(
            False,
            lifecycle.signal_id,
            "none",
            ("no_active_position",),
        )
    if point.tower == held_tower and point.recursive_level >= held_level:
        return ExitDecision(
            True,
            lifecycle.signal_id,
            "exit_full",
            ("same_or_higher_sell",),
        )
    return ExitDecision(
        True,
        lifecycle.signal_id,
        "reduce_tactical",
        ("lower_or_different_structure_sell",),
    )


__all__ = [
    "EntryDecision",
    "ExitDecision",
    "TradingPolicy",
    "evaluate_entry_policy",
    "evaluate_exit_policy",
]
