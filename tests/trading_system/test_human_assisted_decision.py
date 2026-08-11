from __future__ import annotations

from dataclasses import replace

import pytest

from chanlun.decision_support.trading_system.higher_timeframe_gate import (
    HigherTimeframeGateBundle,
    HigherTimeframeGateEvidence,
    HigherTimeframePeriodDiagnostic,
)
from chanlun.decision_support.trading_system.etf_proxy_facts import (
    RiskMappingSupplyFacts,
)
from chanlun.decision_support.trading_system.human_assisted_decision import (
    HumanAssistedDecisionCore,
    MONITOR_ONLY_BUY_REASON_CODE,
    replay_human_assisted_bundles,
    validate_human_assisted_contract_document,
    validate_signal_decision_document,
)
from tests.trading_system.helpers import confirmed_point, deterministic_bundle


def _gate(
    subject: str,
    gate: str,
    *,
    reason_codes: tuple[str, ...] = (),
    period_diagnostics: tuple[HigherTimeframePeriodDiagnostic, ...] = (),
) -> HigherTimeframeGateEvidence:
    return HigherTimeframeGateEvidence(
        subject=subject,
        observed_at=deterministic_bundle().as_of,
        monthly="NONE" if gate == "GREEN" else "PEN_RISK_CONFIRMED",
        weekly="NONE",
        daily="NONE",
        gate=gate,
        grade="RESEARCH_ONLY",
        snapshot_id=f"snapshot:{subject}:{gate}",
        source_revision=f"source:{subject}:{gate}",
        reason_codes=reason_codes,
        period_diagnostics=period_diagnostics,
    )


def _period_diagnostics(subject: str) -> tuple[HigherTimeframePeriodDiagnostic, ...]:
    observed_at = deterministic_bundle().as_of
    unresolved_supply = RiskMappingSupplyFacts(
        classification="NO_LOWER_POINT_EVIDENCE",
        lower_structure_available=True,
        point_evidence_count=0,
        point_type_counts=(
            ("1sell", 0),
            ("2sell", 0),
            ("3sell", 0),
            ("3buy", 0),
        ),
        completed_sell12_count=0,
        in_top_interval_sell12_count=0,
        completed_in_top_interval_sell12_count=0,
        incomplete_in_top_interval_sell12_count=0,
        outside_top_interval_sell12_count=0,
        highest_candidate_center_count=0,
        point_evidence=(),
        diagnostic_buy_point_type_counts=(("1buy", 0), ("2buy", 0)),
        diagnostic_buy_point_evidence=(),
    )
    return tuple(
        HigherTimeframePeriodDiagnostic(
            period=period,
            state="FORMED_UNRESOLVED" if period == "D" else "NONE",
            completed_bar_count=count,
            evidence_bar_end=observed_at if period == "D" else None,
            active_top_interval=(observed_at, observed_at) if period == "D" else None,
            mapping_unique=period != "D",
            mapped_center_id=None,
            mapping_candidate_ids=(),
            blocker_codes=("NO_COMPLETED_LOWER_1SELL_2SELL_CENTER_IN_TOP_FRACTAL",)
            if period == "D"
            else (),
            warning_codes=(),
            source_revision=f"source:{subject}:{period}",
            mapping_supply=unresolved_supply if period == "D" else None,
        )
        for period, count in (("M", 12), ("W", 51), ("D", 243))
    )


def test_page_and_historical_replay_use_identical_decision_documents() -> None:
    core = HumanAssistedDecisionCore()
    bundle = deterministic_bundle()
    selection_sources = ("QMT_SECTOR_TRIGGER",)

    page = core.decision_documents(
        bundle,
        name=None,
        selection_sources=selection_sources,
    )
    historical = replay_human_assisted_bundles(
        (bundle,),
        core=core,
        selection_sources_by_code={bundle.code: selection_sources},
    )

    assert historical == ((bundle.code, page),)
    assert page
    assert all(row["decision_core_id"] == core.contract_id for row in page)
    assert all(row["sector_triggered"] is True for row in page)
    assert all(
        validate_signal_decision_document(row) == row["decision_document_id"]
        for row in page
    )
    assert core.contract.document()["live_status"] == "LIVE_DISABLED"


