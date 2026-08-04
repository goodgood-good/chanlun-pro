from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from chanlun.decision_support.trading_system.recursive_1m_component_replay import (
    Recursive1mExecutionFactAvailability,
    Recursive1mExecutionSignal,
    Recursive1mPosition,
    diagnostic_entry_order,
    diagnostic_execution_status,
    diagnostic_exit_order,
    diagnostic_fee_model,
    size_recursive_1m_diagnostic_entry,
    tactical_reserve_for_fill,
)
from chanlun.decision_support.trading_system.recursive_1m_research import (
    recursive_1m_diagnostic_execution_snapshot,
    recursive_1m_parameter_snapshot,
)
from chanlun.decision_support.trading_system.v3_bar_execution import (
    HistoricalMinuteExecutionBar,
    bar_proxy_parameter_snapshot,
    match_historical_minute_bars,
)
from chanlun.decision_support.trading_system.v3_parameters import (
    etf_parameter_snapshot,
)


CN = ZoneInfo("Asia/Shanghai")
D0 = date(2022, 5, 12)
AT = datetime(2022, 5, 12, 10, 0, tzinfo=CN)


def _signal(kind: str = "ENTRY") -> Recursive1mExecutionSignal:
    return Recursive1mExecutionSignal(
        signal_id=f"signal:{kind}",
        point_id=f"point:{kind}",
        symbol="SH.510300",
        kind=kind,  # type: ignore[arg-type]
        decision_at=AT,
        price_basis_revision="sha256:" + "a" * 64,
        raw_confirmation_high=Decimal("10.000"),
        raw_confirmation_low=Decimal("9.900"),
        confirmation_bar_source_id="bar:signal",
        selection_snapshot_id="sha256:" + "b" * 64,
    )


def _bar(
    *,
    opened_at: datetime = AT,
    low: str = "9.970",
    high: str = "9.990",
    volume: str = "10000",
    sequence: int = 1,
) -> HistoricalMinuteExecutionBar:
    low_value = Decimal(low)
    high_value = Decimal(high)
    return HistoricalMinuteExecutionBar(
        symbol="SH.510300",
        opened_at=opened_at,
        closed_at=opened_at + timedelta(minutes=1),
        sequence=sequence,
        raw_open=low_value,
        raw_high=high_value,
        raw_low=low_value,
        raw_close=high_value,
        raw_volume=Decimal(volume),
        source_id=f"bar:{opened_at.isoformat()}:{sequence}",
    )


def _position() -> Recursive1mPosition:
    return Recursive1mPosition(
        cycle_id="cycle:1",
        symbol="SH.510300",
        slot_number=1,
        quantity=1000,
        opened_at=AT + timedelta(minutes=1),
        entry_point_id="point:ENTRY",
        price_basis_revision="sha256:" + "a" * 64,
        average_entry_price=Decimal("9.99"),
        entry_fees=Decimal("5"),
        entry_cash=Decimal("9995"),
        tactical_cash_reserve=Decimal("3331.66"),
    )


def test_formal_execution_gate_fails_closed_on_missing_vintage_facts() -> None:
    facts = Recursive1mExecutionFactAvailability(
        historical_etf_trade_status=False,
        broker_vintage_fee_schedule=False,
        historical_quantity_increments=False,
        historical_settlement_rules=False,
        historical_price_limit_rules=False,
        historical_quote_or_user_waived_bar_proxy=True,
        corporate_action_ledger=True,
        source_fact_ids=("sha256:" + "c" * 64,),
    )

    assert facts.formal_execution_eligible is False
    assert facts.reason_codes == (
        "HISTORICAL_ETF_TRADE_STATUS_UNAVAILABLE",
        "BROKER_VINTAGE_FEE_SCHEDULE_UNAVAILABLE",
        "HISTORICAL_QUANTITY_INCREMENTS_UNAVAILABLE",
        "HISTORICAL_SETTLEMENT_RULES_UNAVAILABLE",
        "HISTORICAL_PRICE_LIMIT_RULES_UNAVAILABLE",
    )


def test_diagnostic_sizing_reserves_tactical_cash_and_minimum_fee() -> None:
    research = recursive_1m_parameter_snapshot("ETF_PROXY")
    assumptions = recursive_1m_diagnostic_execution_snapshot()
    sized = size_recursive_1m_diagnostic_entry(
        account_equity=Decimal("1000000"),
        broker_cash=Decimal("1000000"),
        gross_market_value=Decimal("0"),
        protected_tactical_cash=Decimal("0"),
        buy_limit=Decimal("10"),
        liquidity_cap=100000,
        occupied_slots=0,
        drawdown=Decimal("0"),
        research=research,
        assumptions=assumptions,
        fee_session=D0,
    )

    assert sized.strategic_slot_cash == Decimal("135000.0000")
    assert sized.quantity == 13500
    assert ("strategic_slot_cap", 13500) in sized.capacity_rows
    assert tactical_reserve_for_fill(
        fill_notional=Decimal("1000"),
        fill_fee=Decimal("5"),
        research=research,
    ) == Decimal("335.00")


