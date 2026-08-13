from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal

from chanlun.decision_support.fingerprints import normalize_datetime
from chanlun.decision_support.trading_system.parameters import (
    STRATEGY_ID,
    StrategyParameters,
)
from chanlun.decision_support.trading_system.portfolio import floor_to_increment


OrderSide = Literal["buy", "sell"]
InstrumentKind = Literal["A_SHARE_STOCK", "EXCHANGE_TRADED_FUND"]
Persistence = Literal["OPTIONAL", "PERSISTENT_EXIT"]
OrderState = Literal["O_IDLE", "O_WORKING", "O_PARTIAL", "O_BLOCKED"]
TradingPhase = Literal["CONTINUOUS", "OPENING_AUCTION", "CLOSING_AUCTION", "CLOSED"]


@dataclass(frozen=True, slots=True)
class FeeRateAt:
    effective_from: date
    commission_rate: Decimal
    minimum_commission: Decimal
    stock_sell_stamp_rate: Decimal
    transfer_rate: Decimal
    other_buy_rate: Decimal = Decimal("0")
    other_sell_rate: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        if any(
            value < 0
            for value in (
                self.commission_rate,
                self.minimum_commission,
                self.stock_sell_stamp_rate,
                self.transfer_rate,
                self.other_buy_rate,
                self.other_sell_rate,
            )
        ):
            raise ValueError("fee values cannot be negative")


@dataclass(frozen=True, slots=True)
class FeeModel:
    schedule_id: str
    rates: tuple[FeeRateAt, ...]
    currency_quantum: Decimal = Decimal("0.01")

    def __post_init__(self) -> None:
        if not self.schedule_id or not self.rates:
            raise ValueError("fee schedule identity and rates are required")
        sessions = tuple(value.effective_from for value in self.rates)
        if sessions != tuple(sorted(set(sessions))):
            raise ValueError("fee rates must be unique and chronological")
        if self.currency_quantum <= 0:
            raise ValueError("currency quantum must be positive")

    def rate_at(self, session: date) -> FeeRateAt:
        available = tuple(value for value in self.rates if value.effective_from <= session)
        if not available:
            raise LookupError(f"fee schedule unavailable for {session.isoformat()}")
        return available[-1]

    def order_cost(
        self,
        *,
        side: OrderSide,
        instrument_kind: InstrumentKind,
        quantity: int,
        price: Decimal,
        session: date,
    ) -> Decimal:
        if quantity <= 0 or price <= 0:
            raise ValueError("fee quantity and price must be positive")
        return self.order_cost_for_fills(
            side=side,
            instrument_kind=instrument_kind,
            fills=((quantity, price),),
            session=session,
        )

    def order_cost_for_fills(
        self,
        *,
        side: OrderSide,
        instrument_kind: InstrumentKind,
        fills: tuple[tuple[int, Decimal], ...],
        session: date,
    ) -> Decimal:
        """根据一个终态委托的实际成交金额统一计算费用。"""

        if not fills or any(quantity <= 0 or price <= 0 for quantity, price in fills):
            raise ValueError("fee fills must contain positive quantities and prices")
        rate = self.rate_at(session)
        notional = sum(
            (Decimal(quantity) * price for quantity, price in fills),
            Decimal("0"),
        )
        commission = max(rate.minimum_commission, notional * rate.commission_rate)
        transfer = notional * rate.transfer_rate
        stamp = (
            notional * rate.stock_sell_stamp_rate
            if side == "sell" and instrument_kind == "A_SHARE_STOCK"
            else Decimal("0")
        )
        other = notional * (
            rate.other_buy_rate if side == "buy" else rate.other_sell_rate
        )
        return (commission + transfer + stamp + other).quantize(
            self.currency_quantum,
            rounding=ROUND_HALF_UP,
        )

    def bound_buy_cost(
        self,
        *,
        instrument_kind: InstrumentKind,
        session: date,
    ):
        def cost(quantity: int, price: Decimal) -> Decimal:
            return self.order_cost(
                side="buy",
                instrument_kind=instrument_kind,
                quantity=quantity,
                price=price,
                session=session,
            )

        return cost


