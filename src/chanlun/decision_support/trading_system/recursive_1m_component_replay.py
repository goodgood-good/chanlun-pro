from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Literal

from chanlun.decision_support.fingerprints import normalize_datetime, sha256_json
from chanlun.decision_support.trading_system.recursive_1m_research import (
    Recursive1mDiagnosticExecutionParameters,
    Recursive1mResearchParameters,
)
from chanlun.decision_support.trading_system.v3_bar_execution import (
    BarProxyExecutionStatus,
    HistoricalMinuteExecutionBar,
)
from chanlun.decision_support.trading_system.v3_execution import (
    V3FeeModel,
    V3FeeRateAt,
    V3OrderIntent,
)
from chanlun.decision_support.trading_system.v3_parameters import (
    StrategyV3Parameters,
    etf_parameter_snapshot,
)
from chanlun.decision_support.trading_system.v3_portfolio import (
    floor_to_increment,
)


_ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class Recursive1mExecutionFactAvailability:
    """Formal historical execution facts required before returns are citable."""

    historical_etf_trade_status: bool
    broker_vintage_fee_schedule: bool
    historical_quantity_increments: bool
    historical_settlement_rules: bool
    historical_price_limit_rules: bool
    historical_quote_or_user_waived_bar_proxy: bool
    corporate_action_ledger: bool
    source_fact_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        values = tuple(self.source_fact_ids)
        object.__setattr__(self, "source_fact_ids", values)
        if not values or any(not value.strip() for value in values):
            raise ValueError("execution-fact availability requires source identities")
        if len(values) != len(set(values)):
            raise ValueError("execution-fact identities must be unique")

    @property
    def formal_execution_eligible(self) -> bool:
        return all(
            (
                self.historical_etf_trade_status,
                self.broker_vintage_fee_schedule,
                self.historical_quantity_increments,
                self.historical_settlement_rules,
                self.historical_price_limit_rules,
                self.historical_quote_or_user_waived_bar_proxy,
                self.corporate_action_ledger,
            )
        )

    @property
    def reason_codes(self) -> tuple[str, ...]:
        checks = (
            (
                self.historical_etf_trade_status,
                "HISTORICAL_ETF_TRADE_STATUS_UNAVAILABLE",
            ),
            (
                self.broker_vintage_fee_schedule,
                "BROKER_VINTAGE_FEE_SCHEDULE_UNAVAILABLE",
            ),
            (
                self.historical_quantity_increments,
                "HISTORICAL_QUANTITY_INCREMENTS_UNAVAILABLE",
            ),
            (
                self.historical_settlement_rules,
                "HISTORICAL_SETTLEMENT_RULES_UNAVAILABLE",
            ),
            (
                self.historical_price_limit_rules,
                "HISTORICAL_PRICE_LIMIT_RULES_UNAVAILABLE",
            ),
            (
                self.historical_quote_or_user_waived_bar_proxy,
                "HISTORICAL_QUOTE_OR_BAR_PROXY_WAIVER_UNAVAILABLE",
            ),
            (
                self.corporate_action_ledger,
                "CORPORATE_ACTION_LEDGER_UNAVAILABLE",
            ),
        )
        return tuple(code for passed, code in checks if not passed)


@dataclass(frozen=True, slots=True)
class Recursive1mExecutionSignal:
    signal_id: str
    point_id: str
    symbol: str
    kind: Literal["ENTRY", "L0_THIRD_SELL"]
    decision_at: datetime
    price_basis_revision: str
    raw_confirmation_high: Decimal
    raw_confirmation_low: Decimal
    confirmation_bar_source_id: str
    selection_snapshot_id: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "decision_at",
            normalize_datetime(self.decision_at, "decision_at"),
        )
        if not all(
            value and value.strip()
            for value in (
                self.signal_id,
                self.point_id,
                self.symbol,
                self.price_basis_revision,
                self.confirmation_bar_source_id,
                self.selection_snapshot_id,
            )
        ):
            raise ValueError("recursive 1m execution signal identity is invalid")
        if (
            self.raw_confirmation_low <= 0
            or self.raw_confirmation_high < self.raw_confirmation_low
        ):
            raise ValueError("recursive 1m confirmation range is invalid")


