from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from decimal import Decimal
import json
from zoneinfo import ZoneInfo

import pytest

from chanlun.decision_support.fingerprints import sha256_json
from chanlun.decision_support.trading_system.higher_timeframe_gate import (
    HigherTimeframePeriodDiagnostic,
    HigherTimeframeSessionEvidence,
    QMT_SECTOR_NATIVE_DAILY_RESEARCH_SOURCE_MODE,
    QMT_SECTOR_SAME_BASE_SOURCE_MODE,
    QmtSectorSameBaseCoverageEvidence,
    sector_native_daily_research_bridge_contract,
)
from chanlun.decision_support.trading_system.human_review_screening import (
    SECTOR_HIGHER_TIMEFRAME_REVIEW_EVIDENCE_SCHEMA,
    HumanReviewAlert,
    HumanReviewFeedback,
    MarketSymbolHigherTimeframeReviewEvidence,
    ReviewPriceBar,
    SectorHigherTimeframeReviewEvidence,
    SectorRankingReviewEvidence,
    append_human_review_feedback,
    evaluate_review_alert,
    human_review_alert_document,
    human_review_screening_parameters,
    load_human_review_feedback_ledger,
    market_symbol_higher_timeframe_review_evidence_from_risk,
    parse_market_symbol_higher_timeframe_review_evidence,
    parse_sector_higher_timeframe_review_evidence,
    parse_sector_ranking_review_evidence,
    review_priority,
    sector_higher_timeframe_review_evidence_from_risk,
    sector_ranking_review_evidence_from_live_sector,
    summarize_event_study,
    validate_human_review_screen_document,
)
from chanlun.decision_support.trading_system.qmt_higher_timeframe import (
    QMT_HIGHER_TIMEFRAME_WARMUP_CONVERGENCE_PARAMETER_SET_ID,
    QmtHigherTimeframeWarmupEvidence,
)
from chanlun.decision_support.trading_system.warmup_convergence import (
    WARMUP_CONVERGENCE_DIAGNOSTIC_CONTRACT_ID,
    WARMUP_CONVERGENCE_ENVELOPE_CONTRACT_ID,
    WARMUP_MAPPING_SUPPLY_DIAGNOSTIC_CONTRACT_ID,
    WarmupMappingSupplySnapshot,
    WarmupPeriodSemanticFacts,
    WarmupPrefixObservation,
    WarmupSemanticSnapshot,
    bind_warmup_convergence_diagnostic,
    bind_warmup_mapping_supply_diagnostic,
    classify_warmup_convergence_envelope,
)
from chanlun.decision_support.trading_system.qmt_native_daily_bridge import (
    QmtNativeDailyReconciliationEvidence,
)
from chanlun.decision_support.trading_system.qmt_same_base_stream import (
    QmtMinuteSessionIssue,
)
from chanlun.decision_support.trading_system.etf_proxy_facts import (
    RiskMappingPointEvidenceFacts,
    RiskMappingSupplyFacts,
)


CN = ZoneInfo("Asia/Shanghai")


def _at(day: int, hour: int = 14) -> datetime:
    return datetime(2026, 1, 1, hour, 0, tzinfo=CN) + timedelta(days=day)


def _alert() -> HumanReviewAlert:
    parameters = human_review_screening_parameters()
    return HumanReviewAlert(
        symbol="SH.600000",
        alert_type="POSSIBLE_30M_BUY",
        signal_at=_at(0, 13),
        review_available_at=_at(0),
        source_point_id="sha256:point",
        structure_snapshot_id="sha256:snapshot",
        sector_id="QMT:GICS3:bank",
        confidence="MEDIUM",
        review_priority=55,
        reference_price=Decimal("100"),
        structural_invalidation_price=Decimal("95"),
        market_risk_gate="AMBER",
        sector_risk_gate="UNRESOLVED",
        symbol_risk_gate="GREEN",
        warning_codes=("STRICT_STRUCTURE_REVIEW",),
        source_fact_ids=("sha256:point", "sha256:snapshot"),
        screening_parameter_set_id=parameters.parameter_set_id,
        signal_alignment_parameter_set_id=(
            parameters.signal_alignment_parameter_set_id
        ),
    )


def _jsonable(value: object) -> object:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    return value


def _screen_report(*, duplicate: bool = False) -> dict[str, object]:
    alert = _alert()
    row = {
        **_jsonable(asdict(alert)),
        "candidate_id": alert.candidate_id,
        "signal_lifecycle_id": alert.signal_lifecycle_id,
    }
    stable: dict[str, object] = {
        "schema": "chanlun-human-review-screen",
        "data_grade": "HUMAN_REVIEW_SCREENING",
        "highest_status": "REVIEW_REQUIRED",
        "live_status": "LIVE_DISABLED",
        "human_confirmation_required": True,
        "automated_order_authorized": False,
        "portfolio_backtest_performed": False,
        "portfolio_performance_evaluable": False,
        "orders_created": 0,
        "fills_created": 0,
        "positions_created": 0,
        "review_queue": [row, row] if duplicate else [row],
    }
    return {**stable, "content_sha256": sha256_json(stable)}


def _warmup(*, sufficient: bool) -> QmtHigherTimeframeWarmupEvidence:
    return QmtHigherTimeframeWarmupEvidence(
        required_daily_bar_count=480,
        full_daily_bar_count=480 if sufficient else 240,
        suffix_daily_bar_count=320 if sufficient else 0,
        converged=sufficient,
        reason_code=(
            "QMT_HIGHER_TIMEFRAME_WARMUP_TAIL_STABLE"
            if sufficient
            else "QMT_HIGHER_TIMEFRAME_WARMUP_HISTORY_INSUFFICIENT"
        ),
        full_signature="sha256:" + "1" * 64,
        suffix_signature=("sha256:" + "1" * 64 if sufficient else None),
    )


def _convergence(
    observed_at: datetime,
    *,
    non_monotonic: bool = True,
):
    signatures = ("a", "b", "a", "a") if non_monotonic else ("a",) * 4
    return classify_warmup_convergence_envelope(
        frequency="d",
        as_of=observed_at,
        parameter_set_id=(
            QMT_HIGHER_TIMEFRAME_WARMUP_CONVERGENCE_PARAMETER_SET_ID
        ),
        observations=tuple(
            WarmupPrefixObservation(
                bar_count=count,
                starts_at=observed_at - timedelta(days=count),
                signature_sha256="sha256:" + signature * 64,
            )
            for count, signature in zip(
                (480, 640, 800, 960),
                signatures,
            )
        ),
    )


