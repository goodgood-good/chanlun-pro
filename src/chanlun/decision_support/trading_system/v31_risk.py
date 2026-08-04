from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Literal

from chanlun.decision_support.trading_system.v31_parameters import (
    StrategyV31Parameters,
)


DrawdownState = Literal["NORMAL", "CAUTION", "ENTRY_HALT", "DELEVERAGE"]


def floor_to_increment(value: Decimal, increment: int) -> int:
    if increment <= 0:
        raise ValueError("quantity increment must be positive")
    integer = int(value.to_integral_value(rounding=ROUND_DOWN))
    return max(0, integer // increment * increment)


@dataclass(frozen=True, slots=True)
class PortfolioRiskSnapshot:
    account_equity: Decimal
    drawdown: Decimal
    gross_exposure: Decimal
    current_open_risk_cash: Decimal
    cluster_exposure: Decimal
    cluster_open_risk_cash: Decimal
    occupied_slots: int
    cluster_occupied_slots: int
    cluster_id: str

    def __post_init__(self) -> None:
        if self.account_equity <= 0:
            raise ValueError("account equity must be positive")
        values = (
            self.drawdown,
            self.gross_exposure,
            self.current_open_risk_cash,
            self.cluster_exposure,
            self.cluster_open_risk_cash,
        )
        if any(value < 0 for value in values):
            raise ValueError("portfolio risk values cannot be negative")
        if self.occupied_slots < 0 or self.cluster_occupied_slots < 0:
            raise ValueError("slot counts cannot be negative")
        if not self.cluster_id.strip():
            raise ValueError("cluster identity is required")


@dataclass(frozen=True, slots=True)
class StructuralEntryRiskInput:
    entry_price_cap: Decimal
    structural_invalidation_price: Decimal
    price_tick: Decimal
    buy_quantity_increment: int
    upstream_quantity_cap: int

    def __post_init__(self) -> None:
        if (
            self.entry_price_cap <= 0
            or self.structural_invalidation_price <= 0
            or self.price_tick <= 0
        ):
            raise ValueError("entry risk prices must be positive")
        if self.structural_invalidation_price > self.entry_price_cap:
            raise ValueError("buy invalidation cannot exceed entry cap")
        if self.buy_quantity_increment <= 0 or self.upstream_quantity_cap < 0:
            raise ValueError("invalid entry quantity bounds")


@dataclass(frozen=True, slots=True)
class StructuralEntryRiskDecision:
    quantity: int
    drawdown_state: DrawdownState
    per_share_risk: Decimal
    risk_budget_cash: Decimal
    position_risk_cap_quantity: int
    portfolio_risk_cap_quantity: int
    cluster_risk_cap_quantity: int
    slot_notional_cap_quantity: int
    gross_exposure_cap_quantity: int
    cluster_exposure_cap_quantity: int
    reason_codes: tuple[str, ...]


def classify_drawdown(
    drawdown: Decimal,
    parameters: StrategyV31Parameters,
) -> DrawdownState:
    if drawdown >= parameters.deleverage_drawdown:
        return "DELEVERAGE"
    if drawdown >= parameters.entry_halt_drawdown:
        return "ENTRY_HALT"
    if drawdown >= parameters.caution_drawdown:
        return "CAUTION"
    return "NORMAL"


def size_structural_entry(
    entry: StructuralEntryRiskInput,
    portfolio: PortfolioRiskSnapshot,
    *,
    parameters: StrategyV31Parameters,
) -> StructuralEntryRiskDecision:
    state = classify_drawdown(portfolio.drawdown, parameters)
    buffer = max(
        entry.entry_price_cap * parameters.structural_gap_buffer_fraction,
        entry.price_tick * parameters.structural_gap_buffer_ticks_min,
    )
    per_share_risk = (
        entry.entry_price_cap - entry.structural_invalidation_price + buffer
    )
    risk_multiplier = (
        parameters.caution_new_risk_multiplier if state == "CAUTION" else Decimal("1")
    )
    risk_budget = (
        portfolio.account_equity
        * parameters.per_position_open_risk_fraction
        * risk_multiplier
    )
    position_qty = floor_to_increment(
        risk_budget / per_share_risk,
        entry.buy_quantity_increment,
    )
    portfolio_risk_available = max(
        Decimal("0"),
        portfolio.account_equity * parameters.portfolio_open_risk_cap
        - portfolio.current_open_risk_cash,
    )
    portfolio_qty = floor_to_increment(
        portfolio_risk_available / per_share_risk,
        entry.buy_quantity_increment,
    )
    cluster_risk_available = max(
        Decimal("0"),
        portfolio.account_equity * parameters.cluster_open_risk_cap
        - portfolio.cluster_open_risk_cash,
    )
    cluster_qty = floor_to_increment(
        cluster_risk_available / per_share_risk,
        entry.buy_quantity_increment,
    )
    slot_notional_qty = floor_to_increment(
        portfolio.account_equity
        * parameters.slot_notional_cap
        / entry.entry_price_cap,
        entry.buy_quantity_increment,
    )
    gross_notional_available = max(
        Decimal("0"),
        portfolio.account_equity * parameters.gross_exposure_cap
        - portfolio.gross_exposure,
    )
    gross_notional_qty = floor_to_increment(
        gross_notional_available / entry.entry_price_cap,
        entry.buy_quantity_increment,
    )
    cluster_notional_available = max(
        Decimal("0"),
        portfolio.account_equity * parameters.cluster_exposure_cap
        - portfolio.cluster_exposure,
    )
    cluster_notional_qty = floor_to_increment(
        cluster_notional_available / entry.entry_price_cap,
        entry.buy_quantity_increment,
    )
    reasons: list[str] = []
    blocked = state in {"ENTRY_HALT", "DELEVERAGE"}
    if blocked:
        reasons.append(f"DRAWDOWN_{state}")
    if portfolio.occupied_slots >= parameters.slot_count:
        reasons.append("NO_FREE_STRATEGIC_SLOT")
        blocked = True
    if portfolio.cluster_occupied_slots >= parameters.max_slots_per_cluster:
        reasons.append("CLUSTER_SLOT_CAP_REACHED")
        blocked = True
    capacities = (
        entry.upstream_quantity_cap,
        position_qty,
        portfolio_qty,
        cluster_qty,
        slot_notional_qty,
        gross_notional_qty,
        cluster_notional_qty,
    )
    quantity = 0 if blocked else min(capacities)
    quantity = floor_to_increment(Decimal(quantity), entry.buy_quantity_increment)
    if quantity == 0 and not reasons:
        reasons.append("STRUCTURAL_RISK_CAP_BELOW_ONE_INCREMENT")
    if state == "CAUTION":
        reasons.append("CAUTION_RISK_BUDGET_HALVED")
    return StructuralEntryRiskDecision(
        quantity=quantity,
        drawdown_state=state,
        per_share_risk=per_share_risk,
        risk_budget_cash=risk_budget,
        position_risk_cap_quantity=position_qty,
        portfolio_risk_cap_quantity=portfolio_qty,
        cluster_risk_cap_quantity=cluster_qty,
        slot_notional_cap_quantity=slot_notional_qty,
        gross_exposure_cap_quantity=gross_notional_qty,
        cluster_exposure_cap_quantity=cluster_notional_qty,
        reason_codes=tuple(reasons),
    )


__all__ = [
    "DrawdownState",
    "PortfolioRiskSnapshot",
    "StructuralEntryRiskDecision",
    "StructuralEntryRiskInput",
    "classify_drawdown",
    "size_structural_entry",
]
