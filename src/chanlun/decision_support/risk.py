from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_FLOOR

from chanlun.recursive_bt.engine.engine import BUYS, recommended_buy_ratio

from .fingerprints import normalize_datetime
from .market_rules import a_share_board, a_share_limit_pct
from .models import DecisionEvent


def _require_decimal(value: object, field_name: str) -> Decimal:
    if not isinstance(value, Decimal):
        raise ValueError(f"{field_name} must be Decimal")
    if not value.is_finite():
        raise ValueError(f"{field_name} must be finite")
    return value


def _require_positive_decimal(value: object, field_name: str) -> Decimal:
    decimal_value = _require_decimal(value, field_name)
    if decimal_value <= 0:
        raise ValueError(f"{field_name} must be positive")
    return decimal_value


def _require_non_negative_decimal(
    value: object,
    field_name: str,
) -> Decimal:
    decimal_value = _require_decimal(value, field_name)
    if decimal_value < 0:
        raise ValueError(f"{field_name} must be non-negative")
    return decimal_value


def _require_positive_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _require_non_negative_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return value


def _require_bool(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} must be boolean")
    return value


def _require_non_empty_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


@dataclass(frozen=True, slots=True)
class QuoteSnapshot:
    code: str
    price: Decimal
    quote_time: datetime
    entry_tradable: bool
    exit_tradable: bool
    limit_up_locked: bool
    limit_down_locked: bool

    def __post_init__(self) -> None:
        _require_non_empty_string(self.code, "code")
        _require_positive_decimal(self.price, "price")
        object.__setattr__(
            self,
            "quote_time",
            normalize_datetime(self.quote_time, "quote_time"),
        )
        for field_name in (
            "entry_tradable",
            "exit_tradable",
            "limit_up_locked",
            "limit_down_locked",
        ):
            _require_bool(getattr(self, field_name), field_name)


@dataclass(frozen=True, slots=True)
class HoldingSnapshot:
    code: str
    shares: int
    sellable_shares: int
    opened_at: datetime
    average_price: Decimal

    def __post_init__(self) -> None:
        _require_non_empty_string(self.code, "code")
        shares = _require_positive_int(self.shares, "shares")
        sellable_shares = _require_non_negative_int(
            self.sellable_shares,
            "sellable_shares",
        )
        if sellable_shares > shares:
            raise ValueError("sellable_shares cannot exceed shares")
        object.__setattr__(
            self,
            "opened_at",
            normalize_datetime(self.opened_at, "opened_at"),
        )
        _require_positive_decimal(self.average_price, "average_price")


@dataclass(frozen=True, slots=True)
class PendingExitSnapshot:
    code: str
    shares: int
    reason: str
    blocked_by_t1: bool
    blocked_by_limit: bool

    def __post_init__(self) -> None:
        _require_non_empty_string(self.code, "code")
        _require_positive_int(self.shares, "shares")
        _require_non_empty_string(self.reason, "reason")
        _require_bool(self.blocked_by_t1, "blocked_by_t1")
        _require_bool(self.blocked_by_limit, "blocked_by_limit")


@dataclass(frozen=True, slots=True)
class RiskContext:
    account_equity: Decimal
    day_start_equity: Decimal
    available_cash: Decimal
    holdings: tuple[HoldingSnapshot, ...]
    pending_exits: tuple[PendingExitSnapshot, ...]
    day_pnl: Decimal
    strategy_drawdown: Decimal
    daily_loss_locked: bool
    drawdown_locked: bool
    quote: QuoteSnapshot
    asof: datetime

    def __post_init__(self) -> None:
        _require_positive_decimal(self.account_equity, "account_equity")
        _require_positive_decimal(self.day_start_equity, "day_start_equity")
        _require_non_negative_decimal(self.available_cash, "available_cash")
        _require_decimal(self.day_pnl, "day_pnl")
        drawdown = _require_non_negative_decimal(
            self.strategy_drawdown,
            "strategy_drawdown",
        )
        if drawdown > 1:
            raise ValueError("strategy_drawdown must not exceed one")
        _require_bool(self.daily_loss_locked, "daily_loss_locked")
        _require_bool(self.drawdown_locked, "drawdown_locked")

        holdings = tuple(self.holdings)
        if not all(isinstance(item, HoldingSnapshot) for item in holdings):
            raise ValueError("holdings must contain HoldingSnapshot values")
        if len({item.code for item in holdings}) != len(holdings):
            raise ValueError("holdings must contain unique codes")
        object.__setattr__(self, "holdings", holdings)

        pending_exits = tuple(self.pending_exits)
        if not all(
            isinstance(item, PendingExitSnapshot) for item in pending_exits
        ):
            raise ValueError(
                "pending_exits must contain PendingExitSnapshot values"
            )
        if len({item.code for item in pending_exits}) != len(pending_exits):
            raise ValueError("pending_exits must contain unique codes")
        holdings_by_code = {item.code: item for item in holdings}
        for pending in pending_exits:
            holding = holdings_by_code.get(pending.code)
            if holding is None or pending.shares > holding.shares:
                raise ValueError("pending exit must match an existing holding")
        object.__setattr__(self, "pending_exits", pending_exits)

        if not isinstance(self.quote, QuoteSnapshot):
            raise ValueError("quote must be QuoteSnapshot")
        asof = normalize_datetime(self.asof, "asof")
        object.__setattr__(self, "asof", asof)
        if self.quote.quote_time > asof:
            raise ValueError("quote_time cannot be after asof")
        if any(item.opened_at > asof for item in holdings):
            raise ValueError("holding opened_at cannot be after asof")


