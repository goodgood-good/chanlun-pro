from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from chanlun.decision_support.trading_system.bar_execution import (
    BarProxyExecutionStatus,
    HistoricalMinuteExecutionBar,
    bar_proxy_parameter_manifest,
    bar_proxy_parameter_snapshot,
    match_historical_minute_bars,
)
from chanlun.decision_support.trading_system.execution import (
    FeeModel,
    FeeRateAt,
    OrderIntent,
)
from chanlun.decision_support.trading_system.parameters import (
    LIVE_STATUS,
    etf_parameter_snapshot,
    individual_parameter_snapshot,
)


CN = ZoneInfo("Asia/Shanghai")
SESSION = date(2026, 7, 24)
SIGNAL_END = datetime(2026, 7, 24, 10, 0, tzinfo=CN)
CONFIRMED = SIGNAL_END + timedelta(seconds=2)


def fee_model() -> FeeModel:
    return FeeModel(
        schedule_id="broker:test",
        rates=(
            FeeRateAt(
                effective_from=date(2023, 8, 28),
                commission_rate=Decimal("0.0003"),
                minimum_commission=Decimal("5"),
                stock_sell_stamp_rate=Decimal("0.0005"),
                transfer_rate=Decimal("0.00001"),
            ),
        ),
    )


def order(
    *,
    side: str = "buy",
    quantity: int = 300,
    expires_at: datetime | None = SIGNAL_END + timedelta(minutes=4),
    persistence: str = "OPTIONAL",
) -> OrderIntent:
    return OrderIntent(
        client_order_id="order:bar:1",
        intent_id="intent:bar:1",
        parameter_set_id=individual_parameter_snapshot().parameter_set_id,
        rule_id="ENTRY" if side == "buy" else "EXIT",
        structure_snapshot_id="structure:snapshot:bar:1",
        selection_snapshot_id="selection:snapshot:bar:1",
        account_snapshot_id="account:snapshot:bar:1",
        symbol="SH.600000",
        instrument_kind="A_SHARE_STOCK",
        side=side,  # type: ignore[arg-type]
        quantity=quantity,
        limit_price=Decimal("10"),
        signal_bar_end=SIGNAL_END,
        created_at=SIGNAL_END + timedelta(seconds=1),
        broker_confirmed_at=CONFIRMED,
        expires_at=expires_at,
        persistence=persistence,  # type: ignore[arg-type]
        quantity_increment=100,
    )


def status(**changes) -> BarProxyExecutionStatus:
    value = BarProxyExecutionStatus(
        known_at=SIGNAL_END - timedelta(seconds=1),
        effective_session=SESSION,
        listed=True,
        suspended=False,
        continuity_active=True,
        point_in_time_state_complete=True,
        corporate_action_state_complete=True,
        sellable_quantity=10_000,
        limit_up=Decimal("11"),
        limit_down=Decimal("9"),
        buy_quantity_increment=100,
        sell_quantity_increment=100,
        fee_schedule_id="broker:test",
    )
    return replace(value, **changes)


def bar(
    *,
    sequence: int,
    minute: int,
    open_: str,
    high: str,
    low: str,
    close: str,
    volume: str = "4000",
    complete: bool = True,
    phase: str = "CONTINUOUS",
) -> HistoricalMinuteExecutionBar:
    opened = SIGNAL_END + timedelta(minutes=minute)
    return HistoricalMinuteExecutionBar(
        symbol="SH.600000",
        opened_at=opened,
        closed_at=opened + timedelta(minutes=1),
        sequence=sequence,
        raw_open=Decimal(open_),
        raw_high=Decimal(high),
        raw_low=Decimal(low),
        raw_close=Decimal(close),
        raw_volume=Decimal(volume),
        source_id=f"qmt-local:{sequence}",
        complete=complete,
        phase=phase,  # type: ignore[arg-type]
    )


def match(value: OrderIntent, bars, state=None, fees=None):
    parameters = individual_parameter_snapshot()
    return match_historical_minute_bars(
        value,
        bars=tuple(bars),
        status=status() if state is None else state,
        fee_model=fee_model() if fees is None else fees,
        fee_session=SESSION,
        strategy_parameters=parameters,
        proxy_parameters=bar_proxy_parameter_snapshot(parameters),
    )


