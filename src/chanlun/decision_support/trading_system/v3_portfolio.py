from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal, ROUND_DOWN
from typing import Callable, Literal

from chanlun.decision_support.fingerprints import normalize_datetime
from chanlun.decision_support.trading_system.v3_parameters import StrategyV3Parameters


StrategicState = Literal[
    "S_FLAT",
    "S_WAIT_CENTER",
    "S_WAIT_DEPARTURE",
    "S_WAIT_RETURN",
    "S_ENTRY_READY",
    "S_ENTRY_WORKING",
    "S_ACTIVE_FULL",
    "S_REDUCE_WORKING",
    "S_ACTIVE_HALF",
    "S_EXIT_WORKING",
    "S_CLOSED",
]
HoldingBucket = Literal["CORE", "TACTICAL", "CORE_REMAINDER", "CORE_ODD_LOT"]
RestoreStatus = Literal[
    "OPEN",
    "PARTIAL",
    "CLOSED",
    "TERMINATED_BY_STRATEGIC",
    "UNRESTORABLE_BY_CORPORATE_ACTION",
]


def floor_to_increment(quantity: int | Decimal, increment: int) -> int:
    if increment <= 0:
        raise ValueError("quantity increment must be positive")
    value = int(Decimal(quantity).to_integral_value(rounding=ROUND_DOWN))
    return max(0, value // increment * increment)


@dataclass(frozen=True, slots=True)
class EntrySizingInput:
    account_equity_at_decision: Decimal
    broker_available_cash: Decimal
    current_gross_market_value: Decimal
    restore_exposure_commitment: Decimal
    restore_cash_reserve: Decimal
    reserved_strategic_entry_notional: Decimal
    active_buy_worst_cash_required: Decimal
    active_buy_restore_cash_allocated: Decimal
    buy_price_cap: Decimal
    q_liquidity_cap: int
    buy_quantity_increment: int
    occupied_slots: int
    drawdown: Decimal
    operations_normal: bool = True
    reconciliation_passed: bool = True

    def __post_init__(self) -> None:
        money = (
            self.account_equity_at_decision,
            self.broker_available_cash,
            self.current_gross_market_value,
            self.restore_exposure_commitment,
            self.restore_cash_reserve,
            self.reserved_strategic_entry_notional,
            self.active_buy_worst_cash_required,
            self.active_buy_restore_cash_allocated,
        )
        if self.account_equity_at_decision <= 0 or self.buy_price_cap <= 0:
            raise ValueError("equity and buy price cap must be positive")
        if any(value < 0 for value in money[1:]) or self.drawdown < 0:
            raise ValueError("cash, exposure and drawdown values cannot be negative")
        if self.active_buy_restore_cash_allocated > self.active_buy_worst_cash_required:
            raise ValueError("restore allocation cannot exceed active buy requirement")
        if self.q_liquidity_cap < 0 or self.buy_quantity_increment <= 0:
            raise ValueError("invalid liquidity cap or quantity increment")
        if self.occupied_slots < 0:
            raise ValueError("occupied slots cannot be negative")


@dataclass(frozen=True, slots=True)
class EntrySizingDecision:
    q_plan: int
    u_slot: Decimal
    u_remain: Decimal
    total_protected_cash: Decimal
    entry_cash_available: Decimal
    max_affordable_buy_qty: int
    capacity_rows: tuple[tuple[str, int], ...]
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TacticalPairObservation:
    pair_id: str
    confirmed_at: datetime
    net_edge_ticks: Decimal | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "confirmed_at",
            normalize_datetime(self.confirmed_at, "confirmed_at"),
        )
        if not self.pair_id:
            raise ValueError("tactical pair identity is required")
        if self.net_edge_ticks is not None and not self.net_edge_ticks.is_finite():
            raise ValueError("tactical pair edge must be finite or NON_EXECUTABLE")


@dataclass(frozen=True, slots=True)
class TacticalAdaptationDecision:
    passed: bool
    resolved: bool
    sample_count: int
    non_executable_count: int
    lower_median_edge_ticks: Decimal | None
    reason_codes: tuple[str, ...]


