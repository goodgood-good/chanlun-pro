from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Literal

from chanlun.decision_support.fingerprints import normalize_datetime
from chanlun.decision_support.trading_system.v3_execution import (
    OrderState,
    TradingPhase,
    V3FeeModel,
    V3OrderIntent,
)
from chanlun.decision_support.trading_system.v3_parameters import (
    LIVE_STATUS,
    SelectionPath,
    StrategyV3Parameters,
    etf_parameter_snapshot,
    individual_parameter_snapshot,
    snapshot_sha256,
)
from chanlun.decision_support.trading_system.v3_portfolio import floor_to_increment


ExecutionMode = Literal["BAR_CAUSAL_PROXY"]
STRICT_BAR_ACTIVATION_RULE = "BAR_OPEN_NOT_BEFORE_BROKER_CONFIRMATION"
STRICT_BAR_EXECUTION_TIMESTAMP_RULE = "COMPLETED_BAR_CLOSE"
STRICT_BAR_PRICE_RULE = "ADVERSE_OBSERVED_BAR_EXTREME_WITHIN_LIMIT"
STRICT_BAR_CROSS_RULE = "ENTIRE_BAR_RANGE_STRICTLY_THROUGH_LIMIT"
STRICT_BAR_VOLUME_PARTICIPATION = Decimal("0.05")


@dataclass(frozen=True, slots=True)
class StrictLimitBarAssessment:
    """One shared conservative OHLC verdict for a limit order.

    A mixed bar proves only that prices existed on both sides of the limit;
    without prints it cannot prove how much volume traded after the cross.
    Consequently only a bar whose *entire* range is strictly through the
    limit contributes executable capacity.
    """

    whole_bar_crossed: bool
    ambiguous_intrabar_cross: bool
    exact_limit_touch: bool
    adverse_observed_price: Decimal | None


def adverse_observed_bar_price(
    *,
    side: Literal["buy", "sell"],
    raw_high: Decimal,
    raw_low: Decimal,
) -> Decimal:
    """Return the conservative whole-bar price shared by every bar proxy."""

    if side not in {"buy", "sell"}:
        raise ValueError("strict bar side is invalid")
    if any(
        not isinstance(value, Decimal)
        or not value.is_finite()
        or value <= 0
        for value in (raw_high, raw_low)
    ) or raw_low > raw_high:
        raise ValueError("strict bar price facts are invalid")
    return raw_high if side == "buy" else raw_low


def assess_strict_limit_bar(
    *,
    side: Literal["buy", "sell"],
    limit_price: Decimal,
    raw_high: Decimal,
    raw_low: Decimal,
) -> StrictLimitBarAssessment:
    """Classify one completed OHLC bar without inferring intrabar volume."""

    adverse_observed = adverse_observed_bar_price(
        side=side,
        raw_high=raw_high,
        raw_low=raw_low,
    )
    if (
        not isinstance(limit_price, Decimal)
        or not limit_price.is_finite()
        or limit_price <= 0
    ):
        raise ValueError("strict bar price facts are invalid")
    if side == "buy":
        whole_bar_crossed = raw_high < limit_price
        ambiguous = raw_low < limit_price <= raw_high
        exact_touch = raw_low == limit_price
        adverse_price = adverse_observed if whole_bar_crossed else None
    else:
        whole_bar_crossed = raw_low > limit_price
        ambiguous = raw_low <= limit_price < raw_high
        exact_touch = raw_high == limit_price
        adverse_price = adverse_observed if whole_bar_crossed else None
    return StrictLimitBarAssessment(
        whole_bar_crossed=whole_bar_crossed,
        ambiguous_intrabar_cross=ambiguous,
        exact_limit_touch=exact_touch,
        adverse_observed_price=adverse_price,
    )


def strict_bar_volume_capacity(
    raw_volume: Decimal,
    *,
    quantity_increment: int,
) -> int:
    """Return the frozen five-percent whole-bar capacity in legal units."""

    if (
        not isinstance(raw_volume, Decimal)
        or not raw_volume.is_finite()
        or raw_volume < 0
        or quantity_increment <= 0
    ):
        raise ValueError("strict bar volume facts are invalid")
    return floor_to_increment(
        raw_volume * STRICT_BAR_VOLUME_PARTICIPATION,
        quantity_increment,
    )


