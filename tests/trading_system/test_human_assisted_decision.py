from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from chanlun.core.strict_structure.current_events import TerminalSegmentReference
from chanlun.core.strict_structure.models import SourceKind
from chanlun.decision_support.trading_system.higher_timeframe_gate import (
    HigherTimeframeGateBundle,
    HigherTimeframeGateEvidence,
    HigherTimeframePeriodDiagnostic,
    HigherTimeframeSessionEvidence,
)
from chanlun.decision_support.trading_system.etf_proxy_facts import (
    RiskMappingSupplyFacts,
)
from chanlun.decision_support.trading_system.human_assisted_decision import (
    FIVE_MINUTE_SETUP_SELECTION_REVISION,
    FORMAL_SELECTION_REQUIRED_REASON_CODE,
    HumanAssistedDecisionCore,
    replay_human_assisted_bundles,
    signal_decision_document_id,
    validate_human_assisted_contract_document,
    validate_signal_decision_document,
)
from chanlun.decision_support.trading_system.lifecycle import (
    STRUCTURE_INVALIDATED_REASON_CODE,
    lifecycle_state_from_signal_document,
)
from chanlun.decision_support.trading_system.models import TradingPolicy
from chanlun.decision_support.trading_system.qmt_same_base_stream import (
    QmtMinuteSessionIssue,
)
from chanlun.decision_support.trading_system.signal_alignment import (
    unified_signal_alignment_contract,
)
from tests.trading_system.helpers import (
    confirmed_point,
    deterministic_bundle,
    eligible_sector,
    provisional_point,
)


def test_confirmed_buy_without_one_minute_remains_visible_but_not_executable() -> None:
    bundle = replace(
        deterministic_bundle(),
        five_points=(confirmed_point("2buy", minutes_after=295),),
        one_points=(),
        entry_execution_boundaries=(),
    )

    [document] = HumanAssistedDecisionCore().decision_documents(bundle)

    assert document["technical_entry_allowed"] is True
    assert document["entry_allowed"] is False
    assert document["position_recommendation"]["status"] == "NOT_ACTIONABLE"
    assert document["position_recommendation"]["basis"] == (
        "ONE_MINUTE_SEGMENT_DIFFERENCE_REQUIRED"
    )
    assert document["position_recommendation"]["recommended_ratio"] is None
    assert document["execution_profile"]["recommendation"] == (
        "WAITING_SEGMENT_DIFFERENCE"
    )
    assert document["execution_profile"]["segment_difference_status"] == (
        "WAITING_ONE_MINUTE"
    )
    assert document["execution_profile"]["precise_execution_ready"] is False
    assert "one_minute_not_confirmed" in document["decision_reasons"]
    assert validate_signal_decision_document(document) == document["decision_document_id"]


def test_preconfirmation_divergence_is_observable_but_not_formal_decision() -> None:
    base = deterministic_bundle()
    candidate = provisional_point("3buy")
    candidate = replace(
        candidate,
        terminal_segment=TerminalSegmentReference(
            role="latest_unfinished",
            structural_level=0,
            unit_id="segment:5m:forming",
            source_kind=SourceKind.SEGMENT,
            direction="down",
            state="forming",
            market_start=candidate.anchor_at - timedelta(minutes=30),
            market_end=candidate.anchor_at,
            available_at=candidate.available_at,
        ),
    )
    divergence = confirmed_point(
        "1buy",
        frequency="1m",
        minutes_after=-5,
    )
    divergence = replace(
        divergence,
        terminal_segment=TerminalSegmentReference(
            role="latest_completed",
            structural_level=0,
            unit_id="segment:1m:divergence",
            source_kind=SourceKind.SEGMENT,
            direction="down",
            state="locked",
            market_start=divergence.anchor_at - timedelta(minutes=1),
            market_end=divergence.anchor_at,
            available_at=divergence.available_at,
        ),
    )
    bundle = replace(
        base,
        five_points=(candidate,),
        one_points=(divergence,),
        opposite_points=(),
        entry_execution_boundaries=(),
    )

    [document] = HumanAssistedDecisionCore().decision_documents(bundle)

    assert document["lifecycle_stage"] == "approaching"
    assert document["segment_difference_1m"] is None
    assert len(document["preconfirmation_divergences_1m"]) == 1
    assert document["preconfirmation_divergences_1m"][0]["point_id"] == (
        divergence.point_id
    )
    assert document["entry_allowed"] is False
    assert document["exit_allowed"] is False
    assert document["execution_profile"]["structure_signal_confirmed"] is False
    assert document["execution_profile"]["segment_difference_status"] == (
        "STRUCTURE_PENDING"
    )
    assert signal_decision_document_id(document) == document["decision_document_id"]
    assert validate_signal_decision_document(document) == document[
        "decision_document_id"
    ]


