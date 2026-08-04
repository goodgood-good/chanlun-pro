from __future__ import annotations

from dataclasses import dataclass

from chanlun.decision_support.trading_system.v3_decision import (
    V3DecisionCore,
    V3DecisionInput,
    V3DecisionIntent,
)
from chanlun.decision_support.trading_system.v31_compliance import (
    ProgramTradingComplianceSnapshot,
    evaluate_program_trading_compliance,
)
from chanlun.decision_support.trading_system.v31_parameters import (
    StrategyV31Parameters,
)
from chanlun.decision_support.trading_system.v31_risk import DrawdownState


_BUY_ACTIONS = {
    "ENTRY_INTENT",
    "TACTICAL_BUYBACK_INTENT",
    "PROTECTIVE_BUYBACK_INTENT",
    "THIRD_SELL_RECOVERY_BUYBACK_INTENT",
}
_ORDER_ACTIONS = _BUY_ACTIONS | {
    "STRATEGIC_REDUCE_INTENT",
    "STRATEGIC_EXIT_INTENT",
    "TACTICAL_SELL_INTENT",
    "TACTICAL_THIRD_SELL_EXIT_INTENT",
}
_TACTICAL_ACTIONS = {
    "TACTICAL_BUYBACK_INTENT",
    "PROTECTIVE_BUYBACK_INTENT",
    "THIRD_SELL_RECOVERY_BUYBACK_INTENT",
    "TACTICAL_SELL_INTENT",
    "TACTICAL_THIRD_SELL_EXIT_INTENT",
}


@dataclass(frozen=True, slots=True)
class V31DecisionInput:
    base: V3DecisionInput
    parameters: StrategyV31Parameters
    compliance: ProgramTradingComplianceSnapshot
    entry_evidence_contract_valid: bool
    structural_invalidation_confirmed: bool
    drawdown_state: DrawdownState
    deleverage_target_quantity: int | None = None
    restoration_buy_allowed: bool = True

    def __post_init__(self) -> None:
        if self.parameters.selection_path != (
            self.base.candidate.selection_path
            if self.base.candidate is not None
            else self.parameters.selection_path
        ):
            raise ValueError("V3.1 candidate and parameter selection paths disagree")
        if self.deleverage_target_quantity is not None and self.deleverage_target_quantity < 0:
            raise ValueError("V3.1 deleverage target cannot be negative")
        if self.structural_invalidation_confirmed and self.base.cycle_ledger is None:
            raise ValueError("V3.1 structural invalidation requires an open cycle")


def _replacement(
    facts: V31DecisionInput,
    *,
    action: str,
    rule_id: str,
    priority: int,
    quantity: int,
    target: int | None,
    persistence: str,
    reasons: tuple[str, ...],
) -> V3DecisionIntent:
    base = facts.base
    return V3DecisionIntent(
        symbol=base.symbol,
        action=action,  # type: ignore[arg-type]
        rule_id=rule_id,
        priority=priority,
        confirmation_time=base.confirmation_time,
        quantity=quantity,
        target_position_quantity=target,
        price_cap_or_floor=base.price_cap_or_floor,
        persistence=persistence,  # type: ignore[arg-type]
        reason_codes=reasons,
        structure_snapshot_id=base.structure_snapshot_id,
        selection_snapshot_id=base.selection_snapshot_id,
        account_snapshot_id=base.account_snapshot_id,
        cancel_active_order_first=base.active_order_id is not None,
    )