def test_proxy_manifest_keeps_individual_and_etf_snapshots_independent() -> None:
    manifest = bar_proxy_parameter_manifest()
    individual = manifest["snapshots"]["INDIVIDUAL_THREE_PROGRAM"]
    etf = manifest["snapshots"]["ETF_PROXY"]
    assert manifest["scope"] == "RESEARCH_ONLY"
    assert manifest["live_status"] == LIVE_STATUS
    assert individual["execution_parameter_set_id"] != etf[
        "execution_parameter_set_id"
    ]
    assert individual["strategy_parameter_set_id"] == (
        individual_parameter_snapshot().parameter_set_id
    )
    assert etf["strategy_parameter_set_id"] == (
        etf_parameter_snapshot().parameter_set_id
    )


def test_proxy_snapshot_rejects_a_weakened_strict_cross_rule() -> None:
    value = bar_proxy_parameter_snapshot(individual_parameter_snapshot())

    with pytest.raises(ValueError, match="strict-cross rule is frozen"):
        replace(value, strict_cross_rule="LOW_TOUCH_COUNTS_AS_FILL")

    with pytest.raises(ValueError, match="activation rule is frozen"):
        replace(value, activation_rule="SIGNAL_BAR_OPEN")
    with pytest.raises(ValueError, match="timestamp rule is frozen"):
        replace(value, execution_timestamp_rule="BAR_OPEN")
    with pytest.raises(ValueError, match="price rule is frozen"):
        replace(value, price_rule="BAR_OPEN")


def test_bar_overlapping_broker_confirmation_cannot_fill() -> None:
    overlapping = bar(
        sequence=1,
        minute=0,
        open_="9.99",
        high="9.99",
        low="9.98",
        close="9.98",
    )
    later = bar(
        sequence=2,
        minute=1,
        open_="9.99",
        high="9.99",
        low="9.98",
        close="9.98",
    )
    result = match(order(quantity=100), (overlapping, later))
    assert result.filled_quantity == 100
    assert result.fills[0].bar_sequence == 2
    assert result.fills[0].exchange_time == later.closed_at
    assert "BAR_OVERLAPS_BROKER_CONFIRMATION_IGNORED" in (
        result.rejection_and_unfilled_reasons
    )


@pytest.mark.parametrize(
    ("candidate", "reason"),
    (
        (
            bar(
                sequence=1,
                minute=1,
                open_="10.01",
                high="10.02",
                low="10.00",
                close="10.01",
            ),
            "EXACT_LIMIT_TOUCH_NOT_FILLED",
        ),
        (
            bar(
                sequence=2,
                minute=1,
                open_="10.01",
                high="10.02",
                low="9.99",
                close="10.00",
            ),
            "INTRABAR_STRICT_CROSS_VOLUME_UNOBSERVABLE",
        ),
    ),
)
def test_touch_and_mixed_intrabar_cross_never_infer_volume(
    candidate: HistoricalMinuteExecutionBar,
    reason: str,
) -> None:
    result = match(order(quantity=100), (candidate,))
    assert result.filled_quantity == 0
    assert reason in result.rejection_and_unfilled_reasons


def test_whole_bar_strict_cross_uses_adverse_observed_price_and_charges_once() -> None:
    result = match(
        order(quantity=300),
        (
            bar(
                sequence=1,
                minute=1,
                open_="9.99",
                high="9.99",
                low="9.98",
                close="9.98",
            ),
        ),
    )
    assert result.state == "O_PARTIAL"
    assert result.filled_quantity == 200
    assert result.remaining_quantity == 100
    assert result.filled_quantity + result.remaining_quantity == 300
    assert result.fills[0].execution_price == Decimal("9.99")
    assert result.total_fees == Decimal("5.02")