def _semantic_convergence(observed_at: datetime):
    def snapshot(weekly_state: str) -> WarmupSemanticSnapshot:
        def period_facts(period: str, index: int) -> WarmupPeriodSemanticFacts:
            state = weekly_state if period == "W" else "NONE"
            unresolved = state != "NONE"
            return WarmupPeriodSemanticFacts(
                period=period,
                state=state,
                evidence_bar_end=observed_at - timedelta(days=index + 1),
                active_top_interval=(
                    (
                        observed_at - timedelta(days=30),
                        observed_at - timedelta(days=1),
                    )
                    if unresolved
                    else None
                ),
                mapping_unique=not unresolved,
                mapped_center_id=None,
                mapping_candidate_ids=(),
                blocker_codes=(
                    (f"{period}_CENTER_MAPPING_UNRESOLVED",)
                    if unresolved
                    else ()
                ),
                warning_codes=(),
            )

        return WarmupSemanticSnapshot(
            periods=tuple(
                period_facts(period, index)
                for index, period in enumerate(("M", "W", "D"))
            ),
            ma5=(
                ("M", Decimal("8")),
                ("W", Decimal("9")),
                ("D", Decimal("10")),
            ),
        )

    baseline = snapshot("NONE")
    middle = snapshot("FORMED")
    snapshots = (baseline, middle, baseline)
    envelope = classify_warmup_convergence_envelope(
        frequency="d",
        as_of=observed_at,
        parameter_set_id=(
            QMT_HIGHER_TIMEFRAME_WARMUP_CONVERGENCE_PARAMETER_SET_ID
        ),
        observations=tuple(
            WarmupPrefixObservation(
                bar_count=count,
                starts_at=observed_at - timedelta(days=count),
                signature_sha256=value.signature_sha256,
            )
            for count, value in zip((480, 640, 800), snapshots)
        ),
    )
    envelope = bind_warmup_convergence_diagnostic(
        envelope,
        snapshots=snapshots,
    )
    no_supply = WarmupMappingSupplySnapshot(
        periods=(("M", None), ("W", None), ("D", None))
    )
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
    middle_supply = WarmupMappingSupplySnapshot(
        periods=(("M", None), ("W", unresolved_supply), ("D", None))
    )
    return bind_warmup_mapping_supply_diagnostic(
        envelope,
        snapshots=(no_supply, middle_supply, no_supply),
    )


def _sector_coverage(
    warmup: QmtHigherTimeframeWarmupEvidence,
    *,
    observed_at: datetime,
) -> QmtSectorSameBaseCoverageEvidence:
    first_visible = observed_at - timedelta(days=700)
    last_visible = observed_at - timedelta(minutes=5)
    return QmtSectorSameBaseCoverageEvidence(
        observed_at=observed_at,
        calendar_first_session=(observed_at - timedelta(days=800)).date(),
        first_visible_bar_at=first_visible,
        last_visible_bar_at=last_visible,
        first_completed_session=first_visible.date(),
        last_completed_session=(observed_at - timedelta(days=1)).date(),
        visible_five_minute_bar_count=max(1, warmup.full_daily_bar_count * 48),
        completed_daily_bar_count=warmup.full_daily_bar_count,
        required_daily_bar_count=warmup.required_daily_bar_count,
        remaining_daily_bar_count=max(
            0,
            warmup.required_daily_bar_count - warmup.full_daily_bar_count,
        ),
        missing_leading_calendar_session_count=(0 if warmup.converged else 240),
        warmup_converged=warmup.converged,
        warmup_reason_code=warmup.reason_code,
        boundary_status=(
            "REQUIRED_HISTORY_CONVERGED"
            if warmup.converged
            else "VISIBLE_PREFIX_STARTS_AFTER_REQUESTED_WARMUP"
        ),
        physical_source_boundary_status=(
            "PHYSICAL_QMT_CACHE_LEFT_BOUNDARY_AFTER_REQUESTED_WARMUP"
        ),
        physical_source_requested_start_at=observed_at - timedelta(days=800),
        physical_source_required_contributor_start_at=first_visible,
        physical_source_representative_member_count=10,
        physical_source_available_member_count=10,
        physical_source_required_contributor_count=8,
        physical_source_inventory_revision="sha256:" + "f" * 64,
    )


def _period_diagnostic(
    period: str,
    state: str,
    *,
    source_digit: str,
) -> HigherTimeframePeriodDiagnostic:
    formed = state != "NONE"
    point_anchor_at = _at(-5, 15)
    point_available_at = _at(-4, 15)
    center_id = f"{period}-center"
    point = RiskMappingPointEvidenceFacts(
        point_id=RiskMappingPointEvidenceFacts.identity(
            source_symbol="SH.600000",
            source_frequency="d",
            center_id=center_id,
            center_level_rank=1,
            point_type="1sell",
            point_anchor_at=point_anchor_at,
            point_available_at=point_available_at,
        ),
        source_symbol="SH.600000",
        source_frequency="d",
        center_id=center_id,
        center_level_rank=1,
        center_completed=True,
        center_expanded=False,
        point_type="1sell",
        point_anchor_at=point_anchor_at,
        point_available_at=point_available_at,
        inside_active_top_interval=True,
        highest_mapping_candidate=True,
    )
    supply = (
        RiskMappingSupplyFacts(
            classification="UNIQUE_MAPPING",
            lower_structure_available=True,
            point_evidence_count=1,
            point_type_counts=(
                ("1sell", 1),
                ("2sell", 0),
                ("3sell", 0),
                ("3buy", 0),
            ),
            completed_sell12_count=1,
            in_top_interval_sell12_count=1,
            completed_in_top_interval_sell12_count=1,
            incomplete_in_top_interval_sell12_count=0,
            outside_top_interval_sell12_count=0,
            highest_candidate_center_count=1,
            point_evidence=(point,),
            diagnostic_buy_point_type_counts=(("1buy", 0), ("2buy", 0)),
            diagnostic_buy_point_evidence=(),
        )
        if formed
        else None
    )
    return HigherTimeframePeriodDiagnostic(
        period=period,  # type: ignore[arg-type]
        state=state,
        completed_bar_count=24,
        evidence_bar_end=_at(-1, 15),
        active_top_interval=(
            (_at(-10, 15), _at(-2, 15)) if formed else None
        ),
        mapping_unique=True,
        mapped_center_id=(center_id if formed else None),
        mapping_candidate_ids=((center_id,) if formed else ()),
        blocker_codes=(),
        warning_codes=(),
        source_revision="sha256:" + source_digit * 64,
        mapping_supply=supply,
    )


def _market_symbol_evidence() -> MarketSymbolHigherTimeframeReviewEvidence:
    market_diagnostics = (
        _period_diagnostic("M", "FORMED", source_digit="1"),
        _period_diagnostic("W", "NONE", source_digit="2"),
        _period_diagnostic("D", "NONE", source_digit="3"),
    )
    symbol_diagnostics = tuple(
        _period_diagnostic(period, "NONE", source_digit=digit)
        for period, digit in zip(("M", "W", "D"), ("4", "5", "6"))
    )
    return market_symbol_higher_timeframe_review_evidence_from_risk(
        {
            "market_gate": "AMBER",
            "market_states": {"M": "FORMED", "W": "NONE", "D": "NONE"},
            "market_reason_codes": ["MARKET_MONTHLY_TOP_FORMED"],
            "market_period_diagnostics": [
                value.document() for value in market_diagnostics
            ],
            "symbol_gate": "GREEN",
            "symbol_states": {"M": "NONE", "W": "NONE", "D": "NONE"},
            "symbol_reason_codes": [],
            "symbol_period_diagnostics": [
                value.document() for value in symbol_diagnostics
            ],
        },
        symbol="SH.600000",
        observed_at=_at(0, 13),
    )


