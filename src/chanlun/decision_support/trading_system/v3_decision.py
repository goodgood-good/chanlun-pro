from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from chanlun.decision_support.fingerprints import normalize_datetime
from chanlun.decision_support.trading_system.v3_parameters import LIVE_STATUS
from chanlun.decision_support.trading_system.v3_portfolio import (
    CycleLedger,
    StrategicState,
    floor_to_increment,
)
from chanlun.decision_support.trading_system.v3_selection import CandidateDecision


Action = Literal[
    "ENTRY_INTENT",
    "STRATEGIC_REDUCE_INTENT",
    "STRATEGIC_EXIT_INTENT",
    "TACTICAL_SELL_INTENT",
    "TACTICAL_BUYBACK_INTENT",
    "PROTECTIVE_BUYBACK_INTENT",
    "THIRD_SELL_RECOVERY_BUYBACK_INTENT",
    "TACTICAL_THIRD_SELL_EXIT_INTENT",
    "WAIT",
    "NO_TRADE",
    "OPERATIONS_HALT",
]
L1Phase = Literal[
    "BUILDING",
    "OSCILLATION",
    "UP_RETURN_PENDING",
    "DOWN_RETURN_PENDING",
    "UPMOVE",
    "DOWNMOVE",
    "EXPANSION_RECLASSIFYING",
]


@dataclass(frozen=True, slots=True)
class SystemHealthFacts:
    data_complete: bool
    broker_healthy: bool
    reconciliation_passed: bool
    timestamps_monotonic: bool
    account_transfer_registered: bool

    @property
    def healthy(self) -> bool:
        return all(
            (
                self.data_complete,
                self.broker_healthy,
                self.reconciliation_passed,
                self.timestamps_monotonic,
                self.account_transfer_registered,
            )
        )


@dataclass(frozen=True, slots=True)
class StrategicSignalFacts:
    trading_continuity_lost: bool = False
    existing_persistent_exit: bool = False
    l0_third_sell: bool = False
    first_up_leg_failed: bool = False
    second_sell_confirmed: bool = False
    l0_upmove_divergence: bool = False


@dataclass(frozen=True, slots=True)
class TacticalSignalFacts:
    l1_phase: L1Phase = "BUILDING"
    l1_third_sell: bool = False
    l1_third_buy: bool = False
    third_sell_recovery_first_or_second_buy: bool = False
    ordinary_sell_signal: bool = False
    ordinary_buyback_signal: bool = False
    l2_signal_confirmed: bool = False
    l2_reached_required_half: bool = False
    zn_at_or_above_a: bool = False
    higher_timeframe_allows_ordinary_buyback: bool = False
    higher_timeframe_allows_third_sell_recovery: bool = False
    tactical_adaptation_passed: bool = False
    every_partial_prefix_edge_passed: bool = False
    broker_sellable_tactical_qty: int = 0
    q_liquidity_cap: int = 0
    cash_affordable_buyback_qty: int = 0

    def __post_init__(self) -> None:
        if any(
            value < 0
            for value in (
                self.broker_sellable_tactical_qty,
                self.q_liquidity_cap,
                self.cash_affordable_buyback_qty,
            )
        ):
            raise ValueError("tactical quantities cannot be negative")


@dataclass(frozen=True, slots=True)
class V3DecisionInput:
    symbol: str
    decision_time: datetime
    confirmation_time: datetime
    structure_snapshot_id: str
    selection_snapshot_id: str | None
    account_snapshot_id: str
    strategic_state: StrategicState
    health: SystemHealthFacts
    strategic: StrategicSignalFacts
    tactical: TacticalSignalFacts
    cycle_ledger: CycleLedger | None
    candidate: CandidateDecision | None
    q_plan: int
    price_cap_or_floor: Decimal | None
    active_order_id: str | None = None
    all_structure_inputs_completed: bool = True

    def __post_init__(self) -> None:
        decision = normalize_datetime(self.decision_time, "decision_time")
        confirmation = normalize_datetime(self.confirmation_time, "confirmation_time")
        object.__setattr__(self, "decision_time", decision)
        object.__setattr__(self, "confirmation_time", confirmation)
        if confirmation > decision:
            raise ValueError("confirmation cannot be in the future")
        if not self.symbol or not self.structure_snapshot_id or not self.account_snapshot_id:
            raise ValueError("decision snapshot identity is required")
        if self.q_plan < 0:
            raise ValueError("Q_PLAN cannot be negative")
        if self.price_cap_or_floor is not None and self.price_cap_or_floor <= 0:
            raise ValueError("intent price boundary must be positive")
        if self.cycle_ledger is not None and self.cycle_ledger.strategic_state != self.strategic_state:
            raise ValueError("strategic state and cycle ledger disagree")


