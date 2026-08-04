import copy
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import tools.backtest_v3_sector_first_full_market as FULL_MARKET

from chanlun.decision_support.fingerprints import sha256_json
from chanlun.decision_support.trading_system.higher_timeframe_gate import (
    QmtSectorSameBaseCoverageEvidence,
    higher_timeframe_effectiveness_audit,
)
from chanlun.decision_support.trading_system.v3_human_review_screening import (
    HumanReviewAlert,
    SectorRankingReviewEvidence,
    parse_human_review_alert,
)
from chanlun.decision_support.trading_system.v3_qmt_higher_timeframe import (
    QMT_HIGHER_TIMEFRAME_WARMUP_CONVERGENCE_PARAMETER_SET_ID,
)
from chanlun.decision_support.trading_system.warmup_convergence import (
    WarmupMappingSupplySnapshot,
    WarmupPeriodSemanticFacts,
    WarmupPrefixObservation,
    WarmupSemanticSnapshot,
    bind_warmup_convergence_diagnostic,
    bind_warmup_mapping_supply_diagnostic,
    classify_warmup_convergence_envelope,
)
from chanlun.decision_support.trading_system.v3_etf_proxy_facts import (
    RiskDiagnosticBuyPointEvidenceFacts,
    RiskMappingPointEvidenceFacts,
    RiskMappingSupplyFacts,
)
from chanlun.decision_support.trading_system.v3_qmt_native_daily_bridge import (
    QmtNativeDailyCalendarCoverageEvidence,
    QmtNativeDailyReconciliationError,
)

from tools.backtest_v3_sector_first_full_market import (
    SymbolContext,
    _entry_dispatch_at,
    _execution_bars,
    _fingerprint_value,
    _decision_source_snapshot,
    _decision_source_snapshot_matches_current,
    _market_history_start,
    _market_grids,
    _performance_adjudication,
    _reconciled_market_calendar,
    _research_risk_disposition,
    _risk_gate,
    _sector_risk_gate,
    _series_metrics,
    _symbol_daily_history_start,
    main,
)

import pytest


CN = ZoneInfo("Asia/Shanghai")


def test_human_review_payload_preserves_nested_ranking_evidence_identity() -> None:
    observed_at = datetime(2026, 4, 20, 10, 0, tzinfo=CN)
    evidence = SectorRankingReviewEvidence(
        source_profile="HISTORICAL_TRIGGER_SUMMARY",
        sector_id="sector:test",
        sector_name="测试板块",
        observed_at=observed_at,
        eligible=True,
        hard_block=False,
        regime="neutral",
        ordinal=1,
        rank_score=5,
        rank_components=(),
        reason_codes=("structural_ranking_only",),
        strength_member_count=1,
    )
    alert = HumanReviewAlert(
        symbol="SZ.000001",
        alert_type="POSSIBLE_30M_BUY",
        signal_at=observed_at,
        review_available_at=observed_at,
        source_point_id="point:test",
        structure_snapshot_id="structure:test",
        sector_id=evidence.sector_id,
        confidence="MEDIUM",
        review_priority=50,
        reference_price=Decimal("10"),
        structural_invalidation_price=Decimal("9"),
        market_risk_gate="AMBER",
        sector_risk_gate="AMBER",
        symbol_risk_gate="AMBER",
        warning_codes=("HUMAN_REVIEW_REQUIRED",),
        source_fact_ids=("fact:test", evidence.evidence_id),
        screening_parameter_set_id=(
            FULL_MARKET.human_review_screening_parameters().parameter_set_id
        ),
        technical_approximation_parameter_set_id=(
            FULL_MARKET.technical_approximation_parameters().parameter_set_id
        ),
        sector_ranking_evidence=evidence,
    )

    payload = FULL_MARKET._human_review_alert_payload(alert)

    assert payload["sector_ranking_evidence"]["evidence_id"] == evidence.evidence_id
    assert parse_human_review_alert(payload) == alert


def _warmup_convergence_document(
    observed_at: datetime,
    *,
    non_monotonic: bool = True,
) -> dict[str, object]:
    signatures = ("a", "b", "a", "a") if non_monotonic else ("a",) * 4
    observations = tuple(
        WarmupPrefixObservation(
            bar_count=count,
            starts_at=observed_at - timedelta(days=count),
            signature_sha256="sha256:" + signature * 64,
        )
        for count, signature in zip((480, 640, 800, 960), signatures)
    )
    return classify_warmup_convergence_envelope(
        frequency="d",
        as_of=observed_at,
        parameter_set_id=(
            QMT_HIGHER_TIMEFRAME_WARMUP_CONVERGENCE_PARAMETER_SET_ID
        ),
        observations=observations,
    ).document()


def _warmup_semantic_documents(
    observed_at: datetime,
) -> tuple[dict[str, object], dict[str, object]]:
    def snapshot(
        *,
        weekly_state: str = "NONE",
        daily_ma5: str = "10",
    ) -> WarmupSemanticSnapshot:
        return WarmupSemanticSnapshot(
            periods=tuple(
                WarmupPeriodSemanticFacts(
                    period=period,
                    state=(weekly_state if period == "W" else "NONE"),
                    evidence_bar_end=observed_at - timedelta(days=index + 1),
                    active_top_interval=None,
                    mapping_unique=False,
                    mapped_center_id=None,
                    mapping_candidate_ids=(),
                    blocker_codes=(f"{period}_CENTER_MAPPING_UNRESOLVED",),
                    warning_codes=(),
                )
                for index, period in enumerate(("M", "W", "D"))
            ),
            ma5=(
                ("M", Decimal("8")),
                ("W", Decimal("9")),
                ("D", Decimal(daily_ma5)),
            ),
        )

    baseline = snapshot()
    intermediate = snapshot(weekly_state="FORMED", daily_ma5="11")
    snapshots = (baseline, intermediate, baseline, baseline)
    observations = tuple(
        WarmupPrefixObservation(
            bar_count=count,
            starts_at=observed_at - timedelta(days=count),
            signature_sha256=value.signature_sha256,
        )
        for count, value in zip((480, 640, 800, 960), snapshots)
    )
    envelope = classify_warmup_convergence_envelope(
        frequency="d",
        as_of=observed_at,
        parameter_set_id=(
            QMT_HIGHER_TIMEFRAME_WARMUP_CONVERGENCE_PARAMETER_SET_ID
        ),
        observations=observations,
    )
    envelope = bind_warmup_convergence_diagnostic(
        envelope,
        snapshots=snapshots,
    )
    assert envelope.diagnostic is not None
    return envelope.document(), envelope.diagnostic.document()


