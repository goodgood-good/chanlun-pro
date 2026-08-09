from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from chanlun.core.strict_structure.center_machine import advance_center, establish_center
from chanlun.core.strict_structure.models import CenterState, SourceKind
from chanlun.decision_support.trading_system.recursive_1m_decision import (
    Recursive1mDataFacts,
    evaluate_recursive_1m_entry,
    evaluate_recursive_1m_exit,
)
from chanlun.decision_support.trading_system.recursive_1m_research import (
    RECURSIVE_1M_RESEARCH_ID,
    Recursive1mResearchParameters,
    Recursive1mDiagnosticExecutionParameters,
    recursive_1m_parameter_manifest,
    recursive_1m_parameter_snapshot,
    recursive_1m_diagnostic_execution_snapshot,
)
from tests.core.strict_structure.helpers import (
    completed_up_center,
    structure_for,
    unit,
    valid_five_up_exit,
)
from tests.trading_system.helpers import confirmed_point


def _nine_touch_center(*, structural_level: int):
    initial = tuple(
        replace(item, source_kind=SourceKind.TREND_TYPE)
        for item in valid_five_up_exit(structural_level=structural_level)
    )
    center = establish_center(
        initial[1:4],
        structural_level,
        SourceKind.TREND_TYPE,
        entry_unit=initial[0],
    )
    assert center is not None
    center, _ = advance_center(center, initial[4])
    additions = (
        unit(
            5,
            "down",
            130,
            110,
            structural_level=structural_level,
            source_kind=SourceKind.TREND_TYPE,
        ),
        unit(
            6,
            "up",
            110,
            120,
            structural_level=structural_level,
            source_kind=SourceKind.TREND_TYPE,
        ),
        unit(
            7,
            "down",
            120,
            110,
            structural_level=structural_level,
            source_kind=SourceKind.TREND_TYPE,
        ),
        unit(
            8,
            "up",
            110,
            120,
            structural_level=structural_level,
            source_kind=SourceKind.TREND_TYPE,
        ),
        unit(
            9,
            "down",
            120,
            110,
            structural_level=structural_level,
            source_kind=SourceKind.TREND_TYPE,
        ),
        unit(
            10,
            "up",
            110,
            120,
            structural_level=structural_level,
            source_kind=SourceKind.TREND_TYPE,
        ),
    )
    for item in additions:
        center, _ = advance_center(center, item)
    center, _ = advance_center(
        center,
        unit(
            11,
            "down",
            120,
            116,
            structural_level=structural_level,
            source_kind=SourceKind.TREND_TYPE,
        ),
    )
    assert center.state is CenterState.COMPLETED
    return center


def _eligible_fixture():
    l0 = completed_up_center()
    l1 = _nine_touch_center(structural_level=1)
    structure = structure_for(l0, l1)
    decision_at = max(l0.available_at, l1.available_at) + timedelta(minutes=1)
    point = confirmed_point(
        "3buy",
        frequency="1m",
        level=0,
        center_id=l0.center_id,
        center_ordinal=1,
        price_basis_revision=structure.price_basis_revision,
    )
    point = replace(
        point,
        anchor_at=decision_at,
        confirmed_at=decision_at,
        available_at=decision_at,
    )
    return structure, point, decision_at


def _data_facts(*, adjustment: bool = True) -> Recursive1mDataFacts:
    return Recursive1mDataFacts(
        complete_contiguous_interval=True,
        point_in_time_adjustment_complete=adjustment,
        missing_data_inferred=False,
        source_fact_ids=("sha256:" + "a" * 64,),
    )


def test_parameter_paths_are_separate_and_permanently_live_disabled() -> None:
    manifest = recursive_1m_parameter_manifest()
    individual = manifest["snapshots"]["INDIVIDUAL_THREE_PROGRAM"]
    etf = manifest["snapshots"]["ETF_PROXY"]

    assert manifest["research_id"] == RECURSIVE_1M_RESEARCH_ID
    assert individual["parameter_set_id"] != etf["parameter_set_id"]
    assert manifest["highest_status"] == "RESEARCH_ONLY"
    assert manifest["live_status"] == "LIVE_DISABLED"
    assert etf["parameters"]["level_mapping"] == {
        "L0": "1m",
        "L1": "5m-derived",
        "L2": "30m-derived",
    }
    assert etf["parameters"]["strategic_slot_fraction"] == "0.1350"
    assert etf["parameters"]["strategic_exit_rule"] == (
        "L0_THIRD_SELL_ONLY_OTHER_V3_EXITS_UNRESOLVED"
    )
    assert etf["parameters"]["full_system_eligible"] is False
    diagnostic = manifest["diagnostic_execution"]
    assert diagnostic["parameters"]["performance_evaluable"] is False
    assert diagnostic["parameters"]["fact_grade"] == (
        "ASSUMPTION_ONLY_NOT_BROKER_VINTAGE"
    )


