from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from itertools import groupby
from operator import attrgetter
from typing import Mapping, Protocol

from chanlun.decision_support.trading_system.backtest.execution import (
    ExecutionPolicy,
    FillDecision,
    OrderIntent,
    fees_for,
    liquidity_slippage,
    try_fill,
)
from chanlun.decision_support.trading_system.backtest.models import (
    BacktestDataset,
    CorporateActionAt,
    MinuteBar,
    SecurityStatus,
)
from chanlun.decision_support.trading_system.engine import (
    EvaluatedSignal,
    SymbolStructureBundle,
)
from chanlun.decision_support.trading_system.models import (
    PointType,
    StructureTower,
)
from chanlun.decision_support.trading_system.portfolio_risk import (
    PortfolioSnapshot,
    RiskCandidate,
    RiskLimits,
    RiskSizedOrder,
    size_entry,
)


_ZERO = Decimal("0")
_ONE = Decimal("1")


def _analysis_price_to_raw(value: Decimal, bar: MinuteBar) -> Decimal:
    """Map a structural price into the execution epoch of this raw bar."""

    return value * bar.raw_close / bar.analysis_close


class StructureReplay(Protocol):
    def bundle_at(
        self,
        *,
        dataset: BacktestDataset,
        closed_at: datetime,
        code: str,
    ) -> SymbolStructureBundle: ...


class DecisionEvaluator(Protocol):
    """Structural interface shared by live-page and historical evaluators."""

    def evaluate_symbol(
        self,
        bundle: SymbolStructureBundle,
    ) -> tuple[EvaluatedSignal, ...]: ...


class CausalBundleBuilder(Protocol):
    def build_bundle(
        self,
        *,
        code: str,
        closed_at: datetime,
        bars: tuple[MinuteBar, ...],
    ) -> SymbolStructureBundle: ...


class CausalStructureReplay:
    def __init__(self, builder: CausalBundleBuilder) -> None:
        self._builder = builder

    def bundle_at(
        self,
        *,
        dataset: BacktestDataset,
        closed_at: datetime,
        code: str,
    ) -> SymbolStructureBundle:
        bars = tuple(
            sorted(
                (
                    bar
                    for bar in dataset.bars
                    if bar.code == code and bar.closed_at <= closed_at
                ),
                key=attrgetter("closed_at"),
            )
        )
        if not bars:
            raise ValueError(f"no causal bars available for {code} at {closed_at}")
        return self._builder.build_bundle(
            code=code,
            closed_at=closed_at,
            bars=bars,
        )


@dataclass(frozen=True, slots=True)
class BacktestTrade:
    code: str
    sector_id: str
    point_type: PointType
    entry_at: datetime
    exit_at: datetime
    shares: int
    entry_price: Decimal
    exit_price: Decimal
    exit_trigger_price: Decimal
    exit_reason: str
    net_pnl: Decimal
    net_return: Decimal
    total_cost: Decimal


@dataclass(frozen=True, slots=True)
class EquityPoint:
    closed_at: datetime
    cash: Decimal
    market_value: Decimal
    equity: Decimal
    open_risk_cash: Decimal


@dataclass(frozen=True, slots=True)
class OpenPosition:
    code: str
    sector_id: str
    point_type: PointType
    shares: int
    opened_at: datetime
    entry_price: Decimal
    structural_stop: Decimal
    last_price: Decimal


@dataclass(frozen=True, slots=True)
class PendingExit:
    code: str
    created_at: datetime
    shares: int
    reason: str


@dataclass(frozen=True, slots=True)
class BacktestRun:
    fills: tuple[FillDecision, ...]
    trades: tuple[BacktestTrade, ...]
    equity_curve: tuple[EquityPoint, ...]
    open_positions: tuple[OpenPosition, ...]
    pending_exits: tuple[PendingExit, ...]


@dataclass(slots=True)
class _PositionState:
    code: str
    sector_id: str
    point_type: PointType
    tower: StructureTower
    recursive_level: int
    shares: int
    opened_at: datetime
    entry_price: Decimal
    structural_stop: Decimal
    last_price: Decimal
    entry_fees: Decimal

    def public(self) -> OpenPosition:
        return OpenPosition(
            code=self.code,
            sector_id=self.sector_id,
            point_type=self.point_type,
            shares=self.shares,
            opened_at=self.opened_at,
            entry_price=self.entry_price,
            structural_stop=self.structural_stop,
            last_price=self.last_price,
        )