@dataclass(frozen=True, slots=True)
class OrderIntent:
    client_order_id: str
    intent_id: str
    parameter_set_id: str
    rule_id: str
    structure_snapshot_id: str
    selection_snapshot_id: str | None
    account_snapshot_id: str
    symbol: str
    instrument_kind: InstrumentKind
    side: OrderSide
    quantity: int
    limit_price: Decimal
    signal_bar_end: datetime
    created_at: datetime
    broker_confirmed_at: datetime
    expires_at: datetime | None
    persistence: Persistence
    quantity_increment: int

    def __post_init__(self) -> None:
        signal = normalize_datetime(self.signal_bar_end, "signal_bar_end")
        created = normalize_datetime(self.created_at, "created_at")
        confirmed = normalize_datetime(self.broker_confirmed_at, "broker_confirmed_at")
        expires = (
            None
            if self.expires_at is None
            else normalize_datetime(self.expires_at, "expires_at")
        )
        object.__setattr__(self, "signal_bar_end", signal)
        object.__setattr__(self, "created_at", created)
        object.__setattr__(self, "broker_confirmed_at", confirmed)
        object.__setattr__(self, "expires_at", expires)
        if not all(
            value and value.strip()
            for value in (
                self.client_order_id,
                self.intent_id,
                self.parameter_set_id,
                self.rule_id,
                self.structure_snapshot_id,
                self.account_snapshot_id,
                self.symbol,
            )
        ):
            raise ValueError("order identity fields are required")
        if self.quantity <= 0 or self.quantity_increment <= 0:
            raise ValueError("order quantity and increment must be positive")
        if self.quantity % self.quantity_increment:
            raise ValueError("order quantity violates its increment")
        if self.limit_price <= 0:
            raise ValueError("order limit price must be positive")
        if not signal <= created <= confirmed:
            raise ValueError("order cannot precede its completed signal bar")
        if self.persistence == "OPTIONAL" and expires is None:
            raise ValueError("optional order requires an expiry")
        if expires is not None and expires < confirmed:
            raise ValueError("order expiry cannot precede broker confirmation")
        if self.persistence == "PERSISTENT_EXIT" and self.side != "sell":
            raise ValueError("persistent exit must be a sell order")


@dataclass(frozen=True, slots=True)
class HistoricalTradePrint:
    exchange_time: datetime
    sequence: int
    trade_price: Decimal
    trade_quantity: int
    best_bid: Decimal
    best_ask: Decimal
    quote_time: datetime
    quote_valid: bool
    phase: TradingPhase = "CONTINUOUS"

    def __post_init__(self) -> None:
        exchange_time = normalize_datetime(self.exchange_time, "exchange_time")
        quote_time = normalize_datetime(self.quote_time, "quote_time")
        object.__setattr__(self, "exchange_time", exchange_time)
        object.__setattr__(self, "quote_time", quote_time)
        if self.sequence < 0 or self.trade_quantity < 0:
            raise ValueError("trade sequence and quantity cannot be negative")
        if any(value <= 0 for value in (self.trade_price, self.best_bid, self.best_ask)):
            raise ValueError("trade and quote prices must be positive")
        if self.best_ask < self.best_bid:
            raise ValueError("best ask cannot be below best bid")
        if quote_time > exchange_time:
            raise ValueError("quote cannot be from the future")


@dataclass(frozen=True, slots=True)
class HistoricalExecutionStatus:
    observed_at: datetime
    listed: bool
    suspended: bool
    continuity_active: bool
    tick_and_quote_data_complete: bool
    sellable_quantity: int
    limit_up: Decimal
    limit_down: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "observed_at",
            normalize_datetime(self.observed_at, "observed_at"),
        )
        if self.sellable_quantity < 0 or self.limit_up <= 0 or self.limit_down <= 0:
            raise ValueError("historical execution status is invalid")
        if self.limit_down >= self.limit_up:
            raise ValueError("limit down must be below limit up")


@dataclass(frozen=True, slots=True)
class StrictFill:
    execution_id: str
    intent_id: str
    parameter_set_id: str
    rule_id: str
    structure_snapshot_id: str
    selection_snapshot_id: str | None
    account_snapshot_id: str
    exchange_time: datetime
    quantity: int
    execution_price: Decimal
    source_sequence: int


@dataclass(frozen=True, slots=True)
class StrictMatchResult:
    order_id: str
    state: OrderState
    fills: tuple[StrictFill, ...]
    filled_quantity: int
    remaining_quantity: int
    total_fees: Decimal
    rejection_and_unfilled_reasons: tuple[str, ...]
    exact_limit_touch_quantity: int


