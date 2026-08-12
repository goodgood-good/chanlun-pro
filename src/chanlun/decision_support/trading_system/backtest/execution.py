from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from chanlun.decision_support.fingerprints import normalize_datetime
from chanlun.decision_support.trading_system.backtest.models import (
    MinuteBar,
    SecurityStatus,
)


OrderSide = Literal["buy", "sell"]
_ZERO = Decimal("0")
_ONE = Decimal("1")
_BASIS_POINTS = Decimal("10000")


@dataclass(frozen=True, slots=True)
class FeeRateAt:
    effective_from: date
    commission_rate: Decimal
    sell_stamp_rate: Decimal
    transfer_rate: Decimal

    def __post_init__(self) -> None:
        if any(
            rate < 0
            for rate in (
                self.commission_rate,
                self.sell_stamp_rate,
                self.transfer_rate,
            )
        ):
            raise ValueError("fee rates cannot be negative")


DEFAULT_FEE_SCHEDULE = (
    FeeRateAt(
        effective_from=date(2015, 8, 1),
        commission_rate=Decimal("0.0003"),
        sell_stamp_rate=Decimal("0.001"),
        transfer_rate=Decimal("0.00002"),
    ),
    FeeRateAt(
        effective_from=date(2022, 4, 29),
        commission_rate=Decimal("0.0003"),
        sell_stamp_rate=Decimal("0.001"),
        transfer_rate=Decimal("0.00001"),
    ),
    FeeRateAt(
        effective_from=date(2023, 8, 28),
        commission_rate=Decimal("0.0003"),
        sell_stamp_rate=Decimal("0.0005"),
        transfer_rate=Decimal("0.00001"),
    ),
)


@dataclass(frozen=True, slots=True)
class ExecutionPolicy:
    max_volume_participation: Decimal = Decimal("0.10")
    base_slippage_bps: Decimal = Decimal("5")
    volatility_slippage_bps: Decimal = Decimal("20")
    minimum_commission: Decimal = Decimal("5")
    price_tick: Decimal = Decimal("0.01")
    entry_risk_ttl_seconds: int = 300
    require_observed_price_range: bool = False
    fee_schedule: tuple[FeeRateAt, ...] = DEFAULT_FEE_SCHEDULE

    def __post_init__(self) -> None:
        if not _ZERO < self.max_volume_participation <= _ONE:
            raise ValueError("max_volume_participation must be in (0, 1]")
        if any(
            value < 0
            for value in (
                self.base_slippage_bps,
                self.volatility_slippage_bps,
                self.minimum_commission,
            )
        ):
            raise ValueError("execution costs cannot be negative")
        if self.price_tick <= 0:
            raise ValueError("price_tick must be positive")
        if (
            type(self.entry_risk_ttl_seconds) is not int
            or self.entry_risk_ttl_seconds <= 0
        ):
            raise ValueError("entry_risk_ttl_seconds must be a positive integer")
        if type(self.require_observed_price_range) is not bool:
            raise ValueError("require_observed_price_range must be boolean")
        if not self.fee_schedule:
            raise ValueError("fee_schedule cannot be empty")
        effective_dates = tuple(row.effective_from for row in self.fee_schedule)
        if effective_dates != tuple(sorted(effective_dates)):
            raise ValueError("fee_schedule must be sorted by effective date")
        if len(effective_dates) != len(set(effective_dates)):
            raise ValueError("fee_schedule effective dates must be unique")


@dataclass(frozen=True, slots=True)
class OrderIntent:
    order_id: str
    signal_id: str
    code: str
    side: OrderSide
    shares: int
    created_at: datetime
    structural_stop: Decimal | None

    def __post_init__(self) -> None:
        if not self.order_id or not self.signal_id or not self.code:
            raise ValueError("order identity fields cannot be empty")
        if self.side not in ("buy", "sell"):
            raise ValueError("side must be buy or sell")
        if self.shares <= 0:
            raise ValueError("shares must be positive")
        if self.structural_stop is not None and self.structural_stop <= 0:
            raise ValueError("structural_stop must be positive")
        object.__setattr__(
            self,
            "created_at",
            normalize_datetime(self.created_at, "created_at"),
        )


@dataclass(frozen=True, slots=True)
class FillDecision:
    order_id: str
    filled: bool
    reason: str
    filled_at: datetime | None
    execution_price: Decimal | None
    shares: int
    fees: Decimal

    @classmethod
    def rejected(cls, order: OrderIntent, reason: str) -> FillDecision:
        return cls(
            order_id=order.order_id,
            filled=False,
            reason=reason,
            filled_at=None,
            execution_price=None,
            shares=0,
            fees=_ZERO,
        )

    @classmethod
    def filled_order(
        cls,
        order: OrderIntent,
        bar: MinuteBar,
        price: Decimal,
        fees: Decimal,
    ) -> FillDecision:
        return cls(
            order_id=order.order_id,
            filled=True,
            reason="filled",
        # ``try_fill`` 使用本分钟完整的开高低收量判断可交易性、容量与滑点。
        # 这些信息到收盘才完整，因此成交时间绝不能记在开盘时刻。
            filled_at=bar.closed_at,
            execution_price=price,
            shares=order.shares,
            fees=fees,
        )