@dataclass(frozen=True, slots=True)
class BarProxyParameters:
    """Frozen research-only substitute for unavailable historical prints.

    This snapshot does not alter the v3 strategy snapshot.  It only records
    the user's explicit decision to validate historical execution with later,
    completed one-minute bars.  It is deliberately stricter than a common
    OHLC touch model: a bar contributes capacity only when its *entire* price
    range is strictly through the order limit.
    """

    selection_path: SelectionPath
    strategy_parameter_set_id: str
    execution_mode: ExecutionMode = "BAR_CAUSAL_PROXY"
    source_bar_minutes: int = 1
    activation_rule: str = STRICT_BAR_ACTIVATION_RULE
    execution_timestamp_rule: str = STRICT_BAR_EXECUTION_TIMESTAMP_RULE
    price_rule: str = STRICT_BAR_PRICE_RULE
    strict_cross_rule: str = STRICT_BAR_CROSS_RULE
    max_bar_volume_participation: Decimal = STRICT_BAR_VOLUME_PARTICIPATION
    allow_signal_bar_fill: bool = False
    allow_exact_limit_touch_fill: bool = False
    allow_mixed_range_volume_inference: bool = False
    live_status: str = LIVE_STATUS

    def __post_init__(self) -> None:
        if self.selection_path not in {
            "INDIVIDUAL_THREE_PROGRAM",
            "ETF_PROXY",
        }:
            raise ValueError("unsupported bar proxy selection path")
        expected = (
            individual_parameter_snapshot()
            if self.selection_path == "INDIVIDUAL_THREE_PROGRAM"
            else etf_parameter_snapshot()
        )
        if self.strategy_parameter_set_id != expected.parameter_set_id:
            raise ValueError("bar proxy is not bound to the frozen v3 snapshot")
        if self.execution_mode != "BAR_CAUSAL_PROXY":
            raise ValueError("bar proxy execution mode is frozen")
        if self.activation_rule != STRICT_BAR_ACTIVATION_RULE:
            raise ValueError("bar proxy activation rule is frozen")
        if self.execution_timestamp_rule != STRICT_BAR_EXECUTION_TIMESTAMP_RULE:
            raise ValueError("bar proxy execution timestamp rule is frozen")
        if self.price_rule != STRICT_BAR_PRICE_RULE:
            raise ValueError("bar proxy price rule is frozen")
        if self.strict_cross_rule != STRICT_BAR_CROSS_RULE:
            raise ValueError("bar proxy strict-cross rule is frozen")
        if self.source_bar_minutes != 1:
            raise ValueError("bar proxy only accepts one-minute bars")
        if (
            self.max_bar_volume_participation
            != STRICT_BAR_VOLUME_PARTICIPATION
        ):
            raise ValueError("bar proxy participation is frozen at five percent")
        if (
            self.allow_signal_bar_fill
            or self.allow_exact_limit_touch_fill
            or self.allow_mixed_range_volume_inference
        ):
            raise ValueError("bar proxy conservative guards cannot be enabled")
        if self.live_status != LIVE_STATUS:
            raise ValueError("bar proxy cannot enable live trading")

    def document(self) -> dict[str, object]:
        value = asdict(self)
        value["max_bar_volume_participation"] = format(
            self.max_bar_volume_participation,
            "f",
        )
        return value

    @property
    def execution_parameter_set_id(self) -> str:
        return snapshot_sha256(self.document())


def bar_proxy_parameter_snapshot(
    strategy_parameters: StrategyV3Parameters,
) -> BarProxyParameters:
    return BarProxyParameters(
        selection_path=strategy_parameters.selection_path,
        strategy_parameter_set_id=strategy_parameters.parameter_set_id,
    )


def bar_proxy_parameter_manifest() -> dict[str, object]:
    snapshots = tuple(
        bar_proxy_parameter_snapshot(value)
        for value in (
            individual_parameter_snapshot(),
            etf_parameter_snapshot(),
        )
    )
    manifest = {
        "schema": "chanlun-v3-bar-causal-proxy-parameters/v1",
        "execution_mode": "BAR_CAUSAL_PROXY",
        "scope": "RESEARCH_ONLY",
        "live_status": LIVE_STATUS,
        "user_waiver": "HISTORICAL_TICK_EVENTS_NOT_REQUIRED",
        "snapshots": {
            value.selection_path: {
                "execution_parameter_set_id": value.execution_parameter_set_id,
                "strategy_parameter_set_id": value.strategy_parameter_set_id,
                "parameters": value.document(),
            }
            for value in snapshots
        },
    }
    manifest["manifest_sha256"] = snapshot_sha256(manifest)
    return manifest