def test_current_five_minute_buy_keeps_waiting_for_one_minute_after_eleven_minutes() -> None:
    base = deterministic_bundle()
    setup = base.five_points[0]
    bundle = replace(
        base,
        as_of=setup.available_at + timedelta(minutes=11),
        one_points=(),
        entry_execution_boundaries=(),
    )

    [document] = HumanAssistedDecisionCore().decision_documents(bundle)

    assert document["technical_entry_allowed"] is True
    assert document["entry_allowed"] is False
    assert document["position_recommendation"]["status"] == "NOT_ACTIONABLE"
    assert document["position_recommendation"]["recommended_percent"] is None
    profile = document["execution_profile"]
    assert profile["recommendation"] == "WAITING_SEGMENT_DIFFERENCE"
    assert profile["segment_difference_status"] == "WAITING_ONE_MINUTE"
    assert profile["segment_difference_ready"] is False
    assert profile["precise_execution_ready"] is False
    assert profile["hard_block_reason_codes"] == []
    assert validate_signal_decision_document(document) == document[
        "decision_document_id"
    ]


def test_hard_block_does_not_turn_current_five_minute_setup_into_time_expiry() -> None:
    base = deterministic_bundle()
    setup = base.five_points[0]
    bundle = replace(
        base,
        as_of=setup.available_at + timedelta(minutes=11),
        one_points=(),
        entry_execution_boundaries=(),
        opposite_points=(confirmed_point("1sell", minutes_after=295),),
    )

    [document] = HumanAssistedDecisionCore().decision_documents(bundle)

    profile = document["execution_profile"]
    assert profile["recommendation"] == "BLOCKED"
    assert profile["segment_difference_status"] == "WAITING_ONE_MINUTE"
    assert "same_or_higher_structure_conflict" in profile["hard_block_reason_codes"]
    assert document["position_recommendation"]["reason_codes"] == [
        "HARD_BLOCKED_NO_TRADE"
    ]
    assert validate_signal_decision_document(document) == document[
        "decision_document_id"
    ]


def test_persisted_one_minute_boundary_reports_expired_at_current_bundle_time() -> None:
    core = HumanAssistedDecisionCore()
    first_bundle = deterministic_bundle()
    [first_document] = core.decision_documents(first_bundle)
    lifecycle, trigger = lifecycle_state_from_signal_document(first_document)
    assert trigger is not None
    [boundary] = first_bundle.entry_execution_boundaries
    expired_bundle = replace(
        first_bundle,
        as_of=boundary.entry_valid_until,
        previous_lifecycles=(lifecycle,),
        previous_trigger_points=(trigger,),
    )

    [expired_document] = core.decision_documents(expired_bundle)

    assert "ONE_MINUTE_SEGMENT_BOUNDARY_EXPIRED" in expired_document[
        "decision_reasons"
    ]
    assert expired_document["execution_profile"]["segment_difference_status"] == (
        "BOUNDARY_EXPIRED"
    )
    assert expired_document["execution_profile"]["segment_difference_ready"] is False
    assert expired_document["execution_profile"]["precise_execution_ready"] is False
    assert validate_signal_decision_document(expired_document) == expired_document[
        "decision_document_id"
    ]