class V31DecisionCore:
    """Safety fold shared byte-for-byte by paper/live adapters and replay."""

    def decide(self, facts: V31DecisionInput) -> V3DecisionIntent:
        base_intent = V3DecisionCore().decide(facts.base)
        if base_intent.action == "OPERATIONS_HALT":
            return base_intent
        ledger = facts.base.cycle_ledger
        if facts.structural_invalidation_confirmed and ledger is not None:
            if facts.base.price_cap_or_floor is None:
                return _replacement(
                    facts,
                    action="OPERATIONS_HALT",
                    rule_id="V31_PRIORITY_02_STRUCTURAL_EXIT_PRICE_MISSING",
                    priority=2,
                    quantity=0,
                    target=None,
                    persistence="NONE",
                    reasons=(
                        "L0_THIRD_BUY_INVALIDATED",
                        "EXECUTABLE_EXIT_PRICE_BOUNDARY_MISSING",
                    ),
                )
            return _replacement(
                facts,
                action="STRATEGIC_EXIT_INTENT",
                rule_id="V31_PRIORITY_02_L0_THIRD_BUY_INVALIDATED",
                priority=2,
                quantity=ledger.q_current,
                target=0,
                persistence="PERSISTENT_EXIT",
                reasons=("L0_THIRD_BUY_INVALIDATED_BY_COMPLETED_STRUCTURE",),
            )
        if (
            facts.drawdown_state == "DELEVERAGE"
            and ledger is not None
            and facts.deleverage_target_quantity is not None
            and ledger.q_current > facts.deleverage_target_quantity
        ):
            if facts.base.price_cap_or_floor is None:
                return _replacement(
                    facts,
                    action="OPERATIONS_HALT",
                    rule_id="V31_PRIORITY_03_DELEVERAGE_PRICE_MISSING",
                    priority=3,
                    quantity=0,
                    target=None,
                    persistence="NONE",
                    reasons=("PORTFOLIO_DELEVERAGE_PRICE_BOUNDARY_MISSING",),
                )
            return _replacement(
                facts,
                action="STRATEGIC_REDUCE_INTENT",
                rule_id="V31_PRIORITY_03_PORTFOLIO_DELEVERAGE",
                priority=3,
                quantity=ledger.q_current - facts.deleverage_target_quantity,
                target=facts.deleverage_target_quantity,
                persistence="PERSISTENT_EXIT",
                reasons=("ACCOUNT_DRAWDOWN_DELEVERAGE",),
            )
        if base_intent.action == "ENTRY_INTENT" and not facts.entry_evidence_contract_valid:
            return _replacement(
                facts,
                action="WAIT",
                rule_id="V31_ENTRY_EVIDENCE_CONTRACT_REQUIRED",
                priority=11,
                quantity=0,
                target=None,
                persistence="NONE",
                reasons=("ENTRY_EVIDENCE_CONTRACT_INVALID",),
            )
        if base_intent.action in _BUY_ACTIONS and facts.drawdown_state in {
            "ENTRY_HALT",
            "DELEVERAGE",
        }:
            return _replacement(
                facts,
                action="WAIT",
                rule_id="V31_DRAWDOWN_BLOCKS_BUY",
                priority=11,
                quantity=0,
                target=None,
                persistence="NONE",
                reasons=(f"DRAWDOWN_{facts.drawdown_state}_BLOCKS_BUY",),
            )
        if (
            base_intent.action
            in {
                "TACTICAL_BUYBACK_INTENT",
                "PROTECTIVE_BUYBACK_INTENT",
                "THIRD_SELL_RECOVERY_BUYBACK_INTENT",
            }
            and not facts.restoration_buy_allowed
        ):
            return _replacement(
                facts,
                action="WAIT",
                rule_id="V31_HIGHER_RISK_BLOCKS_RESTORATION_BUY",
                priority=11,
                quantity=0,
                target=None,
                persistence="NONE",
                reasons=("MARKET_OR_SYMBOL_RISK_BLOCKS_ALL_RESTORATION_BUYS",),
            )
        if (
            base_intent.action in _TACTICAL_ACTIONS
            and not facts.parameters.tactical_enabled
        ):
            return _replacement(
                facts,
                action="WAIT",
                rule_id="V31_TACTICAL_DISABLED_PENDING_SEPARATE_VALIDATION",
                priority=11,
                quantity=0,
                target=None,
                persistence="NONE",
                reasons=("TACTICAL_MODULE_NOT_ACTIVATED",),
            )
        if base_intent.action in _ORDER_ACTIONS:
            if base_intent.price_cap_or_floor is None:
                return _replacement(
                    facts,
                    action="OPERATIONS_HALT",
                    rule_id="V31_ORDER_PRICE_BOUNDARY_REQUIRED",
                    priority=1,
                    quantity=0,
                    target=None,
                    persistence="NONE",
                    reasons=("ORDER_PRICE_BOUNDARY_MISSING",),
                )
            compliance = evaluate_program_trading_compliance(
                facts.compliance,
                as_of=facts.base.decision_time,
                parameters=facts.parameters,
            )
            if not compliance.allowed:
                return _replacement(
                    facts,
                    action="OPERATIONS_HALT",
                    rule_id="V31_PROGRAM_TRADING_COMPLIANCE_REQUIRED",
                    priority=1,
                    quantity=0,
                    target=None,
                    persistence="NONE",
                    reasons=compliance.reason_codes,
                )
        return base_intent


def decide_v31_live(facts: V31DecisionInput) -> V3DecisionIntent:
    return V31DecisionCore().decide(facts)


def decide_v31_backtest(facts: V31DecisionInput) -> V3DecisionIntent:
    return V31DecisionCore().decide(facts)


__all__ = [
    "V31DecisionCore",
    "V31DecisionInput",
    "decide_v31_backtest",
    "decide_v31_live",
]