@dataclass(frozen=True, slots=True)
class Recursive1mPosition:
    cycle_id: str
    symbol: str
    slot_number: int
    quantity: int
    opened_at: datetime
    entry_point_id: str
    price_basis_revision: str
    average_entry_price: Decimal
    entry_fees: Decimal
    entry_cash: Decimal
    tactical_cash_reserve: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "opened_at",
            normalize_datetime(self.opened_at, "opened_at"),
        )
        if (
            not self.cycle_id
            or not self.symbol
            or not self.entry_point_id
            or not self.price_basis_revision
            or self.slot_number <= 0
            or self.quantity <= 0
            or self.average_entry_price <= 0
            or min(self.entry_fees, self.entry_cash, self.tactical_cash_reserve) < 0
        ):
            raise ValueError("recursive 1m position is invalid")


@dataclass(frozen=True, slots=True)
class Recursive1mEntrySizingDecision:
    quantity: int
    strategic_slot_cash: Decimal
    remaining_exposure_cash: Decimal
    protected_tactical_cash: Decimal
    cash_available_for_new_slot: Decimal
    liquidity_cap: int
    capacity_rows: tuple[tuple[str, int], ...]
    reason_codes: tuple[str, ...]


def diagnostic_fee_model(
    assumptions: Recursive1mDiagnosticExecutionParameters,
) -> V3FeeModel:
    return V3FeeModel(
        schedule_id=assumptions.execution_id,
        rates=(
            V3FeeRateAt(
                effective_from=date(1900, 1, 1),
                commission_rate=assumptions.commission_rate,
                minimum_commission=assumptions.minimum_commission,
                stock_sell_stamp_rate=assumptions.etf_sell_stamp_rate,
                transfer_rate=assumptions.transfer_rate,
            ),
        ),
    )


def _maximum_affordable_with_tactical_reserve(
    *,
    cash: Decimal,
    price: Decimal,
    upper_bound: int,
    increment: int,
    reserve_ratio: Decimal,
    fee_model: V3FeeModel,
    session: date,
) -> int:
    quantity = floor_to_increment(upper_bound, increment)
    while quantity > 0:
        notional = Decimal(quantity) * price
        required = (
            notional
            + fee_model.order_cost(
                side="buy",
                instrument_kind="EXCHANGE_TRADED_FUND",
                quantity=quantity,
                price=price,
                session=session,
            )
            + notional * reserve_ratio
        )
        if required <= cash:
            return quantity
        quantity -= increment
    return 0


