from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from chanlun.decision_support.trading_system.backtest.execution import (
    ExecutionPolicy,
    OrderIntent,
    try_fill,
)
from tests.trading_system.backtest.helpers import (
    BAR_AT,
    BAR_OPEN,
    CN,
    minute_bar,
    normal_status,
)


def policy(**overrides: object) -> ExecutionPolicy:
    values: dict[str, object] = {
        "max_volume_participation": Decimal("0.10"),
        "base_slippage_bps": Decimal("5"),
        "volatility_slippage_bps": Decimal("20"),
        "minimum_commission": Decimal("5"),
    }
    values.update(
        {
            key: Decimal(value) if isinstance(value, str) else value
            for key, value in overrides.items()
        }
    )
    return ExecutionPolicy(**values)


def order(
    *,
    side: str = "buy",
    shares: int = 100,
    triggered_at: datetime = BAR_OPEN,
) -> OrderIntent:
    return OrderIntent(
        order_id=f"order-{side}-{shares}-{triggered_at.isoformat()}",
        signal_id="signal-1",
        code="SZ.000001",
        side=side,  # type: ignore[arg-type]
        shares=shares,
        created_at=triggered_at,
        structural_stop=Decimal("9.50") if side == "buy" else Decimal("10.50"),
    )


def test_signal_fills_only_on_a_later_bar() -> None:
    same = try_fill(
        order(triggered_at=BAR_OPEN),
        minute_bar(opened_at=BAR_OPEN),
        normal_status(),
        policy(),
    )
    later = try_fill(
        order(triggered_at=BAR_OPEN),
        minute_bar(opened_at=BAR_AT),
        normal_status(),
        policy(),
    )

    assert same.filled is False
    assert same.reason == "bar_not_after_trigger"
    assert later.filled is True
    assert later.filled_at == BAR_AT


def test_suspension_and_zero_volume_are_not_tradable() -> None:
    suspended = try_fill(
        order(),
        minute_bar(opened_at=BAR_AT),
        normal_status(suspended=True),
        policy(),
    )
    no_volume = try_fill(
        order(),
        minute_bar(opened_at=BAR_AT, volume="0"),
        normal_status(),
        policy(),
    )

    assert suspended.reason == "not_tradable"
    assert no_volume.reason == "not_tradable"


def test_limit_up_lock_blocks_buy() -> None:
    locked_bar = minute_bar(
        opened_at=BAR_AT,
        raw_open="11.00",
        raw_high="11.00",
        raw_low="11.00",
        raw_close="11.00",
    )

    result = try_fill(order(), locked_bar, normal_status(), policy())

    assert result.filled is False
    assert result.reason == "limit_up_locked"


def test_limit_down_lock_blocks_sell() -> None:
    locked_bar = minute_bar(
        opened_at=BAR_AT,
        raw_open="9.00",
        raw_high="9.00",
        raw_low="9.00",
        raw_close="9.00",
    )

    result = try_fill(
        order(side="sell"),
        locked_bar,
        normal_status(),
        policy(),
    )

    assert result.filled is False
    assert result.reason == "limit_down_locked"


def test_volume_participation_caps_fill_size() -> None:
    result = try_fill(
        order(shares=10_000),
        minute_bar(opened_at=BAR_AT, volume="1000"),
        normal_status(),
        policy(max_volume_participation="0.10"),
    )

    assert result.filled is False
    assert result.reason == "volume_capacity_exceeded"


def test_slippage_is_adverse_for_both_sides() -> None:
    bar = minute_bar(opened_at=BAR_AT)

    buy = try_fill(order(), bar, normal_status(), policy())
    sell = try_fill(order(side="sell"), bar, normal_status(), policy())

    assert buy.execution_price is not None
    assert sell.execution_price is not None
    assert buy.execution_price > bar.raw_open
    assert sell.execution_price < bar.raw_open


def test_fee_schedule_uses_historical_effective_date() -> None:
    before_at = datetime(2023, 8, 25, 10, 30, tzinfo=CN)
    after_at = datetime(2023, 8, 28, 10, 30, tzinfo=CN)
    before = try_fill(
        order(
            side="sell",
            shares=10_000,
            triggered_at=before_at - timedelta(minutes=1),
        ),
        minute_bar(opened_at=before_at),
        normal_status(session=date(2023, 8, 25)),
        policy(),
    )
    after = try_fill(
        order(
            side="sell",
            shares=10_000,
            triggered_at=after_at - timedelta(minutes=1),
        ),
        minute_bar(opened_at=after_at),
        normal_status(session=date(2023, 8, 28)),
        policy(),
    )

    assert before.filled is True
    assert after.filled is True
    assert before.fees > after.fees


def test_board_lot_size_is_enforced() -> None:
    result = try_fill(
        order(shares=150),
        minute_bar(opened_at=BAR_AT),
        normal_status(lot_size=100),
        policy(),
    )

    assert result.filled is False
    assert result.reason == "lot_size_mismatch"


def test_board_specific_price_limit_is_used() -> None:
    ten_percent_bar = minute_bar(
        opened_at=BAR_AT,
        raw_open="11.00",
        raw_high="11.00",
        raw_low="11.00",
        raw_close="11.00",
    )

    main_board = try_fill(
        order(),
        ten_percent_bar,
        normal_status(limit_pct=Decimal("0.10")),
        policy(),
    )
    twenty_percent_board = try_fill(
        order(),
        ten_percent_bar,
        normal_status(limit_pct=Decimal("0.20")),
        policy(),
    )

    assert main_board.reason == "limit_up_locked"
    assert twenty_percent_board.filled is True