@dataclass(frozen=True, slots=True)
class V3DecisionIntent:
    symbol: str
    action: Action
    rule_id: str
    priority: int
    confirmation_time: datetime
    quantity: int
    target_position_quantity: int | None
    price_cap_or_floor: Decimal | None
    persistence: Literal["NONE", "OPTIONAL", "PERSISTENT_EXIT"]
    reason_codes: tuple[str, ...]
    structure_snapshot_id: str
    selection_snapshot_id: str | None
    account_snapshot_id: str
    cancel_active_order_first: bool
    live_status: str = LIVE_STATUS

    def __post_init__(self) -> None:
        if self.quantity < 0:
            raise ValueError("intent quantity cannot be negative")
        if self.target_position_quantity is not None and self.target_position_quantity < 0:
            raise ValueError("target position cannot be negative")
        if self.live_status != LIVE_STATUS:
            raise ValueError("v3 decisions cannot enable live trading")


def _intent(
    facts: V3DecisionInput,
    *,
    action: Action,
    rule_id: str,
    priority: int,
    quantity: int = 0,
    target: int | None = None,
    persistence: Literal["NONE", "OPTIONAL", "PERSISTENT_EXIT"] = "NONE",
    reasons: tuple[str, ...],
    use_price_boundary: bool = False,
) -> V3DecisionIntent:
    return V3DecisionIntent(
        symbol=facts.symbol,
        action=action,
        rule_id=rule_id,
        priority=priority,
        confirmation_time=facts.confirmation_time,
        quantity=quantity,
        target_position_quantity=target,
        price_cap_or_floor=(facts.price_cap_or_floor if use_price_boundary else None),
        persistence=persistence,
        reason_codes=reasons,
        structure_snapshot_id=facts.structure_snapshot_id,
        selection_snapshot_id=facts.selection_snapshot_id,
        account_snapshot_id=facts.account_snapshot_id,
        cancel_active_order_first=(facts.active_order_id is not None and priority <= 11),
    )