def test_entry_uses_next_bar_strict_cross_and_supports_partial_fill() -> None:
    assumptions = recursive_1m_diagnostic_execution_snapshot()
    strategy = etf_parameter_snapshot()
    next_bar = _bar(volume="10000")
    order = diagnostic_entry_order(
        signal=_signal(),
        quantity=1000,
        next_bar=next_bar,
        account_snapshot_id="account:1",
        assumptions=assumptions,
    )
    status = diagnostic_execution_status(
        known_at=AT,
        session=D0,
        previous_close=Decimal("10"),
        sellable_quantity=0,
        assumptions=assumptions,
    )
    match = match_historical_minute_bars(
        order,
        bars=(next_bar,),
        status=status,
        fee_model=diagnostic_fee_model(assumptions),
        fee_session=D0,
        strategy_parameters=strategy,
        proxy_parameters=bar_proxy_parameter_snapshot(strategy),
    )

    assert match.filled_quantity == 500
    assert match.remaining_quantity == 500
    assert match.state == "O_PARTIAL"
    assert match.fills[0].exchange_time == next_bar.closed_at
    assert match.fills[0].execution_price == Decimal("9.990")

    exact_touch = _bar(low="10.000", high="10.000", sequence=2)
    exact_order = diagnostic_entry_order(
        signal=_signal(),
        quantity=100,
        next_bar=exact_touch,
        account_snapshot_id="account:1",
        assumptions=assumptions,
    )
    not_filled = match_historical_minute_bars(
        exact_order,
        bars=(exact_touch,),
        status=status,
        fee_model=diagnostic_fee_model(assumptions),
        fee_session=D0,
        strategy_parameters=strategy,
        proxy_parameters=bar_proxy_parameter_snapshot(strategy),
    )
    assert not_filled.filled_quantity == 0
    assert "EXACT_LIMIT_TOUCH_NOT_FILLED" in (
        not_filled.rejection_and_unfilled_reasons
    )


def test_persistent_exit_is_t1_blocked_then_reissued_deterministically() -> None:
    assumptions = recursive_1m_diagnostic_execution_snapshot()
    strategy = etf_parameter_snapshot()
    position = _position()
    sell_signal = _signal("L0_THIRD_SELL")
    same_day = diagnostic_exit_order(
        signal=sell_signal,
        position=position,
        created_at=AT,
        account_snapshot_id="account:2",
        sequence=0,
        assumptions=assumptions,
    )
    blocked_status = diagnostic_execution_status(
        known_at=AT,
        session=D0,
        previous_close=Decimal("10"),
        sellable_quantity=0,
        assumptions=assumptions,
    )
    blocked = match_historical_minute_bars(
        same_day,
        bars=(_bar(low="10.01", high="10.02"),),
        status=blocked_status,
        fee_model=diagnostic_fee_model(assumptions),
        fee_session=D0,
        strategy_parameters=strategy,
        proxy_parameters=bar_proxy_parameter_snapshot(strategy),
    )
    assert blocked.state == "O_BLOCKED"
    assert "T_PLUS_ONE_OR_SELLABLE_QUANTITY_BLOCK" in (
        blocked.rejection_and_unfilled_reasons
    )

    next_at = datetime(2022, 5, 13, 9, 30, tzinfo=CN)
    reissued = diagnostic_exit_order(
        signal=sell_signal,
        position=position,
        created_at=next_at,
        account_snapshot_id="account:3",
        sequence=1,
        assumptions=assumptions,
    )
    repeated = diagnostic_exit_order(
        signal=sell_signal,
        position=position,
        created_at=next_at,
        account_snapshot_id="account:3",
        sequence=1,
        assumptions=assumptions,
    )
    assert repeated.client_order_id == reissued.client_order_id
    assert reissued.client_order_id != same_day.client_order_id
    next_bar = _bar(
        opened_at=next_at,
        low="9.91",
        high="9.93",
        volume="100000",
        sequence=3,
    )
    next_status = diagnostic_execution_status(
        known_at=next_at,
        session=next_at.date(),
        previous_close=Decimal("10"),
        sellable_quantity=1000,
        assumptions=assumptions,
    )
    filled = match_historical_minute_bars(
        reissued,
        bars=(next_bar,),
        status=next_status,
        fee_model=diagnostic_fee_model(assumptions),
        fee_session=next_at.date(),
        strategy_parameters=strategy,
        proxy_parameters=bar_proxy_parameter_snapshot(strategy),
    )
    assert filled.filled_quantity == 1000
    assert filled.remaining_quantity == 0
