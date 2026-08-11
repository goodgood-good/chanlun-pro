from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from chanlun.decision_support.trading_system.decision import (
    StrategicSignalFacts,
    SystemHealthFacts,
    TacticalSignalFacts,
    DecisionInput,
    decide_backtest,
    decide_live,
)
from chanlun.decision_support.trading_system.parameters import (
    individual_parameter_snapshot,
)
from chanlun.decision_support.trading_system.portfolio import CycleLedger
from chanlun.decision_support.trading_system.selection import CandidateDecision


CN = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 7, 24, 14, 31, tzinfo=CN)


def healthy() -> SystemHealthFacts:
    return SystemHealthFacts(True, True, True, True, True)


def accepted_candidate() -> CandidateDecision:
    return CandidateDecision(
        symbol="SH.600000",
        parameter_set_id=individual_parameter_snapshot().parameter_set_id,
        selection_path="INDIVIDUAL_THREE_PROGRAM",
        accepted=True,
        checks=(),
        fundamental_role="LEADER",
        relative_value_status="UNDERVALUED",
        sector_strength=Decimal("8"),
        confirmation_time=NOW - timedelta(minutes=1),
        higher_timeframe_risk_buyable=True,
    )


def active_ledger() -> CycleLedger:
    return CycleLedger.from_entry_fill(
        cycle_id="cycle:1",
        session=date(2026, 7, 24),
        fill_qty=1000,
        buy_quantity_increment=100,
        sell_quantity_increment=100,
        t_plus_days=0,
        tactical_ratio=Decimal("0.25"),
    )


def facts(*, ledger: CycleLedger | None = None) -> DecisionInput:
    return DecisionInput(
        symbol="SH.600000",
        decision_time=NOW,
        confirmation_time=NOW - timedelta(minutes=1),
        structure_snapshot_id="structure:test",
        selection_snapshot_id="selection:test",
        account_snapshot_id="account:test",
        strategic_state="S_WAIT_RETURN" if ledger is None else ledger.strategic_state,
        health=healthy(),
        strategic=StrategicSignalFacts(),
        tactical=TacticalSignalFacts(),
        cycle_ledger=ledger,
        candidate=accepted_candidate(),
        q_plan=1000,
        price_cap_or_floor=Decimal("10"),
    )


def test_live_and_backtest_call_the_identical_decision_core() -> None:
    value = facts()
    live = decide_live(value)
    replay = decide_backtest(value)
    assert live == replay
    assert live.action == "ENTRY_INTENT"
    assert live.live_status == "LIVE_DISABLED"


def test_operations_halt_has_priority_over_all_market_signals() -> None:
    value = facts(ledger=active_ledger())
    value = replace(
        value,
        health=replace(value.health, reconciliation_passed=False),
        strategic=replace(value.strategic, l0_third_sell=True),
        tactical=replace(value.tactical, l1_third_buy=True),
    )
    intent = decide_live(value)
    assert intent.action == "OPERATIONS_HALT"
    assert intent.priority == 1
    assert "RECONCILIATION_FAILED" in intent.reason_codes


def test_trading_continuity_exit_precedes_technical_exit() -> None:
    value = facts(ledger=active_ledger())
    value = replace(
        value,
        strategic=replace(
            value.strategic,
            trading_continuity_lost=True,
            l0_third_sell=True,
        ),
    )
    intent = decide_backtest(value)
    assert intent.rule_id == "PRIORITY_02_TRADING_CONTINUITY_LOST"
    assert intent.persistence == "PERSISTENT_EXIT"
    assert intent.target_position_quantity == 0


def test_l0_divergence_reduces_to_frozen_half_target() -> None:
    value = facts(ledger=active_ledger())
    value = replace(
        value,
        strategic=replace(value.strategic, l0_upmove_divergence=True),
    )
    intent = decide_live(value)
    assert intent.action == "STRATEGIC_REDUCE_INTENT"
    assert intent.target_position_quantity == 500
    assert intent.quantity == 500


def test_l1_third_buy_protection_precedes_ordinary_buyback() -> None:
    ledger = active_ledger().apply_tactical_sell_fill(
        quantity=100,
        execution_id="sell:1",
        exchange_time=NOW - timedelta(minutes=5),
        gross_sell_cash=Decimal("1100"),
        allocated_sell_cost=Decimal("5"),
        cash_reserve=Decimal("1095"),
    )
    value = facts(ledger=ledger)
    value = replace(
        value,
        tactical=TacticalSignalFacts(
            l1_phase="OSCILLATION",
            l1_third_buy=True,
            ordinary_buyback_signal=True,
            l2_signal_confirmed=True,
            l2_reached_required_half=True,
            zn_at_or_above_a=True,
            higher_timeframe_allows_ordinary_buyback=True,
            every_partial_prefix_edge_passed=True,
            q_liquidity_cap=100,
            cash_affordable_buyback_qty=100,
        ),
    )
    intent = decide_backtest(value)
    assert intent.action == "PROTECTIVE_BUYBACK_INTENT"
    assert intent.priority == 7


def test_provisional_structure_has_no_order_permission() -> None:
    value = replace(facts(), all_structure_inputs_completed=False)
    intent = decide_live(value)
    assert intent.action == "NO_TRADE"
    assert intent.quantity == 0