def _gate(
    subject: str,
    gate: str,
    *,
    reason_codes: tuple[str, ...] = (),
    period_diagnostics: tuple[HigherTimeframePeriodDiagnostic, ...] = (),
    session_evidence: HigherTimeframeSessionEvidence | None = None,
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
        session_evidence=session_evidence,
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


def test_structure_invalidation_reason_is_carried_by_decision_document() -> None:
    core = HumanAssistedDecisionCore()
    bundle = replace(deterministic_bundle(), latest_price=9.79)

    [document] = core.decision_documents(bundle)

    assert document["lifecycle_stage"] == "invalidated"
    assert document["current_price"] == 9.79
    assert document["decision_reasons"][-1] == STRUCTURE_INVALIDATED_REASON_CODE
    assert validate_signal_decision_document(document) == document[
        "decision_document_id"
    ]


def test_signal_validator_rejects_contradictory_setup_state() -> None:
    [document] = HumanAssistedDecisionCore().decision_documents(
        deterministic_bundle()
    )

    document["setup_5m"]["formation_state"] = "formed"

    with pytest.raises(ValueError, match="formation_state"):
        validate_signal_decision_document(document)


def test_signal_validator_rejects_a_contradictory_recommendation_label() -> None:
    [document] = HumanAssistedDecisionCore().decision_documents(
        deterministic_bundle()
    )
    document["execution_profile"]["recommendation_label"] = (
        "等待5分钟买卖点正式确认"
    )

    with pytest.raises(ValueError, match="recommendation label changed"):
        validate_signal_decision_document(document)


def test_geometric_candidate_has_a_distinct_non_actionable_state() -> None:
    candidate = replace(
        provisional_point("3sell"),
        evidence_codes=(
            "unfinished_segment_participates",
            "provisional_center_completion",
            "core_boundary_held",
        ),
    )
    bundle = replace(
        deterministic_bundle(),
        five_points=(candidate,),
        one_points=(),
        opposite_points=(),
        entry_execution_boundaries=(),
    )

    [document] = HumanAssistedDecisionCore().decision_documents(bundle)

    assert document["lifecycle_stage"] == "formed"
    assert document["setup_5m"]["formation_state"] == "geometry_ready"
    assert document["execution_profile"]["recommendation"] == (
        "GEOMETRY_AWAITING_CONFIRMATION"
    )
    assert document["execution_profile"]["recommendation_label"] == (
        "5分钟买卖点仅为几何候选，尚未达到操作确认"
    )
    assert document["position_recommendation"]["basis"] == (
        "GEOMETRIC_5M_CANDIDATE_AWAITING_CONFIRMATION"
    )
    assert "five_minute_geometry_candidate_awaiting_confirmation" in document[
        "decision_reasons"
    ]
    assert validate_signal_decision_document(document) == document[
        "decision_document_id"
    ]


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
    assert monitor_evaluated.entry.allowed is True
    assert str(monitor_evaluated.entry.risk_multiplier) == "1.00"
    assert "QMT_SECTOR_TRIGGER_REQUIRED" in monitor_evaluated.advisory_reason_codes
    assert monitor["entry_allowed"] is True
    assert monitor["risk_multiplier"] == "1.00"
    assert "QMT_SECTOR_TRIGGER_REQUIRED" in monitor["decision_reasons"]
    assert monitor["execution_profile"]["recommendation"] == "CAUTION"
    assert triggered["sector_triggered"] is True
    assert triggered["monitor_only"] is False
    assert triggered["formal_selection"]["status"] == "PASS"
    assert triggered["decision_document_id"] != monitor["decision_document_id"]


def test_sector_trigger_cannot_replace_signed_three_program_research() -> None:
    core = HumanAssistedDecisionCore()
    bundle = replace(deterministic_bundle(), selection_research=None)

    [decision] = core.evaluate_symbol(bundle)
    [document] = core.decision_documents(bundle)

    assert decision.technical_entry_allowed is True
    assert decision.entry is not None and decision.entry.allowed is True
    assert FORMAL_SELECTION_REQUIRED_REASON_CODE in decision.advisory_reason_codes
    assert document["sector_triggered"] is True
    assert document["monitor_only"] is True
    assert document["formal_selection"]["research_status"] == "UNRESOLVED"
    assert FORMAL_SELECTION_REQUIRED_REASON_CODE in document["decision_reasons"]


def test_manual_signal_mode_does_not_read_formal_research_as_an_entry_gate() -> None:
    core = HumanAssistedDecisionCore(formal_selection_required=False)
    bundle = replace(deterministic_bundle(), selection_research=None)

    [decision] = core.evaluate_symbol(bundle)
    [document] = core.decision_documents(bundle)

    assert decision.technical_entry_allowed is True
    assert decision.entry is not None and decision.entry.allowed is True
    assert document["formal_selection_required"] is False
    assert document["formal_selection"]["research_status"] == "UNRESOLVED"
    assert document["monitor_only"] is False
    assert document["entry_allowed"] is True
    assert FORMAL_SELECTION_REQUIRED_REASON_CODE not in document["decision_reasons"]
    assert validate_signal_decision_document(document) == document[
        "decision_document_id"
    ]


def test_etf_path_ignores_individual_sector_blocks_but_keeps_market_symbol_gates() -> None:
    core = HumanAssistedDecisionCore(formal_selection_required=False)
    hostile_sector = replace(
        eligible_sector(),
        eligible=False,
        hard_block=True,
        regime="hostile",
        reason_codes=("ETF_INDUSTRY_CLASSIFICATION_NOT_APPLICABLE",),
    )
    bundle = replace(
        deterministic_bundle(),
        sector=hostile_sector,
        selection_path="ETF_PROXY",
        selection_research=None,
        higher_timeframe_gates=HigherTimeframeGateBundle(
            market=_gate("MARKET", "GREEN"),
            sector=_gate(hostile_sector.sector_id, "RED"),
            symbol=_gate("SZ.000001", "GREEN"),
        ),
        enforce_higher_timeframe_entry_gate=True,
    )

    [decision] = core.evaluate_symbol(bundle)
    [document] = core.decision_documents(bundle)

    assert decision.setup.sector_required is False
    assert decision.technical_entry_allowed is True
    assert decision.entry is not None and decision.entry.allowed is True
    assert document["selection_path"] == "ETF_PROXY"
    assert document["entry_allowed"] is True
    assert "sector_hostile" not in document["decision_reasons"]
    assert "HIGHER_TIMEFRAME_GATE_NOT_GREEN" not in document["decision_reasons"]


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
    with pytest.raises(
        ValueError,
        match="precise-execution contract changed|decision identity changed",
    ):
        validate_signal_decision_document(document)


def test_signal_document_rejects_recursive_context_forged_as_5m_trade() -> None:
    core = HumanAssistedDecisionCore()
    [document] = core.decision_documents(deterministic_bundle())
    document["recursive_level"] = 1
    document["setup_5m"]["recursive_level"] = 1
    document["decision_document_id"] = signal_decision_document_id(document)

    with pytest.raises(ValueError, match="physical 5m/L0"):
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
    assert first.contract.signal_alignment_parameter_set_id == (
        unified_signal_alignment_contract().parameter_set_id
    )
    assert first.contract.structure_scope == "physical-timeframe-recursive"
    assert first.contract.recursive_structure_allowed is True
    assert first.contract.five_minute_setup_selection_revision == (
        FIVE_MINUTE_SETUP_SELECTION_REVISION
    )
    assert first.contract.physical_structure_frequencies == (
        "d",
        "30m",
        "5m",
        "1m",
    )
    document = first.contract.document()
    assert document["policy"]["minimum_tick"] == "0.01"
    assert (
        document["policy"][
            "require_one_minute_segment_difference_for_precise_execution"
        ]
        is True
    )
    assert validate_human_assisted_contract_document(document) == (first.contract_id)

    stale_revision = first.contract.document()
    stale_revision["five_minute_setup_selection_revision"] = "legacy-mixed-lane"
    with pytest.raises(ValueError, match="physical structure contract changed"):
        validate_human_assisted_contract_document(stale_revision)

    document["signal_alignment_parameter_set_id"] = "sha256:" + "0" * 64
    with pytest.raises(ValueError, match="physical structure contract changed"):
        validate_human_assisted_contract_document(document)

    with pytest.raises(ValueError, match="requires independent 5m trade"):
        HumanAssistedDecisionCore(
            TradingPolicy(
                require_one_minute_segment_difference_for_precise_execution=False
            )
        )


def test_daily_physical_structure_downgrades_without_erasing_new_buy() -> None:
    core = HumanAssistedDecisionCore()
    bundle = replace(
        deterministic_bundle(),
        daily_direction="down",
        daily_points=(confirmed_point("1sell", frequency="d"),),
        physical_timeframe_recursive=True,
    )

    [decision] = core.evaluate_symbol(bundle)
    [document] = core.decision_documents(bundle)

    assert decision.entry is not None and decision.entry.allowed is True
    assert decision.technical_entry_allowed is True
    assert "daily_structure_hostile" in decision.advisory_reason_codes
    assert document["context_d"]["frequency"] == "d"
    assert document["context_d"]["hard_block"] is True
    assert document["stroke_mode"] == "strict-cl-k-distance"
    assert document["recursive_structure_used"] is True
    assert document["physical_timeframe_recursive"] is True
    assert document["execution_profile"]["context_grade"] == "C"
    assert document["execution_profile"]["recommendation"] == "CAUTION"


def test_mwd_gate_is_legacy_advisory_and_does_not_erase_entry() -> None:
    core = HumanAssistedDecisionCore()
    bundle = replace(
        deterministic_bundle(),
        higher_timeframe_gates=HigherTimeframeGateBundle(
            market=_gate("MARKET", "GREEN"),
            sector=_gate(eligible_sector().sector_id, "GREEN"),
            symbol=_gate("SZ.000001", "RED"),
        ),
        enforce_higher_timeframe_entry_gate=True,
    )

    [decision] = core.evaluate_symbol(bundle)
    [document] = core.decision_documents(bundle)

    assert decision.technical_entry_allowed is True
    assert decision.entry is not None and decision.entry.allowed is True
    assert document["technical_entry_allowed"] is True
    assert document["entry_allowed"] is True
    assert "HIGHER_TIMEFRAME_CONTEXT_NOT_GREEN" in decision.advisory_reason_codes
    assert document["execution_profile"]["recommendation"] == "CAUTION"
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
        "data_integrity_hard_block_reason_codes": [],
        "market_period_diagnostics": [],
        "sector_period_diagnostics": [],
        "symbol_period_diagnostics": [],
        "new_entry_requires_all_green": False,
    }


