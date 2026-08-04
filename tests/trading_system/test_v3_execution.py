from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from chanlun.decision_support.trading_system.v3_execution import (
    BrokerOrderRecoveryState,
    HistoricalExecutionStatus,
    HistoricalTradePrint,
    LocalOrderRecoveryState,
    V3FeeModel,
    V3FeeRateAt,
    V3OrderIntent,
    match_historical_trade_events,
    reconcile_orders_after_restart,
)
from chanlun.decision_support.trading_system.v3_parameters import (
    individual_parameter_snapshot,
)


CN = ZoneInfo("Asia/Shanghai")
SESSION = date(2026, 7, 24)
SIGNAL_END = datetime(2026, 7, 24, 10, 0, tzinfo=CN)
CONFIRMED = SIGNAL_END + timedelta(seconds=2)


def fee_model() -> V3FeeModel:
    return V3FeeModel(
        schedule_id="broker:test:v1",
        rates=(
            V3FeeRateAt(
                effective_from=date(2023, 8, 28),
                commission_rate=Decimal("0.0003"),
                minimum_commission=Decimal("5"),
                stock_sell_stamp_rate=Decimal("0.0005"),
                transfer_rate=Decimal("0.00001"),
            ),
        ),
    )


def order(*, side: str = "buy", quantity: int = 300) -> V3OrderIntent:
    return V3OrderIntent(
        client_order_id="order:1",
        intent_id="intent:1",
        parameter_set_id=individual_parameter_snapshot().parameter_set_id,
        rule_id="V3_ENTRY",
        structure_snapshot_id="structure:snapshot:1",
        selection_snapshot_id="selection:snapshot:1",
        account_snapshot_id="account:snapshot:1",
        symbol="SH.600000",
        instrument_kind="A_SHARE_STOCK",
        side=side,  # type: ignore[arg-type]
        quantity=quantity,
        limit_price=Decimal("10"),
        signal_bar_end=SIGNAL_END,
        created_at=SIGNAL_END + timedelta(seconds=1),
        broker_confirmed_at=CONFIRMED,
        expires_at=SIGNAL_END + timedelta(minutes=1),
        persistence="OPTIONAL",
        quantity_increment=100,
    )


def status(**changes) -> HistoricalExecutionStatus:
    value = HistoricalExecutionStatus(
        observed_at=CONFIRMED,
        listed=True,
        suspended=False,
        continuity_active=True,
        tick_and_quote_data_complete=True,
        sellable_quantity=10000,
        limit_up=Decimal("11"),
        limit_down=Decimal("9"),
    )
    return replace(value, **changes)


def event(
    *,
    sequence: int,
    seconds: int,
    price: str,
    quantity: int = 4000,
    bid: str = "9.99",
    ask: str = "10.00",
) -> HistoricalTradePrint:
    at = CONFIRMED + timedelta(seconds=seconds)
    return HistoricalTradePrint(
        exchange_time=at,
        sequence=sequence,
        trade_price=Decimal(price),
        trade_quantity=quantity,
        best_bid=Decimal(bid),
        best_ask=Decimal(ask),
        quote_time=at - timedelta(milliseconds=1),
        quote_valid=True,
    )


def match(value: V3OrderIntent, events, state=None):
    return match_historical_trade_events(
        value,
        events=tuple(events),
        status=status() if state is None else state,
        fee_model=fee_model(),
        fee_session=SESSION,
        parameters=individual_parameter_snapshot(),
        frozen_latency=timedelta(seconds=1),
    )


def test_strict_cross_fills_only_after_confirmation_latency_at_limit_price() -> None:
    result = match(
        order(quantity=200),
        (
            event(sequence=1, seconds=1, price="9.90"),
            event(sequence=2, seconds=2, price="9.99"),
        ),
    )
    assert result.filled_quantity == 200
    assert result.remaining_quantity == 0
    assert result.fills[0].source_sequence == 2
    assert result.fills[0].execution_price == Decimal("10")
    assert result.fills[0].intent_id == "intent:1"
    assert result.fills[0].structure_snapshot_id == "structure:snapshot:1"


def test_exact_limit_touch_never_assumes_queue_fill() -> None:
    result = match(order(quantity=100), (event(sequence=1, seconds=2, price="10"),))
    assert result.filled_quantity == 0
    assert result.exact_limit_touch_quantity == 4000
    assert "EXACT_LIMIT_TOUCH_NOT_FILLED" in result.rejection_and_unfilled_reasons