def assess_tactical_adaptation(
    observations: tuple[TacticalPairObservation, ...],
    *,
    decision_time: datetime,
    parameters: StrategyV3Parameters,
) -> TacticalAdaptationDecision:
    decision = normalize_datetime(decision_time, "decision_time")
    visible = tuple(
        sorted(
            (row for row in observations if row.confirmed_at <= decision),
            key=lambda row: (row.confirmed_at, row.pair_id),
        )
    )
    identities = tuple(row.pair_id for row in visible)
    if len(identities) != len(set(identities)):
        raise ValueError("tactical pair identities must be unique")
    sample = visible[-parameters.tactical_pair_lookback :]
    if len(sample) < parameters.tactical_pair_minimum:
        return TacticalAdaptationDecision(
            passed=False,
            resolved=False,
            sample_count=len(sample),
            non_executable_count=sum(row.net_edge_ticks is None for row in sample),
            lower_median_edge_ticks=None,
            reason_codes=("TACTICAL_PAIR_SAMPLE_UNRESOLVED",),
        )
    ordered_edges = tuple(
        sorted(
            (row.net_edge_ticks for row in sample),
            key=lambda value: (
                0 if value is None else 1,
                Decimal("0") if value is None else value,
            ),
        )
    )
    lower_median = ordered_edges[(len(ordered_edges) - 1) // 2]
    passed = (
        lower_median is not None
        and lower_median >= parameters.tactical_lower_median_edge_ticks_min
    )
    return TacticalAdaptationDecision(
        passed=passed,
        resolved=True,
        sample_count=len(sample),
        non_executable_count=sum(value is None for value in ordered_edges),
        lower_median_edge_ticks=lower_median,
        reason_codes=(
            ("TACTICAL_LOWER_MEDIAN_EDGE_PASS")
            if passed
            else ("TACTICAL_LOWER_MEDIAN_EDGE_FAIL")
        ,),
    )


@dataclass(frozen=True, slots=True)
class PartialPrefixEdgeDecision:
    passed: bool
    checked_prefixes: tuple[int, ...]
    failed_prefixes: tuple[int, ...]


def check_every_partial_buyback_prefix(
    *,
    quantity: int,
    quantity_increment: int,
    buy_limit_price: Decimal,
    price_tick: Decimal,
    available_net_sell_cash: Callable[[int], Decimal],
    bound_terminal_buy_cost: Callable[[int, Decimal], Decimal],
) -> PartialPrefixEdgeDecision:
    """Require every possible terminal partial fill to retain one tick net cash."""

    if (
        quantity <= 0
        or quantity_increment <= 0
        or quantity % quantity_increment
        or buy_limit_price <= 0
        or price_tick <= 0
    ):
        raise ValueError("invalid partial-prefix edge inputs")
    prefixes = tuple(range(quantity_increment, quantity + 1, quantity_increment))
    failures: list[int] = []
    for prefix in prefixes:
        net_after_buy = (
            available_net_sell_cash(prefix)
            - Decimal(prefix) * buy_limit_price
            - bound_terminal_buy_cost(prefix, buy_limit_price)
        )
        if net_after_buy < Decimal(prefix) * price_tick:
            failures.append(prefix)
    return PartialPrefixEdgeDecision(
        passed=not failures,
        checked_prefixes=prefixes,
        failed_prefixes=tuple(failures),
    )


def _maximum_affordable_quantity(
    *,
    cash: Decimal,
    price: Decimal,
    increment: int,
    upper_bound: int,
    bound_buy_cost: Callable[[int, Decimal], Decimal],
) -> int:
    quantity = floor_to_increment(upper_bound, increment)
    while quantity > 0:
        required = Decimal(quantity) * price + bound_buy_cost(quantity, price)
        if required <= cash:
            return quantity
        quantity -= increment
    return 0


def size_v3_strategic_entry(
    sizing: EntrySizingInput,
    *,
    parameters: StrategyV3Parameters,
    bound_buy_cost: Callable[[int, Decimal], Decimal],
) -> EntrySizingDecision:
    u_slot = sizing.account_equity_at_decision * parameters.slot_fraction
    u_remain = max(
        Decimal("0"),
        sizing.account_equity_at_decision * parameters.account_exposure_cap
        - sizing.current_gross_market_value
        - sizing.restore_exposure_commitment
        - sizing.reserved_strategic_entry_notional,
    )
    total_protected_cash = sizing.restore_cash_reserve + max(
        Decimal("0"),
        sizing.active_buy_worst_cash_required
        - sizing.active_buy_restore_cash_allocated,
    )
    entry_cash_available = max(
        Decimal("0"), sizing.broker_available_cash - total_protected_cash
    )
    slot_qty = floor_to_increment(
        u_slot / sizing.buy_price_cap,
        sizing.buy_quantity_increment,
    )
    exposure_qty = floor_to_increment(
        u_remain / sizing.buy_price_cap,
        sizing.buy_quantity_increment,
    )
    liquidity_qty = floor_to_increment(
        sizing.q_liquidity_cap,
        sizing.buy_quantity_increment,
    )
    # Compute cash capacity independently.  Capping this search by the
    # smallest of the other capacities leaves Q_PLAN unchanged, but falsely
    # reports cash as a co-binding constraint whenever (for example) U_SLOT is
    # the real limit.
    cash_upper = floor_to_increment(
        entry_cash_available / sizing.buy_price_cap,
        sizing.buy_quantity_increment,
    )
    affordable_qty = _maximum_affordable_quantity(
        cash=entry_cash_available,
        price=sizing.buy_price_cap,
        increment=sizing.buy_quantity_increment,
        upper_bound=cash_upper,
        bound_buy_cost=bound_buy_cost,
    )
    capacities = (
        ("slot_cap", slot_qty),
        ("remaining_exposure_cap", exposure_qty),
        ("cash_with_bound_cost_cap", affordable_qty),
        ("liquidity_cap", liquidity_qty),
    )
    reasons: list[str] = []
    blocked = False
    if not sizing.operations_normal or not sizing.reconciliation_passed:
        reasons.append("OPERATIONS_OR_RECONCILIATION_HALT")
        blocked = True
    if sizing.occupied_slots >= parameters.slot_count:
        reasons.append("NO_FREE_STRATEGIC_SLOT")
        blocked = True
    if sizing.drawdown >= parameters.entry_drawdown_halt:
        reasons.append("DRAWDOWN_ENTRY_HALT")
        blocked = True
    q_plan = 0 if blocked else min(value for _name, value in capacities)
    if q_plan <= 0:
        q_plan = 0
        if not blocked:
            reasons.append("LESS_THAN_ONE_BUY_INCREMENT_AFTER_ALL_CAPS")
    else:
        binding = tuple(name for name, value in capacities if value == q_plan)
        reasons.extend(f"BINDING_{name.upper()}" for name in binding)
    return EntrySizingDecision(
        q_plan=q_plan,
        u_slot=u_slot,
        u_remain=u_remain,
        total_protected_cash=total_protected_cash,
        entry_cash_available=entry_cash_available,
        max_affordable_buy_qty=affordable_qty,
        capacity_rows=capacities,
        reason_codes=tuple(reasons),
    )


@dataclass(frozen=True, slots=True)
class HoldingLot:
    lot_id: str
    bucket: HoldingBucket
    acquired_on: date
    quantity: int

    def __post_init__(self) -> None:
        if not self.lot_id or self.quantity <= 0:
            raise ValueError("holding lot identity and positive quantity are required")


@dataclass(frozen=True, slots=True)
class RestoreCohort:
    restore_cohort_id: str
    sell_execution_id: str
    sell_exchange_time: datetime
    open_qty: int
    remaining_qty: int
    gross_sell_cash: Decimal
    allocated_sell_cost: Decimal
    cash_reserve_remaining: Decimal
    foregone_cash_distribution: Decimal = Decimal("0")
    status: RestoreStatus = "OPEN"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "sell_exchange_time",
            normalize_datetime(self.sell_exchange_time, "sell_exchange_time"),
        )
        if not self.restore_cohort_id or not self.sell_execution_id:
            raise ValueError("restore cohort identity is required")
        if self.open_qty <= 0 or not 0 <= self.remaining_qty <= self.open_qty:
            raise ValueError("restore cohort quantities are invalid")
        if any(
            value < 0
            for value in (
                self.gross_sell_cash,
                self.allocated_sell_cost,
                self.cash_reserve_remaining,
                self.foregone_cash_distribution,
            )
        ):
            raise ValueError("restore cohort cash values cannot be negative")
        expected = (
            "CLOSED"
            if self.remaining_qty == 0
            and self.status
            not in {
                "TERMINATED_BY_STRATEGIC",
                "UNRESTORABLE_BY_CORPORATE_ACTION",
            }
            else self.status
        )
        if expected != self.status:
            raise ValueError("zero remaining restore cohort must be closed")