def match_historical_trade_events(
    order: OrderIntent,
    *,
    events: tuple[HistoricalTradePrint, ...],
    status: HistoricalExecutionStatus,
    fee_model: FeeModel,
    fee_session: date,
    parameters: StrategyParameters,
    frozen_latency: timedelta,
) -> StrictMatchResult:
    if frozen_latency < timedelta(0):
        raise ValueError("frozen latency cannot be negative")
    if order.parameter_set_id != parameters.parameter_set_id:
        raise ValueError("order and matcher parameter snapshots differ")
    reasons: list[str] = []
    if status.observed_at < order.broker_confirmed_at:
        reasons.append("EXECUTION_STATUS_PRECEDES_ORDER")
    if not status.listed:
        reasons.append("NOT_LISTED")
    if status.suspended:
        reasons.append("SUSPENDED")
    if not status.continuity_active and order.persistence == "OPTIONAL":
        reasons.append("TRADING_CONTINUITY_LOST")
    if not status.tick_and_quote_data_complete:
        reasons.append("HISTORICAL_TICK_OR_QUOTE_DATA_INCOMPLETE")
    if order.side == "sell" and status.sellable_quantity <= 0:
        reasons.append("T_PLUS_ONE_OR_SELLABLE_QUANTITY_BLOCK")
    if reasons:
        return StrictMatchResult(
            order.client_order_id,
            "O_BLOCKED" if order.persistence == "PERSISTENT_EXIT" else "O_IDLE",
            (),
            0,
            order.quantity,
            Decimal("0"),
            tuple(reasons),
            0,
        )
    ordered_events = tuple(sorted(events, key=lambda value: (value.exchange_time, value.sequence)))
    keys = tuple((value.exchange_time, value.sequence) for value in ordered_events)
    if len(keys) != len(set(keys)):
        raise ValueError("historical trade events must be uniquely sequenced")
    eligible_from = order.broker_confirmed_at + frozen_latency
    remaining = order.quantity
    sellable_remaining = (
        status.sellable_quantity if order.side == "sell" else order.quantity
    )
    fills: list[StrictFill] = []
    touch_quantity = 0
    first_eligible_quote_seen = False
    cancelled_on_boundary = False
    for event in ordered_events:
        if event.exchange_time <= eligible_from:
            continue
        if event.exchange_time <= order.signal_bar_end:
            raise ValueError("post-confirmation event cannot be inside the signal bar")
        if order.expires_at is not None and event.exchange_time > order.expires_at:
            break
        if event.phase != "CONTINUOUS" and order.persistence == "OPTIONAL":
            continue
        if not event.quote_valid:
            reasons.append("INVALID_OR_STALE_QUOTE")
            continue
        if not first_eligible_quote_seen:
            first_eligible_quote_seen = True
            unfavorable = (
                order.side == "buy" and event.best_ask > order.limit_price
            ) or (
                order.side == "sell" and event.best_bid < order.limit_price
            )
            if unfavorable and order.persistence == "OPTIONAL":
                cancelled_on_boundary = True
                reasons.append("FIRST_EXECUTABLE_QUOTE_OUTSIDE_PRICE_BOUNDARY")
                break
        if order.side == "buy" and order.limit_price > status.limit_up:
            reasons.append("BUY_LIMIT_ABOVE_DAILY_LIMIT")
            break
        if order.side == "sell" and order.limit_price < status.limit_down:
            reasons.append("SELL_LIMIT_BELOW_DAILY_LIMIT")
            break
        if event.trade_price == order.limit_price:
            touch_quantity += event.trade_quantity
            continue
        crossed = (
            event.trade_price < order.limit_price
            if order.side == "buy"
            else event.trade_price > order.limit_price
        )
        if not crossed or remaining <= 0:
            continue
        capacity = floor_to_increment(
            Decimal(event.trade_quantity) * parameters.historical_fill_participation,
            order.quantity_increment,
        )
        fill_quantity = min(remaining, capacity, sellable_remaining)
        fill_quantity = floor_to_increment(fill_quantity, order.quantity_increment)
        if fill_quantity <= 0:
            reasons.append("STRICT_CROSS_VOLUME_BELOW_QUANTITY_INCREMENT")
            continue
        fills.append(
            StrictFill(
                execution_id=f"{order.client_order_id}:{event.sequence}",
                intent_id=order.intent_id,
                parameter_set_id=order.parameter_set_id,
                rule_id=order.rule_id,
                structure_snapshot_id=order.structure_snapshot_id,
                selection_snapshot_id=order.selection_snapshot_id,
                account_snapshot_id=order.account_snapshot_id,
                exchange_time=event.exchange_time,
                quantity=fill_quantity,
                execution_price=order.limit_price,
                source_sequence=event.sequence,
            )
        )
        remaining -= fill_quantity
        sellable_remaining -= fill_quantity
        if remaining == 0:
            break
        if order.side == "sell" and sellable_remaining == 0:
            reasons.append("T_PLUS_ONE_PARTIAL_SELLABLE_LIMIT")
            break
    filled = sum(value.quantity for value in fills)
    if touch_quantity:
        reasons.append("EXACT_LIMIT_TOUCH_NOT_FILLED")
    if not fills and not cancelled_on_boundary and not reasons:
        reasons.append("NO_STRICT_POST_CONFIRMATION_CROSS")
    if remaining > 0 and order.expires_at is not None:
        reasons.append("ORDER_EXPIRED_WITH_UNFILLED_QUANTITY")
    total_fees = (
        Decimal("0")
        if filled == 0
        else fee_model.order_cost(
            side=order.side,
            instrument_kind=order.instrument_kind,
            quantity=filled,
            price=order.limit_price,
            session=fee_session,
        )
    )
    state: OrderState
    if remaining == 0:
        state = "O_IDLE"
    elif filled:
        state = "O_PARTIAL"
    elif order.persistence == "PERSISTENT_EXIT":
        state = "O_BLOCKED"
    else:
        state = "O_IDLE"
    return StrictMatchResult(
        order_id=order.client_order_id,
        state=state,
        fills=tuple(fills),
        filled_quantity=filled,
        remaining_quantity=remaining,
        total_fees=total_fees,
        rejection_and_unfilled_reasons=tuple(dict.fromkeys(reasons)),
        exact_limit_touch_quantity=touch_quantity,
    )