def test_higher_timeframe_direction_is_advisory_but_causal_data_error_blocks() -> None:
    bundle = replace(
        deterministic_bundle(),
        higher_timeframe_gates=HigherTimeframeGateBundle(
            market=_gate("MARKET", "GREEN"),
            sector=_gate(eligible_sector().sector_id, "GREEN"),
            symbol=_gate(
                "SZ.000001",
                "UNRESOLVED",
                reason_codes=("QMT_NATIVE_DAILY_AHEAD_OF_ONE_MINUTE_BASE",),
            ),
        ),
        enforce_higher_timeframe_entry_gate=True,
    )

    [decision] = HumanAssistedDecisionCore().evaluate_symbol(bundle)
    [document] = HumanAssistedDecisionCore().decision_documents(bundle)

    assert decision.technical_entry_allowed is True
    assert decision.entry is not None and decision.entry.allowed is False
    assert decision.entry.risk_multiplier == 0
    assert document["entry_allowed"] is False
    assert document["higher_timeframe_risk"][
        "data_integrity_hard_block_reason_codes"
    ] == ["QMT_NATIVE_DAILY_AHEAD_OF_ONE_MINUTE_BASE"]
    assert document["execution_profile"]["recommendation"] == "BLOCKED"


def test_one_minute_session_gap_is_advisory_for_physical_five_minute_signal() -> None:
    session_code = "QMT_ONE_MINUTE_EXPECTED_SESSION_MISSING"
    session_evidence = HigherTimeframeSessionEvidence.exact(
        (
            QmtMinuteSessionIssue(
                session=deterministic_bundle().as_of.date(),
                code=session_code,
                observed_rows=0,
                detail="trading-calendar session is absent from the QMT 1m prefix",
            ),
        )
    )
    bundle = replace(
        deterministic_bundle(),
        higher_timeframe_gates=HigherTimeframeGateBundle(
            market=_gate("MARKET", "GREEN"),
            sector=_gate(eligible_sector().sector_id, "GREEN"),
            symbol=_gate(
                "SZ.000001",
                "UNRESOLVED",
                reason_codes=(session_code,),
                session_evidence=session_evidence,
            ),
        ),
        enforce_higher_timeframe_entry_gate=True,
    )

    [decision] = HumanAssistedDecisionCore().evaluate_symbol(bundle)
    [document] = HumanAssistedDecisionCore().decision_documents(bundle)

    assert decision.technical_entry_allowed is True
    assert decision.entry is not None and decision.entry.allowed is True
    assert document["entry_allowed"] is True
    assert document["higher_timeframe_risk"][
        "data_integrity_hard_block_reason_codes"
    ] == []
    assert session_code in document["execution_profile"]["advisory_reason_codes"]
    assert session_code not in document["execution_profile"]["hard_block_reason_codes"]


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
            sector=_gate(eligible_sector().sector_id, "GREEN"),
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