@dataclass(frozen=True, slots=True)
class RiskPolicy:
    trade_risk_fraction: Decimal
    max_positions: int
    daily_loss_fraction: Decimal
    max_drawdown_fraction: Decimal
    estimate_round_trip_fraction: Decimal
    max_quote_age_seconds: int = 300

    def __post_init__(self) -> None:
        for field_name in (
            "trade_risk_fraction",
            "daily_loss_fraction",
            "max_drawdown_fraction",
        ):
            fraction = _require_positive_decimal(
                getattr(self, field_name),
                field_name,
            )
            if fraction > 1:
                raise ValueError(f"{field_name} must not exceed one")
        _require_non_negative_decimal(
            self.estimate_round_trip_fraction,
            "estimate_round_trip_fraction",
        )
        _require_positive_int(self.max_positions, "max_positions")
        _require_positive_int(
            self.max_quote_age_seconds,
            "max_quote_age_seconds",
        )

    @classmethod
    def conservative(cls) -> RiskPolicy:
        return cls(
            trade_risk_fraction=Decimal("0.005"),
            max_positions=5,
            daily_loss_fraction=Decimal("0.01"),
            max_drawdown_fraction=Decimal("0.08"),
            estimate_round_trip_fraction=Decimal("0.0015"),
        )


@dataclass(frozen=True, slots=True)
class RiskDecision:
    allowed: bool
    shares: int
    planned_risk_cash: Decimal
    target_weight: Decimal
    entry_reference: Decimal
    reasons: tuple[str, ...]
    daily_loss_locked: bool
    drawdown_locked: bool
    evaluated_at: datetime


@dataclass(frozen=True, slots=True)
class ExitDecision:
    allowed: bool
    requested_shares: int
    executable_shares: int
    pending_shares: int
    reason: str
    reasons: tuple[str, ...]
    blocked_by_t1: bool
    blocked_by_limit: bool
    evaluated_at: datetime


def _market_metadata_reasons(event: DecisionEvent) -> list[str]:
    constraints = event.market_constraints
    reasons: list[str] = []
    if event.market != "a":
        reasons.append("unsupported_market")
    if constraints.t_plus != 1:
        reasons.append("invalid_t_plus_metadata")
    if constraints.lot != 100:
        reasons.append("invalid_lot_metadata")

    board = constraints.board.strip().casefold()
    expected_board = a_share_board(event.code, event.name)
    if expected_board == "main_st":
        board_matches = board in {"st", "main_st"}
    else:
        board_matches = board == expected_board
    expected_limit = a_share_limit_pct(board)
    actual_limit = constraints.limit_pct
    if (
        not board_matches
        or expected_limit is None
        or actual_limit is None
        or Decimal(str(actual_limit)) != expected_limit
    ):
        reasons.append("invalid_limit_metadata")
    return reasons