def test_sell_requires_entire_bar_above_limit_and_respects_sellable_qty() -> None:
    result = match(
        order(side="sell", quantity=300),
        (
            bar(
                sequence=1,
                minute=1,
                open_="10.01",
                high="10.02",
                low="10.01",
                close="10.02",
            ),
        ),
        status(sellable_quantity=100),
    )
    assert result.state == "O_PARTIAL"
    assert result.filled_quantity == 100
    assert result.remaining_quantity == 200
    assert result.fills[0].execution_price == Decimal("10.01")
    assert "T_PLUS_ONE_PARTIAL_SELLABLE_LIMIT" in (
        result.rejection_and_unfilled_reasons
    )


@pytest.mark.parametrize(
    ("state", "expected"),
    (
        (status(suspended=True), "SUSPENDED"),
        (status(listed=False), "NOT_LISTED"),
        (
            status(point_in_time_state_complete=False),
            "POINT_IN_TIME_EXECUTION_STATE_INCOMPLETE",
        ),
        (
            status(corporate_action_state_complete=False),
            "POINT_IN_TIME_CORPORATE_ACTION_STATE_INCOMPLETE",
        ),
        (
            status(fee_schedule_id=None),
            "EFFECTIVE_FEE_SCHEDULE_UNBOUND",
        ),
        (
            status(buy_quantity_increment=10),
            "POINT_IN_TIME_QUANTITY_INCREMENT_MISMATCH",
        ),
    ),
)
def test_missing_or_rejected_runtime_facts_fail_closed(
    state: BarProxyExecutionStatus,
    expected: str,
) -> None:
    result = match(order(quantity=100), (), state)
    assert result.filled_quantity == 0
    assert expected in result.rejection_and_unfilled_reasons


def test_t1_zero_sellable_and_persistent_exit_remain_blocked() -> None:
    persistent = order(
        side="sell",
        quantity=100,
        expires_at=None,
        persistence="PERSISTENT_EXIT",
    )
    result = match(persistent, (), status(sellable_quantity=0))
    assert result.state == "O_BLOCKED"
    assert "T_PLUS_ONE_OR_SELLABLE_QUANTITY_BLOCK" in (
        result.rejection_and_unfilled_reasons
    )


def test_incomplete_noncontinuous_and_zero_volume_bars_do_not_fill() -> None:
    candidates = (
        bar(
            sequence=1,
            minute=1,
            open_="9.99",
            high="9.99",
            low="9.98",
            close="9.98",
            complete=False,
        ),
        bar(
            sequence=2,
            minute=2,
            open_="9.99",
            high="9.99",
            low="9.98",
            close="9.98",
            phase="OPENING_AUCTION",
        ),
        bar(
            sequence=3,
            minute=3,
            open_="9.99",
            high="9.99",
            low="9.98",
            close="9.98",
            volume="0",
        ),
    )
    result = match(order(quantity=100), candidates)
    assert result.filled_quantity == 0
    assert "INCOMPLETE_BAR_IGNORED" in result.rejection_and_unfilled_reasons
    assert "NON_CONTINUOUS_BAR_UNSUPPORTED_BY_PROXY" in (
        result.rejection_and_unfilled_reasons
    )
    assert "ZERO_VOLUME_BAR" in result.rejection_and_unfilled_reasons


def test_appending_bars_after_expiry_cannot_change_historical_result() -> None:
    value = order(
        quantity=100,
        expires_at=SIGNAL_END + timedelta(minutes=2),
    )
    before = match(value, ())
    after = match(
        value,
        (
            bar(
                sequence=1,
                minute=2,
                open_="9.99",
                high="9.99",
                low="9.98",
                close="9.98",
            ),
        ),
    )
    assert before == after


def test_proxy_rejects_strategy_snapshot_cross_wiring() -> None:
    individual = individual_parameter_snapshot()
    etf_proxy = bar_proxy_parameter_snapshot(etf_parameter_snapshot())
    with pytest.raises(ValueError, match="bar proxy and strategy"):
        match_historical_minute_bars(
            order(quantity=100),
            bars=(),
            status=status(),
            fee_model=fee_model(),
            fee_session=SESSION,
            strategy_parameters=individual,
            proxy_parameters=etf_proxy,
        )
