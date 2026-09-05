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
from chanlun.decision_support.trading_system.operation_level import (
    is_five_minute_trade_level,
)
from chanlun.decision_support.trading_system.lifecycle import (
    five_minute_setup_is_in_policy_scope,
    is_one_minute_segment_difference,
)
from chanlun.decision_support.trading_system.five_minute_setup_state import (
    setup_state_for_point,
    unconfirmed_setup_reason_code,
)


SELL_STRUCTURE_RELATION_REQUIRED_REASON_CODE = (
    "sell_structure_relation_requires_manual_review"
)


def _valid_one_minute_segment_difference(
    lifecycle: SignalLifecycle,
    point: StructuralPoint,
    segment_difference: StructuralPoint | None,
    *,
    minimum_tick: Decimal,
) -> bool:
    return bool(
        lifecycle.stage in {"triggered", "executable", "active"}
        and segment_difference is not None
        and is_one_minute_segment_difference(
            segment_difference,
            minimum_tick=minimum_tick,
        )
        and segment_difference.side == point.side
        and lifecycle.trigger_point_id == segment_difference.point_id
    )


def evaluate_entry_policy(
    lifecycle: SignalLifecycle,
    setup: TradeSetup,
    segment_difference: StructuralPoint | None,
    conflict: ConflictDecision,
    policy: TradingPolicy,
) -> EntryDecision:
    reasons: list[str] = []
    point = setup.point
    is_confirmed_buy = (
        isinstance(point, StructuralPoint)
        and point.confirmed
        and point.side == "buy"
        and is_five_minute_trade_level(
            point.source_frequency,
            point.recursive_level,
        )
    )
    if policy.require_confirmed_five_minute and not is_confirmed_buy:
        reasons.append(
            unconfirmed_setup_reason_code(
                setup_state_for_point(point).formation_state,
                forming_reason_code="five_minute_not_confirmed",
            )
            if getattr(point, "status", None) == "provisional"
            else "five_minute_not_confirmed"
        )
    if lifecycle.stage not in {"triggered", "executable", "active"}:
        reasons.append("lifecycle_not_actionable")
    # 5 分钟正式买点本身决定结构信号成立；1 分钟区间套是精确执行闸门。
    # 缺失时继续保留并通知 5 分钟信号，但不能发布当前买入资格。
    if (
        policy.require_one_minute_segment_difference_for_precise_execution
        and is_confirmed_buy
        and not _valid_one_minute_segment_difference(
            lifecycle,
            point,
            segment_difference,
            minimum_tick=policy.minimum_tick,
        )
    ):
        reasons.append("one_minute_not_confirmed")
    # 板块和 30 分钟环境只负责分级，不能否定一个已经存在的 5m 买点。
    # 两个旧策略开关仍保留在可移植契约中，用来声明应当生成相应上下文，
    # 但不再作为结构执行硬门槛。
    if conflict.hard_block:
        reasons.append("structure_conflict")
    if isinstance(point, StructuralPoint) and point.point_type == "3buy":
        if not five_minute_setup_is_in_policy_scope(point):
            reasons.append("three_buy_not_first_center")
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
    segment_difference: StructuralPoint | None,
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
        or not is_five_minute_trade_level(
            point.source_frequency,
            point.recursive_level,
        )
    ):
        return ExitDecision(
            False,
            lifecycle.signal_id,
            "none",
            (
                unconfirmed_setup_reason_code(
                    setup_state_for_point(point).formation_state,
                    forming_reason_code="sell_not_confirmed",
                )
                if getattr(point, "status", None) == "provisional"
                else "sell_not_confirmed",
            ),
        )
    if lifecycle.stage not in {"triggered", "executable", "active"}:
        return ExitDecision(
            False,
            lifecycle.signal_id,
            "none",
            ("lifecycle_not_actionable",),
        )
    if not five_minute_setup_is_in_policy_scope(point):
        return ExitDecision(
            False,
            lifecycle.signal_id,
            "none",
            ("three_sell_not_first_center",),
        )
    if (
        policy.require_one_minute_segment_difference_for_precise_execution
        and not _valid_one_minute_segment_difference(
            lifecycle,
            point,
            segment_difference,
            minimum_tick=policy.minimum_tick,
        )
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
            (SELL_STRUCTURE_RELATION_REQUIRED_REASON_CODE,),
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
    "SELL_STRUCTURE_RELATION_REQUIRED_REASON_CODE",
    "TradingPolicy",
    "evaluate_entry_policy",
    "evaluate_exit_policy",
]