def _warmup_mapping_supply_documents(
    observed_at: datetime,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    center_id = "sha256:" + "a" * 64

    def snapshot(*, unique: bool) -> WarmupSemanticSnapshot:
        return WarmupSemanticSnapshot(
            periods=tuple(
                WarmupPeriodSemanticFacts(
                    period=period,
                    state=(
                        "FORMED"
                        if period == "W" and unique
                        else "FORMED_UNRESOLVED"
                        if period == "W"
                        else "NONE"
                    ),
                    evidence_bar_end=observed_at - timedelta(days=6),
                    active_top_interval=(
                        (
                            observed_at - timedelta(days=12),
                            observed_at - timedelta(days=6),
                        )
                        if period == "W"
                        else None
                    ),
                    mapping_unique=(unique if period == "W" else True),
                    mapped_center_id=(
                        center_id if period == "W" and unique else None
                    ),
                    mapping_candidate_ids=(
                        (center_id,) if period == "W" and unique else ()
                    ),
                    blocker_codes=(
                        (
                            "NO_COMPLETED_LOWER_1SELL_2SELL_CENTER_"
                            "IN_TOP_FRACTAL",
                        )
                        if period == "W" and not unique
                        else ()
                    ),
                    warning_codes=(),
                )
                for period in ("M", "W", "D")
            ),
            ma5=(
                ("M", Decimal("8")),
                ("W", Decimal("9")),
                ("D", Decimal("10")),
            ),
        )

    def supply(*, unique: bool) -> RiskMappingSupplyFacts:
        point_type = "1sell" if unique else "3buy"
        point_center = center_id if unique else "sha256:" + "b" * 64
        anchor = observed_at - timedelta(days=8 if unique else 7)
        available = anchor + timedelta(days=1)
        point = RiskMappingPointEvidenceFacts(
            point_id=RiskMappingPointEvidenceFacts.identity(
                source_symbol="SH.000001",
                source_frequency="d",
                center_id=point_center,
                center_level_rank=1,
                point_type=point_type,
                point_anchor_at=anchor,
                point_available_at=available,
            ),
            source_symbol="SH.000001",
            source_frequency="d",
            center_id=point_center,
            center_level_rank=1,
            center_completed=True,
            center_expanded=False,
            point_type=point_type,  # type: ignore[arg-type]
            point_anchor_at=anchor,
            point_available_at=available,
            inside_active_top_interval=True,
            highest_mapping_candidate=unique,
        )
        return RiskMappingSupplyFacts(
            classification=(
                "UNIQUE_MAPPING" if unique else "ONLY_THIRD_CLASS_POINTS"
            ),
            lower_structure_available=True,
            point_evidence_count=1,
            point_type_counts=(
                ("1sell", int(unique)),
                ("2sell", 0),
                ("3sell", 0),
                ("3buy", int(not unique)),
            ),
            completed_sell12_count=int(unique),
            in_top_interval_sell12_count=int(unique),
            completed_in_top_interval_sell12_count=int(unique),
            incomplete_in_top_interval_sell12_count=0,
            outside_top_interval_sell12_count=0,
            highest_candidate_center_count=int(unique),
            point_evidence=(point,),
            diagnostic_buy_point_type_counts=(("1buy", 0), ("2buy", 0)),
            diagnostic_buy_point_evidence=(),
        )

    baseline = snapshot(unique=False)
    intermediate = snapshot(unique=True)
    snapshots = (baseline, intermediate, baseline, baseline)
    observations = tuple(
        WarmupPrefixObservation(
            bar_count=count,
            starts_at=observed_at - timedelta(days=count),
            signature_sha256=value.signature_sha256,
        )
        for count, value in zip((480, 640, 800, 960), snapshots)
    )
    envelope = classify_warmup_convergence_envelope(
        frequency="d",
        as_of=observed_at,
        parameter_set_id=(
            QMT_HIGHER_TIMEFRAME_WARMUP_CONVERGENCE_PARAMETER_SET_ID
        ),
        observations=observations,
    )
    envelope = bind_warmup_convergence_diagnostic(
        envelope,
        snapshots=snapshots,
    )
    baseline_supply = WarmupMappingSupplySnapshot(
        periods=(("M", None), ("W", supply(unique=False)), ("D", None))
    )
    envelope = bind_warmup_mapping_supply_diagnostic(
        envelope,
        snapshots=(
            baseline_supply,
            WarmupMappingSupplySnapshot(
                periods=(("M", None), ("W", supply(unique=True)), ("D", None))
            ),
            baseline_supply,
            baseline_supply,
        ),
    )
    assert envelope.diagnostic is not None
    assert envelope.mapping_supply_diagnostic is not None
    return (
        envelope.document(),
        envelope.diagnostic.document(),
        envelope.mapping_supply_diagnostic.document(),
    )


def _effectiveness_evidence(
    state: str = "NONE",
    *,
    subject: str | None = None,
    gate: str | None = None,
    observed_at: datetime = datetime(2025, 8, 1, 10, 0, tzinfo=CN),
) -> dict[str, object]:
    evidence: dict[str, object] = {
        "monthly": state,
        "weekly": state,
        "daily": state,
        "period_diagnostics": [
            {"period": period, "state": state, "blocker_codes": []}
            for period in ("M", "W", "D")
        ],
        "warmup": {"reason_code": "QMT_HIGHER_TIMEFRAME_WARMUP_CONVERGED"},
    }
    if subject == "sector":
        strict_source = gate == "GREEN"
        full_count = 720 if strict_source else 240
        strict_reason = (
            "QMT_HIGHER_TIMEFRAME_WARMUP_TAIL_STABLE"
            if strict_source
            else "QMT_HIGHER_TIMEFRAME_WARMUP_HISTORY_INSUFFICIENT"
        )
        coverage = QmtSectorSameBaseCoverageEvidence(
            observed_at=observed_at,
            calendar_first_session=date(2023, 5, 4),
            first_visible_bar_at=observed_at.replace(hour=9, minute=35),
            last_visible_bar_at=observed_at,
            first_completed_session=date(2024, 7, 25),
            last_completed_session=observed_at.date() - timedelta(days=1),
            visible_five_minute_bar_count=full_count * 48,
            completed_daily_bar_count=full_count,
            required_daily_bar_count=480,
            remaining_daily_bar_count=max(0, 480 - full_count),
            missing_leading_calendar_session_count=(0 if strict_source else 300),
            warmup_converged=strict_source,
            warmup_reason_code=strict_reason,
            boundary_status=(
                "REQUIRED_HISTORY_CONVERGED"
                if strict_source
                else "VISIBLE_PREFIX_STARTS_AFTER_REQUESTED_WARMUP"
            ),
            physical_source_boundary_status=(
                "REQUESTED_REPLAY_LEFT_BOUNDARY_CLIPS_EARLIER_QMT_HISTORY"
                if strict_source
                else "PHYSICAL_QMT_CACHE_LEFT_BOUNDARY_AFTER_REQUESTED_WARMUP"
            ),
            physical_source_requested_start_at=datetime(
                2023, 5, 1, 9, 30, tzinfo=CN
            ),
            physical_source_required_contributor_start_at=(
                observed_at - timedelta(days=900 if strict_source else 400)
            ),
            physical_source_representative_member_count=24,
            physical_source_available_member_count=23,
            physical_source_required_contributor_count=15,
            physical_source_inventory_revision="sha256:" + "6" * 64,
        )
        evidence.update(
            {
                "source_mode": (
                    "PAGE_PARITY_SAME_5M_BASE"
                    if strict_source
                    else "NATIVE_DAILY_MWD_PLUS_5M_30M_UNRECONCILED_RESEARCH"
                ),
                "strict_same_5m_warmup": {
                    "converged": strict_source,
                    "reason_code": strict_reason,
                    "full_daily_bar_count": full_count,
                    "required_daily_bar_count": 480,
                },
                "strict_same_5m_source_coverage": coverage.document(),
            }
        )
    return evidence


def _effectiveness_row(
    index: int,
    *,
    market: str,
    sector: str,
    symbol: str,
) -> dict[str, object]:
    observed_at = datetime(2025, 8, 1, 10, index, tzinfo=CN)
    row: dict[str, object] = {
        "decision_at": observed_at,
        "accepted": all(value in {"GREEN", "AMBER"} for value in (market, sector, symbol)),
        "exact_green": all(value == "GREEN" for value in (market, sector, symbol)),
        "sector_eligible": True,
        "sector_hard_block": False,
    }
    for subject, gate in (("market", market), ("sector", sector), ("symbol", symbol)):
        row[f"{subject}_risk_gate"] = gate
        row[f"{subject}_risk_blocker_codes"] = (
            [] if gate == "GREEN" else [f"{subject.upper()}_{gate}_REASON"]
        )
        row[f"{subject}_risk_warmup_evidence"] = (
            None
            if gate == "UNRESOLVED"
            else _effectiveness_evidence(
                subject=subject,
                gate=gate,
                observed_at=observed_at,
            )
        )
    return row


def _native_calendar_coverage(
    *,
    symbol: str,
    observed_at: datetime,
    missing: tuple[date, ...] = (),
) -> QmtNativeDailyCalendarCoverageEvidence:
    return QmtNativeDailyCalendarCoverageEvidence(
        symbol=symbol,
        observed_at=observed_at,
        native_first_session=date(2025, 7, 1),
        native_last_session=observed_at.date(),
        calendar_first_session=date(2025, 7, 1),
        calendar_last_session=observed_at.date(),
        native_daily_bar_count=5 - len(missing),
        expected_calendar_session_count=5,
        native_only_sessions=(),
        unexplained_calendar_only_sessions=missing,
        trading_calendar_revision="sha256:" + "9" * 64,
        status=(
            "EXACT"
            if not missing
            else "UNEXPLAINED_CALENDAR_SESSION_MISSING"
        ),
    )


def test_higher_timeframe_effectiveness_separates_strict_and_research_gates() -> None:
    audit = higher_timeframe_effectiveness_audit(
        (
            _effectiveness_row(0, market="GREEN", sector="GREEN", symbol="GREEN"),
            _effectiveness_row(1, market="AMBER", sector="AMBER", symbol="AMBER"),
            _effectiveness_row(
                2,
                market="AMBER",
                sector="AMBER",
                symbol="UNRESOLVED",
            ),
        )
    )

    assert audit["candidate_count"] == 3
    assert audit["strict_green_risk_eligible_count"] == 1
    assert audit["research_green_or_amber_risk_eligible_count"] == 2
    assert audit["research_amber_only_risk_eligible_count"] == 1
    assert audit["hard_rejected_candidate_count"] == 1
    assert audit["subjects"]["symbol"]["gate_counts"] == {
        "AMBER": 1,
        "GREEN": 1,
        "UNRESOLVED": 1,
    }
    assert audit["subjects"]["symbol"]["blocker_candidate_counts"][
        "SYMBOL_UNRESOLVED_REASON"
    ] == 1
    assert audit["subjects"]["sector"]["source_mode_counts"] == {
        "NATIVE_DAILY_MWD_PLUS_5M_30M_UNRECONCILED_RESEARCH": 2,
        "PAGE_PARITY_SAME_5M_BASE": 1,
    }
    assert audit["subjects"]["sector"][
        "strict_same_base_warmup_reason_counts"
    ] == {
        "QMT_HIGHER_TIMEFRAME_WARMUP_HISTORY_INSUFFICIENT": 2,
        "QMT_HIGHER_TIMEFRAME_WARMUP_TAIL_STABLE": 1,
    }
    assert audit["subjects"]["sector"][
        "strict_same_base_source_boundary_counts"
    ] == {
        "REQUIRED_HISTORY_CONVERGED": 1,
        "VISIBLE_PREFIX_STARTS_AFTER_REQUESTED_WARMUP": 2,
    }
    assert audit["subjects"]["sector"][
        "strict_same_base_physical_source_boundary_counts"
    ] == {
        "PHYSICAL_QMT_CACHE_LEFT_BOUNDARY_AFTER_REQUESTED_WARMUP": 2,
        "REQUESTED_REPLAY_LEFT_BOUNDARY_CLIPS_EARLIER_QMT_HISTORY": 1,
    }
    assert audit["subjects"]["sector"][
        "strict_same_base_physical_representative_member_range"
    ] == {"minimum": 24, "maximum": 24}
    assert audit["subjects"]["sector"][
        "strict_same_base_physical_available_member_range"
    ] == {"minimum": 23, "maximum": 23}
    assert audit["subjects"]["sector"][
        "strict_same_base_physical_required_member_range"
    ] == {"minimum": 15, "maximum": 15}
    assert audit["subjects"]["sector"][
        "strict_same_base_physical_requested_start_range"
    ] == {
        "minimum": "2023-05-01T09:30:00+08:00",
        "maximum": "2023-05-01T09:30:00+08:00",
    }
    assert audit["subjects"]["sector"][
        "strict_same_base_completed_daily_bar_range"
    ] == {"minimum": 240, "maximum": 720}
    assert audit["subjects"]["sector"][
        "strict_same_base_remaining_daily_bar_range"
    ] == {"minimum": 0, "maximum": 240}
    assert str(audit["audit_sha256"]).startswith("sha256:")


def test_higher_timeframe_effectiveness_surfaces_multi_prefix_false_stability(
) -> None:
    row = _effectiveness_row(
        0,
        market="GREEN",
        sector="GREEN",
        symbol="GREEN",
    )
    observed_at = row["decision_at"]
    assert isinstance(observed_at, datetime)
    convergence = _warmup_convergence_document(observed_at)
    for subject in ("market", "sector", "symbol"):
        evidence = row[f"{subject}_risk_warmup_evidence"]
        assert isinstance(evidence, dict)
        evidence["warmup"] = {
            "reason_code": "QMT_HIGHER_TIMEFRAME_WARMUP_TAIL_STABLE"
        }
        evidence["warmup_convergence"] = copy.deepcopy(convergence)
    sector_evidence = row["sector_risk_warmup_evidence"]
    assert isinstance(sector_evidence, dict)
    sector_evidence["strict_same_5m_warmup_convergence"] = copy.deepcopy(
        convergence
    )

    audit = higher_timeframe_effectiveness_audit((row,))

    for subject in ("market", "sector", "symbol"):
        summary = audit["subjects"][subject]["warmup_convergence"]
        assert summary["status_counts"] == {"NON_MONOTONIC": 1}
        assert summary["evidence_candidate_count"] == 1
        assert summary["qualified_prefix_count_range"] == {
            "minimum": 4,
            "maximum": 4,
        }
        assert (
            summary[
                "pairwise_stable_without_all_prefix_stability_count"
            ]
            == 1
        )
        assert summary["diagnostic_only"] is True
        assert summary["active_gate_unchanged"] is True
    assert audit["subjects"]["sector"]["warmup_convergence"][
        "strict_same_base_status_counts"
    ] == {"NON_MONOTONIC": 1}
    assert audit["strict_green_risk_eligible_count"] == 1


def test_higher_timeframe_effectiveness_explains_non_monotonic_mwd_facts(
) -> None:
    row = _effectiveness_row(
        0,
        market="GREEN",
        sector="GREEN",
        symbol="GREEN",
    )
    row["sector_id"] = "qmt-gics3:45101010"
    row["symbol"] = "SZ.000001"
    observed_at = row["decision_at"]
    assert isinstance(observed_at, datetime)
    convergence, diagnostic = _warmup_semantic_documents(observed_at)
    for subject in ("market", "sector", "symbol"):
        evidence = row[f"{subject}_risk_warmup_evidence"]
        assert isinstance(evidence, dict)
        evidence["warmup"] = {
            "reason_code": "QMT_HIGHER_TIMEFRAME_WARMUP_TAIL_STABLE"
        }
        evidence["warmup_convergence"] = copy.deepcopy(convergence)
        evidence["warmup_convergence_diagnostic"] = copy.deepcopy(
            diagnostic
        )
    sector_evidence = row["sector_risk_warmup_evidence"]
    assert isinstance(sector_evidence, dict)
    sector_evidence["strict_same_5m_warmup_convergence"] = copy.deepcopy(
        convergence
    )
    sector_evidence[
        "strict_same_5m_warmup_convergence_diagnostic"
    ] = copy.deepcopy(diagnostic)

    audit = higher_timeframe_effectiveness_audit((row,))

    assert audit["schema"] == (
        "chanlun-v3-higher-timeframe-effectiveness-audit/v15"
    )
    for subject in ("market", "sector", "symbol"):
        summary = audit["subjects"][subject]["warmup_convergence"]
        assert summary["semantic_diagnostic_status_counts"] == {
            "NON_MONOTONIC": 1
        }
        assert summary["non_monotonic_changed_period_counts"] == {
            "D": 1,
            "W": 1,
        }
        assert summary["non_monotonic_changed_path_counts"] == {
            "D.ma5": 1,
            "W.state": 1,
        }
        point_audit = audit["subjects"][subject][
            "warmup_non_monotonic_point_audit"
        ]
        assert point_audit["point_count"] == 2
        assert point_audit["distinct_point_id_count"] == 2
        assert [value["chart_interval"] for value in point_audit["points"]] == [
            "1D",
            "1W",
        ]
        assert all(
            value["diagnostic_only"] is True
            and value["active_gate_unchanged"] is True
            for value in point_audit["points"]
        )
    market_points = audit["subjects"]["market"][
        "warmup_non_monotonic_point_audit"
    ]["points"]
    assert {value["source_symbol"] for value in market_points} == {
        "SH.000001"
    }
    assert {
        tuple(value["changed_paths"]) for value in market_points
    } == {("D.ma5",), ("W.state",)}
    daily = next(
        value for value in market_points if value["source_frequency"] == "d"
    )
    weekly = next(
        value for value in market_points if value["source_frequency"] == "w"
    )
    assert (daily["prefix_ma5"], daily["reference_ma5"]) == ("11", "10")
    assert (
        weekly["prefix_period_facts"]["state"],
        weekly["reference_period_facts"]["state"],
    ) == ("FORMED", "NONE")
    assert audit["subjects"]["sector"]["warmup_convergence"][
        "strict_same_base_semantic_diagnostic_status_counts"
    ] == {"NON_MONOTONIC": 1}
    assert audit["strict_green_risk_eligible_count"] == 1


def test_higher_timeframe_effectiveness_rejects_rehashed_semantic_diagnostic(
) -> None:
    row = _effectiveness_row(
        0,
        market="GREEN",
        sector="GREEN",
        symbol="GREEN",
    )
    observed_at = row["decision_at"]
    assert isinstance(observed_at, datetime)
    convergence, diagnostic = _warmup_semantic_documents(observed_at)
    market = row["market_risk_warmup_evidence"]
    assert isinstance(market, dict)
    diagnostic["observations"][1]["changed_paths_from_longest"] = [
        "M.state"
    ]
    stable = dict(diagnostic)
    stable.pop("content_sha256")
    diagnostic["content_sha256"] = sha256_json(stable)
    market["warmup_convergence"] = convergence
    market["warmup_convergence_diagnostic"] = diagnostic

    with pytest.raises(ValueError, match="non-canonical"):
        higher_timeframe_effectiveness_audit((row,))


def test_higher_timeframe_effectiveness_explains_mapping_supply_resegmentation(
) -> None:
    row = _effectiveness_row(
        0,
        market="GREEN",
        sector="GREEN",
        symbol="GREEN",
    )
    row["sector_id"] = "qmt-gics3:45101010"
    row["symbol"] = "SZ.000001"
    observed_at = row["decision_at"]
    assert isinstance(observed_at, datetime)
    convergence, semantic, supply = _warmup_mapping_supply_documents(
        observed_at
    )
    for subject in ("market", "sector", "symbol"):
        evidence = row[f"{subject}_risk_warmup_evidence"]
        assert isinstance(evidence, dict)
        evidence["warmup"] = {
            "reason_code": "QMT_HIGHER_TIMEFRAME_WARMUP_TAIL_STABLE"
        }
        evidence["warmup_convergence"] = copy.deepcopy(convergence)
        evidence["warmup_convergence_diagnostic"] = copy.deepcopy(semantic)
        evidence["warmup_mapping_supply_diagnostic"] = copy.deepcopy(supply)
    sector_evidence = row["sector_risk_warmup_evidence"]
    assert isinstance(sector_evidence, dict)
    sector_evidence["strict_same_5m_warmup_convergence"] = copy.deepcopy(
        convergence
    )
    sector_evidence[
        "strict_same_5m_warmup_convergence_diagnostic"
    ] = copy.deepcopy(semantic)
    sector_evidence[
        "strict_same_5m_warmup_mapping_supply_diagnostic"
    ] = copy.deepcopy(supply)

    audit = higher_timeframe_effectiveness_audit((row,))

    for subject in ("market", "sector", "symbol"):
        summary = audit["subjects"][subject]["warmup_convergence"]
        assert summary["mapping_supply_diagnostic_status_counts"] == {
            "NON_MONOTONIC": 1
        }
        assert summary["mapping_supply_comparison_period_counts"] == {"W": 1}
        assert summary["mapping_supply_transition_code_counts"] == {
            "COMPLETED_IN_INTERVAL_SELL12_DISAPPEARED_WITH_LONGER_HISTORY": 1,
            "HIGHEST_CANDIDATE_DISAPPEARED_WITH_LONGER_HISTORY": 1,
            "POINT_EVIDENCE_GAINED_WITH_LONGER_HISTORY": 1,
            "POINT_EVIDENCE_LOST_WITH_LONGER_HISTORY": 1,
            "POINT_IDENTITY_SET_RESEGMENTED": 1,
            "SELL12_DISAPPEARED_WITH_LONGER_HISTORY": 1,
            "SUPPLY_CLASSIFICATION_CHANGED": 1,
        }
        warmup_point = audit["subjects"][subject][
            "warmup_non_monotonic_point_audit"
        ]["points"][0]
        assert warmup_point["mapping_supply_comparison"]["delta"][
            "prefix_classification"
        ] == "UNIQUE_MAPPING"
        supply_points = audit["subjects"][subject][
            "warmup_mapping_supply_point_audit"
        ]
        assert supply_points["point_count"] == 2
        assert supply_points["distinct_structural_point_id_count"] == 2
        assert supply_points["delta_direction_counts"] == {
            "GAINED_IN_LONGEST": 1,
            "LOST_FROM_LONGEST": 1,
        }
        assert supply_points["point_type_counts"] == {
            "1sell": 1,
            "3buy": 1,
        }
    market_points = audit["subjects"]["market"][
        "warmup_mapping_supply_point_audit"
    ]["points"]
    assert all(value["chart_interval"] == "1D" for value in market_points)
    assert all(value["chart_focus_supported"] is True for value in market_points)
    lost = next(
        value
        for value in market_points
        if value["delta_direction"] == "LOST_FROM_LONGEST"
    )
    assert lost["point_type"] == "1sell"
    assert lost["highest_mapping_candidate"] is True
    assert audit["subjects"]["sector"]["warmup_convergence"][
        "strict_same_base_mapping_supply_diagnostic_status_counts"
    ] == {"NON_MONOTONIC": 1}


def test_higher_timeframe_effectiveness_rejects_rehashed_mapping_supply_delta(
) -> None:
    row = _effectiveness_row(
        0,
        market="GREEN",
        sector="GREEN",
        symbol="GREEN",
    )
    observed_at = row["decision_at"]
    assert isinstance(observed_at, datetime)
    convergence, semantic, supply = _warmup_mapping_supply_documents(
        observed_at
    )
    market = row["market_risk_warmup_evidence"]
    assert isinstance(market, dict)
    supply["comparisons"][0]["delta"]["transition_codes"] = [
        "MAPPING_SUPPLY_UNCHANGED"
    ]
    stable = dict(supply)
    stable.pop("content_sha256")
    supply["content_sha256"] = sha256_json(stable)
    market["warmup_convergence"] = convergence
    market["warmup_convergence_diagnostic"] = semantic
    market["warmup_mapping_supply_diagnostic"] = supply

    with pytest.raises(ValueError, match="malformed"):
        higher_timeframe_effectiveness_audit((row,))


def test_higher_timeframe_effectiveness_rejects_rehashed_convergence_verdict(
) -> None:
    row = _effectiveness_row(
        0,
        market="GREEN",
        sector="GREEN",
        symbol="GREEN",
    )
    observed_at = row["decision_at"]
    assert isinstance(observed_at, datetime)
    market = row["market_risk_warmup_evidence"]
    assert isinstance(market, dict)
    forged = _warmup_convergence_document(observed_at)
    forged["status"] = "STABLE_ALL_PREFIXES"
    forged["stable_all_prefixes"] = True
    forged["match_longest_pattern"] = [True, True, True, True]
    forged["reason_codes"] = ["WARMUP_ENVELOPE_STABLE_ALL_PREFIXES"]
    stable = dict(forged)
    stable.pop("content_sha256")
    forged["content_sha256"] = sha256_json(stable)
    market["warmup_convergence"] = forged

    with pytest.raises(ValueError, match="malformed"):
        higher_timeframe_effectiveness_audit((row,))


def test_higher_timeframe_effectiveness_counts_native_daily_calendar_gaps(
) -> None:
    row = _effectiveness_row(
        0,
        market="GREEN",
        sector="AMBER",
        symbol="UNRESOLVED",
    )
    observed_at = row["decision_at"]
    assert isinstance(observed_at, datetime)
    missing = (date(2025, 7, 31),)
    row["symbol_risk_blocker_codes"] = [
        "QMT_NATIVE_DAILY_TRADING_CALENDAR_MISMATCH"
    ]
    row["market_risk_native_daily_calendar_coverage_evidence"] = (
        _native_calendar_coverage(
            symbol="SH.000001",
            observed_at=observed_at,
        ).document()
    )
    row["sector_risk_native_daily_calendar_coverage_evidence"] = None
    row["symbol_risk_native_daily_calendar_coverage_evidence"] = (
        _native_calendar_coverage(
            symbol="SZ.000001",
            observed_at=observed_at,
            missing=missing,
        ).document()
    )
    row["symbol"] = "SZ.000001"

    audit = higher_timeframe_effectiveness_audit((row,))
    symbol_coverage = audit["subjects"]["symbol"][
        "native_daily_calendar_coverage"
    ]
    assert symbol_coverage["status_counts"] == {
        "UNEXPLAINED_CALENDAR_SESSION_MISSING": 1
    }
    assert symbol_coverage[
        "unexplained_missing_session_occurrence_count"
    ] == 1
    assert symbol_coverage["gap_rows"][0][
        "unexplained_calendar_only_sessions"
    ] == ["2025-07-31"]
    assert symbol_coverage["missing_session_interpretation"] == (
        "UNEXPLAINED_NEVER_INFERRED_AS_SUSPENSION"
    )
    assert audit["subjects"]["market"][
        "native_daily_calendar_coverage"
    ]["status_counts"] == {"EXACT": 1}


def test_higher_timeframe_effectiveness_rejects_missing_resolved_evidence() -> None:
    row = _effectiveness_row(0, market="AMBER", sector="AMBER", symbol="AMBER")
    row["market_risk_warmup_evidence"] = None

    with pytest.raises(ValueError, match="market resolved gate lost"):
        higher_timeframe_effectiveness_audit((row,))


def test_higher_timeframe_effectiveness_rejects_relabelled_period_or_exact_green(
) -> None:
    relabelled = _effectiveness_row(
        0,
        market="AMBER",
        sector="AMBER",
        symbol="AMBER",
    )
    relabelled["market_risk_warmup_evidence"]["period_diagnostics"][0][
        "state"
    ] = "FORMED"
    with pytest.raises(ValueError, match="resolved gate diverges"):
        higher_timeframe_effectiveness_audit((relabelled,))

    forged_exact = _effectiveness_row(
        0,
        market="GREEN",
        sector="GREEN",
        symbol="GREEN",
    )
    forged_exact["exact_green"] = False
    with pytest.raises(ValueError, match="exact-green evidence"):
        higher_timeframe_effectiveness_audit((forged_exact,))


def test_higher_timeframe_effectiveness_keeps_raw_diagnostics_when_warmup_fails(
) -> None:
    row = _effectiveness_row(
        0,
        market="AMBER",
        sector="AMBER",
        symbol="UNRESOLVED",
    )
    unresolved = _effectiveness_evidence("UNRESOLVED")
    unresolved["period_diagnostics"] = [
        {
            "period": period,
            "state": "FORMED_UNRESOLVED",
            "blocker_codes": [f"{period}_CENTER_MAPPING_UNRESOLVED"],
        }
        for period in ("M", "W", "D")
    ]
    unresolved["warmup"] = {
        "reason_code": "QMT_HIGHER_TIMEFRAME_WARMUP_TAIL_DIVERGED"
    }
    row["symbol_risk_warmup_evidence"] = unresolved

    audit = higher_timeframe_effectiveness_audit((row,))

    assert audit["subjects"]["symbol"]["period_state_counts"] == {
        "M": {"FORMED_UNRESOLVED": 1},
        "W": {"FORMED_UNRESOLVED": 1},
        "D": {"FORMED_UNRESOLVED": 1},
    }
    assert audit["subjects"]["symbol"]["effective_period_state_counts"] == {
        "M": {"UNRESOLVED": 1},
        "W": {"UNRESOLVED": 1},
        "D": {"UNRESOLVED": 1},
    }
    assert audit["subjects"]["symbol"]["state_override_candidate_count"] == 1
    assert audit["subjects"]["symbol"]["period_blocker_counts"] == {
        "D_CENTER_MAPPING_UNRESOLVED": 1,
        "M_CENTER_MAPPING_UNRESOLVED": 1,
        "W_CENTER_MAPPING_UNRESOLVED": 1,
    }
    assert audit["subjects"]["symbol"]["warmup_reason_counts"] == {
        "QMT_HIGHER_TIMEFRAME_WARMUP_TAIL_DIVERGED": 1
    }


def test_higher_timeframe_effectiveness_audits_mapping_supply_without_promoting_third_points(
) -> None:
    row = _effectiveness_row(
        0,
        market="AMBER",
        sector="AMBER",
        symbol="AMBER",
    )
    market = row["market_risk_warmup_evidence"]
    market["monthly"] = "FORMED_UNRESOLVED"
    market["period_diagnostics"][0] = {
        "period": "M",
        "state": "FORMED_UNRESOLVED",
        "active_top_interval": [
            "2025-01-01T15:00:00+08:00",
            "2025-03-31T15:00:00+08:00",
        ],
        "mapping_unique": False,
        "mapping_candidate_ids": [],
        "blocker_codes": [
            "NO_COMPLETED_LOWER_1SELL_2SELL_CENTER_IN_TOP_FRACTAL"
        ],
        "mapping_supply": {
            "classification": "ONLY_THIRD_CLASS_POINTS",
            "lower_structure_available": True,
            "point_evidence_count": 5,
            "point_type_counts": {
                "1sell": 0,
                "2sell": 0,
                "3sell": 2,
                "3buy": 3,
            },
            "completed_sell12_count": 0,
            "in_top_interval_sell12_count": 0,
            "completed_in_top_interval_sell12_count": 0,
            "incomplete_in_top_interval_sell12_count": 0,
            "outside_top_interval_sell12_count": 0,
            "highest_candidate_center_count": 0,
        },
    }

    audit = higher_timeframe_effectiveness_audit((row,))
    subject = audit["subjects"]["market"]
    assert subject["mapping_supply_class_counts"] == {
        "ONLY_THIRD_CLASS_POINTS": 1
    }
    assert subject["mapping_point_type_counts"] == {
        "1sell": 0,
        "2sell": 0,
        "3sell": 2,
        "3buy": 3,
    }
    assert subject["mapping_supply_totals"] == {
        "active_top_period_count": 1,
        "retained_mapping_supply_period_count": 1,
        "missing_active_mapping_supply_period_count": 0,
        "point_evidence_count": 5,
        "completed_sell12_count": 0,
        "in_top_interval_sell12_count": 0,
        "completed_in_top_interval_sell12_count": 0,
        "diagnostic_buy_point_evidence_count": 0,
        "diagnostic_buy_point_unrecorded_period_count": 1,
        "diagnostic_buy_point_identified_occurrence_count": 0,
        "diagnostic_buy_point_identity_unrecorded_period_count": 0,
    }
    assert subject["diagnostic_directional_class_counts"] == {
        "NOT_RECORDED_LEGACY": 1
    }
    assert subject["diagnostic_buy_point_type_counts"] == {
        "1buy": 0,
        "2buy": 0,
    }

    forged = copy.deepcopy(row)
    forged["market_risk_warmup_evidence"]["period_diagnostics"][0][
        "mapping_supply"
    ]["point_type_counts"]["1sell"] = 1
    with pytest.raises(ValueError, match="point-type counts do not reconcile"):
        higher_timeframe_effectiveness_audit((forged,))


def test_higher_timeframe_effectiveness_surfaces_buy_side_directional_supply(
) -> None:
    row = _effectiveness_row(
        0,
        market="AMBER",
        sector="AMBER",
        symbol="AMBER",
    )
    market = row["market_risk_warmup_evidence"]
    market["monthly"] = "FORMED_UNRESOLVED"
    market["period_diagnostics"][0] = {
        "period": "M",
        "state": "FORMED_UNRESOLVED",
        "active_top_interval": [
            "2025-01-01T15:00:00+08:00",
            "2025-03-31T15:00:00+08:00",
        ],
        "mapping_unique": False,
        "mapping_candidate_ids": [],
        "blocker_codes": [
            "NO_COMPLETED_LOWER_1SELL_2SELL_CENTER_IN_TOP_FRACTAL"
        ],
        "mapping_supply": {
            "classification": "ONLY_THIRD_CLASS_POINTS",
            "lower_structure_available": True,
            "point_evidence_count": 5,
            "point_type_counts": {
                "1sell": 0,
                "2sell": 0,
                "3sell": 2,
                "3buy": 3,
            },
            "diagnostic_buy_point_type_counts": {
                "1buy": 4,
                "2buy": 2,
            },
            "diagnostic_directional_classification": (
                "BUY12_PRESENT_SELL12_ABSENT"
            ),
            "completed_sell12_count": 0,
            "in_top_interval_sell12_count": 0,
            "completed_in_top_interval_sell12_count": 0,
            "incomplete_in_top_interval_sell12_count": 0,
            "outside_top_interval_sell12_count": 0,
            "highest_candidate_center_count": 0,
        },
    }

    audit = higher_timeframe_effectiveness_audit((row,))
    subject = audit["subjects"]["market"]
    assert subject["diagnostic_directional_class_counts"] == {
        "BUY12_PRESENT_SELL12_ABSENT": 1
    }
    assert subject["diagnostic_buy_point_type_counts"] == {
        "1buy": 4,
        "2buy": 2,
    }
    assert subject["mapping_supply_totals"][
        "diagnostic_buy_point_evidence_count"
    ] == 6
    assert subject["mapping_supply_totals"][
        "diagnostic_buy_point_unrecorded_period_count"
    ] == 0
    unique = subject["unique_active_top_event_audit"]
    assert unique["terminal_diagnostic_directional_class_counts"] == {
        "BUY12_PRESENT_SELL12_ABSENT": 1
    }
    assert unique["terminal_diagnostic_buy_point_type_counts"] == {
        "1buy": 4,
        "2buy": 2,
    }
    # Buy-side diagnostics do not alter the frozen sell-mapping outcome.
    assert unique["terminal_mapping_supply_class_counts"] == {
        "ONLY_THIRD_CLASS_POINTS": 1
    }
    assert unique["terminal_mapping_point_type_counts"]["1sell"] == 0

    forged = copy.deepcopy(row)
    forged["market_risk_warmup_evidence"]["period_diagnostics"][0][
        "mapping_supply"
    ]["diagnostic_directional_classification"] = "SELL12_PRESENT"
    with pytest.raises(
        ValueError, match="diagnostic directional classification is inconsistent"
    ):
        higher_timeframe_effectiveness_audit((forged,))


def test_higher_timeframe_effectiveness_separates_repeated_exposure_from_unique_top_event(
) -> None:
    rows = [
        _effectiveness_row(
            index,
            market="AMBER",
            sector="AMBER",
            symbol="AMBER",
        )
        for index in (0, 1)
    ]
    supplies = (
        {
            "classification": "ONLY_THIRD_CLASS_POINTS",
            "lower_structure_available": True,
            "point_evidence_count": 2,
            "point_type_counts": {
                "1sell": 0,
                "2sell": 0,
                "3sell": 1,
                "3buy": 1,
            },
            "completed_sell12_count": 0,
            "in_top_interval_sell12_count": 0,
            "completed_in_top_interval_sell12_count": 0,
            "incomplete_in_top_interval_sell12_count": 0,
            "outside_top_interval_sell12_count": 0,
            "highest_candidate_center_count": 0,
        },
        {
            "classification": "SELL12_OUTSIDE_TOP_FRACTAL",
            "lower_structure_available": True,
            "point_evidence_count": 3,
            "point_type_counts": {
                "1sell": 1,
                "2sell": 0,
                "3sell": 1,
                "3buy": 1,
            },
            "completed_sell12_count": 1,
            "in_top_interval_sell12_count": 0,
            "completed_in_top_interval_sell12_count": 0,
            "incomplete_in_top_interval_sell12_count": 0,
            "outside_top_interval_sell12_count": 1,
            "highest_candidate_center_count": 0,
        },
    )
    for row, supply in zip(rows, supplies):
        evidence = row["market_risk_warmup_evidence"]
        evidence["monthly"] = "FORMED_UNRESOLVED"
        evidence["period_diagnostics"][0] = {
            "period": "M",
            "state": "FORMED_UNRESOLVED",
            "active_top_interval": [
                "2025-01-01T15:00:00+08:00",
                "2025-03-31T15:00:00+08:00",
            ],
            "mapping_unique": False,
            "mapping_candidate_ids": [],
            "blocker_codes": [
                "NO_COMPLETED_LOWER_1SELL_2SELL_CENTER_IN_TOP_FRACTAL"
            ],
            "mapping_supply": supply,
        }

    audit = higher_timeframe_effectiveness_audit(tuple(rows))
    market = audit["subjects"]["market"]
    assert market["mapping_supply_class_counts"] == {
        "ONLY_THIRD_CLASS_POINTS": 1,
        "SELL12_OUTSIDE_TOP_FRACTAL": 1,
    }
    unique = market["unique_active_top_event_audit"]
    assert unique["candidate_period_exposure_count"] == 2
    assert unique["distinct_active_top_event_count"] == 1
    assert unique["repeated_candidate_exposure_count"] == 1
    assert unique["distinct_retained_supply_snapshot_count"] == 2
    assert unique["events_with_evolving_supply_count"] == 1
    assert unique["max_candidate_references_to_one_event"] == 2
    assert unique["terminal_mapping_supply_class_counts"] == {
        "SELL12_OUTSIDE_TOP_FRACTAL": 1
    }
    assert unique["terminal_mapping_point_type_counts"] == {
        "1sell": 1,
        "2sell": 0,
        "3sell": 1,
        "3buy": 1,
    }


def _identified_third_sell_supply(
    *,
    center_completed: bool = True,
    inside: bool,
) -> dict[str, object]:
    anchor = datetime(2025, 2, 3, 15, tzinfo=CN)
    available = datetime(2025, 2, 4, 15, tzinfo=CN)
    point_id = RiskMappingPointEvidenceFacts.identity(
        source_symbol="SH.000001",
        source_frequency="w",
        center_id="market-week-center",
        center_level_rank=2,
        point_type="3sell",
        point_anchor_at=anchor,
        point_available_at=available,
    )
    point = RiskMappingPointEvidenceFacts(
        point_id=point_id,
        source_symbol="SH.000001",
        source_frequency="w",
        center_id="market-week-center",
        center_level_rank=2,
        center_completed=center_completed,
        center_expanded=False,
        point_type="3sell",
        point_anchor_at=anchor,
        point_available_at=available,
        inside_active_top_interval=inside,
        highest_mapping_candidate=False,
    )
    return {
        "classification": "ONLY_THIRD_CLASS_POINTS",
        "lower_structure_available": True,
        "point_evidence_count": 1,
        "point_type_counts": {"1sell": 0, "2sell": 0, "3sell": 1, "3buy": 0},
        "completed_sell12_count": 0,
        "in_top_interval_sell12_count": 0,
        "completed_in_top_interval_sell12_count": 0,
        "incomplete_in_top_interval_sell12_count": 0,
        "outside_top_interval_sell12_count": 0,
        "highest_candidate_center_count": 0,
        "point_evidence": [point.document()],
    }


def _identified_diagnostic_buy_supply(*, inside: bool) -> dict[str, object]:
    supply = _identified_third_sell_supply(inside=inside)
    anchor = datetime(2025, 2, 3, 15, tzinfo=CN)
    available = datetime(2025, 2, 4, 15, tzinfo=CN)
    point_id = RiskDiagnosticBuyPointEvidenceFacts.identity(
        source_symbol="SH.000001",
        source_frequency="w",
        center_id="market-week-diagnostic-center",
        center_level_rank=2,
        point_type="1buy",
        point_anchor_at=anchor,
        point_available_at=available,
    )
    point = RiskDiagnosticBuyPointEvidenceFacts(
        point_id=point_id,
        source_symbol="SH.000001",
        source_frequency="w",
        center_id="market-week-diagnostic-center",
        center_level_rank=2,
        center_completed=True,
        center_expanded=False,
        point_type="1buy",
        point_anchor_at=anchor,
        point_available_at=available,
        inside_active_top_interval=inside,
    )
    supply.update(
        {
            "diagnostic_buy_point_type_counts": {"1buy": 1, "2buy": 0},
            "diagnostic_directional_classification": (
                "BUY12_PRESENT_SELL12_ABSENT"
            ),
            "diagnostic_buy_point_evidence": [point.document()],
        }
    )
    return supply


def test_higher_timeframe_effectiveness_globally_deduplicates_stable_points(
) -> None:
    rows = [
        _effectiveness_row(index, market="AMBER", sector="AMBER", symbol="AMBER")
        for index in (0, 1)
    ]
    intervals = (
        ("2025-01-01T15:00:00+08:00", "2025-03-31T15:00:00+08:00"),
        ("2025-04-01T15:00:00+08:00", "2025-06-30T15:00:00+08:00"),
    )
    for index, (row, interval) in enumerate(zip(rows, intervals)):
        evidence = row["market_risk_warmup_evidence"]
        evidence["monthly"] = "FORMED_UNRESOLVED"
        evidence["period_diagnostics"][0] = {
            "period": "M",
            "state": "FORMED_UNRESOLVED",
            "active_top_interval": list(interval),
            "mapping_unique": False,
            "mapping_candidate_ids": [],
            "blocker_codes": [
                "NO_COMPLETED_LOWER_1SELL_2SELL_CENTER_IN_TOP_FRACTAL"
            ],
            "mapping_supply": _identified_third_sell_supply(inside=index == 0),
        }

    audit = higher_timeframe_effectiveness_audit(tuple(rows))
    points = audit["subjects"]["market"]["globally_deduplicated_point_audit"]
    assert points["identity_coverage_status"] == "COMPLETE"
    assert points["terminal_event_point_occurrence_count"] == 2
    assert points["identified_terminal_point_occurrence_count"] == 2
    assert points["unidentified_terminal_point_occurrence_count"] == 0
    assert points["distinct_point_id_count"] == 1
    assert points["repeated_terminal_point_occurrence_count"] == 1
    assert points["distinct_point_type_counts"] == {
        "1sell": 0,
        "2sell": 0,
        "3sell": 1,
        "3buy": 0,
    }
    assert points["chart_focus_supported_point_count"] == 1
    assert points["points"][0]["terminal_event_reference_count"] == 2
    assert points["points"][0]["inside_active_top_event_count"] == 1
    assert points["points"][0]["review_as_of_unix"] == int(
        rows[1]["decision_at"].timestamp()
    )
    assert audit["global_point_identity_audit"]["distinct_point_id_count"] == 1


def test_higher_timeframe_effectiveness_separately_deduplicates_diagnostic_buys(
) -> None:
    rows = [
        _effectiveness_row(index, market="AMBER", sector="AMBER", symbol="AMBER")
        for index in (0, 1)
    ]
    intervals = (
        ("2025-01-01T15:00:00+08:00", "2025-03-31T15:00:00+08:00"),
        ("2025-04-01T15:00:00+08:00", "2025-06-30T15:00:00+08:00"),
    )
    for index, (row, interval) in enumerate(zip(rows, intervals)):
        evidence = row["market_risk_warmup_evidence"]
        evidence["monthly"] = "FORMED_UNRESOLVED"
        evidence["period_diagnostics"][0] = {
            "period": "M",
            "state": "FORMED_UNRESOLVED",
            "active_top_interval": list(interval),
            "mapping_unique": False,
            "mapping_candidate_ids": [],
            "blocker_codes": [
                "NO_COMPLETED_LOWER_1SELL_2SELL_CENTER_IN_TOP_FRACTAL"
            ],
            "mapping_supply": _identified_diagnostic_buy_supply(
                inside=index == 0
            ),
        }

    audit = higher_timeframe_effectiveness_audit(tuple(rows))
    market = audit["subjects"]["market"]
    diagnostic = market[
        "globally_deduplicated_diagnostic_buy_point_audit"
    ]
    assert diagnostic["identity_coverage_status"] == "COMPLETE"
    assert diagnostic["terminal_event_point_occurrence_count"] == 2
    assert diagnostic["identified_terminal_point_occurrence_count"] == 2
    assert diagnostic["unidentified_terminal_point_occurrence_count"] == 0
    assert diagnostic["distinct_point_id_count"] == 1
    assert diagnostic["repeated_terminal_point_occurrence_count"] == 1
    assert diagnostic["distinct_point_type_counts"] == {
        "1buy": 1,
        "2buy": 0,
    }
    assert diagnostic["diagnostic_only"] is True
    assert diagnostic["mapping_eligible"] is False
    assert diagnostic["points"][0]["terminal_event_reference_count"] == 2
    assert diagnostic["points"][0]["inside_active_top_event_count"] == 1
    assert diagnostic["points"][0]["mapping_eligible"] is False
    assert audit["global_diagnostic_buy_point_identity_audit"][
        "distinct_point_id_count"
    ] == 1
    # The frozen sell mapping keeps its own identity table and counts.
    mapping = market["globally_deduplicated_point_audit"]
    assert mapping["distinct_point_id_count"] == 1
    assert mapping["distinct_point_type_counts"]["3sell"] == 1


def test_higher_timeframe_effectiveness_rejects_same_time_point_state_conflict(
) -> None:
    rows = [
        _effectiveness_row(index, market="AMBER", sector="AMBER", symbol="AMBER")
        for index in (0, 1)
    ]
    rows[1]["decision_at"] = rows[0]["decision_at"]
    for index, row in enumerate(rows):
        evidence = row["market_risk_warmup_evidence"]
        evidence["monthly"] = "FORMED_UNRESOLVED"
        evidence["period_diagnostics"][0] = {
            "period": "M",
            "state": "FORMED_UNRESOLVED",
            "active_top_interval": [
                f"2025-01-{index + 1:02d}T15:00:00+08:00",
                f"2025-03-{index + 1:02d}T15:00:00+08:00",
            ],
            "mapping_unique": False,
            "mapping_candidate_ids": [],
            "blocker_codes": [
                "NO_COMPLETED_LOWER_1SELL_2SELL_CENTER_IN_TOP_FRACTAL"
            ],
            "mapping_supply": _identified_third_sell_supply(
                center_completed=index == 0,
                inside=True,
            ),
        }

    with pytest.raises(ValueError, match="conflicting center state"):
        higher_timeframe_effectiveness_audit(tuple(rows))


@pytest.mark.parametrize(
    ("market", "sector", "symbol", "triggered", "hard_blocked", "expected"),
    (
        ("AMBER", "UNRESOLVED", "AMBER", True, False, (True, False, True, False)),
        ("AMBER", "AMBER", "AMBER", True, False, (True, True, True, False)),
        ("GREEN", "GREEN", "GREEN", True, False, (True, True, True, True)),
        ("GREEN", "GREEN", "GREEN", True, True, (True, False, True, False)),
        ("GREEN", "GREEN", "GREEN", False, False, (True, True, True, False)),
    ),
)
def test_replay_risk_disposition_requires_the_real_sector_mwd_gate(
    market: str,
    sector: str,
    symbol: str,
    triggered: bool,
    hard_blocked: bool,
    expected: tuple[bool, bool, bool, bool],
) -> None:
    assert _research_risk_disposition(
        market_gate=market,
        sector_gate=sector,
        symbol_gate=symbol,
        triggered=triggered,
        sector_hard_blocked=hard_blocked,
    ) == expected


def _context() -> SymbolContext:
    times = tuple(
        datetime(2025, 8, 1, 10, minute, tzinfo=CN) for minute in (0, 1, 2, 3)
    )
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(times),
            "raw_open": (10, 10, 10, 10),
            "raw_high": (10.1, 10.1, 10.1, 10.1),
            "raw_low": (9.9, 9.9, 9.9, 9.9),
            "raw_close": (10, 10, 10, 10),
            "volume": (1000, 1000, 1000, 1000),
        }
    )
    facts = SimpleNamespace(code="SH.600000", source_revision="sha256:test")
    return SymbolContext(
        facts=facts,  # type: ignore[arg-type]
        frame=frame,
        daily_frame=pd.DataFrame(),
        times=times,
        executable_rows=frame,
        executable_times=times,
        sessions=(date(2025, 8, 1),),
        session_close={date(2025, 8, 1): Decimal("10")},
        session_volume={date(2025, 8, 1): Decimal("4000")},
    )