def _sector_evidence(
    *,
    source_mode: str = QMT_SECTOR_SAME_BASE_SOURCE_MODE,
) -> SectorHigherTimeframeReviewEvidence:
    diagnostics = tuple(
        _period_diagnostic(period, "NONE", source_digit=digit)
        for period, digit in zip(("M", "W", "D"), ("7", "8", "9"))
    )
    native_bridge = source_mode == QMT_SECTOR_NATIVE_DAILY_RESEARCH_SOURCE_MODE
    blocker = "QMT_SECTOR_NATIVE_DAILY_AND_5M_UNRECONCILED_RESEARCH_BRIDGE"
    warmup = _warmup(sufficient=not native_bridge)
    observed_at = _at(0, 13)
    risk: dict[str, object] = {
            "sector_higher_timeframe_source_mode": source_mode,
            "sector_strict_same_5m_warmup_evidence": warmup.document(),
            "sector_strict_same_5m_source_coverage_evidence": (
                _sector_coverage(warmup, observed_at=observed_at).document()
            ),
            "sector_research_bridge_parameter_set_id": (
                sector_native_daily_research_bridge_contract()[
                    "parameter_set_id"
                ]
                if native_bridge
                else None
            ),
            "sector_gate": "AMBER" if native_bridge else "GREEN",
            "sector_states": {"M": "NONE", "W": "NONE", "D": "NONE"},
            "sector_reason_codes": [blocker] if native_bridge else [],
            "sector_period_diagnostics": [
                value.document() for value in diagnostics
            ],
        }
    convergence = _convergence(observed_at)
    risk.update(
        {
            "sector_warmup_convergence_evidence": convergence.document(),
            "sector_strict_same_5m_warmup_convergence_evidence": (
                convergence.document()
            ),
        }
    )
    return sector_higher_timeframe_review_evidence_from_risk(
        risk,
        sector_id="QMT:GICS3:bank",
        observed_at=observed_at,
    )


def _native_daily_evidence(
    symbol: str,
    *,
    source_digit: str,
) -> QmtNativeDailyReconciliationEvidence:
    return QmtNativeDailyReconciliationEvidence(
        symbol=symbol,
        observed_at=_at(0, 13),
        native_daily_bar_count=600,
        one_minute_daily_bar_count=480,
        overlap_session_count=480,
        first_overlap_session="2024-01-02",
        last_overlap_session="2025-12-31",
        native_daily_content_revision="sha256:" + source_digit * 64,
        one_minute_base_revision="sha256:" + "7" * 64,
        price_basis_revision="sha256:" + "8" * 64,
        trading_calendar_revision="sha256:" + "9" * 64,
        price_tolerance_quanta=0,
        price_difference_identities=(),
        max_observed_price_difference_quanta=0,
        reconciled_source_revision="sha256:" + "a" * 64,
    )


def _report_for_alert(alert: HumanReviewAlert) -> dict[str, object]:
    row = {
        **_jsonable(human_review_alert_document(alert)),
        "candidate_id": alert.candidate_id,
        "signal_lifecycle_id": alert.signal_lifecycle_id,
    }
    stable: dict[str, object] = {
        "schema": "chanlun-human-review-screen",
        "data_grade": "HUMAN_REVIEW_SCREENING",
        "highest_status": "REVIEW_REQUIRED",
        "live_status": "LIVE_DISABLED",
        "human_confirmation_required": True,
        "automated_order_authorized": False,
        "portfolio_backtest_performed": False,
        "portfolio_performance_evaluable": False,
        "orders_created": 0,
        "fills_created": 0,
        "positions_created": 0,
        "review_queue": [row],
    }
    return {**stable, "content_sha256": sha256_json(stable)}


def test_screening_contract_and_alert_can_never_authorize_trading() -> None:
    parameters = human_review_screening_parameters()
    alert = _alert()

    assert parameters.event_study_horizons == (5, 10, 20)
    assert parameters.automated_order_authorized is False
    assert parameters.highest_status == "REVIEW_REQUIRED"
    assert parameters.live_status == "LIVE_DISABLED"
    assert alert.automated_action_authorized is False
    assert alert.status == "REVIEW_REQUIRED"
    assert alert.candidate_id.startswith("sha256:")

    with pytest.raises(ValueError, match="risk gate is invalid"):
        replace(alert, sector_risk_gate="NOT_A_GATE")


def test_shared_report_boundary_rejects_missing_safety_and_duplicate_queue() -> None:
    report = _screen_report()
    [alert] = validate_human_review_screen_document(report)
    assert alert.candidate_id == _alert().candidate_id

    missing = _screen_report()
    del missing["positions_created"]
    stable = {key: missing[key] for key in missing if key != "content_sha256"}
    missing["content_sha256"] = sha256_json(stable)
    with pytest.raises(ValueError, match="report_boundary_invalid"):
        validate_human_review_screen_document(missing)

    with pytest.raises(ValueError, match="candidate_duplicate"):
        validate_human_review_screen_document(_screen_report(duplicate=True))


def test_shared_report_rejects_non_decimal_candidate_price_cleanly() -> None:
    report = _screen_report()
    report["review_queue"][0]["reference_price"] = "not-a-decimal"
    stable = {key: report[key] for key in report if key != "content_sha256"}
    report["content_sha256"] = sha256_json(stable)

    with pytest.raises(ValueError, match="human_review_candidate_malformed"):
        validate_human_review_screen_document(report)


def test_sector_source_evidence_uses_current_portable_hash_bound_contract(
) -> None:
    base_alert = _alert()
    assert (
        human_review_alert_document(base_alert)[
            "sector_higher_timeframe_evidence"
        ]
        is None
    )

    source = _sector_evidence()
    alert = replace(
        base_alert,
        sector_risk_gate="GREEN",
        sector_higher_timeframe_evidence=source,
        source_fact_ids=(*base_alert.source_fact_ids, source.evidence_id),
    )
    [restored] = validate_human_review_screen_document(
        _report_for_alert(alert)
    )
    assert restored.sector_higher_timeframe_evidence == source
    assert source.evidence_id in restored.source_fact_ids
    assert parse_sector_higher_timeframe_review_evidence(
        source.document()
    ) == source
    assert source.schema == SECTOR_HIGHER_TIMEFRAME_REVIEW_EVIDENCE_SCHEMA
    with pytest.raises(ValueError, match="decision evidence is not fact-bound"):
        replace(alert, sector_id="QMT:GICS3:other")

    future = replace(
        source.period_diagnostics[0],
        evidence_bar_end=source.observed_at + timedelta(minutes=1),
    )
    with pytest.raises(ValueError, match="contains future data"):
        replace(
            source,
            period_diagnostics=(future, *source.period_diagnostics[1:]),
        )

    forged = _report_for_alert(alert)
    forged_source = forged["review_queue"][0][
        "sector_higher_timeframe_evidence"
    ]
    forged_source["strict_same_5m_warmup_evidence"][
        "full_daily_bar_count"
    ] = 481
    stable = {key: forged[key] for key in forged if key != "content_sha256"}
    forged["content_sha256"] = sha256_json(stable)
    with pytest.raises(ValueError, match="candidate_malformed"):
        validate_human_review_screen_document(forged)