def test_sector_selection_scope_is_shared_and_hash_bound() -> None:
    core = HumanAssistedDecisionCore()
    bundle = deterministic_bundle()

    monitor_bundle = replace(bundle, selection_sources=())
    [monitor_evaluated] = core.evaluate_symbol(monitor_bundle)
    [monitor] = core.decision_documents(monitor_bundle)
    [triggered] = core.decision_documents(
        bundle,
        selection_sources=("QMT_SECTOR_TRIGGER",),
    )

    assert monitor["selection_sources"] == ["INCREMENTAL_SCAN_SCOPE"]
    assert monitor["sector_triggered"] is False
    assert monitor["monitor_only"] is True
    assert monitor_evaluated.entry is not None
    assert monitor_evaluated.entry.allowed is False
    assert monitor_evaluated.entry.risk_multiplier == 0
    assert MONITOR_ONLY_BUY_REASON_CODE in monitor_evaluated.entry.reason_codes
    assert monitor["entry_allowed"] is False
    assert monitor["risk_multiplier"] == "0"
    assert MONITOR_ONLY_BUY_REASON_CODE in monitor["decision_reasons"]
    assert triggered["sector_triggered"] is True
    assert triggered["monitor_only"] is False
    assert triggered["decision_document_id"] != monitor["decision_document_id"]


def test_page_explanation_fields_do_not_change_shared_decision_identity() -> None:
    core = HumanAssistedDecisionCore()
    [document] = core.decision_documents(
        deterministic_bundle(),
        selection_sources=("QMT_SECTOR_TRIGGER",),
    )
    identity = validate_signal_decision_document(document)

    document["chart_urls"] = {"30m": "/chart/30m"}
    document["higher_timeframe_risk"]["symbol_session_evidence"] = {
        "status": "EXPLANATION_ONLY"
    }

    assert validate_signal_decision_document(document) == identity

    document["entry_allowed"] = not document["entry_allowed"]
    with pytest.raises(ValueError, match="decision identity changed"):
        validate_signal_decision_document(document)


def test_decision_core_identity_is_stable_and_parameter_bound() -> None:
    first = HumanAssistedDecisionCore()
    second = HumanAssistedDecisionCore()

    assert first.contract_id == second.contract_id
    assert first.contract_id.startswith("sha256:")
    assert first.contract.human_confirmation_required is True
    assert first.contract.automated_order_authorized is False
    assert first.contract.stroke_mode == "strict-cl-k-distance"
    assert first.contract.strict_base_profile_id == ("chanlun-source-faithful-base")
    assert first.contract.strict_base_profile_revision.startswith("sha256:")
    assert first.contract.structure_scope == "physical-timeframe-recursive"
    assert first.contract.recursive_structure_allowed is True
    assert first.contract.physical_structure_frequencies == (
        "d",
        "30m",
        "5m",
        "1m",
    )
    document = first.contract.document()
    assert document["policy"]["minimum_tick"] == "0.01"
    assert validate_human_assisted_contract_document(document) == (first.contract_id)


def test_daily_physical_structure_can_block_new_buy_with_recursive_graph() -> None:
    core = HumanAssistedDecisionCore()
    bundle = replace(
        deterministic_bundle(),
        daily_direction="down",
        daily_points=(confirmed_point("1sell", frequency="d"),),
        physical_timeframe_recursive=True,
    )

    [decision] = core.evaluate_symbol(bundle)
    [document] = core.decision_documents(bundle)

    assert decision.entry is not None and decision.entry.allowed is False
    assert decision.technical_entry_allowed is False
    assert "daily_structure_hostile" in decision.entry.reason_codes
    assert document["context_d"]["frequency"] == "d"
    assert document["context_d"]["hard_block"] is True
    assert document["stroke_mode"] == "strict-cl-k-distance"
    assert document["recursive_structure_used"] is True
    assert document["physical_timeframe_recursive"] is True


def test_mwd_gate_keeps_candidate_visible_but_blocks_non_green_entry() -> None:
    core = HumanAssistedDecisionCore()
    bundle = replace(
        deterministic_bundle(),
        higher_timeframe_gates=HigherTimeframeGateBundle(
            market=_gate("MARKET", "GREEN"),
            sector=_gate("QMT:GICS3:bank", "GREEN"),
            symbol=_gate("SZ.000001", "RED"),
        ),
        enforce_higher_timeframe_entry_gate=True,
    )

    [decision] = core.evaluate_symbol(bundle)
    [document] = core.decision_documents(bundle)

    assert decision.technical_entry_allowed is True
    assert decision.entry is not None and decision.entry.allowed is False
    assert document["technical_entry_allowed"] is True
    assert document["entry_allowed"] is False
    assert document["higher_timeframe_risk"] == {
        "market_gate": "GREEN",
        "sector_gate": "GREEN",
        "symbol_gate": "RED",
        "market_states": {"M": "NONE", "W": "NONE", "D": "NONE"},
        "sector_states": {"M": "NONE", "W": "NONE", "D": "NONE"},
        "symbol_states": {
            "M": "PEN_RISK_CONFIRMED",
            "W": "NONE",
            "D": "NONE",
        },
        "market_reason_codes": [],
        "sector_reason_codes": [],
        "symbol_reason_codes": [],
        "reason_codes": [],
        "market_period_diagnostics": [],
        "sector_period_diagnostics": [],
        "symbol_period_diagnostics": [],
        "new_entry_requires_all_green": True,
    }