def test_optional_execution_uses_first_bar_opening_after_broker_latency() -> None:
    bars = _execution_bars(
        _context(),
        confirmed_at=datetime(2025, 8, 1, 10, 0, 2, tzinfo=CN),
        valuation_at=datetime(2025, 8, 1, 10, 30, tzinfo=CN),
        optional=True,
    )

    assert len(bars) == 1
    assert bars[0].closed_at == datetime(2025, 8, 1, 10, 2, tzinfo=CN)
    assert bars[0].opened_at > datetime(2025, 8, 1, 10, 0, 2, tzinfo=CN)


def test_result_source_snapshot_rejects_legacy_or_stale_code_identity() -> None:
    snapshot = _decision_source_snapshot()
    paths = tuple(row["path"] for row in snapshot["files"])

    assert snapshot["schema"] == (
        "chanlun-v3-replay-decision-source-snapshot/v1"
    )
    assert snapshot["aggregate_sha256"].startswith("sha256:")
    assert "tools/backtest_v3_sector_first_full_market.py" in paths
    assert "src/chanlun/core/cl.py" in paths
    assert (
        "src/chanlun/decision_support/trading_system/human_assisted_decision.py"
        in paths
    )
    assert "tools/run_v3_forward_paper.py" not in paths
    assert (
        "src/chanlun/decision_support/trading_system/human_paper_ledger.py"
        not in paths
    )
    assert (
        "web/chanlun_chart/cl_app/services/human_review_screening.py" not in paths
    )
    assert _decision_source_snapshot_matches_current(snapshot) is True
    assert _decision_source_snapshot_matches_current(None) is False
    assert set(FULL_MARKET._loaded_replay_source_paths()).issubset(paths)

    forged = {**snapshot, "aggregate_sha256": "sha256:" + "0" * 64}
    assert _decision_source_snapshot_matches_current(forged) is False