def test_sector_preserves_multi_prefix_convergence_and_rejects_relabel(
) -> None:
    source = _sector_evidence()

    assert source.schema == SECTOR_HIGHER_TIMEFRAME_REVIEW_EVIDENCE_SCHEMA
    assert source.warmup_convergence_evidence is not None
    assert source.warmup_convergence_evidence.status == "NON_MONOTONIC"
    assert source.strict_same_5m_warmup_convergence_evidence == (
        source.warmup_convergence_evidence
    )
    assert parse_sector_higher_timeframe_review_evidence(
        source.document()
    ) == source

    forged = json.loads(json.dumps(source.document()))
    convergence = forged["warmup_convergence_evidence"]
    convergence["status"] = "STABLE_ALL_PREFIXES"
    convergence["stable_all_prefixes"] = True
    convergence["match_longest_pattern"] = [True, True, True, True]
    convergence["reason_codes"] = ["WARMUP_ENVELOPE_STABLE_ALL_PREFIXES"]
    stable_convergence = {
        key: value
        for key, value in convergence.items()
        if key != "content_sha256"
    }
    convergence["content_sha256"] = sha256_json(stable_convergence)
    stable = {
        key: value for key, value in forged.items() if key != "evidence_id"
    }
    forged["evidence_id"] = sha256_json(stable)

    with pytest.raises(ValueError, match="evidence is invalid"):
        parse_sector_higher_timeframe_review_evidence(forged)


def test_market_symbol_mwd_evidence_is_causal_and_hash_bound(
) -> None:
    base = _alert()
    base_id = base.candidate_id
    assert (
        human_review_alert_document(base)[
            "market_symbol_higher_timeframe_evidence"
        ]
        is None
    )
    assert base.candidate_id == base_id

    evidence = _market_symbol_evidence()
    alert = replace(
        base,
        market_symbol_higher_timeframe_evidence=evidence,
        source_fact_ids=(*base.source_fact_ids, evidence.evidence_id),
    )
    [restored] = validate_human_review_screen_document(_report_for_alert(alert))
    assert restored.market_symbol_higher_timeframe_evidence == evidence
    assert evidence.evidence_id in restored.source_fact_ids
    assert parse_market_symbol_higher_timeframe_review_evidence(
        evidence.document()
    ) == evidence

    with pytest.raises(ValueError, match="not fact-bound"):
        replace(alert, market_risk_gate="GREEN")

    inconsistent = json.loads(json.dumps(evidence.document()))
    inconsistent["market"]["states"]["M"] = "NONE"
    stable = {key: value for key, value in inconsistent.items() if key != "evidence_id"}
    inconsistent["evidence_id"] = sha256_json(stable)
    with pytest.raises(ValueError, match="evidence is invalid"):
        parse_market_symbol_higher_timeframe_review_evidence(inconsistent)

    future = json.loads(json.dumps(evidence.document()))
    future["market"]["period_diagnostics"][0]["evidence_bar_end"] = (
        _at(1, 15).isoformat()
    )
    stable = {key: value for key, value in future.items() if key != "evidence_id"}
    future["evidence_id"] = sha256_json(stable)
    with pytest.raises(ValueError, match="evidence is invalid"):
        parse_market_symbol_higher_timeframe_review_evidence(future)

    forged_supply = json.loads(json.dumps(evidence.document()))
    forged_supply["market"]["period_diagnostics"][0]["mapping_supply"][
        "point_type_counts"
    ]["3sell"] = 1
    stable = {
        key: value for key, value in forged_supply.items() if key != "evidence_id"
    }
    forged_supply["evidence_id"] = sha256_json(stable)
    with pytest.raises(ValueError, match="evidence is invalid"):
        parse_market_symbol_higher_timeframe_review_evidence(forged_supply)

    malformed_reason = json.loads(json.dumps(evidence.document()))
    malformed_reason["market"]["reason_codes"] = [1]
    stable = {
        key: value
        for key, value in malformed_reason.items()
        if key != "evidence_id"
    }
    malformed_reason["evidence_id"] = sha256_json(stable)
    with pytest.raises(ValueError, match="evidence is invalid"):
        parse_market_symbol_higher_timeframe_review_evidence(malformed_reason)