class V3DecisionCore:
    """Pure priority fold shared by paper/live adapters and causal replay."""

    def decide(self, facts: V3DecisionInput) -> V3DecisionIntent:
        ledger = facts.cycle_ledger
        if not facts.health.healthy:
            failed = tuple(
                name
                for name, passed in (
                    ("DATA_INCOMPLETE", facts.health.data_complete),
                    ("BROKER_UNHEALTHY", facts.health.broker_healthy),
                    ("RECONCILIATION_FAILED", facts.health.reconciliation_passed),
                    ("TIMESTAMP_NON_MONOTONIC", facts.health.timestamps_monotonic),
                    ("UNREGISTERED_ACCOUNT_TRANSFER", facts.health.account_transfer_registered),
                )
                if not passed
            )
            return _intent(
                facts,
                action="OPERATIONS_HALT",
                rule_id="V3_PRIORITY_01_OPERATIONS_HALT",
                priority=1,
                reasons=failed,
            )
        if not facts.all_structure_inputs_completed:
            return _intent(
                facts,
                action="NO_TRADE",
                rule_id="V3_COMPLETED_STRUCTURE_ONLY",
                priority=1,
                reasons=("INCOMPLETE_OR_PROVISIONAL_STRUCTURE",),
            )
        if facts.strategic.trading_continuity_lost:
            return _intent(
                facts,
                action="STRATEGIC_EXIT_INTENT",
                rule_id="V3_PRIORITY_02_TRADING_CONTINUITY_LOST",
                priority=2,
                quantity=0 if ledger is None else ledger.q_current,
                target=0,
                persistence="PERSISTENT_EXIT",
                reasons=("TRADING_CONTINUITY_LOST",),
                use_price_boundary=True,
            )
        if facts.strategic.existing_persistent_exit:
            return _intent(
                facts,
                action="STRATEGIC_EXIT_INTENT",
                rule_id="V3_PRIORITY_03_CONTINUE_PERSISTENT_EXIT",
                priority=3,
                quantity=0 if ledger is None else ledger.q_current,
                target=0,
                persistence="PERSISTENT_EXIT",
                reasons=("CONTINUE_EXISTING_STRATEGIC_EXIT",),
                use_price_boundary=True,
            )
        exit_reasons = tuple(
            code
            for code, active in (
                ("L0_THIRD_SELL", facts.strategic.l0_third_sell),
                ("FIRST_UP_LEG_FAILED", facts.strategic.first_up_leg_failed),
                ("SECOND_SELL_CONFIRM", facts.strategic.second_sell_confirmed),
            )
            if active
        )
        if exit_reasons:
            return _intent(
                facts,
                action="STRATEGIC_EXIT_INTENT",
                rule_id="V3_PRIORITY_04_STRATEGIC_FULL_EXIT",
                priority=4,
                quantity=0 if ledger is None else ledger.q_current,
                target=0,
                persistence="PERSISTENT_EXIT",
                reasons=(exit_reasons[0],),
                use_price_boundary=True,
            )
        if facts.strategic.l0_upmove_divergence and ledger is not None:
            target = floor_to_increment(
                Decimal(ledger.q_cycle) * Decimal("0.50"),
                ledger.sell_quantity_increment,
            )
            if target == 0:
                return _intent(
                    facts,
                    action="STRATEGIC_EXIT_INTENT",
                    rule_id="V3_PRIORITY_05_DIVERGENCE_TARGET_ZERO_EXIT",
                    priority=5,
                    quantity=ledger.q_current,
                    target=0,
                    persistence="PERSISTENT_EXIT",
                    reasons=("L0_UPMOVE_DIVERGENCE", "HALF_TARGET_BELOW_ONE_INCREMENT"),
                    use_price_boundary=True,
                )
            return _intent(
                facts,
                action="STRATEGIC_REDUCE_INTENT",
                rule_id="V3_PRIORITY_05_STRATEGIC_REDUCE_HALF",
                priority=5,
                quantity=max(0, ledger.q_current - target),
                target=target,
                persistence="PERSISTENT_EXIT",
                reasons=("L0_UPMOVE_DIVERGENCE",),
                use_price_boundary=True,
            )
        tactical = facts.tactical
        if tactical.l1_third_sell and ledger is not None:
            quantity = min(
                ledger.tactical_held_qty,
                tactical.broker_sellable_tactical_qty,
            )
            return _intent(
                facts,
                action=("TACTICAL_THIRD_SELL_EXIT_INTENT" if quantity else "WAIT"),
                rule_id="V3_PRIORITY_06_L1_THIRD_SELL",
                priority=6,
                quantity=quantity,
                persistence="PERSISTENT_EXIT" if quantity else "NONE",
                reasons=(
                    "L1_THIRD_SELL_STOP_RESTORE",
                    "TACTICAL_RISK_EXIT" if quantity else "NO_SELLABLE_TACTICAL_INVENTORY",
                ),
                use_price_boundary=bool(quantity),
            )
        if tactical.l1_third_buy and ledger is not None and ledger.pending_restore_qty > 0:
            quantity = min(
                ledger.pending_restore_qty,
                tactical.q_liquidity_cap,
                tactical.cash_affordable_buyback_qty,
            )
            return _intent(
                facts,
                action="PROTECTIVE_BUYBACK_INTENT" if quantity else "WAIT",
                rule_id="V3_PRIORITY_07_L1_THIRD_BUY_PROTECTION",
                priority=7,
                quantity=quantity,
                persistence="OPTIONAL" if quantity else "NONE",
                reasons=("L1_THIRD_BUY_PROTECTION",) if quantity else ("PROTECTIVE_BUYBACK_NOT_AFFORDABLE",),
                use_price_boundary=bool(quantity),
            )
        if (
            tactical.third_sell_recovery_first_or_second_buy
            and ledger is not None
            and ledger.pending_restore_qty > 0
        ):
            permitted = tactical.higher_timeframe_allows_third_sell_recovery
            quantity = (
                min(
                    ledger.pending_restore_qty,
                    tactical.q_liquidity_cap,
                    tactical.cash_affordable_buyback_qty,
                )
                if permitted
                else 0
            )
            return _intent(
                facts,
                action="THIRD_SELL_RECOVERY_BUYBACK_INTENT" if quantity else "WAIT",
                rule_id="V3_PRIORITY_08_THIRD_SELL_RECOVERY",
                priority=8,
                quantity=quantity,
                persistence="OPTIONAL" if quantity else "NONE",
                reasons=("THIRD_SELL_RECOVERY_POINT",) if quantity else ("HIGHER_TIMEFRAME_BLOCKS_THIRD_SELL_RECOVERY",),
                use_price_boundary=bool(quantity),
            )
        if tactical.ordinary_sell_signal and ledger is not None:
            enabled = all(
                (
                    facts.strategic_state == "S_ACTIVE_FULL",
                    ledger.pending_restore_qty == 0,
                    tactical.l1_phase == "OSCILLATION",
                    tactical.l2_signal_confirmed,
                    tactical.l2_reached_required_half,
                    tactical.tactical_adaptation_passed,
                    ledger.tactical_cycles_completed_today == 0,
                )
            )
            quantity = (
                min(
                    ledger.tactical_held_qty,
                    ledger.tactical_eligible_qty,
                    tactical.broker_sellable_tactical_qty,
                    tactical.q_liquidity_cap,
                )
                if enabled
                else 0
            )
            return _intent(
                facts,
                action="TACTICAL_SELL_INTENT" if quantity else "WAIT",
                rule_id="V3_PRIORITY_09_ORDINARY_TACTICAL_SELL",
                priority=9,
                quantity=quantity,
                persistence="OPTIONAL" if quantity else "NONE",
                reasons=("ORDINARY_TACTICAL_SELL_GATES_PASS",) if quantity else ("ORDINARY_TACTICAL_SELL_GATES_FAIL",),
                use_price_boundary=bool(quantity),
            )
        if tactical.ordinary_buyback_signal and ledger is not None:
            enabled = all(
                (
                    ledger.pending_restore_qty > 0,
                    tactical.l1_phase == "OSCILLATION",
                    tactical.l2_signal_confirmed,
                    tactical.l2_reached_required_half,
                    tactical.zn_at_or_above_a,
                    tactical.higher_timeframe_allows_ordinary_buyback,
                    tactical.every_partial_prefix_edge_passed,
                )
            )
            quantity = (
                min(
                    ledger.pending_restore_qty,
                    tactical.q_liquidity_cap,
                    tactical.cash_affordable_buyback_qty,
                )
                if enabled
                else 0
            )
            return _intent(
                facts,
                action="TACTICAL_BUYBACK_INTENT" if quantity else "WAIT",
                rule_id="V3_PRIORITY_10_ORDINARY_TACTICAL_BUYBACK",
                priority=10,
                quantity=quantity,
                persistence="OPTIONAL" if quantity else "NONE",
                reasons=("ORDINARY_BUYBACK_ALL_PREFIX_EDGES_PASS",) if quantity else ("ORDINARY_BUYBACK_GATES_FAIL",),
                use_price_boundary=bool(quantity),
            )
        candidate = facts.candidate
        if candidate is not None and candidate.accepted:
            eligible_state = facts.strategic_state in {
                "S_FLAT",
                "S_WAIT_CENTER",
                "S_WAIT_DEPARTURE",
                "S_WAIT_RETURN",
                "S_ENTRY_READY",
            }
            if eligible_state and facts.q_plan > 0:
                return _intent(
                    facts,
                    action="ENTRY_INTENT",
                    rule_id="V3_PRIORITY_11_STRATEGIC_ENTRY",
                    priority=11,
                    quantity=facts.q_plan,
                    persistence="OPTIONAL",
                    reasons=("ALL_CANDIDATE_AND_SIZING_GATES_PASS",),
                    use_price_boundary=True,
                )
        entry_reasons = (
            ("CANDIDATE_REJECTED",)
            if candidate is not None and not candidate.accepted
            else ("Q_PLAN_ZERO",)
            if candidate is not None and facts.q_plan == 0
            else ("NO_ACTIONABLE_COMPLETED_SIGNAL",)
        )
        return _intent(
            facts,
            action="WAIT",
            rule_id="V3_PRIORITY_12_WAIT",
            priority=12,
            reasons=entry_reasons,
        )


def decide_live(facts: V3DecisionInput) -> V3DecisionIntent:
    return V3DecisionCore().decide(facts)


def decide_backtest(facts: V3DecisionInput) -> V3DecisionIntent:
    return V3DecisionCore().decide(facts)


__all__ = [
    "StrategicSignalFacts",
    "SystemHealthFacts",
    "TacticalSignalFacts",
    "V3DecisionCore",
    "V3DecisionInput",
    "V3DecisionIntent",
    "decide_backtest",
    "decide_live",
]