@dataclass(frozen=True, slots=True)
class HistoricalMinuteExecutionBar:
    symbol: str
    opened_at: datetime
    closed_at: datetime
    sequence: int
    raw_open: Decimal
    raw_high: Decimal
    raw_low: Decimal
    raw_close: Decimal
    raw_volume: Decimal
    source_id: str
    complete: bool = True
    phase: TradingPhase = "CONTINUOUS"

    def __post_init__(self) -> None:
        opened = normalize_datetime(self.opened_at, "opened_at")
        closed = normalize_datetime(self.closed_at, "closed_at")
        object.__setattr__(self, "opened_at", opened)
        object.__setattr__(self, "closed_at", closed)
        if not self.symbol or not self.source_id:
            raise ValueError("bar symbol and source identity are required")
        if self.sequence < 0:
            raise ValueError("bar sequence cannot be negative")
        if closed - opened != timedelta(minutes=1):
            raise ValueError("bar proxy requires an exact one-minute interval")
        if any(
            value <= 0
            for value in (
                self.raw_open,
                self.raw_high,
                self.raw_low,
                self.raw_close,
            )
        ):
            raise ValueError("bar prices must be positive")
        if self.raw_volume < 0:
            raise ValueError("bar volume cannot be negative")
        if self.raw_low > min(self.raw_open, self.raw_close):
            raise ValueError("bar low exceeds its open or close")
        if self.raw_high < max(self.raw_open, self.raw_close):
            raise ValueError("bar high is below its open or close")
        if self.raw_low > self.raw_high:
            raise ValueError("bar low cannot exceed its high")


@dataclass(frozen=True, slots=True)
class BarProxyExecutionStatus:
    known_at: datetime
    effective_session: date
    listed: bool
    suspended: bool
    continuity_active: bool
    point_in_time_state_complete: bool
    corporate_action_state_complete: bool
    sellable_quantity: int
    limit_up: Decimal
    limit_down: Decimal
    buy_quantity_increment: int
    sell_quantity_increment: int
    fee_schedule_id: str | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "known_at",
            normalize_datetime(self.known_at, "known_at"),
        )
        if self.sellable_quantity < 0:
            raise ValueError("sellable quantity cannot be negative")
        if self.limit_up <= 0 or self.limit_down <= 0:
            raise ValueError("daily price limits must be positive")
        if self.limit_down >= self.limit_up:
            raise ValueError("limit down must be below limit up")
        if self.buy_quantity_increment <= 0 or self.sell_quantity_increment <= 0:
            raise ValueError("quantity increments must be positive")


@dataclass(frozen=True, slots=True)
class BarProxyFill:
    execution_id: str
    intent_id: str
    strategy_parameter_set_id: str
    execution_parameter_set_id: str
    rule_id: str
    structure_snapshot_id: str
    selection_snapshot_id: str | None
    account_snapshot_id: str
    bar_source_id: str
    bar_sequence: int
    bar_opened_at: datetime
    exchange_time: datetime
    quantity: int
    execution_price: Decimal


@dataclass(frozen=True, slots=True)
class BarProxyMatchResult:
    order_id: str
    execution_mode: ExecutionMode
    execution_parameter_set_id: str
    state: OrderState
    fills: tuple[BarProxyFill, ...]
    filled_quantity: int
    remaining_quantity: int
    total_fees: Decimal
    rejection_and_unfilled_reasons: tuple[str, ...]
    exact_limit_touch_bars: int
    ambiguous_intrabar_cross_bars: int
    eligible_complete_bars: int


def _blocked(
    order: V3OrderIntent,
    proxy: BarProxyParameters,
    reasons: list[str],
) -> BarProxyMatchResult:
    return BarProxyMatchResult(
        order_id=order.client_order_id,
        execution_mode="BAR_CAUSAL_PROXY",
        execution_parameter_set_id=proxy.execution_parameter_set_id,
        state="O_BLOCKED" if order.persistence == "PERSISTENT_EXIT" else "O_IDLE",
        fills=(),
        filled_quantity=0,
        remaining_quantity=order.quantity,
        total_fees=Decimal("0"),
        rejection_and_unfilled_reasons=tuple(dict.fromkeys(reasons)),
        exact_limit_touch_bars=0,
        ambiguous_intrabar_cross_bars=0,
        eligible_complete_bars=0,
    )