def test_mwd_evidence_keeps_market_and_symbol_causes_separate() -> None:
    core = HumanAssistedDecisionCore()
    market_reason = "MARKET_MAPPING_UNRESOLVED"
    symbol_reason = "SYMBOL_ONE_MINUTE_SESSION_MISSING"
    bundle = replace(
        deterministic_bundle(),
        higher_timeframe_gates=HigherTimeframeGateBundle(
            market=_gate(
                "MARKET",
                "AMBER",
                reason_codes=(market_reason,),
                period_diagnostics=_period_diagnostics("MARKET"),
            ),
            sector=_gate("QMT:GICS3:bank", "GREEN"),
            symbol=_gate(
                "SZ.000001",
                "UNRESOLVED",
                reason_codes=(symbol_reason,),
                period_diagnostics=_period_diagnostics("SZ.000001"),
            ),
        ),
        enforce_higher_timeframe_entry_gate=True,
    )

    [decision] = core.evaluate_symbol(bundle)
    [document] = core.decision_documents(bundle)
    risk = document["higher_timeframe_risk"]

    assert decision.market_higher_timeframe_reason_codes == (market_reason,)
    assert decision.symbol_higher_timeframe_reason_codes == (symbol_reason,)
    assert decision.higher_timeframe_reason_codes == (
        market_reason,
        symbol_reason,
    )
    assert risk["market_reason_codes"] == [market_reason]
    assert risk["symbol_reason_codes"] == [symbol_reason]
    assert risk["reason_codes"] == [market_reason, symbol_reason]
    assert [row["period"] for row in risk["market_period_diagnostics"]] == [
        "M",
        "W",
        "D",
    ]
    daily = risk["symbol_period_diagnostics"][2]
    assert daily["completed_bar_count"] == 243
    assert daily["mapping_unique"] is False
    assert daily["blocker_codes"] == [
        "NO_COMPLETED_LOWER_1SELL_2SELL_CENTER_IN_TOP_FRACTAL"
    ]


def test_unconverged_warmup_blocks_entry_without_hiding_technical_candidate() -> None:
    core = HumanAssistedDecisionCore()
    bundle = replace(
        deterministic_bundle(),
        higher_timeframe_gates=HigherTimeframeGateBundle(
            market=_gate("MARKET", "GREEN"),
            sector=_gate("QMT:GICS3:bank", "GREEN"),
            symbol=_gate("SZ.000001", "GREEN"),
        ),
        enforce_higher_timeframe_entry_gate=True,
        warmup_converged=False,
        warmup_reason_codes=("30M:WARMUP_TAIL_DIVERGED",),
        warmup_by_frequency=(("30m", False, 1600, 1067),),
        warmup_difference_codes_by_frequency=(("30m", ("WARMUP_DIRECTION_CHANGED",)),),
        enforce_warmup_entry_gate=True,
    )

    [decision] = core.evaluate_symbol(bundle)
    [document] = core.decision_documents(bundle)

    assert decision.technical_entry_allowed is True
    assert decision.entry is not None and decision.entry.allowed is False
    assert "WARMUP_CONVERGENCE_GATE_FAILED" in decision.entry.reason_codes
    assert document["warmup"] == {
        "converged": False,
        "by_frequency": [
            {
                "frequency": "30m",
                "converged": False,
                "full_bar_count": 1600,
                "suffix_bar_count": 1067,
            }
        ],
        "reason_codes": ["30M:WARMUP_TAIL_DIVERGED"],
        "difference_codes_by_frequency": [
            {
                "frequency": "30m",
                "difference_codes": ["WARMUP_DIRECTION_CHANGED"],
            }
        ],
        "required_for_new_entry": True,
    }
