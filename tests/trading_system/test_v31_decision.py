from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

from tests.trading_system.test_v3_decision_parity import (
    NOW,
    active_ledger,
    facts,
)

from chanlun.decision_support.trading_system.v31_compliance import (
    ProgramTradingComplianceSnapshot,
)
from chanlun.decision_support.trading_system.v31_decision import (
    V31DecisionInput,
    decide_v31_backtest,
    decide_v31_live,
)
from chanlun.decision_support.trading_system.v31_parameters import (
    v31_parameter_snapshot,
)
from chanlun.decision_support.trading_system.v3_decision import TacticalSignalFacts


def compliance() -> ProgramTradingComplianceSnapshot:
    parameters = v31_parameter_snapshot("INDIVIDUAL_THREE_PROGRAM")
    return ProgramTradingComplianceSnapshot(
        snapshot_id="compliance:v31",
        mode="PAPER",
        observed_at=NOW - timedelta(days=1),
        valid_until=NOW + timedelta(days=1),
        strategy_id=parameters.strategy_id,
        software_version="v31-test",
        program_trading_report_confirmed=False,
        broker_permission_confirmed=False,
        licensed_market_data=True,
        abnormal_trading_monitor_healthy=True,
        order_rate_limit_configured=True,
        cancellation_rate_monitor_configured=True,
    )


def v31(base=None, **changes) -> V31DecisionInput:
    values = {
        "base": facts() if base is None else base,
        "parameters": v31_parameter_snapshot("INDIVIDUAL_THREE_PROGRAM"),
        "compliance": compliance(),
        "entry_evidence_contract_valid": True,
        "structural_invalidation_confirmed": False,
        "drawdown_state": "NORMAL",
    }
    values.update(changes)
    return V31DecisionInput(**values)


def test_v31_live_and_backtest_use_same_full_safety_fold() -> None:
    value = v31()
    assert decide_v31_live(value) == decide_v31_backtest(value)
    assert decide_v31_live(value).action == "ENTRY_INTENT"


def test_v31_missing_entry_price_boundary_halts_instead_of_ordering() -> None:
    value = v31(base=replace(facts(), price_cap_or_floor=None))
    intent = decide_v31_backtest(value)
    assert intent.action == "OPERATIONS_HALT"
    assert intent.reason_codes == ("ORDER_PRICE_BOUNDARY_MISSING",)


def test_v31_structural_invalidation_creates_persistent_full_exit() -> None:
    base = facts(ledger=active_ledger())
    intent = decide_v31_backtest(
        v31(base=base, structural_invalidation_confirmed=True)
    )
    assert intent.action == "STRATEGIC_EXIT_INTENT"
    assert intent.target_position_quantity == 0
    assert intent.persistence == "PERSISTENT_EXIT"
    assert "L0_THIRD_BUY_INVALIDATED" in intent.reason_codes[0]


def test_v31_drawdown_blocks_new_buy_at_equal_boundary() -> None:
    intent = decide_v31_backtest(v31(drawdown_state="ENTRY_HALT"))
    assert intent.action == "WAIT"
    assert intent.reason_codes == ("DRAWDOWN_ENTRY_HALT_BLOCKS_BUY",)


def test_v31_deleverage_reduces_existing_position() -> None:
    base = facts(ledger=active_ledger())
    intent = decide_v31_backtest(
        v31(
            base=base,
            drawdown_state="DELEVERAGE",
            deleverage_target_quantity=500,
        )
    )
    assert intent.action == "STRATEGIC_REDUCE_INTENT"
    assert intent.quantity == 500
    assert intent.target_position_quantity == 500


def test_v31_rejects_invalid_entry_evidence_even_if_candidate_is_accepted() -> None:
    intent = decide_v31_backtest(v31(entry_evidence_contract_valid=False))
    assert intent.action == "WAIT"
    assert intent.reason_codes == ("ENTRY_EVIDENCE_CONTRACT_INVALID",)


def test_v31_disabled_tactical_module_blocks_protective_buyback_too() -> None:
    ledger = active_ledger().apply_tactical_sell_fill(
        quantity=100,
        execution_id="sell:v31",
        exchange_time=NOW - timedelta(minutes=5),
        gross_sell_cash=Decimal("1100"),
        allocated_sell_cost=Decimal("5"),
        cash_reserve=Decimal("1095"),
    )
    base = replace(
        facts(ledger=ledger),
        tactical=TacticalSignalFacts(
            l1_third_buy=True,
            q_liquidity_cap=100,
            cash_affordable_buyback_qty=100,
        ),
    )
    intent = decide_v31_backtest(v31(base=base))
    assert intent.action == "WAIT"
    assert intent.reason_codes == ("TACTICAL_MODULE_NOT_ACTIVATED",)