def size_recursive_1m_diagnostic_entry(
    *,
    account_equity: Decimal,
    broker_cash: Decimal,
    gross_market_value: Decimal,
    protected_tactical_cash: Decimal,
    buy_limit: Decimal,
    liquidity_cap: int,
    occupied_slots: int,
    drawdown: Decimal,
    research: Recursive1mResearchParameters,
    assumptions: Recursive1mDiagnosticExecutionParameters,
    fee_session: date,
) -> Recursive1mEntrySizingDecision:
    if (
        account_equity <= 0
        or broker_cash < 0
        or gross_market_value < 0
        or protected_tactical_cash < 0
        or buy_limit <= 0
        or liquidity_cap < 0
        or occupied_slots < 0
        or drawdown < 0
    ):
        raise ValueError("recursive 1m entry sizing inputs are invalid")
    inherited = research.inherited_v3_parameters
    increment = assumptions.buy_quantity_increment
    strategic_cash = account_equity * research.strategic_slot_fraction
    remaining_exposure = max(
        _ZERO,
        account_equity * inherited.account_exposure_cap - gross_market_value,
    )
    entry_cash = max(_ZERO, broker_cash - protected_tactical_cash)
    slot_qty = floor_to_increment(strategic_cash / buy_limit, increment)
    exposure_qty = floor_to_increment(remaining_exposure / buy_limit, increment)
    liquidity_qty = floor_to_increment(liquidity_cap, increment)
    reserve_ratio = (
        research.tactical_cash_reserve_fraction_of_slot
        / research.strategic_fraction_of_slot
    )
    affordable = _maximum_affordable_with_tactical_reserve(
        cash=entry_cash,
        price=buy_limit,
        upper_bound=min(slot_qty, exposure_qty, liquidity_qty),
        increment=increment,
        reserve_ratio=reserve_ratio,
        fee_model=diagnostic_fee_model(assumptions),
        session=fee_session,
    )
    capacities = (
        ("strategic_slot_cap", slot_qty),
        ("remaining_exposure_cap", exposure_qty),
        ("cash_and_tactical_reserve_cap", affordable),
        ("point_in_time_liquidity_cap", liquidity_qty),
    )
    reasons: list[str] = []
    blocked = False
    if occupied_slots >= inherited.slot_count:
        reasons.append("NO_FREE_STRATEGIC_SLOT")
        blocked = True
    if drawdown >= inherited.entry_drawdown_halt:
        reasons.append("DRAWDOWN_ENTRY_HALT")
        blocked = True
    quantity = 0 if blocked else min(value for _name, value in capacities)
    if quantity <= 0:
        quantity = 0
        if not blocked:
            reasons.append("LESS_THAN_ONE_INCREMENT_AFTER_ALL_CAPS")
    else:
        reasons.extend(
            f"BINDING_{name.upper()}"
            for name, value in capacities
            if value == quantity
        )
    return Recursive1mEntrySizingDecision(
        quantity=quantity,
        strategic_slot_cash=strategic_cash,
        remaining_exposure_cash=remaining_exposure,
        protected_tactical_cash=protected_tactical_cash,
        cash_available_for_new_slot=entry_cash,
        liquidity_cap=liquidity_qty,
        capacity_rows=capacities,
        reason_codes=tuple(reasons),
    )


def _order_id(
    *,
    signal: Recursive1mExecutionSignal,
    side: str,
    quantity: int,
    created_at: datetime,
    sequence: int,
) -> str:
    return sha256_json(
        {
            "schema": "chanlun-recursive-1m-diagnostic-order/v1",
            "signal_id": signal.signal_id,
            "side": side,
            "quantity": quantity,
            "created_at": created_at,
            "sequence": sequence,
        }
    )


def diagnostic_entry_order(
    *,
    signal: Recursive1mExecutionSignal,
    quantity: int,
    next_bar: HistoricalMinuteExecutionBar,
    account_snapshot_id: str,
    assumptions: Recursive1mDiagnosticExecutionParameters,
    strategy: StrategyV3Parameters | None = None,
) -> V3OrderIntent:
    if signal.kind != "ENTRY" or quantity <= 0:
        raise ValueError("diagnostic entry order requires an entry signal and quantity")
    strategy = strategy or etf_parameter_snapshot()
    created_at = signal.decision_at + timedelta(
        seconds=assumptions.broker_latency_seconds
    )
    identity = _order_id(
        signal=signal,
        side="buy",
        quantity=quantity,
        created_at=created_at,
        sequence=0,
    )
    return V3OrderIntent(
        client_order_id=f"recursive-1m:{identity}",
        intent_id=f"recursive-1m-entry:{signal.signal_id}",
        parameter_set_id=strategy.parameter_set_id,
        rule_id="RECURSIVE_1M_L0_FIRST_CENTER_THIRD_BUY",
        structure_snapshot_id=signal.point_id,
        selection_snapshot_id=signal.selection_snapshot_id,
        account_snapshot_id=account_snapshot_id,
        symbol=signal.symbol,
        instrument_kind="EXCHANGE_TRADED_FUND",
        side="buy",
        quantity=quantity,
        limit_price=signal.raw_confirmation_high,
        signal_bar_end=signal.decision_at,
        created_at=created_at,
        broker_confirmed_at=created_at,
        expires_at=next_bar.closed_at,
        persistence="OPTIONAL",
        quantity_increment=assumptions.buy_quantity_increment,
    )