@dataclass(frozen=True, slots=True)
class BuybackRealization:
    quantity: int
    released_cash_reserve: Decimal
    allocated_sell_cash: Decimal
    allocated_sell_cost: Decimal
    allocated_foregone_distribution: Decimal
    buy_cash_and_cost: Decimal

    @property
    def realized_net_cash(self) -> Decimal:
        return (
            self.allocated_sell_cash
            - self.allocated_sell_cost
            - self.allocated_foregone_distribution
            - self.buy_cash_and_cost
        )


@dataclass(frozen=True, slots=True)
class CycleLedger:
    cycle_id: str
    strategic_state: StrategicState
    session: date
    t_plus_days: int
    buy_quantity_increment: int
    sell_quantity_increment: int
    q_cycle: int
    core_target_qty: int
    tactical_target_qty: int
    holding_lots: tuple[HoldingLot, ...]
    restore_cohorts: tuple[RestoreCohort, ...] = ()
    completed_tactical_cycle_sessions: tuple[date, ...] = ()
    terminated_restore_qty: int = 0
    corporate_action_unrestorable_restore_qty: int = 0
    cash_released_by_normalization: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        if not self.cycle_id:
            raise ValueError("cycle_id is required")
        if self.t_plus_days < 0 or self.buy_quantity_increment <= 0 or self.sell_quantity_increment <= 0:
            raise ValueError("invalid settlement or quantity increments")
        quantities = (
            self.q_cycle,
            self.core_target_qty,
            self.tactical_target_qty,
            self.terminated_restore_qty,
            self.corporate_action_unrestorable_restore_qty,
        )
        if any(value < 0 for value in quantities):
            raise ValueError("cycle quantities cannot be negative")
        lot_ids = tuple(value.lot_id for value in self.holding_lots)
        cohort_ids = tuple(value.restore_cohort_id for value in self.restore_cohorts)
        execution_ids = tuple(value.sell_execution_id for value in self.restore_cohorts)
        if len(lot_ids) != len(set(lot_ids)):
            raise ValueError("holding lot ids must be unique")
        if len(cohort_ids) != len(set(cohort_ids)) or len(execution_ids) != len(set(execution_ids)):
            raise ValueError("restore cohort identities must be unique")
        if self.cash_released_by_normalization < 0:
            raise ValueError("released cash cannot be negative")
        if self.completed_tactical_cycle_sessions != tuple(
            sorted(set(self.completed_tactical_cycle_sessions))
        ):
            raise ValueError("at most one tactical cycle may complete per session")
        self.validate_invariants()

    @classmethod
    def from_entry_fill(
        cls,
        *,
        cycle_id: str,
        session: date,
        fill_qty: int,
        buy_quantity_increment: int,
        sell_quantity_increment: int,
        t_plus_days: int,
        tactical_ratio: Decimal,
    ) -> CycleLedger:
        if fill_qty <= 0:
            raise ValueError("entry fill quantity must be positive")
        tactical = floor_to_increment(
            Decimal(fill_qty) * tactical_ratio,
            sell_quantity_increment,
        )
        core = fill_qty - tactical
        lots: list[HoldingLot] = []
        if core:
            lots.append(HoldingLot(f"{cycle_id}:core:0", "CORE", session, core))
        if tactical:
            lots.append(
                HoldingLot(f"{cycle_id}:tactical:0", "TACTICAL", session, tactical)
            )
        return cls(
            cycle_id=cycle_id,
            strategic_state="S_ACTIVE_FULL",
            session=session,
            t_plus_days=t_plus_days,
            buy_quantity_increment=buy_quantity_increment,
            sell_quantity_increment=sell_quantity_increment,
            q_cycle=fill_qty,
            core_target_qty=core,
            tactical_target_qty=tactical,
            holding_lots=tuple(lots),
        )

    @property
    def q_current(self) -> int:
        return sum(value.quantity for value in self.holding_lots)

    @property
    def core_held_qty(self) -> int:
        return sum(
            value.quantity
            for value in self.holding_lots
            if value.bucket in {"CORE", "CORE_REMAINDER", "CORE_ODD_LOT"}
        )

    @property
    def tactical_held_qty(self) -> int:
        return sum(
            value.quantity for value in self.holding_lots if value.bucket == "TACTICAL"
        )

    @property
    def pending_restore_qty(self) -> int:
        return sum(value.remaining_qty for value in self.restore_cohorts if value.status in {"OPEN", "PARTIAL"})

    @property
    def restore_cash_reserve(self) -> Decimal:
        return sum(
            (value.cash_reserve_remaining for value in self.restore_cohorts if value.status in {"OPEN", "PARTIAL"}),
            Decimal("0"),
        )

    @property
    def tactical_eligible_qty(self) -> int:
        cutoff = self.session.toordinal() - self.t_plus_days
        return sum(
            value.quantity
            for value in self.holding_lots
            if value.bucket == "TACTICAL" and value.acquired_on.toordinal() <= cutoff
        )

    @property
    def tactical_locked_qty(self) -> int:
        return self.tactical_held_qty - self.tactical_eligible_qty

    @property
    def tactical_cycles_completed_today(self) -> int:
        return int(self.session in self.completed_tactical_cycle_sessions)

    def validate_invariants(self) -> None:
        if self.q_current > self.q_cycle:
            raise ValueError("Q_CURRENT exceeds Q_CYCLE")
        if self.core_held_qty + self.tactical_held_qty != self.q_current:
            raise ValueError("holding bucket sum does not match Q_CURRENT")
        if self.strategic_state == "S_ACTIVE_FULL":
            if self.tactical_held_qty + self.pending_restore_qty != self.tactical_target_qty:
                raise ValueError("tactical held plus restore must equal tactical target")
            if self.q_current + self.pending_restore_qty != self.q_cycle:
                raise ValueError("Q_CURRENT plus restore must equal Q_CYCLE")
        if not 0 <= self.tactical_locked_qty <= self.tactical_held_qty:
            raise ValueError("locked tactical quantity is invalid")
        if not 0 <= self.tactical_eligible_qty <= self.tactical_held_qty:
            raise ValueError("eligible tactical quantity is invalid")

    def roll_session(self, session: date) -> CycleLedger:
        if session < self.session:
            raise ValueError("ledger session cannot move backwards")
        return replace(self, session=session)

    def apply_tactical_sell_fill(
        self,
        *,
        quantity: int,
        execution_id: str,
        exchange_time: datetime,
        gross_sell_cash: Decimal,
        allocated_sell_cost: Decimal,
        cash_reserve: Decimal,
    ) -> CycleLedger:
        if self.strategic_state != "S_ACTIVE_FULL" or self.pending_restore_qty:
            raise ValueError("ordinary tactical sell is not enabled")
        if self.tactical_cycles_completed_today:
            raise ValueError("daily tactical cycle limit already consumed")
        if quantity <= 0 or quantity % self.sell_quantity_increment:
            raise ValueError("tactical sell quantity violates sell increment")
        if quantity > self.tactical_eligible_qty:
            raise ValueError("tactical sell exceeds T+1 eligible quantity")
        remaining = quantity
        lots: list[HoldingLot] = []
        cutoff = self.session.toordinal() - self.t_plus_days
        for lot in self.holding_lots:
            if (
                remaining
                and lot.bucket == "TACTICAL"
                and lot.acquired_on.toordinal() <= cutoff
            ):
                consumed = min(remaining, lot.quantity)
                remaining -= consumed
                if consumed < lot.quantity:
                    lots.append(replace(lot, quantity=lot.quantity - consumed))
            else:
                lots.append(lot)
        if remaining:
            raise ValueError("eligible tactical lot consumption was incomplete")
        cohort = RestoreCohort(
            restore_cohort_id=f"{self.cycle_id}:restore:{execution_id}",
            sell_execution_id=execution_id,
            sell_exchange_time=exchange_time,
            open_qty=quantity,
            remaining_qty=quantity,
            gross_sell_cash=gross_sell_cash,
            allocated_sell_cost=allocated_sell_cost,
            cash_reserve_remaining=cash_reserve,
        )
        return replace(
            self,
            holding_lots=tuple(lots),
            restore_cohorts=self.restore_cohorts + (cohort,),
        )

    def apply_tactical_buyback_fill(
        self,
        *,
        quantity: int,
        execution_id: str,
        exchange_time: datetime,
        buy_cash_and_cost: Decimal,
    ) -> tuple[CycleLedger, BuybackRealization]:
        observed = normalize_datetime(exchange_time, "exchange_time")
        if quantity <= 0 or quantity % self.buy_quantity_increment:
            raise ValueError("buyback quantity violates buy increment")
        if quantity > self.pending_restore_qty:
            raise ValueError("buyback exceeds pending restore quantity")
        if buy_cash_and_cost < 0:
            raise ValueError("buyback cash cannot be negative")
        remaining = quantity
        cohorts: list[RestoreCohort] = []
        allocated_cash = Decimal("0")
        allocated_cost = Decimal("0")
        allocated_foregone = Decimal("0")
        released_reserve = Decimal("0")
        ordered = sorted(
            self.restore_cohorts,
            key=lambda value: (value.sell_exchange_time, value.sell_execution_id),
        )
        for cohort in ordered:
            if not remaining or cohort.status not in {"OPEN", "PARTIAL"}:
                cohorts.append(cohort)
                continue
            consumed = min(remaining, cohort.remaining_qty)
            remaining -= consumed
            denominator = Decimal(cohort.remaining_qty)
            fraction = Decimal(consumed) / denominator
            sell_cash = cohort.gross_sell_cash * Decimal(consumed) / Decimal(cohort.open_qty)
            sell_cost = cohort.allocated_sell_cost * Decimal(consumed) / Decimal(cohort.open_qty)
            foregone = cohort.foregone_cash_distribution * Decimal(consumed) / Decimal(cohort.open_qty)
            reserve = cohort.cash_reserve_remaining * fraction
            allocated_cash += sell_cash
            allocated_cost += sell_cost
            allocated_foregone += foregone
            released_reserve += reserve
            new_remaining = cohort.remaining_qty - consumed
            cohorts.append(
                replace(
                    cohort,
                    remaining_qty=new_remaining,
                    cash_reserve_remaining=cohort.cash_reserve_remaining - reserve,
                    status="CLOSED" if new_remaining == 0 else "PARTIAL",
                )
            )
        if remaining:
            raise ValueError("FIFO restore consumption was incomplete")
        lot = HoldingLot(
            lot_id=f"{self.cycle_id}:tactical-buy:{execution_id}",
            bucket="TACTICAL",
            acquired_on=observed.date(),
            quantity=quantity,
        )
        completes_cycle = all(
            value.status not in {"OPEN", "PARTIAL"} for value in cohorts
        )
        completed_sessions = self.completed_tactical_cycle_sessions
        if completes_cycle:
            if observed.date() in completed_sessions:
                raise ValueError("daily tactical cycle limit already consumed")
            completed_sessions = completed_sessions + (observed.date(),)
        ledger = replace(
            self,
            holding_lots=self.holding_lots + (lot,),
            restore_cohorts=tuple(cohorts),
            completed_tactical_cycle_sessions=completed_sessions,
        )
        realization = BuybackRealization(
            quantity=quantity,
            released_cash_reserve=released_reserve,
            allocated_sell_cash=allocated_cash,
            allocated_sell_cost=allocated_cost,
            allocated_foregone_distribution=allocated_foregone,
            buy_cash_and_cost=buy_cash_and_cost,
        )
        return ledger, realization

    def terminate_restore_obligations(
        self,
        *,
        target_state: Literal["S_REDUCE_WORKING", "S_EXIT_WORKING"],
    ) -> CycleLedger:
        pending = self.pending_restore_qty
        cohorts = tuple(
            replace(
                cohort,
                remaining_qty=0,
                cash_reserve_remaining=Decimal("0"),
                status="TERMINATED_BY_STRATEGIC",
            )
            if cohort.status in {"OPEN", "PARTIAL"}
            else cohort
            for cohort in self.restore_cohorts
        )
        reduce_target = floor_to_increment(
            Decimal(self.q_cycle) * Decimal("0.50"),
            self.sell_quantity_increment,
        )
        actual_state: Literal["S_REDUCE_WORKING", "S_EXIT_WORKING"] = (
            "S_EXIT_WORKING"
            if target_state == "S_REDUCE_WORKING" and reduce_target == 0
            else target_state
        )
        return replace(
            self,
            strategic_state=actual_state,
            restore_cohorts=cohorts,
            terminated_restore_qty=self.terminated_restore_qty + pending,
            tactical_target_qty=0,
            core_target_qty=(
                reduce_target if actual_state == "S_REDUCE_WORKING" else 0
            ),
            holding_lots=tuple(
                replace(lot, bucket="CORE_REMAINDER")
                if lot.bucket == "TACTICAL"
                else lot
                for lot in self.holding_lots
            ),
        )

    def apply_mandatory_share_action(
        self,
        *,
        share_multiplier: Decimal,
        broker_position_qty: int,
    ) -> CycleLedger:
        """Apply a broker-confirmed non-trade quantity transformation.

        The broker position is authoritative.  Any restore remainder that the
        post-action buy increment cannot reproduce is removed from Q_CYCLE and
        retained in the dedicated audit quantity; the method never rounds up.
        """

        if share_multiplier <= 0 or broker_position_qty < 0:
            raise ValueError("invalid mandatory share action")
        scaled_lots: list[HoldingLot] = []
        for lot in self.holding_lots:
            quantity = int(
                (Decimal(lot.quantity) * share_multiplier).to_integral_value(
                    rounding=ROUND_DOWN
                )
            )
            if quantity:
                scaled_lots.append(replace(lot, quantity=quantity))
        current = sum(value.quantity for value in scaled_lots)
        difference = broker_position_qty - current
        if difference < 0 or (
            difference > 0
            and difference > len(self.holding_lots)
        ):
            raise ValueError("broker corporate-action quantity cannot be reconciled")
        if difference and not scaled_lots:
            raise ValueError("broker position exists without a local holding lot")
        if difference:
            scaled_lots[0] = replace(
                scaled_lots[0], quantity=scaled_lots[0].quantity + difference
            )

        normalized_lots: list[HoldingLot] = []
        for lot in scaled_lots:
            if lot.bucket != "TACTICAL":
                normalized_lots.append(lot)
                continue
            executable = floor_to_increment(lot.quantity, self.sell_quantity_increment)
            odd = lot.quantity - executable
            if executable:
                normalized_lots.append(replace(lot, quantity=executable))
            if odd:
                normalized_lots.append(
                    HoldingLot(
                        lot_id=f"{lot.lot_id}:corporate-odd",
                        bucket="CORE_ODD_LOT",
                        acquired_on=lot.acquired_on,
                        quantity=odd,
                    )
                )

        scaled_cohorts: list[RestoreCohort] = []
        for cohort in self.restore_cohorts:
            if cohort.status not in {"OPEN", "PARTIAL"}:
                scaled_cohorts.append(cohort)
                continue
            open_qty = max(
                1,
                int(
                    (Decimal(cohort.open_qty) * share_multiplier).to_integral_value(
                        rounding=ROUND_DOWN
                    )
                ),
            )
            remaining_qty = int(
                (
                    Decimal(cohort.remaining_qty) * share_multiplier
                ).to_integral_value(rounding=ROUND_DOWN)
            )
            scaled_cohorts.append(
                replace(
                    cohort,
                    open_qty=max(open_qty, remaining_qty),
                    remaining_qty=remaining_qty,
                    status="CLOSED" if remaining_qty == 0 else cohort.status,
                )
            )
        raw_pending = sum(
            value.remaining_qty
            for value in scaled_cohorts
            if value.status in {"OPEN", "PARTIAL"}
        )
        executable_pending = floor_to_increment(
            raw_pending, self.buy_quantity_increment
        )
        discard = raw_pending - executable_pending
        released = Decimal("0")
        if discard:
            for index in range(len(scaled_cohorts) - 1, -1, -1):
                cohort = scaled_cohorts[index]
                if discard == 0 or cohort.status not in {"OPEN", "PARTIAL"}:
                    continue
                consumed = min(discard, cohort.remaining_qty)
                old_remaining = cohort.remaining_qty
                reserve_release = (
                    Decimal("0")
                    if old_remaining == 0
                    else cohort.cash_reserve_remaining
                    * Decimal(consumed)
                    / Decimal(old_remaining)
                )
                remaining = old_remaining - consumed
                scaled_cohorts[index] = replace(
                    cohort,
                    remaining_qty=remaining,
                    cash_reserve_remaining=(
                        cohort.cash_reserve_remaining - reserve_release
                    ),
                    status=(
                        "UNRESTORABLE_BY_CORPORATE_ACTION"
                        if remaining == 0
                        else "PARTIAL"
                    ),
                )
                released += reserve_release
                discard -= consumed
        if discard:
            raise ValueError("corporate-action restore normalization was incomplete")
        tactical_held = sum(
            value.quantity for value in normalized_lots if value.bucket == "TACTICAL"
        )
        core_held = broker_position_qty - tactical_held
        return replace(
            self,
            q_cycle=broker_position_qty + executable_pending,
            core_target_qty=core_held,
            tactical_target_qty=tactical_held + executable_pending,
            holding_lots=tuple(normalized_lots),
            restore_cohorts=tuple(scaled_cohorts),
            corporate_action_unrestorable_restore_qty=(
                self.corporate_action_unrestorable_restore_qty
                + raw_pending
                - executable_pending
            ),
            cash_released_by_normalization=(
                self.cash_released_by_normalization + released
            ),
        )

    def apply_strategic_sell_fill(self, *, quantity: int) -> CycleLedger:
        if self.strategic_state not in {"S_REDUCE_WORKING", "S_EXIT_WORKING"}:
            raise ValueError("strategic sell requires an active strategic order")
        if quantity <= 0 or quantity > self.q_current:
            raise ValueError("strategic sell quantity is invalid")
        remaining = quantity
        lots: list[HoldingLot] = []
        for lot in self.holding_lots:
            if remaining:
                consumed = min(remaining, lot.quantity)
                remaining -= consumed
                if consumed < lot.quantity:
                    lots.append(replace(lot, quantity=lot.quantity - consumed))
            else:
                lots.append(lot)
        target_state: StrategicState = self.strategic_state
        remaining_qty = sum(lot.quantity for lot in lots)
        if self.strategic_state == "S_EXIT_WORKING" and remaining_qty == 0:
            target_state = "S_CLOSED"
        elif self.strategic_state == "S_REDUCE_WORKING" and remaining_qty <= self.core_target_qty:
            target_state = "S_ACTIVE_HALF"
        return replace(self, strategic_state=target_state, holding_lots=tuple(lots))