def test_market_symbol_source_support_preserves_session_warmup_and_native_daily(
) -> None:
    missing_session = QmtMinuteSessionIssue(
        session=_at(0, 13).date(),
        code="QMT_ONE_MINUTE_EXPECTED_SESSION_MISSING",
        observed_rows=0,
        detail="trading-calendar session is absent from the QMT 1m prefix",
    )
    exact_empty = HigherTimeframeSessionEvidence.exact().document()
    risk: dict[str, object] = {
        "market_gate": "UNRESOLVED",
        "market_states": {
            period: "UNRESOLVED" for period in ("M", "W", "D")
        },
        "market_reason_codes": [
            "QMT_HIGHER_TIMEFRAME_WARMUP_HISTORY_INSUFFICIENT"
        ],
        "market_period_diagnostics": [],
        "symbol_gate": "UNRESOLVED",
        "symbol_states": {
            period: "UNRESOLVED" for period in ("M", "W", "D")
        },
        "symbol_reason_codes": [missing_session.code],
        "symbol_period_diagnostics": [],
        "session_evidence_contract_id": (
            "chanlun-higher-timeframe-session-evidence"
        ),
        "market_session_evidence": exact_empty,
        "sector_session_evidence": exact_empty,
        "symbol_session_evidence": (
            HigherTimeframeSessionEvidence.exact((missing_session,)).document()
        ),
        "warmup_evidence_contract_id": (
            "chanlun-qmt-mwd-warmup-evidence"
        ),
        "market_warmup_evidence": _warmup(sufficient=False).document(),
        "sector_warmup_evidence": None,
        "symbol_warmup_evidence": _warmup(sufficient=True).document(),
        "warmup_convergence_contract_id": (
            WARMUP_CONVERGENCE_ENVELOPE_CONTRACT_ID
        ),
        "market_warmup_convergence_evidence": _convergence(
            _at(0, 13)
        ).document(),
        "sector_warmup_convergence_evidence": None,
        "symbol_warmup_convergence_evidence": _convergence(
            _at(0, 13),
            non_monotonic=False,
        ).document(),
        "native_daily_reconciliation_contract_id": (
            "chanlun-qmt-native-daily-reconciled-with-one-minute"
        ),
        "market_native_daily_reconciliation_evidence": (
            _native_daily_evidence(
                "SH.000300",
                source_digit="b",
            ).document()
        ),
        "sector_native_daily_reconciliation_evidence": None,
        "symbol_native_daily_reconciliation_evidence": (
            _native_daily_evidence(
                "SH.600000",
                source_digit="c",
            ).document()
        ),
    }
    evidence = market_symbol_higher_timeframe_review_evidence_from_risk(
        risk,
        symbol="SH.600000",
        observed_at=_at(0, 13),
    )
    assert evidence.market.source_support is not None
    assert evidence.symbol_evidence.source_support is not None
    assert (
        evidence.market.source_support.warmup_evidence
        == _warmup(sufficient=False)
    )
    assert (
        evidence.market.source_support.warmup_convergence_evidence.status
        == "NON_MONOTONIC"
    )
    assert (
        evidence.symbol_evidence.source_support.warmup_convergence_evidence.status
        == "STABLE_ALL_PREFIXES"
    )
    assert evidence.symbol_evidence.source_support.session_evidence == (
        HigherTimeframeSessionEvidence.exact((missing_session,))
    )
    assert (
        evidence.symbol_evidence.source_support.native_daily_reconciliation_evidence.symbol
        == "SH.600000"
    )
    assert parse_market_symbol_higher_timeframe_review_evidence(
        evidence.document()
    ) == evidence

    # Production source-support evidence contains exact ``date`` values in
    # QMT session issues.  It must survive the candidate identity and whole
    # report boundary, not merely its own parser.  This reproduces the full
    # market materialization failure that previously surfaced as
    # ``unsupported canonical value: date``.
    base = _alert()
    alert = replace(
        base,
        market_risk_gate=evidence.market.gate,
        symbol_risk_gate=evidence.symbol_evidence.gate,
        market_symbol_higher_timeframe_evidence=evidence,
        source_fact_ids=(*base.source_fact_ids, evidence.evidence_id),
    )
    assert alert.candidate_id.startswith("sha256:")
    [restored] = validate_human_review_screen_document(
        _report_for_alert(alert)
    )
    assert restored.market_symbol_higher_timeframe_evidence == evidence
    assert restored.candidate_id == alert.candidate_id

    def rehash(document: dict[str, object]) -> None:
        for side_name in ("market", "symbol_evidence"):
            side = document[side_name]
            support = side.get("source_support")
            if support is not None:
                support["support_id"] = sha256_json(
                    {
                        key: value
                        for key, value in support.items()
                        if key != "support_id"
                    }
                )
        document["evidence_id"] = sha256_json(
            {
                key: value
                for key, value in document.items()
                if key != "evidence_id"
            }
        )

    future = json.loads(json.dumps(evidence.document()))
    future["symbol_evidence"]["source_support"]["session_evidence"][
        "issues"
    ][0]["session"] = _at(1, 13).date().isoformat()
    rehash(future)
    with pytest.raises(ValueError, match="evidence is invalid"):
        parse_market_symbol_higher_timeframe_review_evidence(future)

    divergent = json.loads(json.dumps(evidence.document()))
    divergent["market"]["source_support"]["warmup_evidence"][
        "full_daily_bar_count"
    ] = 481
    rehash(divergent)
    with pytest.raises(ValueError, match="evidence is invalid"):
        parse_market_symbol_higher_timeframe_review_evidence(divergent)

    forged_convergence = json.loads(json.dumps(evidence.document()))
    raw_convergence = forged_convergence["market"]["source_support"][
        "warmup_convergence_evidence"
    ]
    raw_convergence["status"] = "STABLE_ALL_PREFIXES"
    raw_convergence["stable_all_prefixes"] = True
    raw_convergence["match_longest_pattern"] = [True, True, True, True]
    raw_convergence["reason_codes"] = [
        "WARMUP_ENVELOPE_STABLE_ALL_PREFIXES"
    ]
    convergence_stable = {
        key: value
        for key, value in raw_convergence.items()
        if key != "content_sha256"
    }
    raw_convergence["content_sha256"] = sha256_json(convergence_stable)
    rehash(forged_convergence)
    with pytest.raises(ValueError, match="evidence is invalid"):
        parse_market_symbol_higher_timeframe_review_evidence(
            forged_convergence
        )

    impossible_overlap = json.loads(json.dumps(evidence.document()))
    impossible_overlap["symbol_evidence"]["source_support"][
        "native_daily_reconciliation_evidence"
    ]["overlap_session_count"] = 700
    rehash(impossible_overlap)
    with pytest.raises(ValueError, match="evidence is invalid"):
        parse_market_symbol_higher_timeframe_review_evidence(impossible_overlap)

    omitted = json.loads(json.dumps(evidence.document()))
    omitted["market"].pop("source_support")
    rehash(omitted)
    with pytest.raises(ValueError, match="evidence is invalid"):
        parse_market_symbol_higher_timeframe_review_evidence(omitted)

    # Market/symbol extraction still consumes an atomic four-field upstream
    # contract.  A malformed sector member may not hide behind the fact that
    # it is not copied into this particular portable evidence object.
    malformed_sector_session = json.loads(json.dumps(risk))
    malformed_sector_session["sector_session_evidence"]["issue_count"] = 1
    with pytest.raises(ValueError, match="session evidence is invalid"):
        market_symbol_higher_timeframe_review_evidence_from_risk(
            malformed_sector_session,
            symbol="SH.600000",
            observed_at=_at(0, 13),
        )

    malformed_sector_warmup = json.loads(json.dumps(risk))
    malformed_sector_warmup["sector_warmup_evidence"] = {}
    with pytest.raises(ValueError, match="warmup evidence"):
        market_symbol_higher_timeframe_review_evidence_from_risk(
            malformed_sector_warmup,
            symbol="SH.600000",
            observed_at=_at(0, 13),
        )