def _quote_reasons(
    event: DecisionEvent,
    context: RiskContext,
    policy: RiskPolicy,
) -> list[str]:
    reasons: list[str] = []
    if context.asof < event.observed_at:
        reasons.append("risk_context_before_event")
    if context.quote.code != event.code:
        reasons.append("quote_code_mismatch")
    event_quote = event.market_constraints.quote_time
    if context.quote.quote_time < event_quote:
        reasons.append("quote_precedes_event_quote")
    quote_age = context.asof - context.quote.quote_time
    if quote_age.total_seconds() > policy.max_quote_age_seconds:
        reasons.append("stale_quote")
    if context.quote.quote_time == event_quote:
        dynamic_pairs = (
            (
                context.quote.entry_tradable,
                event.market_constraints.entry_tradable,
            ),
            (
                context.quote.exit_tradable,
                event.market_constraints.exit_tradable,
            ),
            (
                context.quote.limit_up_locked,
                event.market_constraints.limit_up_locked,
            ),
            (
                context.quote.limit_down_locked,
                event.market_constraints.limit_down_locked,
            ),
        )
        if any(current != frozen for current, frozen in dynamic_pairs):
            reasons.append("quote_constraint_mismatch")
    return reasons


def _target_weight(event: DecisionEvent, policy: RiskPolicy) -> Decimal:
    big_direction: str
    mid_direction: str
    signal_levels = [
        level
        for level in event.levels
        if level.level == event.signal.level
        and level.frequency == event.signal_frequency
    ]
    if len(signal_levels) != 1:
        raise ValueError("signal level snapshot must be unique")
    if event.levels and all(
        level.source_frequency is not None for level in event.levels
    ):
        native = {
            level.source_frequency.casefold(): level
            for level in event.levels
            if level.level == 0
            and level.frequency.casefold()
            == level.source_frequency.casefold()
            and level.source_frequency.casefold() in {"5m", "30m"}
        }
        if set(native) == {"5m", "30m"}:
            big_direction = (
                native["30m"].trade_gate_direction
                or native["30m"].direction
            )
            mid_direction = (
                native["5m"].trade_gate_direction
                or native["5m"].direction
            )
        else:
            signal_source = signal_levels[0].source_frequency
            source_tree = tuple(
                level
                for level in event.levels
                if level.source_frequency == signal_source
            )
            highest_level = max(source_tree, key=lambda level: level.level)
            big_direction = highest_level.direction
            mid_direction = ""
    else:
        highest_level = max(event.levels, key=lambda level: level.level)
        big_direction = highest_level.direction
        mid_direction = ""
    ratio = recommended_buy_ratio(
        event.signal.bs_type,
        policy.max_positions,
        big_dir=big_direction,
        mid_dir=mid_direction or signal_levels[0].direction,
        nest_operable=event.signal.nest_operable,
        nest_depth=event.signal.nest_depth,
    )
    weight = Decimal(str(ratio))
    return weight


def _lot_floor(shares: Decimal, lot: int) -> int:
    lots = (shares / Decimal(lot)).to_integral_value(rounding=ROUND_FLOOR)
    return int(lots) * lot


def _risk_decision(
    *,
    context: RiskContext,
    target_weight: Decimal,
    reasons: tuple[str, ...],
    daily_loss_locked: bool,
    drawdown_locked: bool,
    shares: int = 0,
    planned_risk_cash: Decimal = Decimal("0"),
) -> RiskDecision:
    return RiskDecision(
        allowed=shares > 0 and not reasons,
        shares=shares,
        planned_risk_cash=planned_risk_cash,
        target_weight=target_weight,
        entry_reference=context.quote.price,
        reasons=reasons,
        daily_loss_locked=daily_loss_locked,
        drawdown_locked=drawdown_locked,
        evaluated_at=context.asof,
    )