def test_partial_fill_is_limited_to_five_percent_of_strict_cross_volume() -> None:
    result = match(order(quantity=300), (event(sequence=1, seconds=2, price="9.99"),))
    assert result.state == "O_PARTIAL"
    assert result.filled_quantity == 200
    assert result.remaining_quantity == 100
    assert result.total_fees == Decimal("5.02")


def test_minimum_commission_and_stock_only_sell_stamp_tax() -> None:
    model = fee_model()
    stock_sell = model.order_cost(
        side="sell",
        instrument_kind="A_SHARE_STOCK",
        quantity=100,
        price=Decimal("10"),
        session=SESSION,
    )
    etf_sell = model.order_cost(
        side="sell",
        instrument_kind="EXCHANGE_TRADED_FUND",
        quantity=100,
        price=Decimal("10"),
        session=SESSION,
    )
    assert stock_sell == Decimal("5.51")
    assert etf_sell == Decimal("5.01")


def test_multi_fill_order_charges_minimum_commission_once_on_actual_notionals() -> None:
    cost = fee_model().order_cost_for_fills(
        side="buy",
        instrument_kind="A_SHARE_STOCK",
        fills=((100, Decimal("9.99")), (100, Decimal("9.98"))),
        session=SESSION,
    )

    assert cost == Decimal("5.02")


def test_suspension_delisting_and_t1_are_explicit_unfilled_reasons() -> None:
    trade = (event(sequence=1, seconds=2, price="9.99"),)
    suspended = match(order(), trade, status(suspended=True))
    delisted = match(order(), trade, status(listed=False))
    locked = match(order(side="sell"), trade, status(sellable_quantity=0))
    assert "SUSPENDED" in suspended.rejection_and_unfilled_reasons
    assert "NOT_LISTED" in delisted.rejection_and_unfilled_reasons
    assert "T_PLUS_ONE_OR_SELLABLE_QUANTITY_BLOCK" in locked.rejection_and_unfilled_reasons


def test_partial_sellable_quantity_preserves_order_quantity_conservation() -> None:
    result = match(
        order(side="sell", quantity=300),
        (
            event(
                sequence=1,
                seconds=2,
                price="10.01",
                bid="10.00",
                ask="10.01",
            ),
        ),
        status(sellable_quantity=100),
    )
    assert result.filled_quantity == 100
    assert result.remaining_quantity == 200
    assert result.filled_quantity + result.remaining_quantity == 300
    assert result.state == "O_PARTIAL"
    assert "T_PLUS_ONE_PARTIAL_SELLABLE_LIMIT" in result.rejection_and_unfilled_reasons


def test_daily_limit_and_first_quote_boundary_reject_optional_order() -> None:
    outside_quote = match(
        order(quantity=100),
        (event(sequence=1, seconds=2, price="9.99", ask="10.01"),),
    )
    assert "FIRST_EXECUTABLE_QUOTE_OUTSIDE_PRICE_BOUNDARY" in outside_quote.rejection_and_unfilled_reasons
    illegal_limit = match(
        replace(order(quantity=100), limit_price=Decimal("11.01")),
        (event(sequence=1, seconds=2, price="10.50", ask="10.50"),),
    )
    assert "BUY_LIMIT_ABOVE_DAILY_LIMIT" in illegal_limit.rejection_and_unfilled_reasons


def test_restart_unknown_order_and_regressing_fill_halt_operations() -> None:
    local = LocalOrderRecoveryState(
        client_order_id="order:1",
        intent_id="intent:1",
        broker_order_id="broker:1",
        state="O_PARTIAL",
        cumulative_filled=200,
        last_broker_sequence=5,
        execution_ids=("fill:1", "fill:2"),
    )
    broker = BrokerOrderRecoveryState(
        client_order_id="order:1",
        intent_id="intent:1",
        broker_order_id="broker:1",
        active=True,
        cumulative_filled=100,
        last_broker_sequence=4,
        execution_ids=("fill:1",),
    )
    unknown = BrokerOrderRecoveryState(
        client_order_id="unknown",
        intent_id=None,
        broker_order_id="broker:unknown",
        active=True,
        cumulative_filled=0,
        last_broker_sequence=1,
        execution_ids=(),
    )
    result = reconcile_orders_after_restart((local,), (broker, unknown))
    assert result.passed is False
    assert result.operations_state == "OPERATIONS_HALT"
    assert any(code.startswith("UNKNOWN_ACTIVE_BROKER_ORDER") for code in result.reason_codes)
    assert any(code.startswith("CUMULATIVE_FILL_REGRESSION") for code in result.reason_codes)
