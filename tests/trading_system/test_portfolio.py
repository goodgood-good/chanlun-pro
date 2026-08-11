from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from chanlun.decision_support.trading_system.execution import (
    FeeModel,
    FeeRateAt,
)
from chanlun.decision_support.trading_system.parameters import (
    individual_parameter_snapshot,
)
from chanlun.decision_support.trading_system.portfolio import (
    TacticalPairObservation,
    assess_tactical_adaptation,
    check_every_partial_buyback_prefix,
    EntrySizingInput,
    reconcile_cycle_ledger,
    size_strategic_entry,
    CycleLedger,
)


CN = ZoneInfo("Asia/Shanghai")
SESSION = date(2026, 7, 24)


def fee_model() -> FeeModel:
    return FeeModel(
        "broker:test",
        (
            FeeRateAt(
                effective_from=date(2023, 8, 28),
                commission_rate=Decimal("0.0003"),
                minimum_commission=Decimal("5"),
                stock_sell_stamp_rate=Decimal("0.0005"),
                transfer_rate=Decimal("0.00001"),
            ),
        ),
    )


def test_entry_sizing_reserves_restore_cash_and_minimum_commission() -> None:
    sizing = EntrySizingInput(
        account_equity_at_decision=Decimal("1000000"),
        broker_available_cash=Decimal("200000"),
        current_gross_market_value=Decimal("0"),
        restore_exposure_commitment=Decimal("10000"),
        restore_cash_reserve=Decimal("20000"),
        reserved_strategic_entry_notional=Decimal("0"),
        active_buy_worst_cash_required=Decimal("5000"),
        active_buy_restore_cash_allocated=Decimal("2000"),
        buy_price_cap=Decimal("10"),
        q_liquidity_cap=30000,
        buy_quantity_increment=100,
        occupied_slots=0,
        drawdown=Decimal("0"),
    )
    decision = size_strategic_entry(
        sizing,
        parameters=individual_parameter_snapshot(),
        bound_buy_cost=fee_model().bound_buy_cost(
            instrument_kind="A_SHARE_STOCK", session=SESSION
        ),
    )
    assert decision.total_protected_cash == Decimal("23000")
    assert decision.entry_cash_available == Decimal("177000")
    assert decision.q_plan == 17600
    assert "BINDING_CASH_WITH_BOUND_COST_CAP" in decision.reason_codes


def test_cash_equal_to_one_lot_plus_minimum_fee_is_affordable() -> None:
    model = fee_model()
    price = Decimal("10")
    cost = model.order_cost(
        side="buy",
        instrument_kind="A_SHARE_STOCK",
        quantity=100,
        price=price,
        session=SESSION,
    )
    sizing = EntrySizingInput(
        account_equity_at_decision=Decimal("1000000"),
        broker_available_cash=price * 100 + cost,
        current_gross_market_value=Decimal("0"),
        restore_exposure_commitment=Decimal("0"),
        restore_cash_reserve=Decimal("0"),
        reserved_strategic_entry_notional=Decimal("0"),
        active_buy_worst_cash_required=Decimal("0"),
        active_buy_restore_cash_allocated=Decimal("0"),
        buy_price_cap=price,
        q_liquidity_cap=100,
        buy_quantity_increment=100,
        occupied_slots=0,
        drawdown=Decimal("0"),
    )
    decision = size_strategic_entry(
        sizing,
        parameters=individual_parameter_snapshot(),
        bound_buy_cost=model.bound_buy_cost(
            instrument_kind="A_SHARE_STOCK", session=SESSION
        ),
    )
    assert decision.q_plan == 100


def ledger() -> CycleLedger:
    return CycleLedger.from_entry_fill(
        cycle_id="cycle:1",
        session=SESSION,
        fill_qty=1000,
        buy_quantity_increment=100,
        sell_quantity_increment=100,
        t_plus_days=1,
        tactical_ratio=Decimal("0.25"),
    )


def test_t_plus_one_blocks_same_day_tactical_sale() -> None:
    value = ledger()
    assert value.tactical_locked_qty == 200
    assert value.tactical_eligible_qty == 0
    with pytest.raises(ValueError, match=r"T\+1"):
        value.apply_tactical_sell_fill(
            quantity=100,
            execution_id="sell:1",
            exchange_time=datetime(2026, 7, 24, 14, 0, tzinfo=CN),
            gross_sell_cash=Decimal("1100"),
            allocated_sell_cost=Decimal("5.50"),
            cash_reserve=Decimal("1094.50"),
        )


