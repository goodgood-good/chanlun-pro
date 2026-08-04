from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from chanlun.decision_support.trading_system.v31_compliance import (
    ProgramTradingComplianceSnapshot,
    evaluate_program_trading_compliance,
)
from chanlun.decision_support.trading_system.v31_parameters import (
    v31_parameter_manifest,
    v31_parameter_snapshot,
)
from chanlun.decision_support.trading_system.v31_risk import (
    PortfolioRiskSnapshot,
    StructuralEntryRiskInput,
    classify_drawdown,
    size_structural_entry,
)


CN = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 7, 26, 12, tzinfo=CN)


def portfolio(**changes) -> PortfolioRiskSnapshot:
    values = {
        "account_equity": Decimal("1000000"),
        "drawdown": Decimal("0.02"),
        "gross_exposure": Decimal("0"),
        "current_open_risk_cash": Decimal("0"),
        "cluster_exposure": Decimal("0"),
        "cluster_open_risk_cash": Decimal("0"),
        "occupied_slots": 0,
        "cluster_occupied_slots": 0,
        "cluster_id": "BROAD_CHINA_A",
    }
    values.update(changes)
    return PortfolioRiskSnapshot(**values)


def entry(**changes) -> StructuralEntryRiskInput:
    values = {
        "entry_price_cap": Decimal("10"),
        "structural_invalidation_price": Decimal("9.5"),
        "price_tick": Decimal("0.01"),
        "buy_quantity_increment": 100,
        "upstream_quantity_cap": 18000,
    }
    values.update(changes)
    return StructuralEntryRiskInput(**values)


def compliance(mode: str = "PAPER", **changes) -> ProgramTradingComplianceSnapshot:
    values = {
        "snapshot_id": "compliance:v31",
        "mode": mode,
        "observed_at": NOW - timedelta(days=1),
        "valid_until": NOW + timedelta(days=1),
        "strategy_id": v31_parameter_snapshot("ETF_PROXY").strategy_id,
        "software_version": "v31-test",
        "program_trading_report_confirmed": mode == "LIVE",
        "broker_permission_confirmed": mode == "LIVE",
        "licensed_market_data": True,
        "abnormal_trading_monitor_healthy": True,
        "order_rate_limit_configured": True,
        "cancellation_rate_monitor_configured": True,
    }
    values.update(changes)
    return ProgramTradingComplianceSnapshot(**values)


def test_v31_parameters_are_separate_and_live_disabled() -> None:
    etf = v31_parameter_snapshot("ETF_PROXY")
    individual = v31_parameter_snapshot("INDIVIDUAL_THREE_PROGRAM")
    assert etf.parameter_set_id != individual.parameter_set_id
    assert etf.parent_v3_parameter_set_id != individual.parent_v3_parameter_set_id
    assert etf.live_status == "LIVE_DISABLED"
    assert etf.tactical_enabled is False
    manifest = v31_parameter_manifest()
    assert manifest["live_status"] == "LIVE_DISABLED"
    assert manifest["manifest_sha256"].startswith("sha256:")
    assert set(manifest["snapshots"]) == {
        "ETF_PROXY",
        "INDIVIDUAL_THREE_PROGRAM",
    }


def test_structural_risk_sizing_uses_invalidation_gap_and_all_caps() -> None:
    parameters = v31_parameter_snapshot("ETF_PROXY")
    decision = size_structural_entry(entry(), portfolio(), parameters=parameters)
    # Per-share risk is 0.5 structure distance + max(1% price, 2 ticks)=0.6.
    assert decision.per_share_risk == Decimal("0.60")
    assert decision.position_risk_cap_quantity == 8300
    assert decision.slot_notional_cap_quantity == 18000
    assert decision.gross_exposure_cap_quantity == 90000
    assert decision.cluster_exposure_cap_quantity == 36000
    assert decision.quantity == 8300


def test_slot_gross_and_cluster_notional_caps_are_all_binding_candidates() -> None:
    parameters = v31_parameter_snapshot("ETF_PROXY")
    slot = size_structural_entry(
        entry(
            structural_invalidation_price=Decimal("9.99"),
            upstream_quantity_cap=100_000,
        ),
        portfolio(),
        parameters=parameters,
    )
    assert slot.quantity == 18000
    gross = size_structural_entry(
        entry(
            structural_invalidation_price=Decimal("9.99"),
            upstream_quantity_cap=100_000,
        ),
        portfolio(gross_exposure=Decimal("899500")),
        parameters=parameters,
    )
    assert gross.gross_exposure_cap_quantity == 0
    assert gross.quantity == 0
    cluster = size_structural_entry(
        entry(
            structural_invalidation_price=Decimal("9.99"),
            upstream_quantity_cap=100_000,
        ),
        portfolio(cluster_exposure=Decimal("359000")),
        parameters=parameters,
    )
    assert cluster.cluster_exposure_cap_quantity == 100
    assert cluster.quantity == 100


def test_caution_halves_risk_and_equal_drawdown_boundaries_are_closed() -> None:
    parameters = v31_parameter_snapshot("ETF_PROXY")
    assert classify_drawdown(Decimal("0.05"), parameters) == "CAUTION"
    assert classify_drawdown(Decimal("0.10"), parameters) == "ENTRY_HALT"
    assert classify_drawdown(Decimal("0.12"), parameters) == "DELEVERAGE"
    caution = size_structural_entry(
        entry(), portfolio(drawdown=Decimal("0.05")), parameters=parameters
    )
    assert caution.quantity == 4100
    halted = size_structural_entry(
        entry(), portfolio(drawdown=Decimal("0.10")), parameters=parameters
    )
    assert halted.quantity == 0
    assert "DRAWDOWN_ENTRY_HALT" in halted.reason_codes


def test_cluster_slot_cap_rejects_even_when_cash_exists() -> None:
    parameters = v31_parameter_snapshot("ETF_PROXY")
    decision = size_structural_entry(
        entry(),
        portfolio(cluster_occupied_slots=parameters.max_slots_per_cluster),
        parameters=parameters,
    )
    assert decision.quantity == 0
    assert "CLUSTER_SLOT_CAP_REACHED" in decision.reason_codes


def test_paper_compliance_passes_but_live_is_code_level_disabled() -> None:
    parameters = v31_parameter_snapshot("ETF_PROXY")
    paper = evaluate_program_trading_compliance(
        compliance(), as_of=NOW, parameters=parameters
    )
    assert paper.allowed
    live = evaluate_program_trading_compliance(
        compliance("LIVE"), as_of=NOW, parameters=parameters
    )
    assert not live.allowed
    assert live.highest_mode == "PAPER"
    assert "V31_LIVE_STATUS_DISABLED" in live.reason_codes


def test_missing_or_expired_compliance_data_fails_closed() -> None:
    parameters = v31_parameter_snapshot("ETF_PROXY")
    missing = evaluate_program_trading_compliance(
        compliance(licensed_market_data=False), as_of=NOW, parameters=parameters
    )
    assert not missing.allowed
    expired = evaluate_program_trading_compliance(
        compliance(valid_until=NOW - timedelta(minutes=1)),
        as_of=NOW,
        parameters=parameters,
    )
    assert not expired.allowed
    assert "COMPLIANCE_SNAPSHOT_NOT_VISIBLE_OR_EXPIRED" in expired.reason_codes