def test_research_contract_rejects_live_or_tactical_enablement() -> None:
    with pytest.raises(ValueError, match="contract changed"):
        Recursive1mResearchParameters("ETF_PROXY", live_status="LIVE_ENABLED")
    with pytest.raises(ValueError, match="contract changed"):
        Recursive1mResearchParameters(
            "ETF_PROXY",
            tactical_rule="ENABLED",
        )
    diagnostic = recursive_1m_diagnostic_execution_snapshot()
    with pytest.raises(ValueError, match="diagnostic execution contract changed"):
        Recursive1mDiagnosticExecutionParameters(
            diagnostic.research_parameter_set_id,
            performance_evaluable=True,
        )


def test_shared_decision_accepts_component_but_never_full_system() -> None:
    structure, point, decision_at = _eligible_fixture()
    decision = evaluate_recursive_1m_entry(
        point=point,
        structure=structure,
        observed_at=decision_at,
        parameters=recursive_1m_parameter_snapshot("ETF_PROXY"),
        data_facts=_data_facts(),
    )

    assert decision.component_eligible is True
    assert decision.full_system_eligible is False
    assert decision.active_expansion_ids == ()
    assert decision.l1_context_ids
    assert decision.l2_context_ids
    assert decision.rejected_reason_codes == ()
    assert decision.unresolved_components == (
        "UNRESOLVED_LOWER_LEVEL_LOCATOR_BELOW_L0_1M",
        "UNRESOLVED_TACTICAL_LAYER_BELOW_L0_1M",
    )


def test_late_higher_context_cannot_retroactively_accept_old_l0_point() -> None:
    structure, point, decision_at = _eligible_fixture()
    old_point = replace(
        point,
        anchor_at=decision_at - timedelta(days=1),
        confirmed_at=decision_at - timedelta(days=1),
        available_at=decision_at - timedelta(days=1),
    )

    decision = evaluate_recursive_1m_entry(
        point=old_point,
        structure=structure,
        observed_at=decision_at,
        parameters=recursive_1m_parameter_snapshot("ETF_PROXY"),
        data_facts=_data_facts(),
    )

    assert decision.component_eligible is False
    assert "REJECT_L2_CONTEXT_MISSING_AT_ENTRY" in decision.rejected_reason_codes


def test_future_point_is_rejected() -> None:
    structure, point, decision_at = _eligible_fixture()
    with pytest.raises(ValueError, match="not yet observable"):
        evaluate_recursive_1m_entry(
            point=replace(
                point,
                anchor_at=decision_at + timedelta(minutes=1),
                confirmed_at=decision_at + timedelta(minutes=1),
                available_at=decision_at + timedelta(minutes=1),
            ),
            structure=structure,
            observed_at=decision_at,
            parameters=recursive_1m_parameter_snapshot("ETF_PROXY"),
            data_facts=_data_facts(),
        )


def test_missing_point_in_time_adjustment_fails_closed() -> None:
    structure, point, decision_at = _eligible_fixture()
    decision = evaluate_recursive_1m_entry(
        point=point,
        structure=structure,
        observed_at=decision_at,
        parameters=recursive_1m_parameter_snapshot("ETF_PROXY"),
        data_facts=_data_facts(adjustment=False),
    )

    assert decision.component_eligible is False
    assert "REJECT_PIT_ADJUSTMENT_UNAVAILABLE" in decision.rejected_reason_codes


def test_shared_exit_promotes_only_causal_l0_third_sell() -> None:
    structure, entry, decision_at = _eligible_fixture()
    exit_at = decision_at + timedelta(days=2)
    sell = confirmed_point(
        "3sell",
        frequency="1m",
        level=0,
        price_basis_revision=structure.price_basis_revision,
        minutes_after=int((exit_at - entry.anchor_at).total_seconds() // 60),
    )
    sell = replace(
        sell,
        anchor_at=exit_at,
        confirmed_at=exit_at,
        available_at=exit_at,
    )

    decision = evaluate_recursive_1m_exit(
        point=sell,
        observed_at=exit_at,
        parameters=recursive_1m_parameter_snapshot("ETF_PROXY"),
        data_facts=_data_facts(),
        cycle_id="cycle:test",
        position_opened_at=decision_at + timedelta(minutes=1),
        position_price_basis_revision=structure.price_basis_revision,
        position_quantity=1000,
    )

    assert decision.exit_eligible is True
    assert decision.quantity == 1000
    assert decision.persistence == "PERSISTENT_EXIT"
    assert decision.full_system_eligible is False
    assert decision.rejected_reason_codes == ()

    not_sell = evaluate_recursive_1m_exit(
        point=replace(sell, point_type="2sell"),
        observed_at=exit_at,
        parameters=recursive_1m_parameter_snapshot("ETF_PROXY"),
        data_facts=_data_facts(),
        cycle_id="cycle:test",
        position_opened_at=decision_at + timedelta(minutes=1),
        position_price_basis_revision=structure.price_basis_revision,
        position_quantity=1000,
    )
    assert not_sell.exit_eligible is False
    assert "REJECT_NOT_L0_THIRD_SELL" in not_sell.rejected_reason_codes