@dataclass(slots=True)
class _PendingOrder:
    intent: OrderIntent
    sector_id: str
    point_type: PointType
    tower: StructureTower
    recursive_level: int
    candidate: RiskCandidate | None
    planned_risk_cash: Decimal
    reference_price: Decimal
    reason: str
    exit_trigger_price: Decimal | None = None
    last_rejection_reason: str | None = None
    exit_filled_shares: int = 0
    exit_value: Decimal = _ZERO
    exit_fees: Decimal = _ZERO
    exit_entry_fees: Decimal = _ZERO
    exit_last_filled_at: datetime | None = None


@dataclass(slots=True)
class _PortfolioState:
    cash: Decimal
    peak_equity: Decimal
    positions_by_code: dict[str, _PositionState]
    pending_orders: list[_PendingOrder]
    fills: list[FillDecision]
    trades: list[BacktestTrade]
    equity_curve: list[EquityPoint]
    last_prices: dict[str, Decimal]
    consumed_signal_ids: set[str]

    @classmethod
    def initial(cls, initial_cash: Decimal) -> _PortfolioState:
        if initial_cash <= 0:
            raise ValueError("initial_cash must be positive")
        return cls(
            cash=initial_cash,
            peak_equity=initial_cash,
            positions_by_code={},
            pending_orders=[],
            fills=[],
            trades=[],
            equity_curve=[],
            last_prices={},
            consumed_signal_ids=set(),
        )

    def apply_corporate_actions(
        self,
        actions: tuple[CorporateActionAt, ...],
    ) -> None:
        for action in actions:
            position = self.positions_by_code.get(action.code)
            if position is None:
                continue
            old_shares = position.shares
            cash_delta = (
                action.cash_per_share - action.subscription_cost_per_share
            ) * Decimal(old_shares)
            if cash_delta:
                self.cash += cash_delta
                if self.cash < 0:
                    raise ValueError("corporate action subscription exceeds cash")
            if action.share_multiplier != _ONE:
                new_shares = int(
                    Decimal(old_shares) * action.share_multiplier
                )
                if new_shares <= 0:
                    raise ValueError("corporate action removed all position shares")
                position.shares = new_shares
                position.entry_price /= action.share_multiplier
            position.structural_stop /= action.raw_price_divisor
            position.last_price /= action.raw_price_divisor
            self.last_prices[action.code] = position.last_price
            for pending in self.pending_orders:
                if pending.intent.code != action.code:
                    continue
                shares = pending.intent.shares
                if pending.intent.side == "sell":
                    shares = min(
                        position.shares,
                        max(
                            1,
                            int(Decimal(shares) * action.share_multiplier),
                        ),
                    )
                stop = pending.intent.structural_stop
                pending.intent = replace(
                    pending.intent,
                    shares=shares,
                    structural_stop=(
                        None
                        if stop is None
                        else stop / action.raw_price_divisor
                    ),
                )
                pending.reference_price /= action.raw_price_divisor
                if pending.exit_trigger_price is not None:
                    pending.exit_trigger_price /= action.raw_price_divisor
                if pending.candidate is not None:
                    pending.candidate = replace(
                        pending.candidate,
                        entry_price=(
                            pending.candidate.entry_price
                            / action.raw_price_divisor
                        ),
                        stop_price=(
                            pending.candidate.stop_price
                            / action.raw_price_divisor
                        ),
                    )

    def _has_pending(self, code: str, side: str) -> bool:
        return any(
            row.intent.code == code and row.intent.side == side
            for row in self.pending_orders
        )

    def _is_sellable(
        self,
        position: _PositionState,
        status: SecurityStatus,
    ) -> bool:
        return status.t_plus_days == 0 or status.session > position.opened_at.date()

    def _snapshot(
        self,
        *,
        exclude_order_id: str | None = None,
    ) -> PortfolioSnapshot:
        market_value = sum(
            position.last_price * Decimal(position.shares)
            for position in self.positions_by_code.values()
        )
        equity = self.cash + market_value
        if equity <= 0:
            raise ValueError("portfolio equity is not positive")
        drawdown = (
            _ZERO
            if self.peak_equity <= 0 or equity >= self.peak_equity
            else (self.peak_equity - equity) / self.peak_equity
        )
        sector_values: dict[str, Decimal] = {}
        open_risk = _ZERO
        reserved_cash = _ZERO
        for position in self.positions_by_code.values():
            sector_values[position.sector_id] = sector_values.get(
                position.sector_id,
                _ZERO,
            ) + position.last_price * Decimal(position.shares)
            open_risk += max(
                _ZERO,
                (position.last_price - position.structural_stop)
                * Decimal(position.shares),
            )
        for pending in self.pending_orders:
            if (
                pending.intent.order_id == exclude_order_id
                or pending.intent.side != "buy"
            ):
                continue
            reserved_cash += pending.reference_price * Decimal(
                pending.intent.shares
            )
            open_risk += pending.planned_risk_cash
            sector_values[pending.sector_id] = sector_values.get(
                pending.sector_id,
                _ZERO,
            ) + pending.reference_price * Decimal(pending.intent.shares)
        return PortfolioSnapshot(
            equity=equity,
            available_cash=max(_ZERO, self.cash - reserved_cash),
            drawdown=drawdown,
            open_risk_cash=open_risk,
            sector_market_values=tuple(sorted(sector_values.items())),
        )

    def snapshot(self) -> PortfolioSnapshot:
        return self._snapshot()

    def held_structures(self) -> dict[str, tuple[StructureTower, int]]:
        return {
            code: (position.tower, position.recursive_level)
            for code, position in self.positions_by_code.items()
        }

    def enqueue_entry(
        self,
        sized: RiskSizedOrder,
        *,
        evaluated: EvaluatedSignal,
        bar: MinuteBar,
        created_at: datetime,
    ) -> None:
        entry = evaluated.entry
        self.consumed_signal_ids.add(sized.signal_id)
        if (
            entry is None
            or entry.structural_stop is None
            or sized.shares <= 0
            or bar.code in self.positions_by_code
            or self._has_pending(bar.code, "buy")
        ):
            return
        point_type = evaluated.setup.point.point_type
        tower = evaluated.setup.point.tower
        recursive_level = evaluated.setup.point.recursive_level
        sector_id = evaluated.setup.sector.sector_id
        raw_stop = _analysis_price_to_raw(entry.structural_stop, bar)
        candidate = RiskCandidate(
            signal_id=entry.signal_id,
            sector_id=sector_id,
            entry_price=bar.raw_close,
            stop_price=raw_stop,
            risk_multiplier=entry.risk_multiplier,
        )
        intent = OrderIntent(
            order_id=f"entry:{entry.signal_id}:{created_at.isoformat()}",
            signal_id=entry.signal_id,
            code=bar.code,
            side="buy",
            shares=sized.shares,
            created_at=created_at,
            structural_stop=raw_stop,
        )
        self.pending_orders.append(
            _PendingOrder(
                intent=intent,
                sector_id=sector_id,
                point_type=point_type,
                tower=tower,
                recursive_level=recursive_level,
                candidate=candidate,
                planned_risk_cash=sized.planned_risk_cash,
                reference_price=bar.raw_close,
                reason="entry_signal",
            )
        )

    def _enqueue_exit(
        self,
        *,
        position: _PositionState,
        shares: int,
        signal_id: str,
        created_at: datetime,
        reason: str,
        trigger_price: Decimal,
        last_rejection_reason: str | None = None,
    ) -> None:
        if shares <= 0 or self._has_pending(position.code, "sell"):
            return
        intent = OrderIntent(
            order_id=f"exit:{signal_id}:{created_at.isoformat()}",
            signal_id=signal_id,
            code=position.code,
            side="sell",
            shares=min(shares, position.shares),
            created_at=created_at,
            structural_stop=position.structural_stop,
        )
        self.pending_orders.append(
            _PendingOrder(
                intent=intent,
                sector_id=position.sector_id,
                point_type=position.point_type,
                tower=position.tower,
                recursive_level=position.recursive_level,
                candidate=None,
                planned_risk_cash=_ZERO,
                reference_price=position.last_price,
                reason=reason,
                exit_trigger_price=trigger_price,
                last_rejection_reason=last_rejection_reason,
            )
        )

    def enqueue_structural_exit(
        self,
        evaluated: EvaluatedSignal,
        *,
        code: str,
        created_at: datetime,
        lot_size: int,
    ) -> None:
        exit_decision = evaluated.exit
        if exit_decision is None:
            return
        self.consumed_signal_ids.add(exit_decision.signal_id)
        position = self.positions_by_code.get(code)
        if position is None or self._has_pending(code, "sell"):
            return
        if exit_decision.action == "reduce_tactical":
            shares = max(
                lot_size,
                (position.shares // 2) // lot_size * lot_size,
            )
        else:
            shares = position.shares
        self._enqueue_exit(
            position=position,
            shares=shares,
            signal_id=exit_decision.signal_id,
            created_at=created_at,
            reason=f"signal_{exit_decision.action}",
            trigger_price=position.last_price,
        )

    def _close_position(
        self,
        pending: _PendingOrder,
        fill: FillDecision,
        *,
        exit_reason: str,
        exit_trigger_price: Decimal,
        finalize: bool = True,
    ) -> None:
        position = self.positions_by_code[pending.intent.code]
        if fill.execution_price is None or fill.filled_at is None:
            raise ValueError("filled exit is missing execution details")
        shares = min(fill.shares, position.shares)
        entry_fee_share = (
            position.entry_fees * Decimal(shares) / Decimal(position.shares)
        )
        exit_value = fill.execution_price * Decimal(shares)
        self.cash += exit_value - fill.fees
        pending.exit_filled_shares += shares
        pending.exit_value += exit_value
        pending.exit_fees += fill.fees
        pending.exit_entry_fees += entry_fee_share
        pending.exit_last_filled_at = fill.filled_at
        if finalize:
            total_shares = pending.exit_filled_shares
            entry_value = position.entry_price * Decimal(total_shares)
            entry_cost = entry_value + pending.exit_entry_fees
            net_pnl = (
                pending.exit_value
                - pending.exit_fees
                - entry_value
                - pending.exit_entry_fees
            )
            if pending.exit_last_filled_at is None:
                raise ValueError("completed exit has no fill timestamp")
            self.trades.append(
                BacktestTrade(
                    code=position.code,
                    sector_id=position.sector_id,
                    point_type=position.point_type,
                    entry_at=position.opened_at,
                    exit_at=pending.exit_last_filled_at,
                    shares=total_shares,
                    entry_price=position.entry_price,
                    exit_price=pending.exit_value / Decimal(total_shares),
                    exit_trigger_price=exit_trigger_price,
                    exit_reason=exit_reason,
                    net_pnl=net_pnl,
                    net_return=net_pnl / entry_cost,
                    total_cost=pending.exit_entry_fees + pending.exit_fees,
                )
            )
        if shares == position.shares:
            del self.positions_by_code[position.code]
        else:
            position.shares -= shares
            position.entry_fees -= entry_fee_share

    def _resize_entry_at_fill(
        self,
        pending: _PendingOrder,
        decision: FillDecision,
        status: SecurityStatus,
        risk_limits: RiskLimits,
        bar: MinuteBar,
        execution_policy: ExecutionPolicy,
        *,
        maximum_shares: int,
    ) -> tuple[OrderIntent, FillDecision] | None:
        if decision.execution_price is None or pending.candidate is None:
            raise ValueError("entry fill lacks candidate or price")
        candidate = replace(
            pending.candidate,
            entry_price=decision.execution_price,
        )
        sized = size_entry(
            portfolio=self._snapshot(exclude_order_id=pending.intent.order_id),
            candidate=candidate,
            limits=risk_limits,
        )
        shares = min(pending.intent.shares, sized.shares, maximum_shares)
        shares = shares // status.lot_size * status.lot_size
        while shares > 0:
            intent = replace(pending.intent, shares=shares)
            resized = try_fill(intent, bar, status, execution_policy)
            if not resized.filled or resized.execution_price is None:
                return None
            required_cash = resized.execution_price * Decimal(shares) + resized.fees
            if required_cash <= self.cash:
                return intent, resized
            shares -= status.lot_size
        return None

    def try_pending_orders(
        self,
        bar: MinuteBar,
        status: SecurityStatus,
        execution_policy: ExecutionPolicy,
        risk_limits: RiskLimits,
    ) -> None:
        for pending in tuple(self.pending_orders):
            if pending.intent.code != bar.code:
                continue
            if (
                pending.intent.side == "buy"
                and bar.closed_at
                >= pending.intent.created_at
                + timedelta(seconds=execution_policy.entry_risk_ttl_seconds)
            ):
                self.fills.append(
                    FillDecision.rejected(
                        pending.intent,
                        "risk_snapshot_expired",
                    )
                )
                self.pending_orders.remove(pending)
                continue
            if pending.intent.side == "sell":
                position = self.positions_by_code.get(bar.code)
                if position is None:
                    self.pending_orders.remove(pending)
                    continue
                if not self._is_sellable(position, status):
                    pending.last_rejection_reason = "t_plus_one_locked"
                    self.fills.append(
                        FillDecision.rejected(
                            pending.intent,
                            pending.last_rejection_reason,
                        )
                    )
                    continue
            attempted_intent = pending.intent
            if bar.volume > 0:
                capacity = int(
                    bar.volume * execution_policy.max_volume_participation
                )
                capacity = capacity // status.lot_size * status.lot_size
                if 0 < capacity < pending.intent.shares:
                    attempted_intent = replace(
                        pending.intent,
                        shares=capacity,
                    )
            decision = try_fill(
                attempted_intent,
                bar,
                status,
                execution_policy,
            )
            if not decision.filled:
                pending.last_rejection_reason = decision.reason
                self.fills.append(decision)
                if decision.reason in {
                    "security_mismatch",
                    "status_session_mismatch",
                    "not_listed",
                    "lot_size_mismatch",
                    "fee_schedule_unavailable",
                }:
                    self.pending_orders.remove(pending)
                continue
            if pending.intent.side == "buy":
                resized = self._resize_entry_at_fill(
                    pending,
                    decision,
                    status,
                    risk_limits,
                    bar,
                    execution_policy,
                    maximum_shares=attempted_intent.shares,
                )
                if resized is None:
                    self.fills.append(
                        FillDecision.rejected(
                            pending.intent,
                            "risk_revalidation_blocked",
                        )
                    )
                    self.pending_orders.remove(pending)
                    continue
                intent, decision = resized
                if decision.execution_price is None or decision.filled_at is None:
                    raise ValueError("entry fill lacks execution details")
                self.cash -= (
                    decision.execution_price * Decimal(intent.shares)
                    + decision.fees
                )
                self.positions_by_code[intent.code] = _PositionState(
                    code=intent.code,
                    sector_id=pending.sector_id,
                    point_type=pending.point_type,
                    tower=pending.tower,
                    recursive_level=pending.recursive_level,
                    shares=intent.shares,
                    opened_at=decision.filled_at,
                    entry_price=decision.execution_price,
                    structural_stop=intent.structural_stop
                    if intent.structural_stop is not None
                    else decision.execution_price,
                    last_price=decision.execution_price,
                    entry_fees=decision.fees,
                )
                self.fills.append(decision)
            else:
                trigger = pending.exit_trigger_price or (
                    decision.execution_price or pending.reference_price
                )
                self.fills.append(decision)
                complete = decision.shares >= pending.intent.shares
                self._close_position(
                    pending,
                    decision,
                    exit_reason=pending.reason,
                    exit_trigger_price=trigger,
                    finalize=complete,
                )
                if not complete:
                    pending.intent = replace(
                        pending.intent,
                        shares=pending.intent.shares - decision.shares,
                    )
                    pending.last_rejection_reason = None
                    continue
            self.pending_orders.remove(pending)

    def mark_to_market(self, bar: MinuteBar) -> None:
        self.last_prices[bar.code] = bar.raw_close
        position = self.positions_by_code.get(bar.code)
        if position is not None:
            position.last_price = bar.raw_close

    def _stop_order(
        self,
        position: _PositionState,
        bar: MinuteBar,
    ) -> OrderIntent:
        return OrderIntent(
            order_id=f"stop:{position.code}:{bar.closed_at.isoformat()}",
            signal_id=f"stop:{position.code}:{position.opened_at.isoformat()}",
            code=position.code,
            side="sell",
            shares=position.shares,
            created_at=bar.closed_at,
            structural_stop=position.structural_stop,
        )

    def check_intrabar_structural_stops(
        self,
        bar: MinuteBar,
        status: SecurityStatus,
        execution_policy: ExecutionPolicy,
    ) -> None:
        position = self.positions_by_code.get(bar.code)
        if (
            position is None
        # 以收盘时间记入的开仓，不能被同一分钟内可能更早出现的最低价触发止损。
            or position.opened_at >= bar.closed_at
            or bar.raw_low > position.structural_stop
            or self._has_pending(bar.code, "sell")
        ):
            return
        stop_order = self._stop_order(position, bar)
        if not self._is_sellable(position, status):
            self._enqueue_exit(
                position=position,
                shares=position.shares,
                signal_id=stop_order.signal_id,
                created_at=bar.closed_at,
                reason="structural_stop",
                trigger_price=position.structural_stop,
                last_rejection_reason="t_plus_one_locked",
            )
            self.fills.append(
                FillDecision.rejected(stop_order, "t_plus_one_locked")
            )
            return
        limit_down = _round_price(
            bar.previous_raw_close * (_ONE - status.limit_pct),
            execution_policy.price_tick,
        )
        if status.suspended or bar.volume <= 0:
            reason = "not_tradable"
        elif bar.raw_high <= limit_down:
            reason = "limit_down_locked"
        elif (
            execution_policy.require_observed_price_range
            and bar.raw_high == bar.raw_low
        ):
            reason = "one_price_bar_unfillable"
        elif Decimal(position.shares) > (
            bar.volume * execution_policy.max_volume_participation
        ):
            reason = "volume_capacity_exceeded"
        else:
            reason = ""
        if reason:
            self._enqueue_exit(
                position=position,
                shares=position.shares,
                signal_id=stop_order.signal_id,
                created_at=bar.closed_at,
                reason="structural_stop",
                trigger_price=position.structural_stop,
                last_rejection_reason=reason,
            )
            self.fills.append(FillDecision.rejected(stop_order, reason))
            return

        reference = (
            bar.raw_open
            if bar.raw_open < position.structural_stop
            else position.structural_stop
        )
        slippage = liquidity_slippage(
            replace(stop_order, created_at=bar.opened_at),
            bar,
            execution_policy,
        )
        execution_price = _round_price(
            max(
                reference * (_ONE - slippage),
                bar.raw_low,
                limit_down,
            ),
            execution_policy.price_tick,
        )
        fees = fees_for(
            stop_order,
            execution_price,
            status,
            bar.opened_at.date(),
            execution_policy,
        )
        fill = FillDecision(
            order_id=stop_order.order_id,
            filled=True,
            reason="filled",
            filled_at=bar.closed_at,
            execution_price=execution_price,
            shares=position.shares,
            fees=fees,
        )
        direct = _PendingOrder(
            intent=stop_order,
            sector_id=position.sector_id,
            point_type=position.point_type,
            tower=position.tower,
            recursive_level=position.recursive_level,
            candidate=None,
            planned_risk_cash=_ZERO,
            reference_price=reference,
            reason="structural_stop",
            exit_trigger_price=position.structural_stop,
        )
        self.fills.append(fill)
        self._close_position(
            direct,
            fill,
            exit_reason="structural_stop",
            exit_trigger_price=position.structural_stop,
        )

    def record_equity(self, closed_at: datetime) -> None:
        snapshot = self.snapshot()
        market_value = snapshot.equity - self.cash
        point = EquityPoint(
            closed_at=closed_at,
            cash=self.cash,
            market_value=market_value,
            equity=snapshot.equity,
            open_risk_cash=snapshot.open_risk_cash,
        )
        if self.equity_curve and self.equity_curve[-1].closed_at == closed_at:
            self.equity_curve[-1] = point
        else:
            self.equity_curve.append(point)
        self.peak_equity = max(self.peak_equity, point.equity)

    def force_terminal_liquidation(
        self,
        *,
        bars_by_code: dict[str, MinuteBar],
        dataset: BacktestDataset,
        execution_policy: ExecutionPolicy,
    ) -> None:
        for code in sorted(tuple(self.positions_by_code)):
            position = self.positions_by_code[code]
            bar = bars_by_code[code]
            status = dataset.status_at(code, bar.opened_at.date())
            order = OrderIntent(
                order_id=f"terminal:{code}:{bar.closed_at.isoformat()}",
                signal_id=f"terminal:{code}",
                code=code,
                side="sell",
                shares=position.shares,
                created_at=bar.closed_at,
                structural_stop=position.structural_stop,
            )
            slippage = liquidity_slippage(
                replace(order, created_at=bar.opened_at),
                bar,
                execution_policy,
            )
            price = _round_price(
                max(
                    bar.raw_close * (_ONE - slippage),
                    bar.raw_low,
                ),
                execution_policy.price_tick,
            )
            fees = fees_for(
                order,
                price,
                status,
                bar.opened_at.date(),
                execution_policy,
            )
            fill = FillDecision(
                order_id=order.order_id,
                filled=True,
                reason="filled",
                filled_at=bar.closed_at,
                execution_price=price,
                shares=position.shares,
                fees=fees,
            )
            pending = _PendingOrder(
                intent=order,
                sector_id=position.sector_id,
                point_type=position.point_type,
                tower=position.tower,
                recursive_level=position.recursive_level,
                candidate=None,
                planned_risk_cash=_ZERO,
                reference_price=bar.raw_close,
                reason="forced_liquidation_sensitivity",
                exit_trigger_price=bar.raw_close,
            )
            self.fills.append(fill)
            self._close_position(
                pending,
                fill,
                exit_reason="forced_liquidation_sensitivity",
                exit_trigger_price=bar.raw_close,
            )

    def write_off_position(
        self,
        *,
        code: str,
        closed_at: datetime,
        reason: str,
    ) -> None:
        """Remove an expired security without inventing a stale-price sale."""

        position = self.positions_by_code[code]
        order = OrderIntent(
            order_id=f"writeoff:{code}:{closed_at.isoformat()}",
            signal_id=f"writeoff:{code}",
            code=code,
            side="sell",
            shares=position.shares,
            created_at=closed_at,
            structural_stop=position.structural_stop,
        )
        fill = FillDecision(
            order_id=order.order_id,
            filled=True,
            reason=reason,
            filled_at=closed_at,
            execution_price=_ZERO,
            shares=position.shares,
            fees=_ZERO,
        )
        pending = _PendingOrder(
            intent=order,
            sector_id=position.sector_id,
            point_type=position.point_type,
            tower=position.tower,
            recursive_level=position.recursive_level,
            candidate=None,
            planned_risk_cash=_ZERO,
            reference_price=position.last_price,
            reason=reason,
            exit_trigger_price=_ZERO,
        )
        self.pending_orders = [
            row for row in self.pending_orders if row.intent.code != code
        ]
        self.last_prices[code] = _ZERO
        self.fills.append(fill)
        self._close_position(
            pending,
            fill,
            exit_reason=reason,
            exit_trigger_price=_ZERO,
        )

    def finish(self) -> BacktestRun:
        open_positions = tuple(
            self.positions_by_code[code].public()
            for code in sorted(self.positions_by_code)
        )
        pending_exits = tuple(
            PendingExit(
                code=row.intent.code,
                created_at=row.intent.created_at,
                shares=row.intent.shares,
                reason=row.last_rejection_reason or row.reason,
            )
            for row in sorted(
                (
                    item
                    for item in self.pending_orders
                    if item.intent.side == "sell"
                ),
                key=lambda item: (item.intent.code, item.intent.created_at),
            )
        )
        return BacktestRun(
            fills=tuple(self.fills),
            trades=tuple(self.trades),
            equity_curve=tuple(self.equity_curve),
            open_positions=open_positions,
            pending_exits=pending_exits,
        )


def _round_price(value: Decimal, tick: Decimal) -> Decimal:
    units = (value / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return units * tick


def risk_candidate_from(
    evaluated: EvaluatedSignal,
    bar: MinuteBar,
) -> RiskCandidate:
    entry = evaluated.entry
    if entry is None or entry.structural_stop is None:
        raise ValueError("allowed entry requires a structural stop")
    return RiskCandidate(
        signal_id=entry.signal_id,
        sector_id=evaluated.setup.sector.sector_id,
        entry_price=bar.raw_close,
        stop_price=_analysis_price_to_raw(entry.structural_stop, bar),
        risk_multiplier=entry.risk_multiplier,
    )


def replay_engine_decisions(
    dataset: BacktestDataset,
    *,
    engine: DecisionEvaluator,
    structure_replay: StructureReplay,
    closed_at: datetime,
    held_structures: Mapping[str, tuple[StructureTower, int]] | None = None,
) -> tuple[tuple[str, tuple[EvaluatedSignal, ...]], ...]:
    codes = sorted(
        {bar.code for bar in dataset.bars if bar.closed_at == closed_at}
    )
    positions = held_structures or {}
    output: list[tuple[str, tuple[EvaluatedSignal, ...]]] = []
    for code in codes:
        bundle = structure_replay.bundle_at(
            dataset=dataset,
            closed_at=closed_at,
            code=code,
        )
        held = positions.get(code)
        if held is not None and isinstance(bundle, SymbolStructureBundle):
            bundle = replace(bundle, held_tower=held[0], held_level=held[1])
        output.append((code, engine.evaluate_symbol(bundle)))
    return tuple(output)


def run_event_backtest(
    dataset: BacktestDataset,
    *,
    engine: DecisionEvaluator,
    structure_replay: StructureReplay,
    risk_limits: RiskLimits,
    execution_policy: ExecutionPolicy,
    initial_cash: Decimal,
    terminal_liquidation: bool = False,
) -> BacktestRun:
    state = _PortfolioState.initial(initial_cash)
    if not dataset.bars:
        raise ValueError("backtest dataset has no bars")
    ordered = sorted(dataset.bars, key=lambda item: (item.closed_at, item.code))
    last_bars_by_code: dict[str, MinuteBar] = {}
    for closed_at, timestamp_rows in groupby(
        ordered,
        key=attrgetter("closed_at"),
    ):
        bars = tuple(timestamp_rows)
        state.apply_corporate_actions(dataset.actions_at(closed_at))

        for bar in bars:
            status = dataset.status_at(bar.code, bar.opened_at.date())
            state.try_pending_orders(
                bar,
                status,
                execution_policy,
                risk_limits,
            )
            state.mark_to_market(bar)
            state.check_intrabar_structural_stops(
                bar,
                status,
                execution_policy,
            )
            last_bars_by_code[bar.code] = bar

        decisions = replay_engine_decisions(
            dataset,
            engine=engine,
            structure_replay=structure_replay,
            closed_at=closed_at,
            held_structures=state.held_structures(),
        )
        bars_by_code = {bar.code: bar for bar in bars}
        for code, evaluations in decisions:
            bar = bars_by_code[code]
            status = dataset.status_at(code, bar.opened_at.date())
            for evaluated in evaluations:
                if (
                    evaluated.entry is not None
                    and evaluated.entry.allowed
                    and evaluated.entry.signal_id
                    not in state.consumed_signal_ids
                ):
                    candidate = risk_candidate_from(evaluated, bar)
                    sized = size_entry(
                        portfolio=state.snapshot(),
                        candidate=candidate,
                        limits=risk_limits,
                    )
                    state.enqueue_entry(
                        sized,
                        evaluated=evaluated,
                        bar=bar,
                        created_at=closed_at,
                    )
                if (
                    evaluated.exit is not None
                    and evaluated.exit.allowed
                    and evaluated.exit.signal_id
                    not in state.consumed_signal_ids
                ):
                    state.enqueue_structural_exit(
                        evaluated,
                        code=code,
                        created_at=closed_at,
                        lot_size=status.lot_size,
                    )
        state.record_equity(closed_at)

    if terminal_liquidation:
        state.force_terminal_liquidation(
            bars_by_code=last_bars_by_code,
            dataset=dataset,
            execution_policy=execution_policy,
        )
        state.record_equity(dataset.last_closed_at)
    return state.finish()


__all__ = [
    "BacktestRun",
    "BacktestTrade",
    "CausalStructureReplay",
    "EquityPoint",
    "OpenPosition",
    "PendingExit",
    "StructureReplay",
    "replay_engine_decisions",
    "risk_candidate_from",
    "run_event_backtest",
]