@dataclass(frozen=True, slots=True)
class LedgerReconciliation:
    passed: bool
    reason_codes: tuple[str, ...]


def reconcile_cycle_ledger(
    ledger: CycleLedger,
    *,
    broker_position: int,
    broker_sellable_quantity: int,
    known_execution_ids: tuple[str, ...],
) -> LedgerReconciliation:
    reasons: list[str] = []
    if broker_position != ledger.q_current:
        reasons.append("BROKER_POSITION_MISMATCH")
    if broker_sellable_quantity < 0 or broker_sellable_quantity > broker_position:
        reasons.append("BROKER_SELLABLE_QUANTITY_INVALID")
    if broker_sellable_quantity < ledger.tactical_eligible_qty:
        reasons.append("BROKER_SELLABLE_BELOW_TACTICAL_ELIGIBLE")
    local_ids = {cohort.sell_execution_id for cohort in ledger.restore_cohorts}
    if not local_ids.issubset(set(known_execution_ids)):
        reasons.append("LOCAL_EXECUTION_NOT_IN_BROKER_HISTORY")
    return LedgerReconciliation(not reasons, tuple(reasons))


__all__ = [
    "BuybackRealization",
    "CycleLedger",
    "EntrySizingDecision",
    "EntrySizingInput",
    "HoldingLot",
    "LedgerReconciliation",
    "PartialPrefixEdgeDecision",
    "RestoreCohort",
    "TacticalAdaptationDecision",
    "TacticalPairObservation",
    "assess_tactical_adaptation",
    "check_every_partial_buyback_prefix",
    "floor_to_increment",
    "reconcile_cycle_ledger",
    "size_v3_strategic_entry",
]