def evaluate_entry(
    event: DecisionEvent,
    context: RiskContext,
    policy: RiskPolicy,
) -> RiskDecision:
    if not isinstance(event, DecisionEvent):
        raise TypeError("event must be DecisionEvent")
    if not isinstance(context, RiskContext):
        raise TypeError("context must be RiskContext")
    if not isinstance(policy, RiskPolicy):
        raise TypeError("policy must be RiskPolicy")

    target_weight = _target_weight(event, policy)
    quote = context.quote
    reasons = _quote_reasons(event, context, policy)
    reasons.extend(_market_metadata_reasons(event))
    if event.signal.bs_type not in BUYS:
        reasons.append("unsupported_entry_signal")
    if not quote.entry_tradable:
        reasons.append("entry_not_tradable")
    if quote.limit_up_locked:
        reasons.append("limit_up_locked")
    if any(item.code == event.code for item in context.holdings):
        reasons.append("existing_position")
    if len(context.holdings) >= policy.max_positions:
        reasons.append("max_positions")
    if context.pending_exits:
        reasons.append("pending_exit_lock")

    daily_loss_limit = context.day_start_equity * policy.daily_loss_fraction
    daily_loss_locked = (
        context.daily_loss_locked or context.day_pnl <= -daily_loss_limit
    )
    if daily_loss_locked:
        reasons.append("daily_loss_lock")
    drawdown_locked = (
        context.drawdown_locked
        or context.strategy_drawdown >= policy.max_drawdown_fraction
    )
    if drawdown_locked:
        reasons.append("strategy_drawdown_lock")

    stop_value = event.signal.structural_stop_below
    stop: Decimal | None = None
    if stop_value is None:
        reasons.append("missing_structural_stop")
    else:
        stop = Decimal(str(stop_value))
        if stop <= 0 or stop >= quote.price:
            reasons.append("invalid_structural_stop")

    if reasons:
        return _risk_decision(
            context=context,
            target_weight=target_weight,
            reasons=tuple(reasons),
            daily_loss_locked=daily_loss_locked,
            drawdown_locked=drawdown_locked,
        )

    if stop is None:
        raise RuntimeError("validated structural stop is unexpectedly missing")
    price = quote.price
    risk_per_share = (
        price - stop + price * policy.estimate_round_trip_fraction
    )
    risk_budget = context.account_equity * policy.trade_risk_fraction
    lot = event.market_constraints.lot
    risk_shares = _lot_floor(risk_budget / risk_per_share, lot)
    weight_shares = _lot_floor(
        context.account_equity * target_weight / price,
        lot,
    )
    cash_per_share = price * (
        Decimal("1") + policy.estimate_round_trip_fraction
    )
    cash_shares = _lot_floor(context.available_cash / cash_per_share, lot)
    shares = min(risk_shares, weight_shares, cash_shares)
    if shares <= 0:
        return _risk_decision(
            context=context,
            target_weight=target_weight,
            reasons=("zero_shares",),
            daily_loss_locked=daily_loss_locked,
            drawdown_locked=drawdown_locked,
        )

    return _risk_decision(
        context=context,
        target_weight=target_weight,
        reasons=(),
        daily_loss_locked=daily_loss_locked,
        drawdown_locked=drawdown_locked,
        shares=shares,
        planned_risk_cash=risk_per_share * shares,
    )


def evaluate_exit(
    event: DecisionEvent,
    position: HoldingSnapshot,
    context: RiskContext,
    *,
    reason: str = "structural_exit",
) -> ExitDecision:
    if not isinstance(event, DecisionEvent):
        raise TypeError("event must be DecisionEvent")
    if not isinstance(position, HoldingSnapshot):
        raise TypeError("position must be HoldingSnapshot")
    if not isinstance(context, RiskContext):
        raise TypeError("context must be RiskContext")
    _require_non_empty_string(reason, "reason")

    policy = RiskPolicy.conservative()
    reasons = _quote_reasons(event, context, policy)
    reasons.extend(_market_metadata_reasons(event))
    matching = [item for item in context.holdings if item.code == event.code]
    authoritative = matching[0] if len(matching) == 1 else None
    if authoritative is None:
        reasons.append("position_not_in_context")
        requested_shares = 0
    else:
        requested_shares = authoritative.shares
        if position != authoritative:
            reasons.append("position_snapshot_mismatch")

    blocked_by_t1 = bool(
        authoritative is not None
        and event.market == "a"
        and event.market_constraints.t_plus == 1
        and authoritative.sellable_shares < authoritative.shares
    )
    if blocked_by_t1:
        reasons.append("t_plus_one")

    blocked_by_limit = context.quote.limit_down_locked
    if blocked_by_limit:
        reasons.append("limit_down_locked")
    if not context.quote.exit_tradable:
        reasons.append("exit_not_tradable")

    hard_block_reasons = tuple(
        item for item in reasons if item != "t_plus_one"
    )
    if authoritative is None or hard_block_reasons:
        executable_shares = 0
        pending_shares = requested_shares
    else:
        executable_shares = authoritative.sellable_shares
        pending_shares = authoritative.shares - executable_shares

    return ExitDecision(
        allowed=executable_shares > 0,
        requested_shares=requested_shares,
        executable_shares=executable_shares,
        pending_shares=pending_shares,
        reason=reason,
        reasons=tuple(reasons),
        blocked_by_t1=blocked_by_t1,
        blocked_by_limit=blocked_by_limit,
        evaluated_at=context.asof,
    )