def test_report_publish_rejects_source_drift_without_replacing_old_artifact(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A long replay may not attest newly-written source it never executed."""

    output = tmp_path / "research.json"
    output.write_text("old-artifact\n", encoding="utf-8")
    start_snapshot = _decision_source_snapshot()
    current_snapshot = copy.deepcopy(start_snapshot)
    changed_path = current_snapshot["files"][0]["path"]
    current_snapshot["files"][0]["sha256"] = "sha256:" + "f" * 64
    stable = {
        "schema": current_snapshot["schema"],
        "files": current_snapshot["files"],
    }
    current_snapshot["aggregate_sha256"] = FULL_MARKET.sha256_json(stable)
    monkeypatch.setattr(
        FULL_MARKET,
        "_decision_source_snapshot_matches_current",
        lambda value: False,
    )
    monkeypatch.setattr(
        FULL_MARKET,
        "_decision_source_snapshot",
        lambda: current_snapshot,
    )

    with pytest.raises(
        RuntimeError, match="decision source changed during replay"
    ) as raised:
        FULL_MARKET._atomic_json(
            output,
            {"result": "must-not-publish"},
            expected_decision_source_snapshot=start_snapshot,
        )

    assert str(changed_path) in str(raised.value)
    assert start_snapshot["aggregate_sha256"] in str(raised.value)
    assert current_snapshot["aggregate_sha256"] in str(raised.value)
    assert output.read_text(encoding="utf-8") == "old-artifact\n"
    assert not output.with_suffix(".json.tmp").exists()


def test_trigger_fingerprint_converts_only_bare_date_values() -> None:
    observed_at = datetime(2025, 8, 1, 15, 0, tzinfo=CN)
    payload = _fingerprint_value(
        {
            "anchor_session": date(2025, 7, 29),
            "observed_at": observed_at,
            "strength": Decimal("8.25"),
        }
    )

    assert payload == {
        "anchor_session": "2025-07-29",
        "observed_at": observed_at,
        "strength": Decimal("8.25"),
    }
    assert FULL_MARKET.sha256_json(payload).startswith("sha256:")


def test_market_history_keeps_native_daily_warmup_before_one_minute_cache() -> None:
    later_one_minute = SimpleNamespace(
        facts=SimpleNamespace(
            source_start=datetime(2025, 6, 10, 14, 0, tzinfo=CN)
        )
    )

    assert _market_history_start({"SH.600000": later_one_minute}) == datetime(
        2023, 5, 1, 9, 30, tzinfo=CN
    )
    assert _market_history_start({}) == datetime(
        2023, 5, 1, 9, 30, tzinfo=CN
    )
    assert _symbol_daily_history_start(
        datetime(2025, 6, 10, 14, 0, tzinfo=CN)
    ) == datetime(2023, 5, 1, 0, 0, tzinfo=CN)
    assert _symbol_daily_history_start(
        datetime(2022, 6, 10, 14, 0, tzinfo=CN)
    ) == datetime(2022, 6, 10, 0, 0, tzinfo=CN)


def test_replay_grid_never_uses_the_1500_close_as_a_same_day_decision() -> None:
    grids = _market_grids((date(2025, 8, 1), date(2025, 8, 4)))

    assert len(grids) == 16
    assert grids[0][0].time() == time(9, 30)
    assert grids[7][1].time() == time(15)
    assert all(decision.time() != time(15) for decision, _valuation in grids)


def test_new_entry_skips_0930_without_a_completed_one_minute_bar() -> None:
    decisions = tuple(
        value[0]
        for value in _market_grids((date(2025, 8, 1), date(2025, 8, 4)))
    )

    prior_close_signal = datetime(2025, 7, 31, 15, 0, tzinfo=CN)
    assert _entry_dispatch_at(prior_close_signal, decisions) == datetime(
        2025, 8, 1, 10, 0, tzinfo=CN
    )
    assert _entry_dispatch_at(
        datetime(2025, 8, 1, 9, 31, tzinfo=CN), decisions
    ) == datetime(2025, 8, 1, 10, 0, tzinfo=CN)
    # The ordinary dispatcher remains available to persistent exits at open.
    assert FULL_MARKET._dispatch_at(prior_close_signal, decisions) == datetime(
        2025, 8, 1, 9, 30, tzinfo=CN
    )


def test_forward_checkpoint_retires_only_replay_resolved_persistent_signal() -> None:
    observed_at = datetime(2025, 8, 1, 14, 30, tzinfo=CN)
    strategic = FULL_MARKET.Signal(
        symbol="SH.600000",
        kind="STRATEGIC_EXIT",
        observed_at=observed_at,
        identity="strategic-1",
    )
    tactical = FULL_MARKET.Signal(
        symbol="SZ.300880",
        kind="TACTICAL_SELL",
        observed_at=observed_at,
        identity="tactical-1",
    )
    tactical_id = FULL_MARKET._persistent_signal_id(tactical)

    reconciled = FULL_MARKET._retire_resolved_active_signals(
        {
            "strategic": {strategic.symbol: strategic},
            "tactical": {tactical.symbol: tactical},
        },
        SimpleNamespace(resolved_persistent_intent_ids=(tactical_id,)),
    )

    assert reconciled == {
        "strategic": {strategic.symbol: strategic},
        "tactical": {},
    }


def test_persistent_resolution_session_is_the_last_adjudicated_retry() -> None:
    first_session = date(2025, 8, 1)
    resolved_session = date(2025, 8, 4)
    persistent_id = "persistent:SH.600000:STRATEGIC_EXIT:exit-1"
    batches = (
        SimpleNamespace(
            batch_id="batch-1",
            decision_at=datetime.combine(first_session, time(10), tzinfo=CN),
            events=(),
        ),
        SimpleNamespace(
            batch_id="batch-2",
            decision_at=datetime.combine(resolved_session, time(10), tzinfo=CN),
            events=(),
        ),
    )
    replay = SimpleNamespace(
        resolved_persistent_intent_ids=(persistent_id,),
        intents=(
            SimpleNamespace(
                batch_id="batch-1",
                persistent_intent_id=persistent_id,
            ),
            SimpleNamespace(
                batch_id="batch-2",
                persistent_intent_id=persistent_id,
            ),
        ),
    )

    assert FULL_MARKET._persistent_resolution_sessions(batches, replay) == {
        persistent_id: resolved_session
    }

    with pytest.raises(RuntimeError, match="lacks an adjudicated intent"):
        FULL_MARKET._persistent_resolution_sessions(
            batches,
            SimpleNamespace(
                resolved_persistent_intent_ids=("persistent:missing",),
                intents=(),
            ),
        )


def test_session_checkpoint_retirement_is_effective_only_next_session() -> None:
    observed_at = datetime(2025, 8, 1, 14, 30, tzinfo=CN)
    strategic = FULL_MARKET.Signal(
        symbol="SH.600000",
        kind="STRATEGIC_EXIT",
        observed_at=observed_at,
        identity="strategic-1",
    )
    tactical = FULL_MARKET.Signal(
        symbol="SZ.300880",
        kind="TACTICAL_SELL",
        observed_at=observed_at,
        identity="tactical-1",
    )
    active = {
        "strategic": {strategic.symbol: strategic},
        "tactical": {tactical.symbol: tactical},
    }
    resolution_sessions = {
        FULL_MARKET._persistent_signal_id(strategic): date(2025, 8, 1),
        FULL_MARKET._persistent_signal_id(tactical): date(2025, 8, 4),
    }

    same_session = FULL_MARKET._retire_active_before_session(
        active,
        current_session=date(2025, 8, 1),
        resolution_sessions=resolution_sessions,
    )
    next_session = FULL_MARKET._retire_active_before_session(
        active,
        current_session=date(2025, 8, 4),
        resolution_sessions=resolution_sessions,
    )

    assert same_session == active
    assert next_session == {
        "strategic": {},
        "tactical": {tactical.symbol: tactical},
    }


def test_historical_scheduler_converges_to_daily_checkpoint_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_at = datetime(2025, 8, 1, 10, 0, tzinfo=CN)
    second_at = datetime(2025, 8, 4, 10, 0, tzinfo=CN)
    strategic = FULL_MARKET.Signal(
        symbol="SH.600000",
        kind="STRATEGIC_EXIT",
        observed_at=first_at,
        identity="strategic-1",
    )
    tactical = FULL_MARKET.Signal(
        symbol="SH.600000",
        kind="TACTICAL_SELL",
        observed_at=second_at,
        identity="tactical-1",
    )
    strategic_id = FULL_MARKET._persistent_signal_id(strategic)
    tactical_id = FULL_MARKET._persistent_signal_id(tactical)
    schedules: list[dict[str, date]] = []
    replay_calls: list[tuple[object, ...]] = []

    def batch(batch_id: str, decision_at: datetime, event_id: str) -> object:
        return SimpleNamespace(
            batch_id=batch_id,
            decision_at=decision_at,
            events=(SimpleNamespace(event_id=event_id),),
        )

    def fake_build(**kwargs: object) -> tuple[tuple[object, ...], dict[str, dict]]:
        schedule = dict(kwargs.get("retire_persistent_after_sessions") or {})
        schedules.append(schedule)
        if strategic_id not in schedule:
            built = (
                batch("batch-1", first_at, "strategic:first"),
                batch("batch-2", second_at, "strategic:stale-retry"),
            )
        else:
            built = (
                batch("batch-1", first_at, "strategic:first"),
                batch("batch-2", second_at, "tactical:exposed"),
            )
        return built, {"strategic": {}, "tactical": {}}

    def fake_run(**kwargs: object) -> object:
        built = tuple(kwargs["batches"])
        replay_calls.append(built)
        if built[-1].events[0].event_id == "strategic:stale-retry":
            return SimpleNamespace(
                resolved_persistent_intent_ids=(strategic_id,),
                intents=(
                    SimpleNamespace(
                        batch_id="batch-1",
                        persistent_intent_id=strategic_id,
                    ),
                ),
            )
        return SimpleNamespace(
            resolved_persistent_intent_ids=(strategic_id, tactical_id),
            intents=(
                SimpleNamespace(
                    batch_id="batch-1",
                    persistent_intent_id=strategic_id,
                ),
                SimpleNamespace(
                    batch_id="batch-2",
                    persistent_intent_id=tactical_id,
                ),
            ),
        )

    monkeypatch.setattr(FULL_MARKET, "_build_batches_with_state", fake_build)
    monkeypatch.setattr(FULL_MARKET, "_run", fake_run)

    batches, _active, result, audit = (
        FULL_MARKET._run_historical_session_checkpoint_replay(
            contexts={},
            grids=((first_at, first_at), (second_at, second_at)),
            signals={first_at: (strategic,), second_at: (tactical,)},
            research=None,
            initial_cash=Decimal("1000000"),
            technical_approximation=None,
        )
    )

    assert [value.events[0].event_id for value in batches] == [
        "strategic:first",
        "tactical:exposed",
    ]
    assert result.resolved_persistent_intent_ids == (strategic_id, tactical_id)
    assert schedules == [
        {},
        {strategic_id: date(2025, 8, 1)},
        {
            strategic_id: date(2025, 8, 1),
            tactical_id: date(2025, 8, 4),
        },
    ]
    assert len(replay_calls) == 2
    assert audit["converged"] is True
    assert audit["build_pass_count"] == 3
    assert audit["replay_pass_count"] == 2
    assert audit["initial_event_count"] == 2
    assert audit["final_event_count"] == 2
    assert audit["newly_exposed_event_count"] == 1
    assert audit["removed_stale_retry_event_count"] == 1
    assert audit["forward_checkpoint_equivalent"] is True


def test_historical_ablations_rebuild_independent_source_signal_schedules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = datetime(2025, 8, 1, 10, 0, tzinfo=CN)
    signals = (
        FULL_MARKET.Signal(
            symbol="SH.600000",
            kind="ENTRY",
            observed_at=observed,
            identity="entry-amber",
            exact_risk_green=False,
        ),
        FULL_MARKET.Signal(
            symbol="SZ.000001",
            kind="ENTRY",
            observed_at=observed,
            identity="entry-green",
            exact_risk_green=True,
        ),
        FULL_MARKET.Signal(
            symbol="SH.600000",
            kind="STRATEGIC_EXIT",
            observed_at=observed,
            identity="strategic-exit",
        ),
        FULL_MARKET.Signal(
            symbol="SH.600000",
            kind="TACTICAL_SELL",
            observed_at=observed,
            identity="tactical-sell",
        ),
        FULL_MARKET.Signal(
            symbol="SZ.000001",
            kind="TACTICAL_BUYBACK",
            observed_at=observed,
            identity="tactical-buyback",
        ),
    )
    initial_tactical = {"SH.600000": signals[3]}
    calls: list[dict[str, object]] = []

    def fake_replay(**kwargs: object):
        calls.append(kwargs)
        kinds = tuple(
            signal.kind
            for rows in kwargs["signals"].values()
            for signal in rows
        )
        result = SimpleNamespace(signal_kinds=kinds)
        audit = {"event_schedule_sha256": f"schedule-{len(calls)}"}
        return (), {"strategic": {}, "tactical": {}}, result, audit

    monkeypatch.setattr(
        FULL_MARKET,
        "_run_historical_session_checkpoint_replay",
        fake_replay,
    )

    results, audits = FULL_MARKET._run_historical_causal_ablations(
        contexts={},
        grids=((observed, observed),),
        signals={observed: signals},
        research=None,
        initial_cash=Decimal("1000000"),
        technical_approximation=None,
        initial_strategic_active={},
        initial_tactical_active=initial_tactical,
    )

    assert results["NO_TACTICAL"].signal_kinds == (
        "ENTRY",
        "ENTRY",
        "STRATEGIC_EXIT",
    )
    assert results["EXACT_GREEN_HIGHER_TIMEFRAME_ONLY"].signal_kinds == (
        "ENTRY",
        "STRATEGIC_EXIT",
        "TACTICAL_SELL",
        "TACTICAL_BUYBACK",
    )
    assert calls[0]["initial_tactical_active"] == {}
    assert calls[1]["initial_tactical_active"] == initial_tactical
    assert audits == {
        "NO_TACTICAL": {"event_schedule_sha256": "schedule-1"},
        "EXACT_GREEN_HIGHER_TIMEFRAME_ONLY": {
            "event_schedule_sha256": "schedule-2"
        },
    }


def test_full_market_event_identity_is_content_addressed_not_ordinal() -> None:
    decision_at = datetime(2025, 8, 1, 14, 30, tzinfo=CN)
    signal = FULL_MARKET.Signal(
        symbol="SH.600000",
        kind="ENTRY",
        observed_at=decision_at,
        identity="sha256:" + "a" * 64,
    )

    event_id = FULL_MARKET._signal_event_id(signal, decision_at)

    assert event_id == (
        "V3FM:2025-08-01T14:30:00+08:00:ENTRY:SH.600000:sha256:"
        + "a" * 64
    )
    assert FULL_MARKET._signal_event_id(signal, decision_at) == event_id
    changed = FULL_MARKET.Signal(
        symbol=signal.symbol,
        kind=signal.kind,
        observed_at=signal.observed_at,
        identity="sha256:" + "b" * 64,
    )
    assert FULL_MARKET._signal_event_id(changed, decision_at) != event_id


def test_tactical_execution_audit_explains_zero_lot_and_missing_dispatch() -> None:
    sell_at = datetime(2025, 8, 1, 14, 0, tzinfo=CN)
    buy_at = datetime(2025, 8, 4, 10, 0, tzinfo=CN)
    sell = FULL_MARKET.Signal(
        symbol="SZ.300880",
        kind="TACTICAL_SELL",
        observed_at=sell_at,
        identity="sell-zero-lot",
    )
    buyback = FULL_MARKET.Signal(
        symbol="SH.600000",
        kind="TACTICAL_BUYBACK",
        observed_at=buy_at,
        identity="buyback-not-dispatched",
    )
    persistent_id = FULL_MARKET._persistent_signal_id(sell)
    intent = SimpleNamespace(
        symbol=sell.symbol,
        confirmation_time=sell_at,
        structure_snapshot_id=sell.identity,
        action="WAIT",
        reason_codes=(
            "L1_THIRD_SELL_STOP_RESTORE",
            "NO_SELLABLE_TACTICAL_INVENTORY",
        ),
    )
    result = SimpleNamespace(
        intents=(
            SimpleNamespace(
                persistent_intent_id=persistent_id,
                event_id="V3FM:test:TACTICAL_SELL:SZ.300880:0",
                intent=intent,
            ),
        ),
        orders=(),
        suppressed_persistent_event_counts=((persistent_id, 7),),
        metrics=SimpleNamespace(tactical_cycle_count=0),
    )

    audit = FULL_MARKET._tactical_execution_audit(
        signals={sell_at: (sell,), buy_at: (buyback,)},
        replay_result=result,
    )

    assert audit["generated_signal_count"] == 2
    assert audit["dispatched_source_signal_count"] == 1
    assert audit["order_count"] == 0
    assert audit["fill_count"] == 0
    assert audit["suppressed_retry_count"] == 7
    assert audit["disposition_counts"] == {
        "NOT_DISPATCHED_BY_PRIORITY_OR_REPLACEMENT": 1,
        "NO_EXECUTABLE_TACTICAL_LOT": 1,
    }
    assert audit["adjudication"] == (
        "TACTICAL_SIGNALS_PRESENT_BUT_NO_LEGAL_LOT_UNDER_FROZEN_PARAMETERS"
    )


def test_series_metrics_exposes_return_and_drawdown_for_nonempty_curve() -> None:
    metrics = _series_metrics(
        (
            (date(2025, 1, 1), Decimal("100")),
            (date(2025, 1, 2), Decimal("90")),
            (date(2025, 1, 3), Decimal("110")),
        )
    )

    assert metrics["status"] == "EVALUATED"
    assert metrics["net_return"] == Decimal("0.1")
    assert metrics["max_drawdown"] == Decimal("0.1")


def test_risk_gate_preserves_mwd_warmup_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_at = datetime(2025, 8, 1, 14, 30, tzinfo=CN)
    warmup = {
        "contract_id": "chanlun-qmt-mwd-warmup-evidence/v1",
        "required_daily_bars": 480,
        "full_daily_bar_count": 479,
        "suffix_daily_bar_count": 0,
        "converged": False,
        "reason_code": "QMT_HIGHER_TIMEFRAME_WARMUP_HISTORY_INSUFFICIENT",
    }
    monkeypatch.setattr(
        FULL_MARKET,
        "build_qmt_same_base_stream_frames",
        lambda **_kwargs: SimpleNamespace(
            daily=pd.DataFrame(),
            thirty_minute=pd.DataFrame(),
            complete_sessions=(observed_at.date(),),
            source_base_stream_revision="sha256:" + "a" * 64,
        ),
    )
    monkeypatch.setattr(
        FULL_MARKET,
        "build_qmt_native_daily_bridge",
        lambda **_kwargs: SimpleNamespace(
            daily=pd.DataFrame(),
            thirty_minute=pd.DataFrame(),
            evidence=SimpleNamespace(
                reconciled_source_revision="sha256:" + "a" * 64,
                document=lambda: {"contract_id": "native-daily-test"},
            ),
            calendar_coverage_evidence=SimpleNamespace(
                document=lambda: {"status": "EXACT"}
            ),
        ),
    )
    monkeypatch.setattr(
        FULL_MARKET,
        "qmt_higher_timeframe_inputs",
        lambda **_kwargs: SimpleNamespace(source_revision="sha256:" + "b" * 64),
    )
    monkeypatch.setattr(
        FULL_MARKET,
        "build_qmt_higher_timeframe_risk",
        lambda **_kwargs: SimpleNamespace(
            risk=SimpleNamespace(gate="UNRESOLVED", snapshot=None),
            blockers=(
                SimpleNamespace(
                    code="QMT_HIGHER_TIMEFRAME_WARMUP_HISTORY_INSUFFICIENT"
                ),
            ),
            warmup=SimpleNamespace(document=lambda: warmup),
        ),
    )
    period_diagnostics = tuple(
        {
            "period": period,
            "state": "UNRESOLVED",
            "blocker_codes": [
                "QMT_HIGHER_TIMEFRAME_WARMUP_HISTORY_INSUFFICIENT"
            ],
        }
        for period in ("M", "W", "D")
    )
    monkeypatch.setattr(
        FULL_MARKET,
        "higher_timeframe_gate_evidence_from_envelope",
        lambda _envelope: SimpleNamespace(
            gate="UNRESOLVED",
            snapshot_id="sha256:" + "b" * 64,
            reason_codes=(
                "QMT_HIGHER_TIMEFRAME_WARMUP_HISTORY_INSUFFICIENT",
            ),
            source_revision="sha256:" + "b" * 64,
            monthly="UNRESOLVED",
            weekly="UNRESOLVED",
            daily="UNRESOLVED",
            grade="UNRESOLVED",
            period_diagnostics=tuple(
                SimpleNamespace(document=lambda row=row: row)
                for row in period_diagnostics
            ),
            session_evidence=SimpleNamespace(
                document=lambda: {"issues": []}
            ),
            warmup_evidence=SimpleNamespace(document=lambda: warmup),
        ),
    )

    gate, identity, blockers, evidence, calendar_coverage = _risk_gate(
        symbol="SH.600000",
        frame=pd.DataFrame(),
        native_daily_frame=pd.DataFrame(
            {"date": (datetime(2025, 8, 1, 15, tzinfo=CN),)}
        ),
        observed_at=observed_at,
        expected_sessions=(observed_at.date(),),
    )

    assert gate == "UNRESOLVED"
    assert identity == "sha256:" + "b" * 64
    assert blockers == (
        "QMT_HIGHER_TIMEFRAME_WARMUP_HISTORY_INSUFFICIENT",
    )
    assert evidence == {
        "source_revision": "sha256:" + "b" * 64,
        "monthly": "UNRESOLVED",
        "weekly": "UNRESOLVED",
        "daily": "UNRESOLVED",
        "grade": "UNRESOLVED",
        "period_diagnostics": period_diagnostics,
        "session_evidence": {"issues": []},
        "warmup": warmup,
        "native_daily_reconciliation": {"contract_id": "native-daily-test"},
        "one_minute_source_alignment": {
            "evaluation_not_before": None,
            "source_boundary_exclusions": (),
        },
    }
    assert calendar_coverage == {"status": "EXACT"}


def test_risk_gate_preserves_canonical_native_daily_calendar_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_at = datetime(2025, 8, 1, 14, 30, tzinfo=CN)
    coverage = _native_calendar_coverage(
        symbol="SH.600000",
        observed_at=observed_at,
        missing=(date(2025, 7, 31),),
    )
    monkeypatch.setattr(
        FULL_MARKET,
        "build_qmt_same_base_stream_frames",
        lambda **_kwargs: SimpleNamespace(),
    )

    def fail_bridge(**_kwargs):
        raise QmtNativeDailyReconciliationError(
            "QMT_NATIVE_DAILY_TRADING_CALENDAR_MISMATCH",
            "calendar_only=[datetime.date(2025, 7, 31)]",
            calendar_coverage_evidence=coverage,
        )

    monkeypatch.setattr(
        FULL_MARKET,
        "build_qmt_native_daily_bridge",
        fail_bridge,
    )

    gate, identity, blockers, evidence, raw_coverage = _risk_gate(
        symbol="SH.600000",
        frame=pd.DataFrame(),
        native_daily_frame=pd.DataFrame(
            {"date": (datetime(2025, 8, 1, 15, tzinfo=CN),)}
        ),
        observed_at=observed_at,
        expected_sessions=(observed_at.date(),),
    )

    assert gate == "UNRESOLVED"
    assert str(identity).startswith("sha256:")
    assert blockers == ("QMT_NATIVE_DAILY_TRADING_CALENDAR_MISMATCH",)
    assert evidence is None
    assert raw_coverage == coverage.document()


def test_sector_risk_gate_uses_canonical_period_documents_for_mapping_supply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_at = datetime(2025, 8, 1, 14, 30, tzinfo=CN)
    mapping_supply = {
        "classification": "ONLY_THIRD_CLASS_POINTS",
        "lower_structure_available": True,
        "point_evidence_count": 2,
        "point_type_counts": {
            "1sell": 0,
            "2sell": 0,
            "3sell": 1,
            "3buy": 1,
        },
        "completed_sell12_count": 0,
        "in_top_interval_sell12_count": 0,
        "completed_in_top_interval_sell12_count": 0,
        "incomplete_in_top_interval_sell12_count": 0,
        "outside_top_interval_sell12_count": 0,
        "highest_candidate_center_count": 0,
    }
    diagnostic_document = {
        "period": "M",
        "state": "FORMED_UNRESOLVED",
        "mapping_supply": mapping_supply,
    }
    # ``point_type_counts`` is deliberately represented as tuple pairs on the
    # object.  Generic dataclass conversion would leak that list shape; the
    # public document contract must preserve the keyed mapping above.
    diagnostic = SimpleNamespace(
        mapping_supply=SimpleNamespace(
            point_type_counts=tuple(mapping_supply["point_type_counts"].items())
        ),
        document=lambda: diagnostic_document,
    )
    evidence = SimpleNamespace(
        gate="AMBER",
        snapshot_id="sha256:" + "1" * 64,
        reason_codes=(
            "NO_COMPLETED_LOWER_1SELL_2SELL_CENTER_IN_TOP_FRACTAL",
        ),
        source_revision="sha256:" + "2" * 64,
        monthly="FORMED_UNRESOLVED",
        weekly="NONE",
        daily="NONE",
        grade="RESEARCH_ONLY",
        period_diagnostics=(diagnostic,),
        session_evidence=SimpleNamespace(document=lambda: {"issues": []}),
        warmup_evidence=SimpleNamespace(document=lambda: {"converged": True}),
    )
    monkeypatch.setattr(
        FULL_MARKET,
        "resolve_sector_higher_timeframe_gate",
        lambda **_kwargs: SimpleNamespace(
            evidence=evidence,
            source_mode="PAGE_PARITY_SAME_5M_BASE",
            strict_warmup_evidence=SimpleNamespace(
                document=lambda: {"converged": True}
            ),
            strict_source_coverage_evidence=SimpleNamespace(
                document=lambda: {
                    "contract_id": (
                        "chanlun-qmt-sector-same-5m-source-coverage/v1"
                    ),
                    "boundary_status": "REQUIRED_HISTORY_CONVERGED",
                }
            ),
            fallback_unavailable_reason_codes=(),
        ),
    )
    source = SimpleNamespace(
        five_minute_prefix=lambda **_kwargs: pd.DataFrame(),
        native_daily_prefix=lambda **_kwargs: pd.DataFrame(),
    )

    gate, _identity, _blockers, raw = _sector_risk_gate(
        sector_id="QMT:GICS3:test",
        sector_members=("SH.600000",),
        observed_at=observed_at,
        expected_sessions=(observed_at.date(),),
        composite_source=source,
    )

    assert gate == "AMBER"
    assert raw is not None
    assert raw["period_diagnostics"][0]["mapping_supply"] == mapping_supply
    assert isinstance(
        raw["period_diagnostics"][0]["mapping_supply"]["point_type_counts"],
        dict,
    )
    assert raw["strict_same_5m_source_coverage"]["boundary_status"] == (
        "REQUIRED_HISTORY_CONVERGED"
    )


def test_market_calendar_requires_two_matching_index_daily_paths() -> None:
    sessions = tuple(
        pd.bdate_range("2024-01-02", periods=480, tz=CN)
    )

    def daily(values):
        frame = pd.DataFrame({"date": values})
        frame.attrs["qmt_local_cache_source_sha256"] = "sha256:" + "1" * 64
        return frame

    calendar, evidence = _reconciled_market_calendar(
        daily(tuple(value.normalize() + pd.Timedelta(hours=15) for value in sessions)),
        daily(tuple(value.normalize() + pd.Timedelta(hours=15) for value in sessions)),
    )
    assert len(calendar) == 480
    assert evidence["data_grade"] == "RESEARCH_ONLY"

    mismatched = list(sessions)
    mismatched.pop(200)
    with pytest.raises(ValueError, match="do not reconcile"):
        _reconciled_market_calendar(
            daily(tuple(value.normalize() + pd.Timedelta(hours=15) for value in sessions)),
            daily(
                tuple(
                    value.normalize() + pd.Timedelta(hours=15)
                    for value in mismatched
                )
            ),
        )


def test_performance_adjudication_never_calls_a_nonempty_invalid_replay_empty() -> None:
    unresolved = SimpleNamespace(
        metrics=SimpleNamespace(
            performance_evaluable=False,
            empty_replay=False,
            ledger_valid=False,
            strategic_cycle_count=1,
        )
    )
    no_cycle = SimpleNamespace(
        metrics=SimpleNamespace(
            performance_evaluable=False,
            empty_replay=False,
            ledger_valid=True,
            strategic_cycle_count=0,
        )
    )

    assert _performance_adjudication(unresolved) == (
        "NOT_EVALUABLE_UNRESOLVED_LEDGER",
        "UNRESOLVED_FACTS_OR_VALUATIONS_PRESENT",
    )
    assert _performance_adjudication(no_cycle) == (
        "NOT_EVALUABLE_NO_CLOSED_STRATEGIC_CYCLE",
        "NO_CLOSED_STRATEGIC_CYCLE",
    )


def test_performance_adjudication_labels_evaluable_but_insufficient_samples() -> None:
    insufficient = SimpleNamespace(
        metrics=SimpleNamespace(
            performance_evaluable=True,
            strategic_sample_insufficient=True,
            tactical_sample_insufficient=True,
        )
    )
    sufficient = SimpleNamespace(
        metrics=SimpleNamespace(
            performance_evaluable=True,
            strategic_sample_insufficient=False,
            tactical_sample_insufficient=False,
        )
    )

    assert _performance_adjudication(insufficient) == (
        "EVALUATED_SAMPLE_INSUFFICIENT",
        "STRATEGIC_SAMPLE_BELOW_100+TACTICAL_SAMPLE_BELOW_200",
    )
    assert _performance_adjudication(sufficient) == ("EVALUATED", None)


def test_terminal_accounting_attribution_closes_the_equity_identity() -> None:
    observed_at = datetime(2026, 7, 24, 15, 0, tzinfo=CN)
    position = SimpleNamespace(
        symbol="SH.600000",
        cycle_id="cycle:open",
        slot_number=1,
        opened_at=datetime(2026, 7, 1, 10, 0, tzinfo=CN),
        quantity=100,
        entry_cash=Decimal("400"),
        cumulative_cash_flow=Decimal("-390"),
        cumulative_fees=Decimal("5"),
        turnover_notional=Decimal("400"),
        tactical_cycles_completed=0,
        last_price=Decimal("4.5"),
        market_value=Decimal("450"),
        marked_at=observed_at,
        mark_complete=True,
    )
    result = SimpleNamespace(
        initial_cash=Decimal("1000"),
        final_cash=Decimal("600"),
        equity_curve=(
            SimpleNamespace(
                observed_at=observed_at,
                cash=Decimal("600"),
                market_value=Decimal("450"),
                equity=Decimal("1050"),
                complete=True,
                reason_codes=(),
            ),
        ),
        positions=(position,),
        closed_cycles=(
            SimpleNamespace(
                symbol="SH.600001",
                cycle_id="cycle:closed",
                slot_number=2,
                opened_at=datetime(2026, 6, 1, 10, 0, tzinfo=CN),
                closed_at=datetime(2026, 6, 20, 10, 0, tzinfo=CN),
                entry_cash=Decimal("200"),
                net_pnl=Decimal("-10"),
            ),
        ),
    )

    value = FULL_MARKET._terminal_accounting_attribution(
        result,
        sector_by_symbol={
            "SH.600000": "sector:a",
            "SH.600001": "sector:a",
        },
        sector_name_by_id={"sector:a": "测试行业"},
        sector_membership_mode="CURRENT_CAPTURE_BACKFILLED_USER_AUTHORIZED",
    )

    assert value["status"] == "EVALUATED"
    assert value["terminal"]["total_net_pnl"] == Decimal("50")
    assert value["pnl_decomposition"]["closed_cycle_realized_net_pnl"] == (
        Decimal("-10")
    )
    assert value["pnl_decomposition"]["open_cycle_marked_net_pnl"] == (
        Decimal("60")
    )
    assert value["pnl_decomposition"]["identity_difference"] == Decimal("0")
    assert value["open_positions"][0]["marked_net_pnl"] == Decimal("60")
    assert value["closed_cycles"][0]["cycle_id"] == "cycle:closed"
    assert value["closed_cycles"][0]["realized_net_pnl"] == Decimal("-10")
    assert value["concentration"]["max_symbol"] == "SH.600000"
    assert value["concentration"]["max_symbol_invested_fraction"] == Decimal(
        "1"
    )
    assert value["sector_attribution"][0]["total_attributed_net_pnl"] == (
        Decimal("50")
    )


def test_terminal_accounting_attribution_fails_closed_without_a_terminal_mark() -> None:
    observed_at = datetime(2026, 7, 24, 15, 0, tzinfo=CN)
    result = SimpleNamespace(
        initial_cash=Decimal("1000"),
        final_cash=Decimal("600"),
        equity_curve=(
            SimpleNamespace(
                observed_at=observed_at,
                cash=Decimal("600"),
                market_value=Decimal("0"),
                equity=Decimal("600"),
                complete=False,
                reason_codes=("UNRESOLVED_VALUATION_MARK:SH.600000",),
            ),
        ),
        positions=(
            SimpleNamespace(
                symbol="SH.600000",
                cycle_id="cycle:open",
                slot_number=1,
                opened_at=observed_at,
                quantity=100,
                entry_cash=Decimal("400"),
                cumulative_cash_flow=Decimal("-400"),
                cumulative_fees=Decimal("5"),
                turnover_notional=Decimal("400"),
                tactical_cycles_completed=0,
                last_price=None,
                market_value=None,
                marked_at=None,
                mark_complete=False,
            ),
        ),
        closed_cycles=(),
    )

    value = FULL_MARKET._terminal_accounting_attribution(
        result,
        sector_by_symbol={"SH.600000": "sector:a"},
        sector_name_by_id={"sector:a": "测试行业"},
        sector_membership_mode="CURRENT_CAPTURE_BACKFILLED_USER_AUTHORIZED",
    )

    assert value["status"] == "NOT_EVALUABLE"
    assert "UNRESOLVED_TERMINAL_MARK:SH.600000" in value["reason_codes"]


def _headline_attribution(
    *,
    total: str,
    closed: str,
    open_marked: str,
) -> dict[str, object]:
    return {
        "status": "EVALUATED",
        "reason_codes": (),
        "terminal": {
            "initial_cash": Decimal("1000"),
            "total_net_pnl": Decimal(total),
        },
        "pnl_decomposition": {
            "closed_cycle_realized_net_pnl": Decimal(closed),
            "open_cycle_marked_net_pnl": Decimal(open_marked),
            "identity_difference": Decimal("0"),
            "pure_unrealized_reason": "SEPARATE_COST_BASIS_REQUIRED",
        },
        "concentration": {
            "open_position_count": 1,
            "max_symbol": "SH.600000",
            "max_symbol_equity_fraction": Decimal("0.2"),
            "max_symbol_invested_fraction": Decimal("1"),
        },
    }


def test_terminal_accounting_headline_exposes_open_mark_dependency() -> None:
    value = FULL_MARKET._terminal_accounting_headline(
        _headline_attribution(total="50", closed="-10", open_marked="60")
    )

    assert value["status"] == "EVALUATED"
    assert value["return_dependency_status"] == (
        "POSITIVE_TOTAL_DEPENDS_ON_OPEN_CYCLE_MARKS"
    )
    assert value["positive_total_depends_on_open_cycle_marks"] is True
    assert value["total_net_return_on_initial_cash"] == Decimal("0.05")
    assert value["closed_cycle_realized_return_on_initial_cash"] == (
        Decimal("-0.01")
    )
    assert value["open_cycle_marked_return_on_initial_cash"] == Decimal(
        "0.06"
    )
    assert value["max_open_symbol"] == "SH.600000"
    assert value["diagnostic_only"] is True
    assert value["decisions_unchanged"] is True
    assert value["live_status"] == "LIVE_DISABLED"


def test_terminal_accounting_headline_distinguishes_realized_support() -> None:
    value = FULL_MARKET._terminal_accounting_headline(
        _headline_attribution(total="50", closed="60", open_marked="-10")
    )

    assert value["return_dependency_status"] == (
        "POSITIVE_TOTAL_SUPPORTED_BY_REALIZED_CYCLES"
    )
    assert value["positive_total_depends_on_open_cycle_marks"] is False


def test_terminal_accounting_headline_preserves_not_evaluable_gate() -> None:
    value = FULL_MARKET._terminal_accounting_headline(
        {
            "status": "NOT_EVALUABLE",
            "reason_codes": ("UNRESOLVED_TERMINAL_MARK:SH.600000",),
        }
    )

    assert value == {
        "schema": "chanlun-v3-terminal-accounting-headline/v1",
        "status": "NOT_EVALUABLE",
        "reason_codes": ("UNRESOLVED_TERMINAL_MARK:SH.600000",),
        "return_dependency_status": "NOT_EVALUABLE",
        "positive_total_depends_on_open_cycle_marks": None,
        "diagnostic_only": True,
        "decisions_unchanged": True,
        "live_status": "LIVE_DISABLED",
    }


def test_terminal_accounting_headline_rejects_rehashed_inconsistent_identity() -> None:
    malformed = _headline_attribution(
        total="50",
        closed="-10",
        open_marked="60",
    )
    malformed["pnl_decomposition"]["open_cycle_marked_net_pnl"] = Decimal(
        "59"
    )

    with pytest.raises(ValueError, match="P&L identity is inconsistent"):
        FULL_MARKET._terminal_accounting_headline(malformed)


def _performance_research_result() -> dict[str, object]:
    return {
        "performance_status": "EVALUATED_SAMPLE_INSUFFICIENT",
        "performance_reason": (
            "STRATEGIC_SAMPLE_BELOW_100+TACTICAL_SAMPLE_BELOW_200"
        ),
        "daily_metrics": {
            "status": "EVALUATED_SAMPLE_INSUFFICIENT",
            "start": "2025-08-01",
            "end": "2026-07-24",
            "observations": 237,
            "net_return": Decimal("0.02"),
            "annualized_return": Decimal("0.021"),
            "max_drawdown": Decimal("0.12"),
            "sharpe": Decimal("0.30"),
        },
        "replay": {
            "metrics": {
                "annualized_return": None,
                "warnings": (
                    "INSUFFICIENT_CALENDAR_SPAN_FOR_ANNUALIZATION",
                ),
            }
        },
        "periods": {
            "train_validation_holdout": {
                "FINAL_HOLDOUT_20": {
                    "status": "EVALUATED",
                    "start": "2026-05-18",
                    "end": "2026-07-24",
                    "observations": 49,
                    "net_return": Decimal("-0.01"),
                    "annualized_return": Decimal("-0.05"),
                    "max_drawdown": Decimal("0.11"),
                    "sharpe": Decimal("-0.20"),
                }
            }
        },
    }


def _performance_benchmarks() -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "symbol": symbol,
            "definition": definition,
            "metrics": {
                "status": "EVALUATED",
                "start": "2025-08-01",
                "end": "2026-07-24",
                "observations": 237,
                "net_return": net_return,
                "annualized_return": net_return,
                "max_drawdown": drawdown,
                "sharpe": sharpe,
            },
        }
        for symbol, definition, net_return, drawdown, sharpe in (
            (
                "SH.000001",
                "SSE Composite close-to-close",
                Decimal("0.07"),
                Decimal("0.10"),
                Decimal("0.50"),
            ),
            (
                "SH.000300",
                "CSI 300 close-to-close",
                Decimal("0.14"),
                Decimal("0.09"),
                Decimal("0.80"),
            ),
        )
    )


def test_performance_headline_exposes_holdout_benchmark_and_annualization() -> None:
    value = FULL_MARKET._performance_headline(
        _performance_research_result(),
        _performance_benchmarks(),
        terminal_headline={
            "return_dependency_status": (
                "POSITIVE_TOTAL_DEPENDS_ON_OPEN_CYCLE_MARKS"
            )
        },
    )

    assert value["status"] == "EVALUATED_SAMPLE_INSUFFICIENT"
    assert value["relative_performance_status"] == (
        "POSITIVE_FULL_SAMPLE_BUT_NEGATIVE_HOLDOUT_AND_"
        "UNDERPERFORMS_ALL_BENCHMARKS"
    )
    assert value["strategy_net_return"] == Decimal("0.02")
    assert value["final_holdout_net_return"] == Decimal("-0.01")
    assert value["final_holdout_negative"] is True
    assert value["strategy_underperformed_all_benchmarks"] is True
    assert value["benchmarks"][0]["strategy_excess_net_return"] == (
        Decimal("-0.05")
    )
    assert value["benchmarks"][1]["strategy_excess_net_return"] == (
        Decimal("-0.12")
    )
    assert value["annualization_status"] == (
        "MATHEMATICAL_ESTIMATE_BELOW_FULL_CALENDAR_YEAR"
    )
    assert value["calendar_span_days"] == 357
    assert value["daily_annualized_return_estimate"] == Decimal("0.021")
    assert value["event_annualized_return"] is None
    assert value["return_dependency_status"] == (
        "POSITIVE_TOTAL_DEPENDS_ON_OPEN_CYCLE_MARKS"
    )
    assert value["diagnostic_only"] is True
    assert value["decisions_unchanged"] is True
    assert value["live_status"] == "LIVE_DISABLED"


def test_performance_headline_fails_closed_without_comparable_benchmarks() -> None:
    research = _performance_research_result()
    with pytest.raises(ValueError, match="at least one benchmark"):
        FULL_MARKET._performance_headline(
            research,
            (),
            terminal_headline={},
        )

    mismatched = copy.deepcopy(_performance_benchmarks())
    mismatched[0]["metrics"]["observations"] = 236
    with pytest.raises(ValueError, match="range does not match"):
        FULL_MARKET._performance_headline(
            research,
            mismatched,
            terminal_headline={},
        )


def test_performance_headline_fails_closed_on_annualization_contradiction() -> None:
    research = _performance_research_result()
    research["replay"]["metrics"]["annualized_return"] = Decimal("0.021")

    with pytest.raises(ValueError, match="warning contradicts"):
        FULL_MARKET._performance_headline(
            research,
            _performance_benchmarks(),
            terminal_headline={},
        )

    research = _performance_research_result()
    research["replay"]["metrics"]["warnings"] = ()
    with pytest.raises(ValueError, match="missing annualization warning"):
        FULL_MARKET._performance_headline(
            research,
            _performance_benchmarks(),
            terminal_headline={},
        )


def test_performance_headline_preserves_not_evaluable_gate() -> None:
    value = FULL_MARKET._performance_headline(
        {
            "performance_status": "NOT_EVALUABLE_EMPTY_REPLAY",
            "performance_reason": "NO_SPEC_COMPLIANT_ORDER_OR_FILL",
        },
        (),
        terminal_headline={},
    )

    assert value == {
        "schema": "chanlun-v3-performance-headline/v1",
        "status": "NOT_EVALUABLE",
        "reason": "NO_SPEC_COMPLIANT_ORDER_OR_FILL",
        "relative_performance_status": "NOT_EVALUABLE",
        "diagnostic_only": True,
        "decisions_unchanged": True,
        "live_status": "LIVE_DISABLED",
    }


def test_human_review_mode_requires_approximation_and_cannot_write_replay_state(
    tmp_path,
) -> None:
    with pytest.raises(ValueError, match="requires --no-three-program"):
        main(
            (
                "--qmt-local-data-dir",
                str(tmp_path),
                "--human-review-only",
            )
        )

    with pytest.raises(ValueError, match="cannot consume or write replay state"):
        main(
            (
                "--qmt-local-data-dir",
                str(tmp_path),
                "--no-three-program",
                "--approximate-technical-points",
                "--human-review-only",
                "--batch-output",
                str(tmp_path / "forbidden.pkl"),
            )
        )

    assert not (tmp_path / "forbidden.pkl").exists()


def test_nonempty_historical_human_review_alert_carries_sector_gate() -> None:
    decision_at = datetime(2025, 8, 1, 10, 2, tzinfo=CN)
    signal = FULL_MARKET.Signal(
        symbol="SH.600000",
        kind="ENTRY",
        observed_at=decision_at,
        identity="sha256:" + "a" * 64,
    )
    context = _context()
    context.frame["close"] = context.frame["raw_close"]
    [alert] = FULL_MARKET._human_review_alerts(
        signals={decision_at: (signal,)},
        candidate_audit=(
            {
                "accepted": True,
                "symbol": signal.symbol,
                "decision_at": decision_at,
                "technical_approximation_confidence": "MEDIUM",
                "technical_approximation_warning_codes": (),
                "strict_reason_codes_reclassified_as_warnings": (),
                "market_risk_gate": "AMBER",
                "sector_risk_gate": "RED",
                "symbol_risk_gate": "GREEN",
                "exact_green": False,
                "sector_id": "qmt-gics3:test",
                "sector_ranking_source_profile": (
                    "HISTORICAL_TRIGGER_SUMMARY"
                ),
                "sector_name": "测试行业",
                "sector_eligible": True,
                "sector_hard_block": False,
                "sector_regime": "neutral",
                "sector_ordinal": 1,
                "sector_rank_score": 5,
                "sector_rank_reason_codes": ("structural_ranking_only",),
                "sector_horizontal_strength": None,
                "sector_horizontal_rank": None,
                "sector_strength_observed_at": None,
                "sector_strength_source_revision": None,
                "sector_strength_evidence_revision": None,
                "sector_strength_anchor_session": None,
                "sector_strength_member_count": 0,
                "sector_strength_reason_codes": (),
            },
        ),
        contexts={signal.symbol: context},
    )

    assert alert.market_risk_gate == "AMBER"
    assert alert.sector_risk_gate == "RED"
    assert alert.symbol_risk_gate == "GREEN"
    assert alert.sector_ranking_evidence is not None
    assert alert.sector_ranking_evidence.source_profile == (
        "HISTORICAL_TRIGGER_SUMMARY"
    )
    assert alert.sector_ranking_evidence.rank_components == ()
    assert alert.sector_ranking_evidence.evidence_id in alert.source_fact_ids
    assert alert.candidate_id.startswith("sha256:")