def test_non_trade_period_warmup_divergence_is_advisory_only() -> None:
    core = HumanAssistedDecisionCore()
    bundle = replace(
        deterministic_bundle(),
        higher_timeframe_gates=HigherTimeframeGateBundle(
            market=_gate("MARKET", "GREEN"),
            sector=_gate(eligible_sector().sector_id, "GREEN"),
            symbol=_gate("SZ.000001", "GREEN"),
        ),
        enforce_higher_timeframe_entry_gate=True,
        warmup_converged=False,
        warmup_reason_codes=(
            "D:WARMUP_TAIL_STABLE",
            "30M:WARMUP_TAIL_DIVERGED",
            "5M:WARMUP_TAIL_STABLE",
            "1M:WARMUP_TAIL_STABLE",
        ),
        warmup_by_frequency=(
            ("d", True, 600, 400),
            ("30m", False, 1600, 1067),
            ("5m", True, 1600, 1067),
            ("1m", True, 1800, 1200),
        ),
        warmup_difference_codes_by_frequency=(
            ("d", ()),
            ("30m", ("WARMUP_DIRECTION_CHANGED",)),
            ("5m", ()),
            ("1m", ()),
        ),
        enforce_warmup_entry_gate=True,
    )

    [decision] = core.evaluate_symbol(bundle)
    [document] = core.decision_documents(bundle)

    assert decision.technical_entry_allowed is True
    assert decision.entry is not None and decision.entry.allowed is True
    assert "WARMUP_CONVERGENCE_GATE_FAILED" not in decision.entry.reason_codes
    assert "30M:WARMUP_TAIL_DIVERGED" in decision.advisory_reason_codes
    assert document["execution_profile"]["recommendation"] == "CAUTION"
    assert document["execution_profile"]["hard_blocked"] is False
    assert document["execution_profile"]["hard_block_reason_codes"] == []
    assert "30M:WARMUP_TAIL_DIVERGED" in document["execution_profile"][
        "advisory_reason_codes"
    ]
    assert document["warmup"] == {
        "converged": False,
        "by_frequency": [
            {
                "frequency": "d",
                "converged": True,
                "full_bar_count": 600,
                "suffix_bar_count": 400,
            },
            {
                "frequency": "30m",
                "converged": False,
                "full_bar_count": 1600,
                "suffix_bar_count": 1067,
            },
            {
                "frequency": "5m",
                "converged": True,
                "full_bar_count": 1600,
                "suffix_bar_count": 1067,
            },
            {
                "frequency": "1m",
                "converged": True,
                "full_bar_count": 1800,
                "suffix_bar_count": 1200,
            },
        ],
        "reason_codes": [
            "D:WARMUP_TAIL_STABLE",
            "30M:WARMUP_TAIL_DIVERGED",
            "5M:WARMUP_TAIL_STABLE",
            "1M:WARMUP_TAIL_STABLE",
        ],
        "difference_codes_by_frequency": [
            {"frequency": "d", "difference_codes": []},
            {
                "frequency": "30m",
                "difference_codes": ["WARMUP_DIRECTION_CHANGED"],
            },
            {"frequency": "5m", "difference_codes": []},
            {"frequency": "1m", "difference_codes": []},
        ],
        "required_for_new_entry": True,
    }