def test_partial_sell_and_fifo_partial_buyback_preserve_invariants() -> None:
    value = ledger().roll_session(SESSION + timedelta(days=1))
    value = value.apply_tactical_sell_fill(
        quantity=100,
        execution_id="sell:1",
        exchange_time=datetime(2026, 7, 25, 10, 0, tzinfo=CN),
        gross_sell_cash=Decimal("1100"),
        allocated_sell_cost=Decimal("5.50"),
        cash_reserve=Decimal("1094.50"),
    )
    assert value.pending_restore_qty == 100
    assert value.q_current == 900
    restored, realization = value.apply_tactical_buyback_fill(
        quantity=100,
        execution_id="buy:1",
        exchange_time=datetime(2026, 7, 25, 10, 5, tzinfo=CN),
        buy_cash_and_cost=Decimal("1005"),
    )
    assert restored.pending_restore_qty == 0
    assert restored.q_current == restored.q_cycle == 1000
    assert restored.tactical_locked_qty == 100
    assert restored.tactical_cycles_completed_today == 1
    assert realization.realized_net_cash == Decimal("89.50")


def test_tactical_adaptation_retains_non_executable_pairs_and_lower_median() -> None:
    observed = datetime(2026, 7, 24, 14, 0, tzinfo=CN)
    rows = tuple(
        TacticalPairObservation(
            pair_id=f"pair:{index}",
            confirmed_at=observed - timedelta(days=6 - index),
            net_edge_ticks=edge,
        )
        for index, edge in enumerate(
            (None, None, Decimal("1"), Decimal("1"), Decimal("2"), Decimal("2"))
        )
    )
    result = assess_tactical_adaptation(
        rows,
        decision_time=observed,
        parameters=individual_parameter_snapshot(),
    )
    assert result.passed is True
    assert result.non_executable_count == 2
    assert result.lower_median_edge_ticks == Decimal("1")


def test_tactical_adaptation_missing_sample_is_unresolved() -> None:
    observed = datetime(2026, 7, 24, 14, 0, tzinfo=CN)
    rows = tuple(
        TacticalPairObservation(
            pair_id=f"pair:{index}",
            confirmed_at=observed,
            net_edge_ticks=Decimal("2"),
        )
        for index in range(4)
    )
    result = assess_tactical_adaptation(
        rows,
        decision_time=observed,
        parameters=individual_parameter_snapshot(),
    )
    assert result.resolved is False
    assert result.passed is False


def test_every_possible_partial_prefix_includes_terminal_minimum_fee() -> None:
    model = fee_model()
    result = check_every_partial_buyback_prefix(
        quantity=200,
        quantity_increment=100,
        buy_limit_price=Decimal("10"),
        price_tick=Decimal("0.01"),
        available_net_sell_cash=lambda quantity: (
            Decimal(quantity) * Decimal("10.08") - Decimal("5")
        ),
        bound_terminal_buy_cost=model.bound_buy_cost(
            instrument_kind="A_SHARE_STOCK",
            session=SESSION,
        ),
    )
    assert result.passed is False
    assert result.checked_prefixes == (100, 200)
    assert result.failed_prefixes == (100,)


def test_strategic_intent_terminates_restore_obligation() -> None:
    value = ledger().roll_session(SESSION + timedelta(days=1))
    value = value.apply_tactical_sell_fill(
        quantity=100,
        execution_id="sell:1",
        exchange_time=datetime(2026, 7, 25, 10, 0, tzinfo=CN),
        gross_sell_cash=Decimal("1100"),
        allocated_sell_cost=Decimal("5"),
        cash_reserve=Decimal("1095"),
    )
    exiting = value.terminate_restore_obligations(target_state="S_EXIT_WORKING")
    assert exiting.pending_restore_qty == 0
    assert exiting.terminated_restore_qty == 100
    assert exiting.restore_cash_reserve == 0
    assert exiting.strategic_state == "S_EXIT_WORKING"


def test_company_action_rebases_quantity_without_trade_addition() -> None:
    value = ledger()
    adjusted = value.apply_mandatory_share_action(
        share_multiplier=Decimal("1.5"),
        broker_position_qty=1500,
    )
    assert adjusted.q_current == 1500
    assert adjusted.q_cycle == 1500
    assert adjusted.tactical_held_qty == 300
    assert adjusted.core_held_qty == 1200


def test_restart_reconciliation_detects_position_and_execution_mismatch() -> None:
    result = reconcile_cycle_ledger(
        ledger(),
        broker_position=900,
        broker_sellable_quantity=900,
        known_execution_ids=(),
    )
    assert result.passed is False
    assert "BROKER_POSITION_MISMATCH" in result.reason_codes