def test_market_symbol_converter_authenticates_semantic_warmup_diagnostic(
) -> None:
    observed_at = _at(0, 13)
    convergence = _semantic_convergence(observed_at)
    assert convergence.diagnostic is not None
    assert convergence.mapping_supply_diagnostic is not None
    diagnostics = tuple(
        _period_diagnostic(period, "NONE", source_digit=digit)
        for period, digit in zip(("M", "W", "D"), ("1", "2", "3"))
    )
    risk: dict[str, object] = {
        "market_gate": "GREEN",
        "market_states": {period: "NONE" for period in ("M", "W", "D")},
        "market_reason_codes": [],
        "market_period_diagnostics": [
            value.document() for value in diagnostics
        ],
        "symbol_gate": "GREEN",
        "symbol_states": {period: "NONE" for period in ("M", "W", "D")},
        "symbol_reason_codes": [],
        "symbol_period_diagnostics": [
            value.document() for value in diagnostics
        ],
        "warmup_convergence_contract_id": (
            WARMUP_CONVERGENCE_ENVELOPE_CONTRACT_ID
        ),
        "warmup_convergence_diagnostic_contract_id": (
            WARMUP_CONVERGENCE_DIAGNOSTIC_CONTRACT_ID
        ),
        "warmup_mapping_supply_diagnostic_contract_id": (
            WARMUP_MAPPING_SUPPLY_DIAGNOSTIC_CONTRACT_ID
        ),
        **{
            f"{subject}_warmup_convergence_evidence": copy.deepcopy(
                convergence.document()
            )
            for subject in ("market", "sector", "symbol")
        },
        **{
            f"{subject}_warmup_convergence_diagnostic_evidence": (
                copy.deepcopy(convergence.diagnostic.document())
            )
            for subject in ("market", "sector", "symbol")
        },
        **{
            f"{subject}_warmup_mapping_supply_diagnostic_evidence": (
                copy.deepcopy(
                    convergence.mapping_supply_diagnostic.document()
                )
            )
            for subject in ("market", "sector", "symbol")
        },
    }

    evidence = market_symbol_higher_timeframe_review_evidence_from_risk(
        risk,
        symbol="SH.600000",
        observed_at=observed_at,
    )

    assert evidence.market.source_support is not None
    retained = evidence.market.source_support.warmup_convergence_evidence
    assert retained is not None and retained.diagnostic is not None
    assert retained.diagnostic.status == "NON_MONOTONIC"
    assert retained.mapping_supply_diagnostic is not None
    assert retained.mapping_supply_diagnostic.status == "NON_MONOTONIC"

    forged_supply = risk[
        "symbol_warmup_mapping_supply_diagnostic_evidence"
    ]
    forged_supply["comparisons"][0]["delta"]["transition_codes"] = [
        "MAPPING_SUPPLY_UNCHANGED"
    ]
    stable_supply = dict(forged_supply)
    stable_supply.pop("content_sha256")
    forged_supply["content_sha256"] = sha256_json(stable_supply)
    with pytest.raises(ValueError, match="mapping supply"):
        market_symbol_higher_timeframe_review_evidence_from_risk(
            risk,
            symbol="SH.600000",
            observed_at=observed_at,
        )

    risk["symbol_warmup_mapping_supply_diagnostic_evidence"] = copy.deepcopy(
        convergence.mapping_supply_diagnostic.document()
    )

    forged = risk["symbol_warmup_convergence_diagnostic_evidence"]
    forged["observations"][1]["changed_paths_from_longest"] = ["M.state"]
    stable = dict(forged)
    stable.pop("content_sha256")
    forged["content_sha256"] = sha256_json(stable)
    with pytest.raises(ValueError, match="semantic diagnostic"):
        market_symbol_higher_timeframe_review_evidence_from_risk(
            risk,
            symbol="SH.600000",
            observed_at=observed_at,
        )


def test_unresolved_safety_overlay_retains_raw_period_diagnostics() -> None:
    """A fail-closed warmup overlay must not erase replayable M/W/D facts."""

    observed_at = _at(0, 13)
    raw_diagnostics = tuple(
        _period_diagnostic(period, state, source_digit=digit)
        for period, state, digit in (
            ("M", "FORMED", "1"),
            ("W", "NONE", "2"),
            ("D", "NONE", "3"),
        )
    )
    symbol_diagnostics = tuple(
        _period_diagnostic(period, "NONE", source_digit=digit)
        for period, digit in zip(("M", "W", "D"), ("4", "5", "6"))
    )
    risk: dict[str, object] = {
        "market_gate": "UNRESOLVED",
        "market_states": {
            period: "UNRESOLVED" for period in ("M", "W", "D")
        },
        "market_reason_codes": [
            "EFFECTIVE_STATES_REMOVED_BY_SAFETY_LOGIC"
        ],
        "market_period_diagnostics": [
            value.document() for value in raw_diagnostics
        ],
        "symbol_gate": "GREEN",
        "symbol_states": {period: "NONE" for period in ("M", "W", "D")},
        "symbol_reason_codes": [],
        "symbol_period_diagnostics": [
            value.document() for value in symbol_diagnostics
        ],
    }

    evidence = market_symbol_higher_timeframe_review_evidence_from_risk(
        risk,
        symbol="SH.600000",
        observed_at=observed_at,
    )

    assert dict(evidence.market.states) == {
        period: "UNRESOLVED" for period in ("M", "W", "D")
    }
    assert evidence.market.period_diagnostics == raw_diagnostics
    assert parse_market_symbol_higher_timeframe_review_evidence(
        evidence.document()
    ) == evidence


def test_sector_ranking_evidence_is_portable_explicit_and_hash_bound() -> None:
    raw_sector: dict[str, object] = {
        "sector_id": "QMT:GICS3:bank",
        "sector_name": "银行",
        "eligible": True,
        "hard_block": False,
        "regime": "supportive",
        "rank": 2,
        "rank_score": 45,
        "rank_components": {
            "five_support": 0,
            "neutral_access": 5,
            "thirty_support": 40,
        },
        "reason_codes": ["structural_ranking_only"],
        "horizontal_strength": "7.5",
        "horizontal_rank": 1,
        "strength_anchor_session": _at(-1).date().isoformat(),
        "strength_member_count": 42,
        "strength_source_revision": "sha256:" + "7" * 64,
        "strength_reason_codes": [],
    }
    evidence = sector_ranking_review_evidence_from_live_sector(
        raw_sector,
        observed_at=_at(0, 13),
        strength_evidence_revision="sha256:" + "8" * 64,
        sector_catalog_revision="sha256:" + "9" * 64,
    )
    assert isinstance(evidence, SectorRankingReviewEvidence)
    assert dict(evidence.rank_components)["thirty_support"] == 40
    assert evidence.sector_catalog_revision == "sha256:" + "9" * 64
    assert parse_sector_ranking_review_evidence(evidence.document()) == evidence

    base = _alert()
    alert = replace(
        base,
        sector_ranking_evidence=evidence,
        source_fact_ids=(*base.source_fact_ids, evidence.evidence_id),
    )
    [restored] = validate_human_review_screen_document(_report_for_alert(alert))
    assert restored.sector_ranking_evidence == evidence

    tampered = json.loads(json.dumps(evidence.document()))
    tampered["rank_score"] = 44
    tampered["evidence_id"] = sha256_json(
        {key: value for key, value in tampered.items() if key != "evidence_id"}
    )
    with pytest.raises(ValueError, match="evidence is invalid"):
        parse_sector_ranking_review_evidence(tampered)

    future = replace(
        evidence,
        observed_at=_at(1, 13),
        strength_observed_at=_at(1, 13),
    )
    with pytest.raises(ValueError, match="not fact-bound"):
        replace(
            alert,
            sector_ranking_evidence=future,
            source_fact_ids=(*base.source_fact_ids, future.evidence_id),
        )

    with pytest.raises(ValueError, match="components do not sum"):
        replace(evidence, rank_components=())

    with pytest.raises(ValueError, match="catalog identity is missing"):
        replace(evidence, sector_catalog_revision=None)