def test_unconverged_five_minute_warmup_blocks_only_with_five_minute_cause() -> None:
    bundle = replace(
        deterministic_bundle(),
        warmup_converged=False,
        warmup_reason_codes=(
            "D:WARMUP_TAIL_STABLE",
            "30M:WARMUP_TAIL_STABLE",
            "5M:WARMUP_TAIL_DIVERGED",
            "1M:WARMUP_TAIL_STABLE",
        ),
        warmup_by_frequency=(
            ("d", True, 600, 400),
            ("30m", True, 1600, 1067),
            ("5m", False, 1600, 1067),
            ("1m", True, 1800, 1200),
        ),
        warmup_difference_codes_by_frequency=(
            ("d", ()),
            ("30m", ()),
            ("5m", ("WARMUP_DIRECTION_CHANGED",)),
            ("1m", ()),
        ),
        enforce_warmup_entry_gate=True,
    )

    [decision] = HumanAssistedDecisionCore().evaluate_symbol(bundle)
    [document] = HumanAssistedDecisionCore().decision_documents(bundle)

    assert decision.technical_entry_allowed is True
    assert decision.entry is not None and decision.entry.allowed is False
    assert decision.entry.reason_codes[-2:] == (
        "WARMUP_CONVERGENCE_GATE_FAILED",
        "5M:WARMUP_TAIL_DIVERGED",
    )
    assert document["execution_profile"]["hard_block_reason_codes"][-2:] == [
        "WARMUP_CONVERGENCE_GATE_FAILED",
        "5M:WARMUP_TAIL_DIVERGED",
    ]
    assert "D:WARMUP_TAIL_STABLE" not in document["execution_profile"][
        "hard_block_reason_codes"
    ]