@dataclass(frozen=True, slots=True)
class LocalOrderRecoveryState:
    client_order_id: str
    intent_id: str
    broker_order_id: str | None
    state: OrderState
    cumulative_filled: int
    last_broker_sequence: int
    execution_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.cumulative_filled < 0 or self.last_broker_sequence < 0:
            raise ValueError("local recovery counters cannot be negative")
        if len(self.execution_ids) != len(set(self.execution_ids)):
            raise ValueError("execution ids must be unique")


@dataclass(frozen=True, slots=True)
class BrokerOrderRecoveryState:
    client_order_id: str
    intent_id: str | None
    broker_order_id: str
    active: bool
    cumulative_filled: int
    last_broker_sequence: int
    execution_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RestartReconciliation:
    passed: bool
    operations_state: Literal["NORMAL", "OPERATIONS_HALT"]
    reason_codes: tuple[str, ...]


def reconcile_orders_after_restart(
    local_orders: tuple[LocalOrderRecoveryState, ...],
    broker_orders: tuple[BrokerOrderRecoveryState, ...],
) -> RestartReconciliation:
    reasons: list[str] = []
    local = {value.client_order_id: value for value in local_orders}
    broker = {value.client_order_id: value for value in broker_orders}
    if len(local) != len(local_orders) or len(broker) != len(broker_orders):
        reasons.append("DUPLICATE_ORDER_ID")
    for order_id, remote in broker.items():
        stored = local.get(order_id)
        if stored is None:
            if remote.active:
                reasons.append(f"UNKNOWN_ACTIVE_BROKER_ORDER:{order_id}")
            continue
        if remote.intent_id != stored.intent_id:
            reasons.append(f"INTENT_ID_MISMATCH:{order_id}")
        if stored.broker_order_id not in {None, remote.broker_order_id}:
            reasons.append(f"BROKER_ORDER_ID_MISMATCH:{order_id}")
        if remote.cumulative_filled < stored.cumulative_filled:
            reasons.append(f"CUMULATIVE_FILL_REGRESSION:{order_id}")
        if remote.last_broker_sequence < stored.last_broker_sequence:
            reasons.append(f"BROKER_SEQUENCE_REGRESSION:{order_id}")
        if not set(stored.execution_ids).issubset(set(remote.execution_ids)):
            reasons.append(f"EXECUTION_HISTORY_MISMATCH:{order_id}")
    for order_id, stored in local.items():
        if stored.state in {"O_WORKING", "O_PARTIAL"} and order_id not in broker:
            reasons.append(f"LOCAL_ACTIVE_ORDER_MISSING_AT_BROKER:{order_id}")
    return RestartReconciliation(
        passed=not reasons,
        operations_state="NORMAL" if not reasons else "OPERATIONS_HALT",
        reason_codes=tuple(reasons),
    )


def build_client_order_id(
    *,
    parameter_set_id: str,
    symbol: str,
    rule_id: str,
    confirmation_time: datetime,
    sequence: int,
) -> str:
    observed = normalize_datetime(confirmation_time, "confirmation_time")
    if sequence < 0:
        raise ValueError("order sequence cannot be negative")
    return "/".join(
        (
            STRATEGY_ID,
            parameter_set_id,
            symbol,
            rule_id,
            observed.isoformat(),
            str(sequence),
        )
    )


__all__ = [
    "BrokerOrderRecoveryState",
    "HistoricalExecutionStatus",
    "HistoricalTradePrint",
    "LocalOrderRecoveryState",
    "RestartReconciliation",
    "StrictFill",
    "StrictMatchResult",
    "FeeModel",
    "FeeRateAt",
    "OrderIntent",
    "build_client_order_id",
    "match_historical_trade_events",
    "reconcile_orders_after_restart",
]