def _rounded_price(value: Decimal, tick: Decimal) -> Decimal:
    units = (value / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return units * tick


def _price_limits(
    bar: MinuteBar,
    status: SecurityStatus,
    policy: ExecutionPolicy,
) -> tuple[Decimal, Decimal]:
    return (
        _rounded_price(
            bar.previous_raw_close * (_ONE + status.limit_pct),
            policy.price_tick,
        ),
        _rounded_price(
            bar.previous_raw_close * (_ONE - status.limit_pct),
            policy.price_tick,
        ),
    )


def liquidity_slippage(
    order: OrderIntent,
    bar: MinuteBar,
    policy: ExecutionPolicy,
) -> Decimal:
    range_ratio = (bar.raw_high - bar.raw_low) / bar.raw_open
    participation = Decimal(order.shares) / bar.volume
    capacity_ratio = participation / policy.max_volume_participation
    slippage_bps = (
        policy.base_slippage_bps
        + policy.volatility_slippage_bps * range_ratio
        + policy.base_slippage_bps * capacity_ratio
    )
    return slippage_bps / _BASIS_POINTS


def _fee_rate_at(
    session: date,
    schedule: tuple[FeeRateAt, ...],
) -> FeeRateAt:
    available = tuple(row for row in schedule if row.effective_from <= session)
    if not available:
        raise LookupError(f"fee schedule unavailable for {session.isoformat()}")
    return available[-1]


def fees_for(
    order: OrderIntent,
    execution_price: Decimal,
    status: SecurityStatus,
    session: date,
    policy: ExecutionPolicy,
) -> Decimal:
    if status.code != order.code:
        raise ValueError("status code does not match order")
    rate = _fee_rate_at(session, policy.fee_schedule)
    notional = execution_price * Decimal(order.shares)
    commission = max(policy.minimum_commission, notional * rate.commission_rate)
    stamp = notional * rate.sell_stamp_rate if order.side == "sell" else _ZERO
    transfer = notional * rate.transfer_rate
    return (commission + stamp + transfer).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )


def try_fill(
    order: OrderIntent,
    bar: MinuteBar,
    status: SecurityStatus,
    policy: ExecutionPolicy,
) -> FillDecision:
    if order.code != bar.code or status.code != order.code:
        return FillDecision.rejected(order, "security_mismatch")
    if status.session != bar.opened_at.date():
        return FillDecision.rejected(order, "status_session_mismatch")
    if not status.listed:
        return FillDecision.rejected(order, "not_listed")
        # A 股买入按整手执行；公司行为后遗留的零股可以一次卖出，
        # 因此不能把买入手数约束照搬到退出订单。
    if order.side == "buy" and order.shares % status.lot_size != 0:
        return FillDecision.rejected(order, "lot_size_mismatch")
        # 信号在来源 K 线收盘时生成；本执行模型会等待后续 K 线收盘，
        # 再使用该根 K 线的开高低收量。
    if bar.closed_at <= order.created_at:
        return FillDecision.rejected(order, "bar_not_after_trigger")
    if status.suspended or bar.volume <= 0:
        return FillDecision.rejected(order, "not_tradable")

    limit_up, limit_down = _price_limits(bar, status, policy)
    if order.side == "buy" and bar.raw_low >= limit_up:
        return FillDecision.rejected(order, "limit_up_locked")
    if order.side == "sell" and bar.raw_high <= limit_down:
        return FillDecision.rejected(order, "limit_down_locked")
        # 认证回放不会臆造历史 ST 涨跌停比例。若下一完整分钟只有一个成交价，
        # 排队优先级便不可知，因此无论标签为何都禁止模拟成交。
    if policy.require_observed_price_range and bar.raw_high == bar.raw_low:
        return FillDecision.rejected(order, "one_price_bar_unfillable")
    if Decimal(order.shares) > bar.volume * policy.max_volume_participation:
        return FillDecision.rejected(order, "volume_capacity_exceeded")

    slippage = liquidity_slippage(order, bar, policy)
    if order.side == "buy":
        adverse_price = bar.raw_close * (_ONE + slippage)
        execution_price = min(adverse_price, bar.raw_high, limit_up)
    else:
        adverse_price = bar.raw_close * (_ONE - slippage)
        execution_price = max(adverse_price, bar.raw_low, limit_down)
    execution_price = _rounded_price(execution_price, policy.price_tick)
    try:
        fees = fees_for(
            order,
            execution_price,
            status,
            bar.opened_at.date(),
            policy,
        )
    except LookupError:
        return FillDecision.rejected(order, "fee_schedule_unavailable")
    return FillDecision.filled_order(order, bar, execution_price, fees)