def diagnostic_exit_order(
    *,
    signal: Recursive1mExecutionSignal,
    position: Recursive1mPosition,
    created_at: datetime,
    account_snapshot_id: str,
    sequence: int,
    assumptions: Recursive1mDiagnosticExecutionParameters,
    strategy: StrategyV3Parameters | None = None,
) -> V3OrderIntent:
    if signal.kind != "L0_THIRD_SELL" or position.quantity <= 0:
        raise ValueError("diagnostic exit order requires a third sell and position")
    strategy = strategy or etf_parameter_snapshot()
    created = normalize_datetime(created_at, "created_at")
    identity = _order_id(
        signal=signal,
        side="sell",
        quantity=position.quantity,
        created_at=created,
        sequence=sequence,
    )
    return V3OrderIntent(
        client_order_id=f"recursive-1m:{identity}",
        intent_id=f"recursive-1m-exit:{position.cycle_id}:{signal.signal_id}",
        parameter_set_id=strategy.parameter_set_id,
        rule_id="RECURSIVE_1M_L0_THIRD_SELL_FULL_EXIT",
        structure_snapshot_id=signal.point_id,
        selection_snapshot_id=signal.selection_snapshot_id,
        account_snapshot_id=account_snapshot_id,
        symbol=signal.symbol,
        instrument_kind="EXCHANGE_TRADED_FUND",
        side="sell",
        quantity=position.quantity,
        limit_price=signal.raw_confirmation_low,
        signal_bar_end=signal.decision_at,
        created_at=created,
        broker_confirmed_at=created,
        expires_at=None,
        persistence="PERSISTENT_EXIT",
        quantity_increment=assumptions.sell_quantity_increment,
    )


def _round_limit(value: Decimal, tick: Decimal) -> Decimal:
    return (value / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick


def diagnostic_execution_status(
    *,
    known_at: datetime,
    session: date,
    previous_close: Decimal,
    sellable_quantity: int,
    assumptions: Recursive1mDiagnosticExecutionParameters,
) -> BarProxyExecutionStatus:
    if previous_close <= 0 or sellable_quantity < 0:
        raise ValueError("diagnostic status price or sellable quantity is invalid")
    limit_up = _round_limit(
        previous_close * (Decimal("1") + assumptions.daily_limit_fraction),
        assumptions.price_tick,
    )
    limit_down = _round_limit(
        previous_close * (Decimal("1") - assumptions.daily_limit_fraction),
        assumptions.price_tick,
    )
    if limit_down >= limit_up:
        raise ValueError("diagnostic status limits are invalid")
    return BarProxyExecutionStatus(
        known_at=known_at,
        effective_session=session,
        listed=True,
        suspended=False,
        continuity_active=True,
        point_in_time_state_complete=True,
        corporate_action_state_complete=True,
        sellable_quantity=sellable_quantity,
        limit_up=limit_up,
        limit_down=limit_down,
        buy_quantity_increment=assumptions.buy_quantity_increment,
        sell_quantity_increment=assumptions.sell_quantity_increment,
        fee_schedule_id=assumptions.execution_id,
    )


def tactical_reserve_for_fill(
    *,
    fill_notional: Decimal,
    fill_fee: Decimal,
    research: Recursive1mResearchParameters,
) -> Decimal:
    if fill_notional < 0 or fill_fee < 0:
        raise ValueError("fill cash values cannot be negative")
    ratio = (
        research.tactical_cash_reserve_fraction_of_slot
        / research.strategic_fraction_of_slot
    )
    return ((fill_notional + fill_fee) * ratio).quantize(
        Decimal("0.01"),
        rounding=ROUND_DOWN,
    )


__all__ = (
    "Recursive1mEntrySizingDecision",
    "Recursive1mExecutionFactAvailability",
    "Recursive1mExecutionSignal",
    "Recursive1mPosition",
    "diagnostic_entry_order",
    "diagnostic_execution_status",
    "diagnostic_exit_order",
    "diagnostic_fee_model",
    "size_recursive_1m_diagnostic_entry",
    "tactical_reserve_for_fill",
)