def test_pre_sector_gate_archive_is_rejected(
) -> None:
    current = _alert()
    incomplete_identity = human_review_alert_document(current)
    for field in (
        "sector_risk_gate",
        "entry_confirmation_bar_closed_at",
        "entry_price_cap",
        "entry_valid_until",
        "entry_boundary_evidence_id",
    ):
        incomplete_identity.pop(field, None)
    incomplete_candidate_id = sha256_json(incomplete_identity)
    row = {
        **_jsonable(incomplete_identity),
        "candidate_id": incomplete_candidate_id,
        "signal_lifecycle_id": current.signal_lifecycle_id,
    }
    stable: dict[str, object] = {
        "schema": "chanlun-human-review-screen",
        "data_grade": "HUMAN_REVIEW_SCREENING",
        "highest_status": "REVIEW_REQUIRED",
        "live_status": "LIVE_DISABLED",
        "human_confirmation_required": True,
        "automated_order_authorized": False,
        "portfolio_backtest_performed": False,
        "portfolio_performance_evaluable": False,
        "orders_created": 0,
        "fills_created": 0,
        "positions_created": 0,
        "review_queue": [row],
    }
    report = {**stable, "content_sha256": sha256_json(stable)}

    with pytest.raises(ValueError, match="human_review_candidate_malformed"):
        validate_human_review_screen_document(report)


def test_native_daily_sector_review_evidence_preserves_amber_cap() -> None:
    source = _sector_evidence(
        source_mode=QMT_SECTOR_NATIVE_DAILY_RESEARCH_SOURCE_MODE
    )
    blocker = "QMT_SECTOR_NATIVE_DAILY_AND_5M_UNRECONCILED_RESEARCH_BRIDGE"
    base = _alert()
    alert = replace(
        base,
        sector_risk_gate="AMBER",
        warning_codes=(*base.warning_codes, blocker),
        source_fact_ids=(*base.source_fact_ids, source.evidence_id),
        sector_higher_timeframe_evidence=source,
    )
    assert validate_human_review_screen_document(_report_for_alert(alert))

    with pytest.raises(ValueError, match="decision evidence is not fact-bound"):
        replace(alert, sector_risk_gate="GREEN")
    with pytest.raises(ValueError, match="gate cannot be reproduced"):
        replace(source, gate="GREEN")
    with pytest.raises(ValueError, match="research bridge gate"):
        replace(alert, warning_codes=base.warning_codes)
    with pytest.raises(ValueError, match="research bridge evidence is missing"):
        replace(alert, sector_higher_timeframe_evidence=None)


def test_unresolved_sector_safety_overlay_retains_raw_diagnostics() -> None:
    source = _sector_evidence()
    overlaid = replace(
        source,
        gate="UNRESOLVED",
        states=tuple((period, "UNRESOLVED") for period in ("M", "W", "D")),
        reason_codes=("EFFECTIVE_STATES_REMOVED_BY_SAFETY_LOGIC",),
    )

    assert overlaid.period_diagnostics == source.period_diagnostics
    assert parse_sector_higher_timeframe_review_evidence(
        overlaid.document()
    ) == overlaid


def test_review_priority_is_a_transparent_ordering_rule() -> None:
    assert review_priority(
        confidence="HIGH",
        exact_green=True,
        market_risk_gate="GREEN",
        sector_risk_gate="GREEN",
        symbol_risk_gate="GREEN",
        warning_count=0,
    ) == 95
    assert review_priority(
        confidence="LOW",
        exact_green=False,
        market_risk_gate="AMBER",
        sector_risk_gate="AMBER",
        symbol_risk_gate="AMBER",
        warning_count=20,
    ) == 0


def test_event_study_uses_only_complete_sessions_after_review_date() -> None:
    alert = _alert()
    bars = [
        ReviewPriceBar(_at(0, 13), Decimal("101"), Decimal("99"), Decimal("100")),
        # A dramatic same-day move is deliberately excluded from a complete
        # forward-session horizon.
        ReviewPriceBar(_at(0, 15), Decimal("151"), Decimal("49"), Decimal("50")),
    ]
    for day in range(1, 21):
        close = Decimal("95") if day == 3 else Decimal(100 + day)
        bars.append(
            ReviewPriceBar(
                _at(day, 15),
                max(close, Decimal(100 + day)) + Decimal("1"),
                min(close, Decimal(100 + day)) - Decimal("1"),
                close,
            )
        )

    observations = evaluate_review_alert(alert, bars)
    five = observations[0]

    assert tuple(value.horizon_sessions for value in observations) == (5, 10, 20)
    assert five.complete is True
    assert five.end_session == _at(5).date()
    assert five.reference_price == Decimal("100")
    assert five.close_return == Decimal("0.05")
    assert five.maximum_favorable_excursion == Decimal("0.06")
    assert five.maximum_adverse_excursion == Decimal("-0.06")
    assert five.invalidation_observed is True
    assert five.first_invalidation_at == _at(3, 15)

    summary = summarize_event_study(observations)
    assert summary["5"]["false_positive_proxy_rate"] == Decimal("1")
    assert summary["10"]["false_positive_proxy_rate"] == Decimal("1")


def test_event_study_marks_unavailable_horizons_without_inventing_data() -> None:
    alert = _alert()
    bars = [
        ReviewPriceBar(_at(0, 13), Decimal("101"), Decimal("99"), Decimal("100")),
        *(
            ReviewPriceBar(
                _at(day, 15),
                Decimal("102"),
                Decimal("98"),
                Decimal("100"),
            )
            for day in range(1, 5)
        ),
    ]

    observations = evaluate_review_alert(alert, bars)

    assert all(value.complete is False for value in observations)
    assert all(value.reason_code == "INSUFFICIENT_FUTURE_SESSIONS" for value in observations)
    assert all(value.close_return is None for value in observations)


def test_event_study_uses_market_close_not_the_structure_anchor() -> None:
    observations = evaluate_review_alert(
        _alert(),
        (
            ReviewPriceBar(
                _at(0, 13),
                Decimal("100"),
                Decimal("98"),
                Decimal("99"),
            ),
            *(
                ReviewPriceBar(
                    _at(day, 15),
                    Decimal("100"),
                    Decimal("98"),
                    Decimal("99"),
                )
                for day in range(1, 6)
            ),
        ),
    )

    assert _alert().reference_price == Decimal("100")
    assert observations[0].reference_price == Decimal("99")
    assert observations[0].close_return == Decimal("0")


def test_event_study_rejects_duplicate_price_timestamps() -> None:
    bar = ReviewPriceBar(
        _at(0, 13),
        Decimal("100"),
        Decimal("98"),
        Decimal("99"),
    )
    with pytest.raises(ValueError, match="duplicate timestamps"):
        evaluate_review_alert(_alert(), (bar, bar))


def test_event_study_third_buy_invalidation_uses_low_and_strict_boundary() -> None:
    alert = _alert()
    bars = [
        ReviewPriceBar(_at(0, 13), Decimal("101"), Decimal("99"), Decimal("100")),
        # Equality at ZG is explicitly still a valid third-buy return.
        ReviewPriceBar(_at(1, 15), Decimal("98"), Decimal("95"), Decimal("96")),
        # A later completed bar trades below ZG but closes back above it.
        ReviewPriceBar(_at(2, 15), Decimal("98"), Decimal("94.99"), Decimal("96")),
        ReviewPriceBar(_at(3, 15), Decimal("98"), Decimal("96"), Decimal("97")),
        ReviewPriceBar(_at(4, 15), Decimal("99"), Decimal("97"), Decimal("98")),
        ReviewPriceBar(_at(5, 15), Decimal("100"), Decimal("98"), Decimal("99")),
    ]

    five = evaluate_review_alert(alert, bars)[0]

    assert five.complete is True
    assert five.invalidation_observed is True
    assert five.first_invalidation_at == _at(2, 15)