def match_historical_minute_bars(
    order: V3OrderIntent,
    *,
    bars: tuple[HistoricalMinuteExecutionBar, ...],
    status: BarProxyExecutionStatus,
    fee_model: V3FeeModel,
    fee_session: date,
    strategy_parameters: StrategyV3Parameters,
    proxy_parameters: BarProxyParameters,
) -> BarProxyMatchResult:
    """Causally match a v3 order using completed one-minute bars.

    A mixed bar (for example ``low < buy_limit <= high``) proves that a cross
    occurred but not how much volume crossed.  It therefore contributes zero
    capacity.  A buy only uses a bar with ``high < limit``; a sell only uses a
    bar with ``low > limit``.  The result is a lower-bound execution proxy,
    never a claim of tick-equivalent replay.
    """

    if order.parameter_set_id != strategy_parameters.parameter_set_id:
        raise ValueError("order and strategy parameter snapshots differ")
    if proxy_parameters.strategy_parameter_set_id != order.parameter_set_id:
        raise ValueError("bar proxy and strategy parameter snapshots differ")
    if proxy_parameters.selection_path != strategy_parameters.selection_path:
        raise ValueError("bar proxy selection path differs from the strategy")

    reasons: list[str] = []
    expected_increment = (
        status.buy_quantity_increment
        if order.side == "buy"
        else status.sell_quantity_increment
    )
    if status.known_at > order.broker_confirmed_at:
        reasons.append("POINT_IN_TIME_STATUS_KNOWN_AFTER_ORDER")
    if status.effective_session != fee_session:
        reasons.append("STATUS_OR_FEE_SESSION_MISMATCH")
    if not status.point_in_time_state_complete:
        reasons.append("POINT_IN_TIME_EXECUTION_STATE_INCOMPLETE")
    if not status.corporate_action_state_complete:
        reasons.append("POINT_IN_TIME_CORPORATE_ACTION_STATE_INCOMPLETE")
    if not status.listed:
        reasons.append("NOT_LISTED")
    if status.suspended:
        reasons.append("SUSPENDED")
    if not status.continuity_active and order.persistence == "OPTIONAL":
        reasons.append("TRADING_CONTINUITY_LOST")
    if order.side == "sell" and status.sellable_quantity <= 0:
        reasons.append("T_PLUS_ONE_OR_SELLABLE_QUANTITY_BLOCK")
    if order.quantity_increment != expected_increment:
        reasons.append("POINT_IN_TIME_QUANTITY_INCREMENT_MISMATCH")
    if not status.fee_schedule_id:
        reasons.append("EFFECTIVE_FEE_SCHEDULE_UNBOUND")
    elif status.fee_schedule_id != fee_model.schedule_id:
        reasons.append("FEE_SCHEDULE_ID_MISMATCH")
    try:
        fee_model.rate_at(fee_session)
    except LookupError:
        reasons.append("EFFECTIVE_FEE_RATE_UNAVAILABLE")
    if order.side == "buy" and order.limit_price > status.limit_up:
        reasons.append("BUY_LIMIT_ABOVE_DAILY_LIMIT")
    if order.side == "sell" and order.limit_price < status.limit_down:
        reasons.append("SELL_LIMIT_BELOW_DAILY_LIMIT")
    if reasons:
        return _blocked(order, proxy_parameters, reasons)

    ordered = tuple(
        sorted(bars, key=lambda value: (value.opened_at, value.sequence))
    )
    keys = tuple((value.opened_at, value.sequence) for value in ordered)
    if len(keys) != len(set(keys)):
        raise ValueError("historical bars must have unique time/sequence keys")
    if any(value.symbol != order.symbol for value in ordered):
        raise ValueError("historical bar symbol differs from the order")

    remaining = order.quantity
    sellable_remaining = (
        status.sellable_quantity if order.side == "sell" else order.quantity
    )
    fills: list[BarProxyFill] = []
    exact_touch_bars = 0
    ambiguous_bars = 0
    eligible_bars = 0
    expired = False
    for bar in ordered:
        if bar.closed_at <= order.signal_bar_end:
            continue
        if bar.opened_at < order.broker_confirmed_at:
            reasons.append("BAR_OVERLAPS_BROKER_CONFIRMATION_IGNORED")
            continue
        if order.expires_at is not None and bar.closed_at > order.expires_at:
            expired = True
            break
        if bar.opened_at.date() != status.effective_session:
            reasons.append("BAR_SESSION_MISMATCH_IGNORED")
            continue
        if not bar.complete:
            reasons.append("INCOMPLETE_BAR_IGNORED")
            continue
        if bar.phase != "CONTINUOUS":
            reasons.append("NON_CONTINUOUS_BAR_UNSUPPORTED_BY_PROXY")
            continue
        eligible_bars += 1
        if bar.raw_volume <= 0:
            reasons.append("ZERO_VOLUME_BAR")
            continue

        assessment = assess_strict_limit_bar(
            side=order.side,
            limit_price=order.limit_price,
            raw_high=bar.raw_high,
            raw_low=bar.raw_low,
        )
        if assessment.exact_limit_touch:
            exact_touch_bars += 1
        if assessment.ambiguous_intrabar_cross:
            ambiguous_bars += 1
            continue
        if not assessment.whole_bar_crossed or remaining <= 0:
            continue

        capacity = strict_bar_volume_capacity(
            bar.raw_volume,
            quantity_increment=order.quantity_increment,
        )
        fill_quantity = min(remaining, capacity, sellable_remaining)
        fill_quantity = floor_to_increment(
            fill_quantity,
            order.quantity_increment,
        )
        if fill_quantity <= 0:
            reasons.append("WHOLE_BAR_CROSS_VOLUME_BELOW_QUANTITY_INCREMENT")
            continue
        execution_price = assessment.adverse_observed_price
        if execution_price is None:  # pragma: no cover - guarded above
            raise AssertionError("strictly crossed bar has no execution price")
        fills.append(
            BarProxyFill(
                execution_id=f"{order.client_order_id}:bar:{bar.sequence}",
                intent_id=order.intent_id,
                strategy_parameter_set_id=order.parameter_set_id,
                execution_parameter_set_id=(
                    proxy_parameters.execution_parameter_set_id
                ),
                rule_id=order.rule_id,
                structure_snapshot_id=order.structure_snapshot_id,
                selection_snapshot_id=order.selection_snapshot_id,
                account_snapshot_id=order.account_snapshot_id,
                bar_source_id=bar.source_id,
                bar_sequence=bar.sequence,
                bar_opened_at=bar.opened_at,
                exchange_time=bar.closed_at,
                quantity=fill_quantity,
                execution_price=execution_price,
            )
        )
        remaining -= fill_quantity
        sellable_remaining -= fill_quantity
        if remaining == 0:
            break
        if order.side == "sell" and sellable_remaining == 0:
            reasons.append("T_PLUS_ONE_PARTIAL_SELLABLE_LIMIT")
            break

    if exact_touch_bars:
        reasons.append("EXACT_LIMIT_TOUCH_NOT_FILLED")
    if ambiguous_bars:
        reasons.append("INTRABAR_STRICT_CROSS_VOLUME_UNOBSERVABLE")
    if not fills and eligible_bars == 0:
        reasons.append("NO_COMPLETE_POST_CONFIRMATION_BAR")
    elif not fills and not exact_touch_bars and not ambiguous_bars:
        reasons.append("NO_WHOLE_BAR_STRICT_CROSS")
    if remaining > 0 and (expired or order.expires_at is not None):
        reasons.append("ORDER_EXPIRED_WITH_UNFILLED_QUANTITY")

    filled = sum(value.quantity for value in fills)
    total_fees = (
        Decimal("0")
        if filled == 0
        else fee_model.order_cost_for_fills(
            side=order.side,
            instrument_kind=order.instrument_kind,
            fills=tuple(
                (value.quantity, value.execution_price) for value in fills
            ),
            session=fee_session,
        )
    )
    if remaining == 0:
        state: OrderState = "O_IDLE"
    elif filled:
        state = "O_PARTIAL"
    elif order.persistence == "PERSISTENT_EXIT":
        state = "O_BLOCKED"
    else:
        state = "O_IDLE"
    return BarProxyMatchResult(
        order_id=order.client_order_id,
        execution_mode="BAR_CAUSAL_PROXY",
        execution_parameter_set_id=proxy_parameters.execution_parameter_set_id,
        state=state,
        fills=tuple(fills),
        filled_quantity=filled,
        remaining_quantity=remaining,
        total_fees=total_fees,
        rejection_and_unfilled_reasons=tuple(dict.fromkeys(reasons)),
        exact_limit_touch_bars=exact_touch_bars,
        ambiguous_intrabar_cross_bars=ambiguous_bars,
        eligible_complete_bars=eligible_bars,
    )


__all__ = [
    "BarProxyExecutionStatus",
    "BarProxyFill",
    "BarProxyMatchResult",
    "BarProxyParameters",
    "HistoricalMinuteExecutionBar",
    "STRICT_BAR_ACTIVATION_RULE",
    "STRICT_BAR_CROSS_RULE",
    "STRICT_BAR_EXECUTION_TIMESTAMP_RULE",
    "STRICT_BAR_PRICE_RULE",
    "STRICT_BAR_VOLUME_PARTICIPATION",
    "StrictLimitBarAssessment",
    "adverse_observed_bar_price",
    "assess_strict_limit_bar",
    "bar_proxy_parameter_manifest",
    "bar_proxy_parameter_snapshot",
    "match_historical_minute_bars",
    "strict_bar_volume_capacity",
]
