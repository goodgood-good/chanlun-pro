from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

import pytest

from tests.trading_system.test_v3_decision_parity import NOW, active_ledger, facts
from tests.trading_system.test_v31_decision import v31

from chanlun.decision_support.trading_system.v3_bar_execution import (
    BarProxyExecutionStatus,
    HistoricalMinuteExecutionBar,
)
from chanlun.decision_support.trading_system.v3_execution import (
    V3FeeModel,
    V3FeeRateAt,
)
from chanlun.decision_support.trading_system.v31_decision import (
    decide_v31_backtest,
)
from chanlun.decision_support.trading_system.v31_execution import (
    match_v31_historical_minute_bars,
    prepare_v31_order,
)
from chanlun.decision_support.trading_system.v31_parameters import (
    v31_parameter_snapshot,
)


SESSION = date(2026, 7, 24)


def fee_model() -> V3FeeModel:
    return V3FeeModel(
        schedule_id="broker:v31-test:v1",
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


def status(*, sellable_quantity: int = 10_000) -> BarProxyExecutionStatus:
    return BarProxyExecutionStatus(
        known_at=NOW - timedelta(minutes=2),
        effective_session=SESSION,
        listed=True,
        suspended=False,
        continuity_active=True,
        point_in_time_state_complete=True,
        corporate_action_state_complete=True,
        sellable_quantity=sellable_quantity,
        limit_up=Decimal("11"),
        limit_down=Decimal("9"),
        buy_quantity_increment=100,
        sell_quantity_increment=100,
        fee_schedule_id="broker:v31-test:v1",
    )


def bar(*, minute: int, sequence: int, price: str = "9.99"):
    opened = NOW + timedelta(minutes=minute)
    value = Decimal(price)
    return HistoricalMinuteExecutionBar(
        symbol="SH.600000",
        opened_at=opened,
        closed_at=opened + timedelta(minutes=1),
        sequence=sequence,
        raw_open=value,
        raw_high=value,
        raw_low=value - Decimal("0.01"),
        raw_close=value - Decimal("0.01"),
        raw_volume=Decimal("4000"),
        source_id=f"qmt-v31:{sequence}",
    )


def test_v31_decision_order_and_fill_are_one_traceable_causal_path() -> None:
    decision_input = v31()
    intent = decide_v31_backtest(decision_input)
    parameters = v31_parameter_snapshot("INDIVIDUAL_THREE_PROGRAM")
    prepared = prepare_v31_order(
        intent,
        parameters=parameters,
        compliance=decision_input.compliance,
        instrument_kind="A_SHARE_STOCK",
        created_at=NOW,
        broker_confirmed_at=NOW + timedelta(seconds=2),
        quantity_increment=100,
        expires_at=NOW + timedelta(minutes=4),
    )
    result = match_v31_historical_minute_bars(
        prepared,
        parameters=parameters,
        bars=(bar(minute=0, sequence=1), bar(minute=1, sequence=2)),
        status=status(),
        fee_model=fee_model(),
        fee_session=SESSION,
    )
    assert prepared.order.signal_bar_end == intent.confirmation_time
    assert prepared.order.structure_snapshot_id == intent.structure_snapshot_id
    assert prepared.order.selection_snapshot_id == intent.selection_snapshot_id
    assert result.v31_parameter_set_id == parameters.parameter_set_id
    assert result.parent_v3_parameter_set_id == parameters.parent_v3_parameter_set_id
    assert result.result.filled_quantity == 200
    assert result.result.remaining_quantity == 800
    assert result.result.fills[0].bar_sequence == 2
    assert result.result.total_fees == Decimal("5.02")
    assert "BAR_OVERLAPS_BROKER_CONFIRMATION_IGNORED" in (
        result.result.rejection_and_unfilled_reasons
    )


def test_v31_persistent_exit_reaches_t_plus_one_block_without_expiring() -> None:
    base = facts(ledger=active_ledger())
    decision_input = v31(
        base=base,
        structural_invalidation_confirmed=True,
    )
    intent = decide_v31_backtest(decision_input)
    parameters = v31_parameter_snapshot("INDIVIDUAL_THREE_PROGRAM")
    prepared = prepare_v31_order(
        intent,
        parameters=parameters,
        compliance=decision_input.compliance,
        instrument_kind="A_SHARE_STOCK",
        created_at=NOW,
        broker_confirmed_at=NOW + timedelta(seconds=2),
        quantity_increment=100,
        expires_at=None,
    )
    result = match_v31_historical_minute_bars(
        prepared,
        parameters=parameters,
        bars=(bar(minute=1, sequence=1, price="10.02"),),
        status=status(sellable_quantity=0),
        fee_model=fee_model(),
        fee_session=SESSION,
    )
    assert prepared.order.persistence == "PERSISTENT_EXIT"
    assert result.result.state == "O_BLOCKED"
    assert result.result.remaining_quantity == 1000
    assert "T_PLUS_ONE_OR_SELLABLE_QUANTITY_BLOCK" in (
        result.result.rejection_and_unfilled_reasons
    )


def test_v31_order_preparation_never_bypasses_live_disabled_compliance() -> None:
    decision_input = v31()
    live_compliance = replace(
        decision_input.compliance,
        mode="LIVE",
        program_trading_report_confirmed=True,
        broker_permission_confirmed=True,
    )
    with pytest.raises(ValueError, match="V31_LIVE_STATUS_DISABLED"):
        prepare_v31_order(
            decide_v31_backtest(decision_input),
            parameters=v31_parameter_snapshot("INDIVIDUAL_THREE_PROGRAM"),
            compliance=live_compliance,
            instrument_kind="A_SHARE_STOCK",
            created_at=NOW,
            broker_confirmed_at=NOW + timedelta(seconds=2),
            quantity_increment=100,
            expires_at=NOW + timedelta(minutes=4),
        )


def test_v31_optional_order_requires_explicit_causal_expiry() -> None:
    decision_input = v31()
    with pytest.raises(ValueError, match="explicit expiry"):
        prepare_v31_order(
            decide_v31_backtest(decision_input),
            parameters=v31_parameter_snapshot("INDIVIDUAL_THREE_PROGRAM"),
            compliance=decision_input.compliance,
            instrument_kind="A_SHARE_STOCK",
            created_at=NOW,
            broker_confirmed_at=NOW + timedelta(seconds=2),
            quantity_increment=100,
            expires_at=None,
        )