def test_feedback_ledger_is_idempotent_hash_chained_and_tamper_evident(
    tmp_path,
) -> None:
    path = tmp_path / "feedback.json"
    alert = _alert()
    feedback = HumanReviewFeedback(
        candidate_id=alert.candidate_id,
        source_screen_content_sha256="sha256:" + "1" * 64,
        reviewer="human-a",
        reviewed_at=_at(1),
        center_judgement="UNCERTAIN",
        trend_judgement="CONSOLIDATION",
        level_judgement="30M",
        point_judgement="UNCERTAIN",
        disposition="WATCH",
        decomposition_judgement="COMBINED",
        center_expansion_judgement="REJECTED",
        nine_segment_upgrade_judgement="CONFIRMED",
        locator_judgement="UNCERTAIN",
        notes="需要人工复核同级别分解。",
    )

    first = append_human_review_feedback(path, feedback)
    duplicate = append_human_review_feedback(path, feedback)
    loaded = load_human_review_feedback_ledger(path)

    assert len(first["entries"]) == 1
    assert duplicate == first
    assert loaded == first
    assert loaded["entries"][0]["previous_entry_sha256"] is None
    assert loaded["entries"][0]["decomposition_judgement"] == "COMBINED"
    assert loaded["entries"][0]["nine_segment_upgrade_judgement"] == (
        "CONFIRMED"
    )
    assert loaded["automated_order_authorized"] is False

    tampered = json.loads(path.read_text(encoding="utf-8"))
    tampered["entries"][0]["reviewer"] = "forged"
    path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ValueError, match="content hash mismatch"):
        load_human_review_feedback_ledger(path)


def test_feedback_request_retry_uses_one_stable_identity(tmp_path) -> None:
    path = tmp_path / "feedback.json"
    alert = _alert()
    feedback = HumanReviewFeedback(
        candidate_id=alert.candidate_id,
        source_screen_content_sha256="sha256:" + "1" * 64,
        reviewer="human-a",
        reviewed_at=_at(1),
        center_judgement="CONFIRMED",
        trend_judgement="UP",
        level_judgement="30M",
        point_judgement="BUY_3",
        disposition="PAPER_OBSERVE",
        notes="stable request",
        request_id="review-request-1",
        signal_lifecycle_id=alert.signal_lifecycle_id,
    )
    retry = replace(feedback, reviewed_at=_at(2))

    assert retry.feedback_id == feedback.feedback_id
    first = append_human_review_feedback(path, feedback)
    second = append_human_review_feedback(path, retry)
    assert first == second
    assert len(second["entries"]) == 1
    assert second["entries"][0]["reviewed_at"] == _at(1).isoformat()

    with pytest.raises(ValueError, match="reused with different values"):
        append_human_review_feedback(
            path,
            replace(retry, disposition="REJECT"),
        )


def test_feedback_ledger_serializes_concurrent_writers(tmp_path) -> None:
    path = tmp_path / "feedback.json"
    alert = _alert()

    def append(index: int) -> None:
        append_human_review_feedback(
            path,
            HumanReviewFeedback(
                candidate_id=alert.candidate_id,
                source_screen_content_sha256="sha256:" + "1" * 64,
                reviewer="human-a",
                reviewed_at=_at(1),
                center_judgement="UNCERTAIN",
                trend_judgement="UNCERTAIN",
                level_judgement="UNCERTAIN",
                point_judgement="UNCERTAIN",
                disposition="WATCH",
                notes=f"concurrent-{index}",
                request_id=f"concurrent-request-{index}",
                signal_lifecycle_id=alert.signal_lifecycle_id,
            ),
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        tuple(pool.map(append, range(16)))

    ledger = load_human_review_feedback_ledger(path)
    assert len(ledger["entries"]) == 16
    assert {row["request_id"] for row in ledger["entries"]} == {
        f"concurrent-request-{index}" for index in range(16)
    }


def test_feedback_ledger_without_structured_fields_is_rejected(tmp_path) -> None:
    path = tmp_path / "incomplete-feedback.json"
    feedback = HumanReviewFeedback(
        candidate_id=_alert().candidate_id,
        source_screen_content_sha256="sha256:" + "1" * 64,
        reviewer="incomplete-writer",
        reviewed_at=_at(1),
        center_judgement="UNCERTAIN",
        trend_judgement="UNCERTAIN",
        level_judgement="UNCERTAIN",
        point_judgement="UNCERTAIN",
        disposition="WATCH",
    )
    newly_structured = {
        "decomposition_judgement",
        "center_expansion_judgement",
        "nine_segment_upgrade_judgement",
        "locator_judgement",
    }
    incomplete_identity_fields = tuple(
        field
        for field in HumanReviewFeedback.__dataclass_fields__
        if field
        not in {"request_id", "signal_lifecycle_id", *newly_structured}
    )
    incomplete_identity_values = {
        field: getattr(feedback, field) for field in incomplete_identity_fields
    }
    stable_entry = {
        **{
            **asdict(feedback),
            "reviewed_at": feedback.reviewed_at.isoformat(),
        },
        "feedback_id": sha256_json(incomplete_identity_values),
        "previous_entry_sha256": None,
    }
    stable_entry.pop("request_id")
    stable_entry.pop("signal_lifecycle_id")
    for field in newly_structured:
        stable_entry.pop(field)
    entry = {**stable_entry, "entry_sha256": sha256_json(stable_entry)}
    stable_document = {
        "schema": "chanlun-human-review-feedback-ledger",
        "entries": [entry],
        "automated_order_authorized": False,
        "live_status": "LIVE_DISABLED",
    }
    path.write_text(
        json.dumps(
            {**stable_document, "content_sha256": sha256_json(stable_document)},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="feedback ledger entry 0 is malformed"):
        load_human_review_feedback_ledger(path)


def test_structured_human_review_labels_fail_closed() -> None:
    base = HumanReviewFeedback(
        candidate_id=_alert().candidate_id,
        source_screen_content_sha256="sha256:" + "1" * 64,
        reviewer="structured-human",
        reviewed_at=_at(1),
        center_judgement="UNCERTAIN",
        trend_judgement="UNCERTAIN",
        level_judgement="UNCERTAIN",
        point_judgement="UNCERTAIN",
        disposition="WATCH",
    )

    assert base.decomposition_judgement == "UNCERTAIN"
    assert base.center_expansion_judgement == "UNCERTAIN"
    with pytest.raises(ValueError, match="decomposition"):
        replace(base, decomposition_judgement="INVENTED")
    with pytest.raises(ValueError, match="nine_segment_upgrade"):
        replace(base, nine_segment_upgrade_judgement="INVENTED")
