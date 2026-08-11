from dataclasses import replace
from decimal import Decimal

import pytest

from chanlun.decision_support.trading_system.data_audit import (
    DataContractEvidence,
    audit_bar_proxy_data_contract,
    audit_data_contract,
)
from chanlun.decision_support.trading_system.parameters import (
    LIVE_STATUS,
    etf_parameter_snapshot,
    individual_parameter_snapshot,
    parameter_snapshot_manifest,
)


def complete_evidence() -> DataContractEvidence:
    return DataContractEvidence(
        one_minute_available=True,
        five_minute_from_same_one_minute_source=True,
        thirty_minute_from_same_one_minute_source=True,
        daily_from_same_source=True,
        weekly_from_completed_daily=True,
        monthly_from_completed_daily=True,
        completed_bar_enforcement=True,
        point_in_time_adjustment_factors=True,
        point_in_time_security_master=True,
        point_in_time_sector_membership=True,
        point_in_time_suspension_st_limits=True,
        delisting_and_continuity_events=True,
        point_in_time_corporate_actions=True,
        point_in_time_fundamental_research=True,
        point_in_time_market_cap_and_peer_sets=True,
        t_plus_one_and_sellable_quantity=True,
        effective_fee_schedule=True,
        buy_sell_quantity_increments=True,
        historical_quotes_and_trades=True,
        frozen_broker_latency=True,
        survivorship_free_universe=True,
        missing_data_retained_as_rejection=True,
        historical_quotes_for_selection=True,
    )


def test_selection_paths_have_separate_immutable_snapshots() -> None:
    individual = individual_parameter_snapshot()
    etf = etf_parameter_snapshot()
    assert individual.parameter_set_id != etf.parameter_set_id
    assert individual.live_status == etf.live_status == LIVE_STATUS
    manifest = parameter_snapshot_manifest()
    assert set(manifest["snapshots"]) == {
        "INDIVIDUAL_THREE_PROGRAM",
        "ETF_PROXY",
    }


def test_any_frozen_parameter_change_requires_a_new_strategy_identity() -> None:
    snapshot = individual_parameter_snapshot()
    with pytest.raises(ValueError, match="frozen parameters changed"):
        replace(snapshot, valid_quote_coverage_min=Decimal("0.98"))


def test_complete_point_in_time_data_is_full_system_eligible_but_not_live() -> None:
    result = audit_data_contract(complete_evidence())
    assert result.eligibility == "FULL_SYSTEM_ELIGIBLE"
    assert result.full_system_pnl_allowed is True
    assert individual_parameter_snapshot().live_status == "LIVE_DISABLED"


def test_missing_selection_and_tick_evidence_is_component_only_without_pnl() -> None:
    evidence = replace(
        complete_evidence(),
        point_in_time_fundamental_research=False,
        point_in_time_market_cap_and_peer_sets=False,
        point_in_time_sector_membership=False,
        historical_quotes_and_trades=False,
        frozen_broker_latency=False,
    )
    result = audit_data_contract(evidence)
    assert result.eligibility == "COMPONENT_ONLY"
    assert result.pnl_evaluation_allowed is False
    assert "point_in_time_fundamental_research" in result.full_system_failures
    assert "strict_historical_fill_validation_unavailable" in result.warnings


def test_missing_causal_adjustment_downgrades_to_research_only() -> None:
    result = audit_data_contract(
        replace(complete_evidence(), point_in_time_adjustment_factors=False)
    )
    assert result.eligibility == "RESEARCH_ONLY"
    assert result.pnl_evaluation_allowed is False
    assert result.full_system_pnl_allowed is False


def test_bar_proxy_waives_only_tick_and_latency_evidence() -> None:
    evidence = replace(
        complete_evidence(),
        historical_quotes_and_trades=False,
    )
    result = audit_bar_proxy_data_contract(evidence)
    assert result.execution_mode == "BAR_CAUSAL_PROXY"
    assert result.eligibility == "FULL_SYSTEM_ELIGIBLE"
    assert result.pnl_evaluation_allowed is True
    assert result.waived_requirements == ("historical_trade_prints",)
    assert "bar_proxy_is_not_tick_equivalent" in result.warnings


def test_bar_proxy_does_not_waive_fees_t1_or_point_in_time_adjustment() -> None:
    evidence = replace(
        complete_evidence(),
        historical_quotes_and_trades=False,
        frozen_broker_latency=False,
        point_in_time_adjustment_factors=False,
        t_plus_one_and_sellable_quantity=False,
        effective_fee_schedule=False,
    )
    result = audit_bar_proxy_data_contract(evidence)
    assert result.eligibility == "RESEARCH_ONLY"
    assert result.pnl_evaluation_allowed is False
    assert result.component_failures == (
        "point_in_time_adjustment_factors",
        "t_plus_one_and_sellable_quantity",
        "effective_fee_schedule",
    )


def test_bar_proxy_still_requires_historical_quotes_for_selection() -> None:
    evidence = replace(
        complete_evidence(),
        historical_quotes_and_trades=False,
        historical_quotes_for_selection=False,
    )
    result = audit_bar_proxy_data_contract(evidence)
    assert result.eligibility == "COMPONENT_ONLY"
    assert result.full_system_pnl_allowed is False
    assert "historical_quotes_for_selection" in result.full_system_failures