def test_lower_or_unrelated_conflict_is_advisory_not_a_hard_block() -> None:
    unrelated_sell = confirmed_point(
        "1sell",
        center_id="unrelated-center",
        minutes_after=296,
    )
    bundle = replace(
        deterministic_bundle(),
        opposite_points=(unrelated_sell,),
    )

    [decision] = HumanAssistedDecisionCore().evaluate_symbol(bundle)
    [document] = HumanAssistedDecisionCore().decision_documents(bundle)

    assert decision.conflict.hard_block is False
    assert decision.conflict.risk_only_point_ids == (unrelated_sell.point_id,)
    assert decision.entry is not None and decision.entry.allowed is True
    assert document["execution_profile"]["recommendation"] == "CAUTION"
    assert document["execution_profile"]["hard_blocked"] is False
    assert document["execution_profile"]["hard_block_reason_codes"] == []
    assert "lower_or_unrelated_structure_risk" in document["execution_profile"][
        "advisory_reason_codes"
    ]


def test_mixed_hard_and_risk_only_conflicts_do_not_duplicate_hard_reason_as_advisory() -> None:
    blocking_sell = confirmed_point("1sell", minutes_after=295)
    unrelated_sell = confirmed_point(
        "1sell",
        center_id="unrelated-center",
        minutes_after=296,
    )
    bundle = replace(
        deterministic_bundle(),
        opposite_points=(blocking_sell, unrelated_sell),
    )

    [decision] = HumanAssistedDecisionCore().evaluate_symbol(bundle)
    [document] = HumanAssistedDecisionCore().decision_documents(bundle)
    profile = document["execution_profile"]

    assert decision.conflict.hard_block is True
    assert decision.conflict.blocking_point_ids == (blocking_sell.point_id,)
    assert decision.conflict.risk_only_point_ids == (unrelated_sell.point_id,)
    assert profile["recommendation"] == "BLOCKED"
    assert "same_or_higher_structure_conflict" in profile["hard_block_reason_codes"]
    assert "same_or_higher_structure_conflict" not in profile["advisory_reason_codes"]
