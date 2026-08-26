from __future__ import annotations

import copy
import json
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from chanlun.core.strict_structure.current_events import TerminalSegmentReference
from chanlun.core.strict_structure.models import SourceKind
from chanlun.decision_support.trading_system import live_human_review as subject
from chanlun.decision_support.fingerprints import sha256_json
from chanlun.decision_support.trading_system.human_assisted_decision import (
    apply_formal_selection_scope,
    signal_decision_document_id,
)
from chanlun.decision_support.trading_system.decision_source_provenance import (
    current_decision_source_snapshot,
    decision_source_snapshot_id,
)
from chanlun.decision_support.trading_system.human_assisted_decision import (
    HumanAssistedDecisionCore,
)
from chanlun.decision_support.trading_system.higher_timeframe_gate import (
    HIGHER_TIMEFRAME_SESSION_EVIDENCE_CONTRACT_ID,
    HigherTimeframeSessionEvidence,
    QMT_SECTOR_NATIVE_DAILY_RESEARCH_SOURCE_MODE,
    QMT_SECTOR_SAME_BASE_SOURCE_MODE,
    QmtSectorSameBaseCoverageEvidence,
    sector_native_daily_research_bridge_contract,
)
from chanlun.decision_support.trading_system.lifecycle import (
    structural_point_occurrence_id,
)
from chanlun.decision_support.trading_system.models import (
    EntryExecutionBoundary,
    StructuralPoint,
)
from chanlun.decision_support.trading_system.position_recommendation import (
    build_position_recommendation,
)
from chanlun.decision_support.trading_system.screening_warmup import (
    SCREENING_QMT_30M_FALLBACK_REASON_CODE,
    SCREENING_WARMUP_FREQUENCIES,
    SCREENING_WARMUP_REQUIRED_BARS,
    expected_screening_warmup_suffix_bar_count,
)
from chanlun.decision_support.trading_system.sector_strength import (
    build_horizontal_sector_strength_batch,
)
from chanlun.decision_support.trading_system.selection import (
    SectorMemberHistory,
)
from chanlun.decision_support.trading_system.qmt_higher_timeframe import (
    QMT_HIGHER_TIMEFRAME_WARMUP_CONVERGENCE_PARAMETER_SET_ID,
    QMT_HIGHER_TIMEFRAME_WARMUP_EVIDENCE_CONTRACT_ID,
    QmtHigherTimeframeWarmupEvidence,
)
from chanlun.decision_support.trading_system.qmt_native_daily_bridge import (
    QMT_NATIVE_DAILY_CALENDAR_COVERAGE_EVIDENCE_CONTRACT_ID,
    QMT_NATIVE_DAILY_RECONCILIATION_CONTRACT_ID,
)
from chanlun.decision_support.trading_system.warmup_convergence import (
    WARMUP_CONVERGENCE_DIAGNOSTIC_CONTRACT_ID,
    WARMUP_CONVERGENCE_ENVELOPE_CONTRACT_ID,
    WARMUP_MAPPING_SUPPLY_DIAGNOSTIC_CONTRACT_ID,
    WarmupConvergenceEnvelope,
)
from chanlun.decision_support.trading_system.live_human_review import (
    COVERAGE_MANIFEST_SCHEMA,
    COVERAGE_STATE_CONTRACT_ID,
    LIVE_SCREENING_SCHEMA,
    MONITOR_INSTRUMENT_EXCLUSION_CONTRACT_ID,
    SECTOR_COVERAGE_CONTRACT_ID,
    SIGNAL_DOCUMENT_CONTRACT_ID,
    _decision_context_is_consistent,
    _displayed_decision_evidence_is_consistent,
    _etf_proxy_sector_is_consistent,
    _jsonable,
    _mwd_warmup_diagnostic_chain_is_consistent,
    _risk_evidence_is_consistent,
    _five_minute_trade_signal_is_fresh,
    coverage_manifest_dispositions_are_consistent,
    live_human_review_document,
    live_screening_semantic_snapshot_document,
    live_screening_snapshot_content_sha256,
    live_signal_human_review_alert,
    screening_coverage_epoch_id,
    validate_live_screening_market_watermark,
    validate_live_review_snapshot,
)
from chanlun.decision_support.trading_system.warmup_structure_lineage import (
    WARMUP_STRUCTURE_LINEAGE_DIAGNOSTIC_CONTRACT_ID,
)
from chanlun.decision_support.trading_system.human_review_screening import (
    HigherTimeframePeriodDiagnostic,
)
from tests.trading_system.helpers import (
    confirmed_point,
    deterministic_bundle,
    provisional_point,
)
from tests.trading_system.test_warmup_structure_lineage import lineage_envelope


SECTOR_CATALOG_REVISION = "sha256:" + "7" * 64


def test_live_report_json_adapter_serializes_point_in_time_dates() -> None:
    """QMT ranking/risk evidence carries pure dates, not only datetimes."""

    assert _jsonable({"anchor_session": date(2026, 8, 3)}) == {
        "anchor_session": "2026-08-03"
    }


def test_five_minute_sell_freshness_counts_only_same_session_trading_minutes() -> None:
    cn = deterministic_bundle().as_of.tzinfo
    assert cn is not None

    assert _five_minute_trade_signal_is_fresh(
        datetime(2026, 7, 20, 10, 0, tzinfo=cn),
        datetime(2026, 7, 20, 10, 10, 30, tzinfo=cn),
        symbol="SZ.000001",
    )
    assert not _five_minute_trade_signal_is_fresh(
        datetime(2026, 7, 20, 10, 0, tzinfo=cn),
        datetime(2026, 7, 20, 10, 11, tzinfo=cn),
        symbol="SZ.000001",
    )
    assert _five_minute_trade_signal_is_fresh(
        datetime(2026, 7, 20, 11, 25, tzinfo=cn),
        datetime(2026, 7, 20, 13, 5, tzinfo=cn),
        symbol="SZ.000001",
    )
    assert not _five_minute_trade_signal_is_fresh(
        datetime(2026, 7, 20, 14, 55, tzinfo=cn),
        datetime(2026, 7, 21, 9, 31, tzinfo=cn),
        symbol="SZ.000001",
    )


def test_one_minute_nesting_witness_can_be_known_before_five_minute_setup() -> None:
    [signal] = HumanAssistedDecisionCore().decision_documents(deterministic_bundle())
    setup = copy.deepcopy(signal["setup_5m"])
    nesting_witness = copy.deepcopy(signal["segment_difference_1m"])
    setup_available_at = datetime.fromisoformat(str(setup["available_at"]))
    witness_available_at = setup_available_at - timedelta(minutes=1)
    nesting_witness["available_at"] = witness_available_at.isoformat()
    nesting_witness["terminal_segment_available_at"] = witness_available_at.isoformat()

    assert subject._one_minute_nesting_witness_matches_five_minute_setup(
        setup,
        nesting_witness,
        decision_at=setup_available_at,
    )
    assert not subject._one_minute_nesting_witness_matches_five_minute_setup(
        setup,
        nesting_witness,
        decision_at=setup_available_at - timedelta(microseconds=1),
    )


def test_one_minute_segment_difference_rejects_partial_interval_overlap() -> None:
    [signal] = HumanAssistedDecisionCore().decision_documents(deterministic_bundle())
    setup = copy.deepcopy(signal["setup_5m"])
    nesting_witness = copy.deepcopy(signal["segment_difference_1m"])
    outer_start_at = datetime.fromisoformat(str(setup["terminal_segment_start_at"]))
    nesting_witness["terminal_segment_start_at"] = (
        outer_start_at - timedelta(minutes=5)
    ).isoformat()

    assert not subject._one_minute_nesting_witness_matches_five_minute_setup(
        setup,
        nesting_witness,
        decision_at=signal["observed_at"],
    )


def test_nesting_document_normalizes_completed_bar_start_labels() -> None:
    [signal] = HumanAssistedDecisionCore().decision_documents(deterministic_bundle())
    setup = copy.deepcopy(signal["setup_5m"])
    nesting_witness = copy.deepcopy(signal["segment_difference_1m"])
    outer_start_at = datetime.fromisoformat(str(setup["terminal_segment_start_at"]))
    nesting_witness["terminal_segment_start_at"] = (
        outer_start_at - timedelta(minutes=4)
    ).isoformat()

    assert subject._one_minute_nesting_witness_matches_five_minute_setup(
        setup,
        nesting_witness,
        decision_at=signal["observed_at"],
    )


def test_nested_witness_does_not_require_legacy_setup_price_proximity() -> None:
    [signal] = HumanAssistedDecisionCore().decision_documents(deterministic_bundle())
    setup = copy.deepcopy(signal["setup_5m"])
    nesting_witness = copy.deepcopy(signal["segment_difference_1m"])
    nesting_witness["anchor_price"] = float(setup["anchor_price"]) + 100.0
    nesting_witness["invalidation_price"] = float(setup["invalidation_price"]) + 100.0

    assert subject._one_minute_nesting_witness_matches_five_minute_setup(
        setup,
        nesting_witness,
        decision_at=signal["observed_at"],
    )


def _sector_coverage(
    warmup: QmtHigherTimeframeWarmupEvidence,
    *,
    observed_at: datetime,
) -> QmtSectorSameBaseCoverageEvidence:
    first_visible = observed_at - timedelta(days=700)
    return QmtSectorSameBaseCoverageEvidence(
        observed_at=observed_at,
        calendar_first_session=(observed_at - timedelta(days=800)).date(),
        first_visible_bar_at=first_visible,
        last_visible_bar_at=observed_at - timedelta(minutes=5),
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


def _attach_complete_warmup_chain(
    risk: dict[str, object],
    *,
    observed_at: datetime,
    include_strict_sector: bool = False,
) -> WarmupConvergenceEnvelope:
    envelope = lineage_envelope(
        as_of=observed_at,
        parameter_set_id=(QMT_HIGHER_TIMEFRAME_WARMUP_CONVERGENCE_PARAMETER_SET_ID),
    )
    assert envelope.diagnostic is not None
    assert envelope.mapping_supply_diagnostic is not None
    assert envelope.structure_lineage_diagnostic is not None
    risk.update(
        {
            "warmup_convergence_contract_id": (WARMUP_CONVERGENCE_ENVELOPE_CONTRACT_ID),
            "warmup_convergence_diagnostic_contract_id": (
                WARMUP_CONVERGENCE_DIAGNOSTIC_CONTRACT_ID
            ),
            "warmup_mapping_supply_diagnostic_contract_id": (
                WARMUP_MAPPING_SUPPLY_DIAGNOSTIC_CONTRACT_ID
            ),
            "warmup_structure_lineage_diagnostic_contract_id": (
                WARMUP_STRUCTURE_LINEAGE_DIAGNOSTIC_CONTRACT_ID
            ),
            **{
                f"{subject}_warmup_convergence_evidence": copy.deepcopy(
                    envelope.document()
                )
                for subject in ("market", "sector", "symbol")
            },
            **{
                f"{subject}_warmup_convergence_diagnostic_evidence": (
                    copy.deepcopy(envelope.diagnostic.document())
                )
                for subject in ("market", "sector", "symbol")
            },
            **{
                f"{subject}_warmup_mapping_supply_diagnostic_evidence": (
                    copy.deepcopy(envelope.mapping_supply_diagnostic.document())
                )
                for subject in ("market", "sector", "symbol")
            },
            **{
                f"{subject}_warmup_structure_lineage_diagnostic_evidence": (
                    copy.deepcopy(envelope.structure_lineage_diagnostic.document())
                )
                for subject in ("market", "sector", "symbol")
            },
        }
    )
    if include_strict_sector:
        risk.update(
            {
                "sector_strict_same_5m_warmup_convergence_evidence": (
                    copy.deepcopy(envelope.document())
                ),
                "sector_strict_same_5m_warmup_convergence_diagnostic_evidence": (
                    copy.deepcopy(envelope.diagnostic.document())
                ),
                "sector_strict_same_5m_warmup_mapping_supply_diagnostic_evidence": (
                    copy.deepcopy(envelope.mapping_supply_diagnostic.document())
                ),
                "sector_strict_same_5m_warmup_structure_lineage_diagnostic_evidence": (
                    copy.deepcopy(envelope.structure_lineage_diagnostic.document())
                ),
            }
        )
    return envelope


def _attach_strength_evidence(
    snapshot: dict[str, object],
    *,
    include_signal_members: bool = True,
    additional_members: dict[str, tuple[str, ...]] | None = None,
    decision_time: datetime | None = None,
) -> None:
    as_of = deterministic_bundle().as_of
    sector_ids = tuple(sorted(str(value["sector_id"]) for value in snapshot["sectors"]))
    signal_members: dict[str, set[str]] = {sector_id: set() for sector_id in sector_ids}
    if include_signal_members:
        for signal in snapshot["signals"]:
            sector = signal.get("sector")
            code = signal.get("code")
            if isinstance(sector, dict) and isinstance(code, str):
                sector_id = str(sector.get("sector_id"))
                if sector_id in signal_members:
                    signal_members[sector_id].add(code)
    for sector_id, members in (additional_members or {}).items():
        signal_members.setdefault(sector_id, set()).update(members)
    batch = build_horizontal_sector_strength_batch(
        decision_time=as_of if decision_time is None else decision_time,
        benchmark_symbol="SH.000300",
        benchmark_daily=(),
        members_by_sector={
            sector_id: tuple(
                SectorMemberHistory(
                    symbol,
                    as_of.date(),
                    "NEW_LISTING",
                    (),
                )
                for symbol in sorted(signal_members[sector_id])
            )
            for sector_id in sector_ids
        },
        membership_revision=SECTOR_CATALOG_REVISION,
    )
    snapshot["sector_strength_evidence"] = batch.evidence_document()
    snapshot["sector_strength_evidence_revision"] = batch.evidence_revision
    manifest = snapshot["coverage_manifest"]
    assert isinstance(manifest, dict)
    manifest["sector_strength_evidence_revision"] = batch.evidence_revision
    by_id = {str(value["sector_id"]): value for value in snapshot["sectors"]}
    for sector_id, document in by_id.items():
        evidence = batch[sector_id]
        document.update(
            {
                "horizontal_strength": None,
                "horizontal_rank": None,
                "strength_anchor_session": None,
                "strength_member_count": evidence.member_count,
                "strength_source_revision": evidence.source_revision,
                "strength_reason_codes": list(evidence.reason_codes),
            }
        )
    for signal in snapshot["signals"]:
        sector = signal.get("sector")
        if not isinstance(sector, dict):
            continue
        source = by_id.get(str(sector.get("sector_id")))
        if source is not None:
            for field in (
                "horizontal_strength",
                "horizontal_rank",
                "strength_anchor_session",
                "strength_member_count",
                "strength_source_revision",
                "strength_reason_codes",
            ):
                sector[field] = source[field]
        signal["decision_document_id"] = signal_decision_document_id(signal)
    epoch_id = screening_coverage_epoch_id(
        market_data_as_of=as_of,
        universe_revision=str(manifest["universe_revision"]),
        sector_catalog_revision=str(manifest["sector_catalog_revision"]),
        sector_strength_evidence_revision=batch.evidence_revision,
        decision_core_id=str(snapshot["decision_core_id"]),
        screening_policy_id=str(snapshot["screening_policy_id"]),
        structure_contract_id=str(snapshot["structure_contract_id"]),
        parameter_set_id=str(snapshot["parameter_set_id"]),
    )
    snapshot["coverage_epoch_id"] = epoch_id
    manifest["coverage_epoch_id"] = epoch_id


def _nested_sell_points(*, fresh: bool) -> tuple[StructuralPoint, StructuralPoint]:
    setup_minutes = 295 if fresh else 0
    setup = confirmed_point("1sell", minutes_after=setup_minutes)
    setup = replace(
        setup,
        terminal_segment=TerminalSegmentReference(
            role="latest_completed",
            structural_level=0,
            unit_id=f"segment:5m:{setup.point_id}",
            source_kind=SourceKind.SEGMENT,
            direction="up",
            state="locked",
            market_start=setup.anchor_at - timedelta(minutes=30),
            market_end=setup.anchor_at,
            available_at=setup.available_at,
        ),
    )
    segment_difference = confirmed_point(
        "1sell",
        frequency="1m",
        minutes_after=setup_minutes - 1,
        available_minutes_after=2,
    )
    segment_difference = replace(
        segment_difference,
        terminal_segment=TerminalSegmentReference(
            role="latest_completed",
            structural_level=0,
            unit_id=f"segment:1m:{segment_difference.point_id}",
            source_kind=SourceKind.SEGMENT,
            direction="up",
            state="locked",
            market_start=segment_difference.anchor_at - timedelta(minutes=1),
            market_end=segment_difference.anchor_at,
            available_at=segment_difference.available_at,
        ),
    )
    return setup, segment_difference


def _decisions(*, fresh_sell: bool = False):
    core = HumanAssistedDecisionCore()
    base = deterministic_bundle()
    segment_difference = base.one_points[0]
    buy_bundle = replace(
        base,
        entry_execution_boundaries=(
            EntryExecutionBoundary(
                symbol=base.code,
                setup_occurrence_id=(
                    base.entry_execution_boundaries[0].setup_occurrence_id
                ),
                point_id=segment_difference.point_id,
                source_frequency="1m",
                confirmation_bar_closed_at=segment_difference.available_at,
                raw_open=Decimal("10.10"),
                raw_high=Decimal("10.25"),
                raw_low=Decimal("10.00"),
                raw_close=Decimal("10.20"),
                raw_volume=Decimal("10000"),
                entry_valid_until=(
                    segment_difference.available_at + timedelta(minutes=1)
                ),
                raw_price_basis_revision="qmt-none-test",
            ),
        ),
    )
    sell_setup, sell_segment_difference = _nested_sell_points(fresh=fresh_sell)
    sell_bundle = replace(
        buy_bundle,
        five_points=(sell_setup,),
        one_points=(sell_segment_difference,),
        opposite_points=(),
        entry_execution_boundaries=(),
    )
    setup_identities = {
        point.point_id: structural_point_occurrence_id(point)
        for bundle in (buy_bundle, sell_bundle)
        for point in bundle.five_points
        if isinstance(point, StructuralPoint)
    }
    return (
        core,
        (
            *core.decision_documents(buy_bundle),
            *core.decision_documents(sell_bundle),
        ),
        setup_identities,
    )


def live_snapshot(*, fresh_sell: bool = False) -> dict[str, object]:
    core, signals, setup_identities = _decisions(fresh_sell=fresh_sell)
    as_of = deterministic_bundle().as_of
    contexts = {
        frequency: {
            "frequency": frequency,
            "direction": "up" if frequency == "30m" else "neutral",
            "disposition": ("supportive" if frequency == "30m" else "neutral"),
            "hard_block": False,
            "dominant_point_id": ("sha256:" + "a" * 64 if frequency == "30m" else None),
            "dominant_point_type": "3buy" if frequency == "30m" else None,
            "reason_codes": [
                (
                    "confirmed_buy_structure"
                    if frequency == "30m"
                    else "stock_one_minute_segment_difference_only"
                    if frequency == "1m"
                    else "no_active_directional_point"
                )
            ],
            "observed_at": as_of.isoformat(),
        }
        for frequency in ("30m", "5m", "1m")
    }
    sector_document: dict[str, object] = {
        "sector_id": "qmt-gics3:test",
        "sector_name": "测试行业",
        "eligible": True,
        "hard_block": False,
        "regime": "supportive",
        "rank": 1,
        "rank_score": 45,
        "rank_components": {
            "thirty_support": 40,
            "five_support": 0,
            "neutral_access": 5,
        },
        "reason_codes": ["structural_ranking_only"],
        "horizontal_strength": None,
        "horizontal_rank": None,
        "strength_anchor_session": None,
        "strength_member_count": 0,
        "strength_source_revision": None,
        "strength_reason_codes": [],
        "context_30m": contexts["30m"],
        "context_5m": contexts["5m"],
        "context_1m": contexts["1m"],
    }
    scoped_signals = []
    for value in signals:
        signal = dict(value)
        signal["selection_sources"] = ["QMT_SECTOR_TRIGGER"]
        signal["sector_triggered"] = True
        signal["monitor_only"] = False
        signal["sector"] = {**sector_document, "rank": None}
        # This helper replaces the decision core's original synthetic sector
        # with the QMT-scoped sector above. Re-identify the setup/lifecycle just
        # as production does when it evaluates directly inside that sector;
        # otherwise the fixture itself would carry a decorative stale ID.
        signal["physical_timeframe_recursive"] = True
        signal["higher_timeframe_risk"].update(
            {
                "market_states": {period: "UNRESOLVED" for period in ("M", "W", "D")},
                "sector_states": {period: "UNRESOLVED" for period in ("M", "W", "D")},
                "symbol_states": {period: "UNRESOLVED" for period in ("M", "W", "D")},
                "new_entry_requires_all_green": False,
                "session_evidence_contract_id": (
                    HIGHER_TIMEFRAME_SESSION_EVIDENCE_CONTRACT_ID
                ),
                "market_session_evidence": (
                    HigherTimeframeSessionEvidence.unavailable().document()
                ),
                "sector_session_evidence": (
                    HigherTimeframeSessionEvidence.unavailable().document()
                ),
                "symbol_session_evidence": (
                    HigherTimeframeSessionEvidence.unavailable().document()
                ),
                "warmup_evidence_contract_id": (
                    QMT_HIGHER_TIMEFRAME_WARMUP_EVIDENCE_CONTRACT_ID
                ),
                "market_warmup_evidence": None,
                "sector_warmup_evidence": None,
                "symbol_warmup_evidence": None,
                "warmup_convergence_contract_id": (
                    WARMUP_CONVERGENCE_ENVELOPE_CONTRACT_ID
                ),
                "market_warmup_convergence_evidence": None,
                "sector_warmup_convergence_evidence": None,
                "symbol_warmup_convergence_evidence": None,
                "warmup_convergence_diagnostic_contract_id": (
                    WARMUP_CONVERGENCE_DIAGNOSTIC_CONTRACT_ID
                ),
                "market_warmup_convergence_diagnostic_evidence": None,
                "sector_warmup_convergence_diagnostic_evidence": None,
                "symbol_warmup_convergence_diagnostic_evidence": None,
                "warmup_mapping_supply_diagnostic_contract_id": (
                    WARMUP_MAPPING_SUPPLY_DIAGNOSTIC_CONTRACT_ID
                ),
                "market_warmup_mapping_supply_diagnostic_evidence": None,
                "sector_warmup_mapping_supply_diagnostic_evidence": None,
                "symbol_warmup_mapping_supply_diagnostic_evidence": None,
                "warmup_structure_lineage_diagnostic_contract_id": (
                    WARMUP_STRUCTURE_LINEAGE_DIAGNOSTIC_CONTRACT_ID
                ),
                "market_warmup_structure_lineage_diagnostic_evidence": None,
                "sector_warmup_structure_lineage_diagnostic_evidence": None,
                "symbol_warmup_structure_lineage_diagnostic_evidence": None,
                "native_daily_reconciliation_contract_id": (
                    QMT_NATIVE_DAILY_RECONCILIATION_CONTRACT_ID
                ),
                "market_native_daily_reconciliation_evidence": None,
                "sector_native_daily_reconciliation_evidence": None,
                "symbol_native_daily_reconciliation_evidence": None,
                "native_daily_calendar_coverage_contract_id": (
                    QMT_NATIVE_DAILY_CALENDAR_COVERAGE_EVIDENCE_CONTRACT_ID
                ),
                "market_native_daily_calendar_coverage_evidence": None,
                "sector_native_daily_calendar_coverage_evidence": None,
                "symbol_native_daily_calendar_coverage_evidence": None,
            }
        )
        # 月/周/日旧风险包保留用于审计，但不再覆盖 5m/1m 已确认的物理结构。
        signal["warmup"] = {
            "converged": True,
            "by_frequency": [
                {
                    "frequency": frequency,
                    "converged": True,
                    "full_bar_count": SCREENING_WARMUP_REQUIRED_BARS[frequency],
                    "suffix_bar_count": (
                        expected_screening_warmup_suffix_bar_count(
                            SCREENING_WARMUP_REQUIRED_BARS[frequency]
                        )
                    ),
                }
                for frequency in SCREENING_WARMUP_FREQUENCIES
            ],
            "reason_codes": [
                f"{frequency.upper()}:WARMUP_TAIL_STABLE"
                for frequency in SCREENING_WARMUP_FREQUENCIES
            ],
            "difference_codes_by_frequency": [
                {"frequency": frequency, "difference_codes": []}
                for frequency in SCREENING_WARMUP_FREQUENCIES
            ],
            "required_for_new_entry": True,
        }
        signal["setup_id"] = sha256_json(
            {
                "schema": "chanlun-trade-setup",
                "point_id": setup_identities[signal["setup_5m"]["point_id"]],
                "sector_id": sector_document["sector_id"],
                "sector_required": True,
            }
        )
        signal["signal_id"] = sha256_json(
            {
                "schema": "chanlun-signal-lifecycle",
                "setup_id": signal["setup_id"],
                "side": signal["setup_5m"]["side"],
            }
        )
        signal["decision_document_id"] = signal_decision_document_id(signal)
        scoped_signals.append(signal)
    sectors = [sector_document]
    codes = sorted({str(value["code"]) for value in signals})
    coverage_epoch_id = "sha256:" + "c" * 64
    screening_policy = {"schema": "test-screening-policy"}
    screening_policy_id = sha256_json(screening_policy)
    universe_revision = sha256_json(
        {"schema": "test-screening-universe", "codes": codes}
    )
    stable: dict[str, object] = {
        "schema": LIVE_SCREENING_SCHEMA,
        "structure_contract_id": "test-structure",
        "parameter_set_id": "test-parameter",
        "signal_document_contract_id": SIGNAL_DOCUMENT_CONTRACT_ID,
        "sector_coverage_contract_id": SECTOR_COVERAGE_CONTRACT_ID,
        "monitor_instrument_exclusion_contract_id": (
            MONITOR_INSTRUMENT_EXCLUSION_CONTRACT_ID
        ),
        "available": True,
        "scan_state": "complete",
        "scanned_at": as_of.isoformat(),
        "as_of": as_of.isoformat(),
        "market_data_as_of": as_of.isoformat(),
        "coverage_epoch_id": coverage_epoch_id,
        "sector_first": True,
        "read_only": True,
        "research_only": True,
        "no_order_execution": True,
        "decision_core_id": core.contract_id,
        "decision_core": core.contract.document(),
        "screening_policy": screening_policy,
        "screening_policy_id": screening_policy_id,
        "sectors": sectors,
        "monitor_instrument_exclusions": [],
        "signals": scoped_signals,
        "counts_by_stage": {"triggered": 2},
        "counts_by_point_type": {"2buy": 1, "1sell": 1},
        "scan_audit": {
            "coverage_cycle_complete": True,
            "coverage_cycle_completion_ratio": "1",
            "sector_discovered_count": 1,
            "sector_completed_count": 1,
            "sector_excluded_count": 0,
            "sector_failed_count": 0,
            "sector_resolved_count": 1,
            "sector_completion_ratio": "1",
            "sector_resolution_ratio": "1",
            "sector_failure_counts": {},
            "sector_exclusion_counts": {},
            "selected_sector_count": 1,
            "discovered_symbol_count": len(codes),
            "coverage_cycle_attempted_symbol_count": len(codes),
            "coverage_cycle_completed_symbol_count": len(codes),
            "coverage_cycle_excluded_symbol_count": 0,
            "coverage_cycle_failed_symbol_count": 0,
            "coverage_cycle_resolved_symbol_count": len(codes),
            "coverage_cycle_resolution_ratio": "1",
            "immediate_pending_symbol_count": 0,
            "pending_symbol_count": 0,
            "retry_symbol_count": 0,
            "backoff_retry_symbol_count": 0,
            "next_epoch_retry_symbol_count": 0,
            "stock_failure_counts": {},
            "stock_exclusion_counts": {},
            "monitor_instrument_exclusion_count": 0,
            "full_market_history_scan": False,
            "coverage_cycle_batch_count": 1,
        },
        "coverage_manifest": {
            "schema": COVERAGE_MANIFEST_SCHEMA,
            "coverage_state_contract_id": COVERAGE_STATE_CONTRACT_ID,
            "complete": True,
            "signal_document_contract_id": SIGNAL_DOCUMENT_CONTRACT_ID,
            "coverage_epoch_id": coverage_epoch_id,
            "screening_policy_id": screening_policy_id,
            "market_data_as_of": as_of.isoformat(),
            "source_cutoff": as_of.isoformat(),
            "superseded_coverage_epoch_id": None,
            "superseded_market_data_as_of": None,
            "batch_count": 1,
            "universe_revision": universe_revision,
            "sector_catalog_revision": SECTOR_CATALOG_REVISION,
            "sector_strength_evidence_revision": None,
            "discovered_codes": codes,
            "completed_codes": codes,
            "excluded_codes": [],
            "failed_codes": [],
            "exclusions": [],
            "discarded_out_of_scope_retry_codes": [],
            "pending_frequencies": {},
            "backoff_frequencies": {},
            "deferred_frequencies": {},
        },
        "errors": [],
        "sector_exclusions": [],
    }
    _attach_strength_evidence(stable)
    return {
        **stable,
        "snapshot_content_sha256": live_screening_snapshot_content_sha256(stable),
    }


def _with_monitor_instrument_exclusion(
    snapshot: dict[str, object],
    *,
    code: str = "SH.000001",
    instrument_type: str = "index_cn",
) -> dict[str, object]:
    unresolved = instrument_type == "unresolved_cn"
    snapshot["monitor_instrument_exclusions"] = [
        {
            "code": code,
            "eligibility": (
                "UNRESOLVED_FROM_TRADING_SCREENING"
                if unresolved
                else "EXCLUDED_FROM_TRADING_SCREENING"
            ),
            "reason_code": (
                "QMT_NATIVE_INSTRUMENT_TYPE_UNRESOLVED"
                if unresolved
                else "QMT_NATIVE_STOCK_OR_ETF_REQUIRED"
            ),
            "selection_sources": ["ACTIVE_WATCHLIST_MONITOR"],
            "evidence_source": "QMT_GET_INSTRUMENT_TYPE",
            "qmt_instrument_type": instrument_type,
            "diagnostic_only": True,
            "tick_data_used": False,
            "real_account_accessed": False,
            "real_order_transport_enabled": False,
            "live_status": "LIVE_DISABLED",
        }
    ]
    snapshot["scan_audit"]["monitor_instrument_exclusion_count"] = 1
    snapshot["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
        snapshot
    )
    return snapshot


def test_semantic_snapshot_normalization_does_not_mutate_source() -> None:
    snapshot = live_snapshot()
    snapshot["scan_audit"]["batch_duration_ms"] = 123
    snapshot["coverage_manifest"]["batch_count"] = 7
    original_audit = copy.deepcopy(snapshot["scan_audit"])
    original_manifest = copy.deepcopy(snapshot["coverage_manifest"])

    stable = live_screening_semantic_snapshot_document(snapshot)

    assert "batch_duration_ms" not in stable["scan_audit"]
    assert "batch_count" not in stable["coverage_manifest"]
    assert snapshot["scan_audit"] == original_audit
    assert snapshot["coverage_manifest"] == original_manifest
    stable["scan_audit"]["pending_symbol_count"] = 999
    stable["coverage_manifest"]["complete"] = False
    assert snapshot["scan_audit"]["pending_symbol_count"] == 0
    assert snapshot["coverage_manifest"]["complete"] is True


def test_monitor_instrument_exclusion_contract_accepts_canonical_diagnostic() -> None:
    snapshot = _with_monitor_instrument_exclusion(live_snapshot())

    assert validate_live_screening_market_watermark(snapshot) == (
        deterministic_bundle().as_of
    )
    validate_live_review_snapshot(snapshot)


def test_postclose_review_uses_authenticated_ranking_availability_time() -> None:
    snapshot = live_snapshot()
    market_cutoff = deterministic_bundle().as_of
    ranking_available_at = market_cutoff + timedelta(hours=5)
    snapshot["scanned_at"] = ranking_available_at.isoformat()
    _attach_strength_evidence(snapshot, decision_time=ranking_available_at)
    snapshot["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
        snapshot
    )

    decision_at, _signals = validate_live_review_snapshot(snapshot)
    report = live_human_review_document(
        live_snapshot=snapshot,
        source_snapshot_sha256=str(snapshot["snapshot_content_sha256"]),
        session=market_cutoff.date(),
    )

    assert decision_at == market_cutoff
    assert report["sample"]["market_data_as_of"] == market_cutoff.isoformat()
    assert report["review_queue"]
    assert {
        row["review_available_at"] for row in report["review_queue"]
    } == {ranking_available_at.isoformat()}


def test_monitor_instrument_exclusion_contract_preserves_unresolved_type() -> None:
    snapshot = _with_monitor_instrument_exclusion(
        live_snapshot(),
        code="SH.600000",
        instrument_type="unresolved_cn",
    )

    validate_live_screening_market_watermark(snapshot)
    validate_live_review_snapshot(snapshot)


def test_market_watermark_rejects_rehashed_monitor_exclusion_forgery() -> None:
    canonical = _with_monitor_instrument_exclusion(live_snapshot())
    validate_live_screening_market_watermark(canonical)

    deleted = copy.deepcopy(canonical)
    deleted.pop("monitor_instrument_exclusions")
    deleted["scan_audit"]["monitor_instrument_exclusion_count"] = 0

    forged_safety = copy.deepcopy(canonical)
    forged_safety["monitor_instrument_exclusions"][0].update(
        {
            "eligibility": "ELIGIBLE",
            "evidence_source": "FORGED",
            "diagnostic_only": False,
            "tick_data_used": True,
            "real_account_accessed": True,
            "real_order_transport_enabled": True,
            "live_status": "LIVE_ENABLED",
        }
    )

    forged_scope = copy.deepcopy(canonical)
    forged_scope["monitor_instrument_exclusions"][0]["selection_sources"] = [
        "QMT_SECTOR_TRIGGER"
    ]

    forged_tradable_type = copy.deepcopy(canonical)
    forged_tradable_type["monitor_instrument_exclusions"][0]["qmt_instrument_type"] = (
        "stock_cn"
    )

    rediscovered = copy.deepcopy(canonical)
    code = "SH.000001"
    for field in ("discovered_codes", "completed_codes"):
        rediscovered["coverage_manifest"][field] = sorted(
            (*rediscovered["coverage_manifest"][field], code)
        )
    rediscovered_count = len(rediscovered["coverage_manifest"]["discovered_codes"])
    rediscovered["scan_audit"].update(
        {
            "discovered_symbol_count": rediscovered_count,
            "coverage_cycle_attempted_symbol_count": rediscovered_count,
            "coverage_cycle_completed_symbol_count": rediscovered_count,
            "coverage_cycle_resolved_symbol_count": rediscovered_count,
        }
    )

    for forged in (
        deleted,
        forged_safety,
        forged_scope,
        forged_tradable_type,
        rediscovered,
    ):
        forged["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
            forged
        )
        with pytest.raises(
            ValueError,
            match="market watermark is unverified",
        ):
            validate_live_screening_market_watermark(forged)


def _formed_unresolved_diagnostics(observed_at: str) -> list[dict[str, object]]:
    return [
        {
            "period": period,
            "state": "FORMED_UNRESOLVED",
            "completed_bar_count": 10,
            "evidence_bar_end": observed_at,
            "active_top_interval": [observed_at, observed_at],
            "mapping_unique": False,
            "mapped_center_id": None,
            "mapping_candidate_ids": [],
            "blocker_codes": ["NO_COMPLETED_LOWER_1SELL_2SELL_CENTER_IN_TOP_FRACTAL"],
            "warning_codes": [],
            "source_revision": "sha256:" + period.lower() * 64,
        }
        for period in ("M", "W", "D")
    ]


def _with_hostile_sector(snapshot: dict[str, object]) -> dict[str, object]:
    hostile = json.loads(json.dumps(snapshot["sectors"][0]))
    hostile.update(
        {
            "sector_id": "qmt-gics3:hostile",
            "sector_name": "高级别风险行业",
            "eligible": False,
            "hard_block": True,
            "regime": "hostile",
            "rank": None,
            "rank_score": 0,
            "rank_components": {
                "thirty_support": 0,
                "five_support": 0,
                "neutral_access": 0,
            },
            "reason_codes": ["higher_structure_sell_risk"],
        }
    )
    hostile["context_30m"].update(
        {
            "direction": "down",
            "disposition": "hostile",
            "hard_block": True,
            "dominant_point_id": "sha256:" + "e" * 64,
            "dominant_point_type": "1sell",
            "reason_codes": ["confirmed_sell_with_down_structure"],
        }
    )
    snapshot["sectors"].append(hostile)
    snapshot["scan_audit"].update(
        {
            "sector_discovered_count": 2,
            "sector_completed_count": 2,
            "sector_excluded_count": 0,
            "sector_failed_count": 0,
            "sector_resolved_count": 2,
            "sector_completion_ratio": "1",
            "sector_resolution_ratio": "1",
            "sector_failure_counts": {},
            "sector_exclusion_counts": {},
            "selected_sector_count": 1,
        }
    )
    _attach_strength_evidence(snapshot)
    snapshot["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
        snapshot
    )
    return snapshot


def test_live_snapshot_recomputes_ranked_subset_from_full_sector_set() -> None:
    """Hard-blocked assessments remain visible but must stay unranked."""

    snapshot = _with_hostile_sector(live_snapshot())
    _review_at, signals = validate_live_review_snapshot(snapshot)

    assert len(signals) == 2
    assert snapshot["sectors"][0]["rank"] == 1
    assert snapshot["sectors"][1]["rank"] is None


def test_qmt_sector_trigger_requires_authenticated_current_member() -> None:
    """An eligible sector label cannot make a non-member a sector trigger."""

    snapshot = live_snapshot()
    _attach_strength_evidence(snapshot, include_signal_members=False)
    snapshot["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
        snapshot
    )

    with pytest.raises(ValueError, match="timeframe provenance"):
        validate_live_review_snapshot(snapshot)


def test_live_snapshot_requires_every_eligible_sector_member_in_coverage() -> None:
    """A self-consistent subset cannot claim full current-sector coverage."""

    snapshot = live_snapshot()
    sector_id = str(snapshot["sectors"][0]["sector_id"])
    _attach_strength_evidence(
        snapshot,
        additional_members={sector_id: ("SZ.000002",)},
    )
    snapshot["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
        snapshot
    )

    with pytest.raises(ValueError, match="eligible sector member coverage"):
        validate_live_review_snapshot(snapshot)


@pytest.mark.parametrize("scope_mode", ("VALIDATION_COHORT", "LARGE_SCOPE"))
def test_bounded_review_requires_only_admitted_strength_peers(
    scope_mode: str,
) -> None:
    snapshot = live_snapshot()
    discovered = list(snapshot["coverage_manifest"]["discovered_codes"])
    snapshot.update(
        {
            "screening_scope_mode": scope_mode,
            "effective_monitor_universe_limit": 12,
            "admitted_universe_codes": discovered,
        }
    )
    sector_id = str(snapshot["sectors"][0]["sector_id"])
    _attach_strength_evidence(
        snapshot,
        additional_members={sector_id: ("SZ.000002",)},
    )
    snapshot["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
        snapshot
    )

    _review_at, signals = validate_live_review_snapshot(snapshot)

    assert signals
    assert "SZ.000002" not in discovered


def test_bounded_review_requires_every_admitted_strength_peer_in_coverage() -> None:
    snapshot = live_snapshot()
    discovered = list(snapshot["coverage_manifest"]["discovered_codes"])
    admitted_peer = "SZ.000002"
    snapshot.update(
        {
            "screening_scope_mode": "VALIDATION_COHORT",
            "effective_monitor_universe_limit": 12,
            "admitted_universe_codes": [*discovered, admitted_peer],
        }
    )
    sector_id = str(snapshot["sectors"][0]["sector_id"])
    _attach_strength_evidence(
        snapshot,
        additional_members={sector_id: (admitted_peer,)},
    )
    snapshot["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
        snapshot
    )

    with pytest.raises(ValueError, match="eligible sector member coverage"):
        validate_live_review_snapshot(snapshot)


def test_bounded_review_rejects_discovered_code_outside_admission() -> None:
    snapshot = live_snapshot()
    snapshot.update(
        {
            "screening_scope_mode": "VALIDATION_COHORT",
            "effective_monitor_universe_limit": 12,
            "admitted_universe_codes": ["SH.600000"],
        }
    )
    snapshot["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
        snapshot
    )

    with pytest.raises(ValueError, match="scope admission"):
        validate_live_review_snapshot(snapshot)


def test_live_snapshot_rejects_self_attested_sector_strength_source_hash() -> None:
    """A replacement SHA cannot override the batch recomputation evidence."""

    snapshot = live_snapshot()
    forged = "sha256:" + "f" * 64
    snapshot["sectors"][0]["strength_source_revision"] = forged
    for signal in snapshot["signals"]:
        signal["sector"]["strength_source_revision"] = forged
    snapshot["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
        snapshot
    )

    with pytest.raises(ValueError, match="sector ranking"):
        validate_live_review_snapshot(snapshot)


def test_live_snapshot_rejects_tampered_strength_batch_revision() -> None:
    snapshot = live_snapshot()
    forged = "sha256:" + "f" * 64
    snapshot["sector_strength_evidence_revision"] = forged
    manifest = snapshot["coverage_manifest"]
    manifest["sector_strength_evidence_revision"] = forged
    epoch_id = screening_coverage_epoch_id(
        market_data_as_of=deterministic_bundle().as_of,
        universe_revision=str(manifest["universe_revision"]),
        sector_catalog_revision=str(manifest["sector_catalog_revision"]),
        sector_strength_evidence_revision=forged,
        decision_core_id=str(snapshot["decision_core_id"]),
        screening_policy_id=str(snapshot["screening_policy_id"]),
        structure_contract_id=str(snapshot["structure_contract_id"]),
        parameter_set_id=str(snapshot["parameter_set_id"]),
    )
    snapshot["coverage_epoch_id"] = epoch_id
    manifest["coverage_epoch_id"] = epoch_id
    snapshot["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
        snapshot
    )

    with pytest.raises(ValueError, match="strength evidence"):
        validate_live_review_snapshot(snapshot)


def test_live_snapshot_recomputes_coverage_epoch_identity() -> None:
    snapshot = live_snapshot()
    forged = "sha256:" + "0" * 64
    snapshot["coverage_epoch_id"] = forged
    snapshot["coverage_manifest"]["coverage_epoch_id"] = forged
    snapshot["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
        snapshot
    )

    with pytest.raises(ValueError, match="boundary is incomplete"):
        validate_live_review_snapshot(snapshot)


def test_live_snapshot_rejects_market_cutoff_after_the_decision_snapshot() -> None:
    """A self-consistent epoch cannot attach future market facts to a page."""

    snapshot = live_snapshot()
    review_at = deterministic_bundle().as_of
    future_cutoff = review_at + timedelta(minutes=1)
    manifest = snapshot["coverage_manifest"]
    snapshot["market_data_as_of"] = future_cutoff.isoformat()
    manifest["market_data_as_of"] = future_cutoff.isoformat()
    epoch_id = screening_coverage_epoch_id(
        market_data_as_of=future_cutoff,
        universe_revision=str(manifest["universe_revision"]),
        sector_catalog_revision=str(manifest["sector_catalog_revision"]),
        sector_strength_evidence_revision=str(
            manifest["sector_strength_evidence_revision"]
        ),
        decision_core_id=str(snapshot["decision_core_id"]),
        screening_policy_id=str(snapshot["screening_policy_id"]),
        structure_contract_id=str(snapshot["structure_contract_id"]),
        parameter_set_id=str(snapshot["parameter_set_id"]),
    )
    snapshot["coverage_epoch_id"] = epoch_id
    manifest["coverage_epoch_id"] = epoch_id
    snapshot["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
        snapshot
    )

    with pytest.raises(ValueError, match="boundary is incomplete"):
        validate_live_review_snapshot(snapshot, session=review_at.date())


def test_live_snapshot_rejects_structure_observed_after_market_cutoff() -> None:
    """Post-cutoff computation may not turn into post-cutoff market facts."""

    snapshot = live_snapshot()
    review_at = deterministic_bundle().as_of
    earlier_cutoff = review_at - timedelta(minutes=1)
    manifest = snapshot["coverage_manifest"]
    snapshot["market_data_as_of"] = earlier_cutoff.isoformat()
    manifest["market_data_as_of"] = earlier_cutoff.isoformat()
    epoch_id = screening_coverage_epoch_id(
        market_data_as_of=earlier_cutoff,
        universe_revision=str(manifest["universe_revision"]),
        sector_catalog_revision=str(manifest["sector_catalog_revision"]),
        sector_strength_evidence_revision=str(
            manifest["sector_strength_evidence_revision"]
        ),
        decision_core_id=str(snapshot["decision_core_id"]),
        screening_policy_id=str(snapshot["screening_policy_id"]),
        structure_contract_id=str(snapshot["structure_contract_id"]),
        parameter_set_id=str(snapshot["parameter_set_id"]),
    )
    snapshot["coverage_epoch_id"] = epoch_id
    manifest["coverage_epoch_id"] = epoch_id
    snapshot["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
        snapshot
    )

    with pytest.raises(ValueError, match="sector ranking is invalid"):
        validate_live_review_snapshot(snapshot)


def test_live_snapshot_rejects_setup_available_after_market_cutoff() -> None:
    snapshot = live_snapshot()
    future = deterministic_bundle().as_of + timedelta(minutes=1)
    snapshot["signals"][0]["setup_5m"]["available_at"] = future.isoformat()
    snapshot["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
        snapshot
    )

    with pytest.raises(ValueError, match="timeframe provenance is invalid"):
        validate_live_review_snapshot(snapshot)


def test_live_snapshot_rejects_removed_invalidated_point_status() -> None:
    snapshot = live_snapshot()
    snapshot["signals"][0]["setup_5m"]["status"] = "invalidated"
    snapshot["signals"][0]["setup_5m"]["confirmed_at"] = None
    snapshot["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
        snapshot
    )

    with pytest.raises(ValueError, match="timeframe provenance is invalid"):
        validate_live_review_snapshot(snapshot)


def test_live_snapshot_rejects_nested_evidence_after_signal_observation() -> None:
    """The market cutoff cannot authorize facts that postdate this signal."""

    snapshot = live_snapshot()
    snapshot["signals"][0]["observed_at"] = (
        deterministic_bundle().as_of - timedelta(hours=6)
    ).isoformat()
    snapshot["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
        snapshot
    )

    with pytest.raises(ValueError, match="timeframe provenance is invalid"):
        validate_live_review_snapshot(snapshot)


def test_live_snapshot_rejects_setup_that_disagrees_with_outer_point() -> None:
    snapshot = live_snapshot()
    snapshot["signals"][0]["setup_5m"]["point_type"] = "3buy"
    snapshot["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
        snapshot
    )

    with pytest.raises(ValueError, match="timeframe provenance is invalid"):
        validate_live_review_snapshot(snapshot)


def test_live_snapshot_recomputes_setup_and_signal_lifecycle_ids() -> None:
    snapshot = live_snapshot()
    snapshot["signals"][0]["setup_id"] = "sha256:" + "1" * 64
    snapshot["signals"][0]["signal_id"] = "sha256:" + "2" * 64
    snapshot["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
        snapshot
    )

    with pytest.raises(ValueError, match="timeframe provenance is invalid"):
        validate_live_review_snapshot(snapshot)


def test_live_snapshot_accepts_engine_structural_occurrence_setup_identity() -> None:
    snapshot = live_snapshot()
    signal = snapshot["signals"][0]
    setup = signal["setup_5m"]
    legacy_point_id_setup = sha256_json(
        {
            "schema": "chanlun-trade-setup",
            "point_id": setup["point_id"],
            "sector_id": signal["sector"]["sector_id"],
            "sector_required": True,
        }
    )

    assert setup["status"] == "confirmed"
    assert signal["setup_id"] != legacy_point_id_setup
    _review_at, signals = validate_live_review_snapshot(snapshot)
    assert len(signals) == 2


@pytest.mark.parametrize(
    ("mutation", "value"),
    (
        ("lifecycle_stage", "closed"),
        ("market_gate", "NOT_A_GATE"),
        ("sector_gate", "NOT_A_GATE"),
    ),
)
def test_live_snapshot_validator_covers_review_candidate_conversion(
    mutation: str,
    value: str,
) -> None:
    """Archive readiness must include the later alert-construction boundary."""

    snapshot = live_snapshot()
    if mutation == "lifecycle_stage":
        snapshot["signals"][0]["lifecycle_stage"] = value
    else:
        snapshot["signals"][0]["higher_timeframe_risk"][mutation] = value
    snapshot["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
        snapshot
    )

    expected = (
        "timeframe provenance is invalid"
        if mutation == "lifecycle_stage"
        else "risk evidence is invalid"
    )
    with pytest.raises(ValueError, match=expected):
        validate_live_review_snapshot(snapshot)


def test_invalidated_snapshot_row_is_audited_but_not_revived_for_review() -> None:
    snapshot = live_snapshot()
    invalidated = snapshot["signals"][0]
    invalidated["lifecycle_stage"] = "invalidated"
    invalidated["technical_entry_allowed"] = False
    invalidated["entry_allowed"] = False
    invalidated["risk_multiplier"] = "1.00"
    invalidated["decision_reasons"] = [
        "lifecycle_not_actionable",
        "one_minute_not_confirmed",
        "SAME_PERIOD_CONTEXT_GRADE_UNRESOLVED",
        "structure_invalidated",
    ]
    position_recommendation = build_position_recommendation(
        side="buy",
        recommendation="BLOCKED",
        risk_multiplier="1.00",
        context_risk_scale="0.50",
        entry_price=invalidated["setup_5m"]["anchor_price"],
        structural_stop=invalidated["setup_5m"]["invalidation_price"],
        exit_action="none",
    ).document()
    invalidated["position_recommendation"] = position_recommendation
    invalidated["execution_profile"].update(
        one_minute_segment_difference_present=False,
        segment_difference_status="WAITING_ONE_MINUTE",
        segment_difference_ready=False,
        precise_execution_ready=False,
        recommendation="BLOCKED",
        recommendation_label="当前不满足操作条件，等待结构或数据恢复",
        hard_blocked=True,
        hard_block_reason_codes=["structure_invalidated"],
        advisory_reason_codes=["SAME_PERIOD_CONTEXT_GRADE_UNRESOLVED"],
        position_recommendation=position_recommendation,
    )
    invalidated["decision_document_id"] = signal_decision_document_id(invalidated)
    snapshot["counts_by_stage"] = {"invalidated": 1, "triggered": 1}
    snapshot["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
        snapshot
    )

    _review_at, signals = validate_live_review_snapshot(snapshot)
    report = live_human_review_document(
        live_snapshot=snapshot,
        source_snapshot_sha256=str(snapshot["snapshot_content_sha256"]),
        session=deterministic_bundle().as_of.date(),
    )

    assert len(signals) == 2
    assert report["candidate_funnel"] == {
        "live_screen_candidate_count": 2,
        "review_candidate_count": 1,
    }
    assert len(report["review_queue"]) == 1


def test_sealed_snapshot_validation_is_reused_only_for_the_exact_mapping(
    monkeypatch,
) -> None:
    snapshot = live_snapshot()
    snapshot_sha256 = str(snapshot["snapshot_content_sha256"])
    session = deterministic_bundle().as_of.date()
    validation = subject._validated_live_review_snapshot(
        snapshot,
        session=session,
    )

    def unexpected_revalidation(*_args, **_kwargs):
        raise AssertionError("sealed snapshot was validated twice")

    monkeypatch.setattr(
        subject,
        "validate_live_review_snapshot",
        unexpected_revalidation,
    )
    report = subject.live_human_review_document(
        live_snapshot=snapshot,
        source_snapshot_sha256=snapshot_sha256,
        session=session,
        _validated_snapshot=validation,
    )

    assert report["input_hashes"]["live_screening_snapshot"] == snapshot_sha256
    with pytest.raises(ValueError, match="validation token is invalid"):
        subject.live_human_review_document(
            live_snapshot=copy.deepcopy(snapshot),
            source_snapshot_sha256=snapshot_sha256,
            session=session,
            _validated_snapshot=validation,
        )


def test_etf_proxy_sector_contract_does_not_require_a_stock_sector() -> None:
    code = "SH.513100"
    assert _etf_proxy_sector_is_consistent(
        {
            "sector_id": f"etf-proxy:{code}",
            "sector_name": "ETF代理路径（不要求个股行业）",
            "eligible": True,
            "hard_block": False,
            "regime": "neutral",
            "rank": None,
            "rank_score": 0,
            "rank_components": {},
            "reason_codes": ["ETF_PROXY_SECTOR_NOT_REQUIRED"],
            "horizontal_strength": None,
            "horizontal_rank": None,
            "strength_anchor_session": None,
            "strength_member_count": 0,
            "strength_source_revision": None,
            "strength_reason_codes": [],
            "context_30m": None,
            "context_5m": None,
            "context_1m": None,
        },
        code=code,
    )


def test_live_snapshot_recomputes_warmup_convergence_from_frequency_rows() -> None:
    """A fresh content hash must not turn divergent prefixes into convergence."""

    snapshot = live_snapshot()
    warmup = snapshot["signals"][0]["warmup"]
    warmup.update(
        {
            "converged": True,
            "by_frequency": [
                {
                    "frequency": "d",
                    "converged": True,
                    "full_bar_count": 480,
                    "suffix_bar_count": 320,
                },
                {
                    "frequency": "30m",
                    "converged": False,
                    "full_bar_count": 480,
                    "suffix_bar_count": 320,
                },
                {
                    "frequency": "5m",
                    "converged": True,
                    "full_bar_count": 960,
                    "suffix_bar_count": 640,
                },
            ],
            "reason_codes": [
                "D:WARMUP_TAIL_STABLE",
                "30M:WARMUP_TAIL_DIVERGED",
                "5M:WARMUP_TAIL_STABLE",
            ],
            "required_for_new_entry": True,
        }
    )
    snapshot["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
        snapshot
    )

    with pytest.raises(ValueError, match="warmup evidence is invalid"):
        validate_live_review_snapshot(snapshot)


def test_live_snapshot_accepts_current_qmt_30m_fallback_reason() -> None:
    snapshot = live_snapshot()
    reasons = snapshot["signals"][0]["warmup"]["reason_codes"]
    thirty_index = reasons.index("30M:WARMUP_TAIL_STABLE")
    reasons.insert(
        thirty_index + 1,
        f"30M:{SCREENING_QMT_30M_FALLBACK_REASON_CODE}",
    )
    snapshot["signals"][0]["decision_document_id"] = signal_decision_document_id(
        snapshot["signals"][0]
    )
    snapshot["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
        snapshot
    )

    validate_live_review_snapshot(snapshot)


def test_live_snapshot_rejects_qmt_30m_fallback_reason_on_other_frequency() -> None:
    snapshot = live_snapshot()
    reasons = snapshot["signals"][0]["warmup"]["reason_codes"]
    five_index = reasons.index("5M:WARMUP_TAIL_STABLE")
    reasons.insert(
        five_index + 1,
        f"5M:{SCREENING_QMT_30M_FALLBACK_REASON_CODE}",
    )
    snapshot["signals"][0]["decision_document_id"] = signal_decision_document_id(
        snapshot["signals"][0]
    )
    snapshot["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
        snapshot
    )

    with pytest.raises(ValueError, match="warmup evidence is invalid"):
        validate_live_review_snapshot(snapshot)


def test_live_snapshot_preserves_and_recomputes_mwd_multi_prefix_evidence() -> None:
    snapshot = live_snapshot()
    as_of = deterministic_bundle().as_of
    risk = snapshot["signals"][0]["higher_timeframe_risk"]
    _attach_complete_warmup_chain(risk, observed_at=as_of)
    snapshot["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
        snapshot
    )

    validate_live_review_snapshot(snapshot)

    forged = risk["symbol_warmup_convergence_evidence"]
    forged["status"] = "STABLE_ALL_PREFIXES"
    forged["stable_all_prefixes"] = True
    forged["match_longest_pattern"] = [True] * len(forged["observations"])
    forged["reason_codes"] = ["WARMUP_ENVELOPE_STABLE_ALL_PREFIXES"]
    stable = dict(forged)
    stable.pop("content_sha256")
    forged["content_sha256"] = sha256_json(stable)
    snapshot["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
        snapshot
    )

    with pytest.raises(ValueError, match="risk evidence is invalid"):
        validate_live_review_snapshot(snapshot)


def test_live_snapshot_authenticates_warmup_semantic_diagnostic() -> None:
    snapshot = live_snapshot()
    as_of = deterministic_bundle().as_of
    risk = snapshot["signals"][0]["higher_timeframe_risk"]
    _attach_complete_warmup_chain(risk, observed_at=as_of)
    snapshot["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
        snapshot
    )
    validate_live_review_snapshot(snapshot)

    valid_snapshot = copy.deepcopy(snapshot)
    forged_supply = risk["symbol_warmup_mapping_supply_diagnostic_evidence"]
    forged_supply["comparisons"][0]["delta"]["transition_codes"] = [
        "MAPPING_SUPPLY_UNCHANGED"
    ]
    stable_supply = dict(forged_supply)
    stable_supply.pop("content_sha256")
    forged_supply["content_sha256"] = sha256_json(stable_supply)
    snapshot["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
        snapshot
    )
    with pytest.raises(ValueError, match="risk evidence is invalid"):
        validate_live_review_snapshot(snapshot)
    snapshot = valid_snapshot
    risk = snapshot["signals"][0]["higher_timeframe_risk"]

    forged = risk["symbol_warmup_convergence_diagnostic_evidence"]
    forged["observations"][1]["changed_paths_from_longest"] = ["M.state"]
    stable = dict(forged)
    stable.pop("content_sha256")
    forged["content_sha256"] = sha256_json(stable)
    snapshot["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
        snapshot
    )

    with pytest.raises(ValueError, match="risk evidence is invalid"):
        validate_live_review_snapshot(snapshot)


def test_warmup_diagnostic_chain_cache_uses_recomputed_content() -> None:
    """Equal evidence is reused, but a forged document cannot reuse its verdict."""

    observed_at = datetime.fromisoformat("2026-06-01T11:30:00+08:00")
    envelope = lineage_envelope(
        as_of=observed_at,
        parameter_set_id=(QMT_HIGHER_TIMEFRAME_WARMUP_CONVERGENCE_PARAMETER_SET_ID),
    )
    assert envelope.diagnostic is not None
    assert envelope.mapping_supply_diagnostic is not None
    assert envelope.structure_lineage_diagnostic is not None
    documents = {
        "envelope_raw": envelope.document(),
        "semantic_raw": envelope.diagnostic.document(),
        "supply_raw": envelope.mapping_supply_diagnostic.document(),
        "lineage_raw": envelope.structure_lineage_diagnostic.document(),
    }
    memo: dict[tuple[str, str], bool] = {}

    assert _mwd_warmup_diagnostic_chain_is_consistent(
        **documents,
        evidence_cutoff=observed_at,
        memo=memo,
    )
    assert _mwd_warmup_diagnostic_chain_is_consistent(
        **copy.deepcopy(documents),
        evidence_cutoff=observed_at,
        memo=memo,
    )
    assert len(memo) == 1

    forged = copy.deepcopy(documents)
    forged["lineage_raw"]["active_gate_unchanged"] = False
    assert not _mwd_warmup_diagnostic_chain_is_consistent(
        **forged,
        evidence_cutoff=observed_at,
        memo=memo,
    )
    assert len(memo) == 2


def test_live_snapshot_recomputes_risk_gate_from_mwd_states() -> None:
    """A fresh content hash must not relabel formed M/W/D risk as GREEN."""

    snapshot = live_snapshot()
    risk = snapshot["signals"][0]["higher_timeframe_risk"]
    risk.update(
        {
            "market_gate": "GREEN",
            "market_states": {
                "M": "FORMED_UNRESOLVED",
                "W": "FORMED_UNRESOLVED",
                "D": "FORMED_UNRESOLVED",
            },
            "market_period_diagnostics": _formed_unresolved_diagnostics(
                snapshot["market_data_as_of"]
            ),
            "new_entry_requires_all_green": False,
        }
    )
    market_reasons = [
        "NO_COMPLETED_LOWER_1SELL_2SELL_CENTER_IN_TOP_FRACTAL",
        "M_CENTER_MAPPING_UNRESOLVED",
        "W_CENTER_MAPPING_UNRESOLVED",
        "D_CENTER_MAPPING_UNRESOLVED",
    ]
    risk["market_reason_codes"] = market_reasons
    risk["reason_codes"] = list(
        dict.fromkeys(
            (
                *market_reasons,
                *risk["sector_reason_codes"],
                *risk["symbol_reason_codes"],
            )
        )
    )
    snapshot["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
        snapshot
    )

    with pytest.raises(ValueError, match="risk evidence is invalid"):
        validate_live_review_snapshot(snapshot)


def test_live_snapshot_rejects_future_mwd_diagnostic() -> None:
    snapshot = live_snapshot()
    risk = snapshot["signals"][0]["higher_timeframe_risk"]
    future = (deterministic_bundle().as_of + timedelta(minutes=1)).isoformat()
    market_reasons = [
        "NO_COMPLETED_LOWER_1SELL_2SELL_CENTER_IN_TOP_FRACTAL",
        "M_CENTER_MAPPING_UNRESOLVED",
        "W_CENTER_MAPPING_UNRESOLVED",
        "D_CENTER_MAPPING_UNRESOLVED",
    ]
    risk.update(
        {
            "market_gate": "AMBER",
            "market_states": {
                "M": "FORMED_UNRESOLVED",
                "W": "FORMED_UNRESOLVED",
                "D": "FORMED_UNRESOLVED",
            },
            "market_reason_codes": market_reasons,
            "reason_codes": list(
                dict.fromkeys(
                    (
                        *market_reasons,
                        *risk["sector_reason_codes"],
                        *risk["symbol_reason_codes"],
                    )
                )
            ),
            "market_period_diagnostics": _formed_unresolved_diagnostics(future),
        }
    )
    snapshot["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
        snapshot
    )

    with pytest.raises(ValueError, match="risk evidence is invalid"):
        validate_live_review_snapshot(snapshot)


def test_session_gap_diagnostic_is_recomputed_and_cannot_claim_suspension() -> None:
    snapshot = live_snapshot()
    risk = copy.deepcopy(snapshot["signals"][0]["higher_timeframe_risk"])
    cutoff = datetime.fromisoformat(str(snapshot["market_data_as_of"]))
    missing_code = "QMT_ONE_MINUTE_EXPECTED_SESSION_MISSING"
    # The current row is valid only while it does not claim a session problem
    # that would require exact affected dates.
    assert _risk_evidence_is_consistent(risk, evidence_cutoff=cutoff) is True
    risk["symbol_reason_codes"] = list(
        dict.fromkeys((*risk["symbol_reason_codes"], missing_code))
    )
    risk["reason_codes"] = list(
        dict.fromkeys(
            (
                *risk["market_reason_codes"],
                *risk["sector_reason_codes"],
                *risk["symbol_reason_codes"],
            )
        )
    )
    assert _risk_evidence_is_consistent(risk, evidence_cutoff=cutoff) is False
    exact_empty = {
        "contract_id": HIGHER_TIMEFRAME_SESSION_EVIDENCE_CONTRACT_ID,
        "status": "EXACT",
        "issue_count": 0,
        "issues": [],
        "entry_disposition": "NO_SESSION_BLOCKER",
    }
    risk.update(
        {
            "session_evidence_contract_id": (
                HIGHER_TIMEFRAME_SESSION_EVIDENCE_CONTRACT_ID
            ),
            "market_session_evidence": exact_empty,
            "sector_session_evidence": exact_empty,
            "symbol_session_evidence": {
                "contract_id": HIGHER_TIMEFRAME_SESSION_EVIDENCE_CONTRACT_ID,
                "status": "EXACT",
                "issue_count": 1,
                "issues": [
                    {
                        "session": cutoff.date().isoformat(),
                        "code": missing_code,
                        "observed_rows": 0,
                        "classification": ("UNCLASSIFIED_EXPECTED_SESSION_ABSENCE"),
                        "detail": (
                            "trading-calendar session is absent from the QMT 1m prefix"
                        ),
                        "historical_trade_status_proven": False,
                        "entry_disposition": "FAIL_CLOSED",
                    }
                ],
                "entry_disposition": "FAIL_CLOSED",
            },
            # 1m 会话缺口仍需精确保留和验证，但只关闭高周期环境证据；
            # 不能关闭独立物理 5m 已确认的买卖信号。
            "data_integrity_hard_block_reason_codes": [],
        }
    )

    assert _risk_evidence_is_consistent(risk, evidence_cutoff=cutoff) is True

    forged = copy.deepcopy(risk)
    forged["symbol_session_evidence"]["issues"][0]["historical_trade_status_proven"] = (
        True
    )
    assert _risk_evidence_is_consistent(forged, evidence_cutoff=cutoff) is False

    erased = copy.deepcopy(risk)
    erased["symbol_session_evidence"] = exact_empty
    assert _risk_evidence_is_consistent(erased, evidence_cutoff=cutoff) is False

    signal = copy.deepcopy(snapshot["signals"][0])
    signal["higher_timeframe_risk"] = risk
    alert = live_signal_human_review_alert(
        signal,
        review_available_at=cutoff,
        source_snapshot_sha256="sha256:" + "d" * 64,
    )
    evidence = alert.market_symbol_higher_timeframe_evidence
    assert evidence is not None
    assert evidence.market.source_support is not None
    assert evidence.symbol_evidence.source_support is not None
    assert evidence.market.source_support.session_evidence.document() == (exact_empty)
    assert (
        evidence.symbol_evidence.source_support.session_evidence.document()
        == risk["symbol_session_evidence"]
    )
    assert (
        evidence.market.source_support.support_id
        == (evidence.document()["market"]["source_support"]["support_id"])
    )


def test_live_snapshot_binds_final_entry_to_risk_and_warmup_gates() -> None:
    snapshot = live_snapshot()
    buy = next(value for value in snapshot["signals"] if value["side"] == "buy")
    assert buy["technical_entry_allowed"] is True
    assert buy["higher_timeframe_risk"]["market_gate"] == "UNRESOLVED"
    # 5 分钟正式点已经允许；无硬阻断时把它篡改为不允许同样会造成展示分叉。
    buy["entry_allowed"] = False
    snapshot["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
        snapshot
    )

    with pytest.raises(ValueError, match="entry gate is invalid"):
        validate_live_review_snapshot(snapshot)


@pytest.mark.parametrize(
    "mutation",
    ("context_30m", "conflict", "risk_multiplier", "decision_reasons"),
)
def test_live_snapshot_recomputes_displayed_buy_decision_evidence(
    mutation: str,
) -> None:
    """Prominent human-review evidence cannot remain a signed self-attestation."""

    snapshot = live_snapshot()
    buy = next(value for value in snapshot["signals"] if value["side"] == "buy")
    if mutation == "context_30m":
        buy["context_30m"]["disposition"] = "supportive"
    elif mutation == "conflict":
        buy["conflict"]["hard_block"] = True
    elif mutation == "risk_multiplier":
        buy["risk_multiplier"] = "999"
    else:
        buy["decision_reasons"] = []
    snapshot["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
        snapshot
    )

    expected = "decision evidence is invalid"
    with pytest.raises(ValueError, match=expected):
        validate_live_review_snapshot(snapshot)


def test_displayed_decision_recomputes_provisional_setup_reason() -> None:
    """Approaching candidates retain the core's non-actionable conflict reason."""

    core = HumanAssistedDecisionCore()
    bundle = replace(
        deterministic_bundle(),
        five_points=(provisional_point("2buy"),),
        one_points=(),
        opposite_points=(),
        entry_execution_boundaries=(),
    )
    signal = core.decision_documents(bundle)[0]
    policy = core.contract.document()["policy"]
    risk = signal["higher_timeframe_risk"]
    warmup = signal["warmup"]
    assert isinstance(policy, dict)
    assert isinstance(risk, dict)
    assert isinstance(warmup, dict)
    assert _displayed_decision_evidence_is_consistent(
        signal,
        policy=policy,
        risk=risk,
        warmup=warmup,
    )

    signal["decision_reasons"] = [
        value for value in signal["decision_reasons"] if value != "setup_not_confirmed"
    ]
    assert not _displayed_decision_evidence_is_consistent(
        signal,
        policy=policy,
        risk=risk,
        warmup=warmup,
    )


def test_displayed_decision_accepts_context_warmup_as_advisory_only() -> None:
    core = HumanAssistedDecisionCore()
    bundle = replace(
        deterministic_bundle(),
        warmup_converged=False,
        warmup_reason_codes=(
            "D:WARMUP_TAIL_STABLE",
            "30M:WARMUP_TAIL_DIVERGED",
            "5M:WARMUP_TAIL_STABLE",
            "1M:WARMUP_TAIL_STABLE",
        ),
        warmup_by_frequency=(
            ("d", True, 480, 320),
            ("30m", False, 480, 320),
            ("5m", True, 960, 640),
            ("1m", True, 1440, 960),
        ),
        warmup_difference_codes_by_frequency=(
            ("d", ()),
            ("30m", ("WARMUP_DIRECTION_CHANGED",)),
            ("5m", ()),
            ("1m", ()),
        ),
        enforce_warmup_entry_gate=True,
    )
    [signal] = core.decision_documents(bundle)
    policy = core.contract.document()["policy"]

    assert signal["entry_allowed"] is True
    assert signal["execution_profile"]["hard_blocked"] is False
    assert (
        "30M:WARMUP_TAIL_DIVERGED"
        in signal["execution_profile"]["advisory_reason_codes"]
    )
    assert _displayed_decision_evidence_is_consistent(
        signal,
        policy=policy,
        risk=signal["higher_timeframe_risk"],
        warmup=signal["warmup"],
    )


def test_displayed_decision_accepts_risk_only_conflict_as_advisory() -> None:
    core = HumanAssistedDecisionCore()
    bundle = replace(
        deterministic_bundle(),
        opposite_points=(
            confirmed_point(
                "1sell",
                center_id="unrelated-center",
                minutes_after=296,
            ),
        ),
    )
    [signal] = core.decision_documents(bundle)
    policy = core.contract.document()["policy"]

    assert signal["conflict"]["hard_block"] is False
    assert signal["entry_allowed"] is True
    assert signal["execution_profile"]["hard_blocked"] is False
    assert (
        "lower_or_unrelated_structure_risk"
        in signal["execution_profile"]["advisory_reason_codes"]
    )
    assert _displayed_decision_evidence_is_consistent(
        signal,
        policy=policy,
        risk=signal["higher_timeframe_risk"],
        warmup=signal["warmup"],
    )


@pytest.mark.parametrize("field", ("counts_by_stage", "counts_by_point_type"))
def test_live_snapshot_recomputes_signal_aggregate_counts(field: str) -> None:
    snapshot = live_snapshot()
    snapshot[field] = {"forged": 999}
    snapshot["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
        snapshot
    )

    with pytest.raises(ValueError, match="signal aggregates are invalid"):
        validate_live_review_snapshot(snapshot)


@pytest.mark.parametrize(
    "mutation",
    ("hostile_rank", "hostile_order", "hostile_qmt_trigger"),
)
def test_live_snapshot_rejects_hostile_sector_in_ranked_subset(
    mutation: str,
) -> None:
    snapshot = _with_hostile_sector(live_snapshot())
    if mutation == "hostile_rank":
        snapshot["sectors"][1]["rank"] = 2
    elif mutation == "hostile_order":
        snapshot["sectors"].reverse()
    else:
        snapshot["signals"][0]["sector"] = json.loads(
            json.dumps(snapshot["sectors"][1])
        )
    snapshot["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
        snapshot
    )

    with pytest.raises(ValueError):
        validate_live_review_snapshot(snapshot)


def test_live_snapshot_rejects_third_class_one_minute_segment_difference() -> None:
    snapshot = live_snapshot()
    signal = next(
        value
        for value in snapshot["signals"]
        if value["segment_difference_1m"] is not None
    )
    signal["segment_difference_1m"]["point_type"] = (
        "3buy" if signal["side"] == "buy" else "3sell"
    )
    signal["decision_document_id"] = signal_decision_document_id(signal)
    snapshot["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
        snapshot
    )

    with pytest.raises(ValueError, match="timeframe provenance"):
        validate_live_review_snapshot(snapshot)


def test_live_snapshot_recomputes_sector_failure_coverage() -> None:
    """Failed assessments stay visible and are excluded from ranking by evidence."""

    snapshot = _with_hostile_sector(live_snapshot())
    template = snapshot["sectors"][1]
    for suffix in ("a", "b", "c"):
        value = json.loads(json.dumps(template))
        value["sector_id"] = f"qmt-gics3:hostile-{suffix}"
        value["sector_name"] = f"失败覆盖行业-{suffix}"
        snapshot["sectors"].append(value)
    failed_id = "qmt-gics3:hostile-c"
    failed_sector = next(
        sector for sector in snapshot["sectors"] if sector["sector_id"] == failed_id
    )
    failed_sector.update(
        {
            "context_30m": None,
            "context_5m": None,
            "context_1m": None,
            "rank_components": {},
            "rank_score": 0,
            "reason_codes": ["sector_price_basis_unavailable"],
            "strength_anchor_session": (
                deterministic_bundle().as_of.date().isoformat()
            ),
            "strength_member_count": 0,
            "strength_source_revision": "sha256:" + "8" * 64,
            "strength_reason_codes": ["EMPTY_POINT_IN_TIME_BASKET"],
        }
    )
    snapshot["scan_audit"].update(
        {
            "sector_discovered_count": 5,
            "sector_completed_count": 4,
            "sector_excluded_count": 0,
            "sector_failed_count": 1,
            "sector_resolved_count": 4,
            "sector_completion_ratio": "0.8",
            "sector_resolution_ratio": "0.8",
            "sector_failure_counts": {
                "sector_price_basis_unavailable": 1,
            },
            "sector_exclusion_counts": {},
            "selected_sector_count": 1,
        }
    )
    snapshot["errors"] = [
        {
            "sector_id": failed_id,
            "code": "SH.880999",
            "error_type": "sector_price_basis_unavailable",
            "reason": "industry index price basis unavailable",
        }
    ]
    _attach_strength_evidence(snapshot)
    snapshot["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
        snapshot
    )

    validate_live_review_snapshot(snapshot)

    snapshot["scan_audit"]["sector_completed_count"] = 5
    snapshot["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
        snapshot
    )
    with pytest.raises(ValueError, match="sector coverage"):
        validate_live_review_snapshot(snapshot)


def test_live_snapshot_authenticates_sector_eligibility_exclusions() -> None:
    """Small catalog sectors resolve coverage without masquerading as success."""

    snapshot = _with_hostile_sector(live_snapshot())
    template = snapshot["sectors"][1]
    for suffix in ("a", "b", "c"):
        value = json.loads(json.dumps(template))
        value["sector_id"] = f"qmt-gics3:hostile-{suffix}"
        value["sector_name"] = f"资格排除覆盖行业-{suffix}"
        snapshot["sectors"].append(value)
    excluded_ids = ("qmt-gics3:hostile-b", "qmt-gics3:hostile-c")
    for excluded_id in excluded_ids:
        excluded_sector = next(
            sector
            for sector in snapshot["sectors"]
            if sector["sector_id"] == excluded_id
        )
        excluded_sector.update(
            {
                "context_30m": None,
                "context_5m": None,
                "context_1m": None,
                "rank_components": {},
                "rank_score": 0,
                "reason_codes": [
                    "sector_member_coverage_insufficient",
                    "sector_constituent_count_below_minimum",
                ],
            }
        )
    snapshot["scan_audit"].update(
        {
            "sector_discovered_count": 5,
            "sector_completed_count": 3,
            "sector_excluded_count": 2,
            "sector_failed_count": 0,
            "sector_resolved_count": 5,
            "sector_completion_ratio": "0.6",
            "sector_resolution_ratio": "1",
            "sector_failure_counts": {},
            "sector_exclusion_counts": {
                "sector_member_coverage_insufficient": 2,
            },
            "selected_sector_count": 1,
        }
    )
    snapshot["errors"] = []
    snapshot["sector_exclusions"] = [
        {
            "sector_id": excluded_id,
            "code": f"GICS3资格排除覆盖行业-{excluded_id.rsplit('-', 1)[-1]}",
            "exclusion_type": "sector_analysis_exclusion",
            "eligibility": "MINIMUM_SECTOR_MEMBERS_NOT_MET",
            "reason_code": "sector_member_coverage_insufficient",
            "reason": "catalog_members=7; universe_members=7; required=8",
            "detail_code": "sector_constituent_count_below_minimum",
            "catalog_member_count": 7,
            "universe_member_count": 7,
            "required_member_count": 8,
            "deterministic_for_catalog_revision": True,
            "retry_policy": "NEXT_SECTOR_CATALOG_REVISION",
        }
        for excluded_id in excluded_ids
    ]
    _attach_strength_evidence(snapshot)
    snapshot["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
        snapshot
    )

    validate_live_review_snapshot(snapshot)

    forged = json.loads(json.dumps(snapshot))
    forged["sector_exclusions"][0]["required_member_count"] = 9
    forged["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(forged)
    with pytest.raises(ValueError, match="sector exclusion"):
        validate_live_review_snapshot(forged)


def test_live_adapter_preserves_30m_5m_1m_roles() -> None:
    snapshot = live_snapshot()
    source_sha256 = sha256_json(snapshot)
    source_implementation = current_decision_source_snapshot()
    report = live_human_review_document(
        live_snapshot=snapshot,
        source_snapshot_sha256=source_sha256,
        session=deterministic_bundle().as_of.date(),
        decision_source_snapshot=source_implementation,
    )

    assert report["scope"]["strategic_frequency"] == "30m"
    assert report["scope"]["tactical_frequency"] == "5m"
    assert report["scope"]["context_frequency"] == "30m"
    assert report["scope"]["trade_frequency"] == "5m"
    assert report["scope"]["segment_difference_frequency"] == "1m"
    assert report["scope"]["segment_difference_required_for_trade_signal"] is False
    for signal in snapshot["signals"]:
        profile = signal["execution_profile"]
        assert profile["segment_difference_status"] in {
            "STRUCTURE_PENDING",
            "WAITING_ONE_MINUTE",
            "BOUNDARY_EXPIRED",
            "BOUNDARY_MISSING",
            "READY",
        }
        assert type(profile["segment_difference_ready"]) is bool
    assert report["signal_counts"]["by_alert_type"] == {
        "POSSIBLE_30M_BUY": 0,
        "POSSIBLE_30M_EXIT": 0,
        "POSSIBLE_SELL_REVIEW": 0,
        "POSSIBLE_5M_TACTICAL_SELL": 0,
        "POSSIBLE_5M_TACTICAL_BUYBACK": 0,
        "POSSIBLE_5M_TRADE_BUY": 1,
        "POSSIBLE_5M_TRADE_SELL": 1,
    }
    by_type = {row["alert_type"]: row for row in report["review_queue"]}
    buy = by_type["POSSIBLE_5M_TRADE_BUY"]
    assert "CONTEXT_30M" in buy["warning_codes"]
    assert "TRADE_SIGNAL_5M" in buy["warning_codes"]
    assert "SEGMENT_DIFFERENCE_1M_PRESENT" in buy["warning_codes"]
    source_buy = next(value for value in snapshot["signals"] if value["side"] == "buy")
    assert Decimal(str(buy["reference_price"])) == Decimal(
        str(source_buy["setup_5m"]["anchor_price"])
    )
    assert Decimal(str(buy["entry_price_cap"])) == Decimal("10.25")
    assert buy["entry_price_cap"] != buy["reference_price"]
    assert buy["entry_execution_boundary"]["raw_high"] == "10.25"
    assert (
        buy["entry_execution_boundary"]["evidence_id"]
        == buy["entry_boundary_evidence_id"]
    )
    assert "BUY_ENTRY_BOUNDARY_ALREADY_EXPIRED" not in buy["warning_codes"]
    ranking = buy["sector_ranking_evidence"]
    assert ranking["rank_components"] == source_buy["sector"]["rank_components"]
    assert ranking["rank_score"] == source_buy["sector"]["rank_score"]
    assert ranking["evidence_id"] in buy["source_fact_ids"]
    assert (
        ranking["strength_evidence_revision"]
        == snapshot["sector_strength_evidence_revision"]
    )
    assert (
        ranking["sector_catalog_revision"]
        == snapshot["coverage_manifest"]["sector_catalog_revision"]
    )
    sell = by_type["POSSIBLE_5M_TRADE_SELL"]
    assert "TRADE_SIGNAL_5M" in sell["warning_codes"]
    # 此夹具中的卖点发生在上午、快照在收盘时复核；结构仍保留，但不能
    # 继续占用 80+ 的“新卖点”即时优先级。
    assert 40 <= sell["review_priority"] <= 69
    assert sell["position_recommendation"]["status"] == "CONDITIONAL"
    assert sell["position_recommendation"]["conditional_options"] == [
        {
            "condition": "FIVE_MINUTE_SAME_OR_HIGHER_LEVEL_EXIT",
            "recommended_ratio": "1",
            "recommended_percent": "100",
        },
        {
            "condition": "FIVE_MINUTE_LOWER_OR_DIFFERENT_STRUCTURE_REDUCTION",
            "recommended_ratio": "0.25",
            "recommended_percent": "25",
        },
    ]
    assert (
        "REFERENCE_PRICE_IS_STRUCTURE_ANCHOR_NOT_EXECUTION_QUOTE"
        in report["data_caveats"]
    )
    assert report["orders_created"] == report["fills_created"] == 0
    assert report["live_status"] == "LIVE_DISABLED"
    assert decision_source_snapshot_id(report["decision_source_snapshot"]) == (
        decision_source_snapshot_id(source_implementation)
    )
    assert report["input_hashes"]["decision_source_snapshot_id"] == (
        decision_source_snapshot_id(source_implementation)
    )


def test_live_adapter_reserves_immediate_priority_for_fresh_five_minute_sell() -> None:
    snapshot = live_snapshot(fresh_sell=True)
    report = live_human_review_document(
        live_snapshot=snapshot,
        source_snapshot_sha256=sha256_json(snapshot),
        session=deterministic_bundle().as_of.date(),
    )

    sell = next(
        row
        for row in report["review_queue"]
        if row["alert_type"] == "POSSIBLE_5M_TRADE_SELL"
    )
    assert 80 <= sell["review_priority"] <= 89


def test_live_alert_retains_structured_sector_source_evidence() -> None:
    snapshot = live_snapshot()
    review_at, signals = validate_live_review_snapshot(snapshot)
    signal = copy.deepcopy(signals[0])
    stable_warmup = QmtHigherTimeframeWarmupEvidence(
        required_daily_bar_count=480,
        full_daily_bar_count=480,
        suffix_daily_bar_count=320,
        converged=True,
        reason_code="QMT_HIGHER_TIMEFRAME_WARMUP_TAIL_STABLE",
        full_signature="sha256:" + "4" * 64,
        suffix_signature="sha256:" + "4" * 64,
    )
    signal_at = datetime.fromisoformat(signal["observed_at"])
    risk = signal["higher_timeframe_risk"]
    _attach_complete_warmup_chain(
        risk,
        observed_at=signal_at,
        include_strict_sector=True,
    )
    risk.update(
        {
            "sector_higher_timeframe_source_mode": (QMT_SECTOR_SAME_BASE_SOURCE_MODE),
            "sector_strict_same_5m_warmup_evidence": (stable_warmup.document()),
            "sector_strict_same_5m_source_coverage_evidence": (
                _sector_coverage(
                    stable_warmup,
                    observed_at=signal_at,
                ).document()
            ),
            "sector_research_bridge_parameter_set_id": None,
        }
    )
    strict = live_signal_human_review_alert(
        signal,
        review_available_at=review_at,
        source_snapshot_sha256="sha256:" + "5" * 64,
    )
    assert strict.sector_higher_timeframe_evidence is not None
    assert strict.sector_higher_timeframe_evidence.source_mode == (
        QMT_SECTOR_SAME_BASE_SOURCE_MODE
    )
    assert (
        strict.sector_higher_timeframe_evidence.sector_id
        == (signal["sector"]["sector_id"])
    )
    assert strict.sector_higher_timeframe_evidence.observed_at == (
        datetime.fromisoformat(signal["observed_at"])
    )
    assert (
        strict.sector_higher_timeframe_evidence.gate
        == (signal["higher_timeframe_risk"]["sector_gate"])
    )
    assert strict.sector_higher_timeframe_evidence.evidence_id in strict.source_fact_ids

    research_signal = copy.deepcopy(signal)
    insufficient = QmtHigherTimeframeWarmupEvidence(
        required_daily_bar_count=480,
        full_daily_bar_count=240,
        suffix_daily_bar_count=0,
        converged=False,
        reason_code="QMT_HIGHER_TIMEFRAME_WARMUP_HISTORY_INSUFFICIENT",
        full_signature="sha256:" + "6" * 64,
        suffix_signature=None,
    )
    blocker = "QMT_SECTOR_NATIVE_DAILY_AND_5M_UNRECONCILED_RESEARCH_BRIDGE"
    risk = research_signal["higher_timeframe_risk"]
    signal_at = datetime.fromisoformat(research_signal["observed_at"])
    diagnostics = tuple(
        HigherTimeframePeriodDiagnostic(
            period=period,
            state="NONE",
            completed_bar_count=24,
            evidence_bar_end=signal_at - timedelta(days=1),
            active_top_interval=None,
            mapping_unique=True,
            mapped_center_id=None,
            mapping_candidate_ids=(),
            blocker_codes=(),
            warning_codes=(),
            source_revision="sha256:" + digit * 64,
            mapping_supply=None,
        )
        for period, digit in zip(("M", "W", "D"), ("4", "5", "6"))
    )
    risk["sector_gate"] = "AMBER"
    risk["sector_states"] = {"M": "NONE", "W": "NONE", "D": "NONE"}
    risk["sector_reason_codes"] = [blocker]
    risk["sector_period_diagnostics"] = [value.document() for value in diagnostics]
    risk["reason_codes"] = list(
        dict.fromkeys(
            (
                *risk["market_reason_codes"],
                *risk["sector_reason_codes"],
                *risk["symbol_reason_codes"],
            )
        )
    )
    risk.update(
        {
            "sector_higher_timeframe_source_mode": (
                QMT_SECTOR_NATIVE_DAILY_RESEARCH_SOURCE_MODE
            ),
            "sector_strict_same_5m_warmup_evidence": insufficient.document(),
            "sector_strict_same_5m_source_coverage_evidence": (
                _sector_coverage(
                    insufficient,
                    observed_at=signal_at,
                ).document()
            ),
            "sector_research_bridge_parameter_set_id": (
                sector_native_daily_research_bridge_contract()["parameter_set_id"]
            ),
        }
    )
    research = live_signal_human_review_alert(
        research_signal,
        review_available_at=review_at,
        source_snapshot_sha256="sha256:" + "7" * 64,
    )
    assert research.sector_higher_timeframe_evidence is not None
    assert research.sector_higher_timeframe_evidence.source_mode == (
        QMT_SECTOR_NATIVE_DAILY_RESEARCH_SOURCE_MODE
    )
    assert blocker in research.warning_codes
    assert research.sector_risk_gate == "AMBER"
    assert dict(research.sector_higher_timeframe_evidence.states) == {
        "M": "NONE",
        "W": "NONE",
        "D": "NONE",
    }
    assert research.sector_higher_timeframe_evidence.period_diagnostics == (diagnostics)


def test_live_snapshot_requires_sector_catalog_identity() -> None:
    snapshot = live_snapshot()
    snapshot["coverage_manifest"].pop("sector_catalog_revision")
    snapshot["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
        snapshot
    )

    with pytest.raises(ValueError, match="review boundary"):
        validate_live_review_snapshot(snapshot)


@pytest.mark.parametrize(
    "mutation",
    (
        "rank",
        "order",
        "nested_sector",
        "empty_selection_sources",
        "missing_strength_source",
    ),
)
def test_live_snapshot_recomputes_sector_ranking_and_signal_linkage(
    mutation: str,
) -> None:
    snapshot = live_snapshot()
    if mutation == "rank":
        snapshot["sectors"][0]["rank"] = 2
    elif mutation == "order":
        second = json.loads(json.dumps(snapshot["sectors"][0]))
        second["sector_id"] = "qmt-gics3:aaa"
        second["sector_name"] = "排序反证行业"
        second["rank"] = 2
        snapshot["sectors"].append(second)
    elif mutation == "nested_sector":
        snapshot["signals"][0]["sector"]["sector_name"] = "伪造行业"
    elif mutation == "empty_selection_sources":
        snapshot["signals"][0]["selection_sources"] = []
        snapshot["signals"][0]["sector_triggered"] = False
        snapshot["signals"][0]["monitor_only"] = True
    else:
        sector_id = snapshot["sectors"][0]["sector_id"]
        snapshot["sectors"][0].update(
            {
                "horizontal_strength": "1",
                "horizontal_rank": 1,
                "strength_anchor_session": deterministic_bundle()
                .as_of.date()
                .isoformat(),
                "strength_member_count": 1,
                "strength_source_revision": None,
            }
        )
        for signal in snapshot["signals"]:
            if signal["sector"]["sector_id"] == sector_id:
                signal["sector"].update(
                    {
                        "horizontal_strength": "1",
                        "horizontal_rank": 1,
                        "strength_anchor_session": (
                            deterministic_bundle().as_of.date().isoformat()
                        ),
                        "strength_member_count": 1,
                        "strength_source_revision": None,
                    }
                )
    snapshot["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
        snapshot
    )

    with pytest.raises(ValueError):
        validate_live_review_snapshot(snapshot)


def test_live_alert_lifecycle_is_stable_across_snapshot_revisions() -> None:
    snapshot = live_snapshot()
    review_at, signals = validate_live_review_snapshot(snapshot)
    first = live_signal_human_review_alert(
        signals[0],
        review_available_at=review_at,
        source_snapshot_sha256="sha256:" + "1" * 64,
    )
    second = live_signal_human_review_alert(
        signals[0],
        review_available_at=review_at,
        source_snapshot_sha256="sha256:" + "2" * 64,
    )

    assert first.candidate_id != second.candidate_id
    assert first.signal_lifecycle_id == second.signal_lifecycle_id
    evidence = first.market_symbol_higher_timeframe_evidence
    assert evidence is not None
    assert evidence.symbol == first.symbol
    assert evidence.observed_at == first.signal_at
    assert evidence.evidence_id in first.source_fact_ids
    assert (
        dict(evidence.market.states)
        == signals[0]["higher_timeframe_risk"]["market_states"]
    )
    assert (
        dict(evidence.symbol_evidence.states)
        == signals[0]["higher_timeframe_risk"]["symbol_states"]
    )
    assert evidence.market.reason_codes == tuple(
        signals[0]["higher_timeframe_risk"]["market_reason_codes"]
    )
    assert evidence.symbol_evidence.reason_codes == tuple(
        signals[0]["higher_timeframe_risk"]["symbol_reason_codes"]
    )
    assert (
        first.market_symbol_higher_timeframe_evidence.evidence_id
        == second.market_symbol_higher_timeframe_evidence.evidence_id
    )


def test_live_alert_explains_watchlist_monitor_origin() -> None:
    snapshot = live_snapshot()
    signal = snapshot["signals"][0]
    signal["selection_sources"] = ["ACTIVE_WATCHLIST_MONITOR"]
    signal["sector_triggered"] = False
    signal["monitor_only"] = True
    snapshot["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
        snapshot
    )
    with pytest.raises(ValueError, match="entry gate is invalid"):
        validate_live_review_snapshot(snapshot)

    signal["decision_reasons"] = [
        reason
        for reason in signal["decision_reasons"]
        if reason
        not in {
            "SIGNED_SELECTION_RESEARCH_REQUIRED",
            "QMT_SECTOR_TRIGGER_REQUIRED",
        }
    ]
    apply_formal_selection_scope(signal, ("ACTIVE_WATCHLIST_MONITOR",))
    signal["decision_document_id"] = signal_decision_document_id(signal)
    snapshot["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
        snapshot
    )

    review_at, signals = validate_live_review_snapshot(snapshot)
    alert = live_signal_human_review_alert(
        signals[0],
        review_available_at=review_at,
        source_snapshot_sha256="sha256:" + "3" * 64,
    )

    assert "SELECTION_SOURCE_ACTIVE_WATCHLIST_MONITOR" in alert.warning_codes
    assert "MONITOR_ONLY_FORMAL_SELECTION_NOT_PASSED" in alert.warning_codes


def test_live_alert_falls_back_without_one_minute_segment_difference() -> None:
    snapshot = live_snapshot()
    signal = json.loads(json.dumps(snapshot["signals"][0]))
    signal["segment_difference_1m"] = None
    signal["entry_execution_boundary"] = None
    review_at, _signals = validate_live_review_snapshot(snapshot)

    alert = live_signal_human_review_alert(
        signal,
        review_available_at=review_at,
        source_snapshot_sha256="sha256:" + "4" * 64,
    )

    assert alert.reference_price == Decimal(str(signal["setup_5m"]["anchor_price"]))
    assert alert.entry_price_cap is None
    assert "BUY_EXECUTION_BOUNDARY_MISSING_REVIEW_ONLY" in alert.warning_codes


def test_live_adapter_rejects_incomplete_coverage_or_wrong_segment_difference() -> None:
    snapshot = live_snapshot()
    snapshot["scan_audit"]["coverage_cycle_complete"] = False
    with pytest.raises(ValueError, match="boundary is incomplete"):
        validate_live_review_snapshot(snapshot)

    snapshot = live_snapshot()
    snapshot["signals"][0]["segment_difference_1m"]["source_frequency"] = "5m"
    snapshot["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
        snapshot
    )
    with pytest.raises(ValueError, match="timeframe provenance"):
        validate_live_review_snapshot(snapshot)

    tampered = live_snapshot()
    tampered["signals"][0]["entry_execution_boundary"]["raw_high"] = "999"
    tampered["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
        tampered
    )
    with pytest.raises(ValueError, match="boundary identity changed"):
        validate_live_review_snapshot(tampered)

    malformed_basis = live_snapshot()
    malformed_basis["signals"][0]["entry_execution_boundary"][
        "raw_price_basis_revision"
    ] = 123
    malformed_basis["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
        malformed_basis
    )
    with pytest.raises(ValueError, match="boundary is malformed"):
        validate_live_review_snapshot(malformed_basis)


def test_live_adapter_recomputes_entry_boundary_expiry() -> None:
    core = HumanAssistedDecisionCore()
    bundle = deterministic_bundle()
    expired_at = bundle.entry_execution_boundaries[0].entry_valid_until
    [signal] = core.decision_documents(replace(bundle, as_of=expired_at))
    assert "ONE_MINUTE_SEGMENT_BOUNDARY_EXPIRED" in signal["decision_reasons"]

    signal["decision_reasons"] = [
        reason
        for reason in signal["decision_reasons"]
        if reason != "ONE_MINUTE_SEGMENT_BOUNDARY_EXPIRED"
    ]
    signal["entry_allowed"] = True
    signal["risk_multiplier"] = "1.00"
    profile = signal["execution_profile"]
    profile["hard_block_reason_codes"] = [
        reason
        for reason in profile["hard_block_reason_codes"]
        if reason != "ONE_MINUTE_SEGMENT_BOUNDARY_EXPIRED"
    ]
    profile["hard_blocked"] = False
    profile["recommendation"] = (
        "CAUTION" if profile["advisory_reason_codes"] else "READY"
    )
    signal["decision_document_id"] = signal_decision_document_id(signal)

    assert not _displayed_decision_evidence_is_consistent(
        signal,
        policy=core.contract.document()["policy"],
        risk=signal["higher_timeframe_risk"],
        warmup=signal["warmup"],
    )


def test_live_adapter_rejects_incomplete_or_mixed_signal_contract() -> None:
    incomplete = live_snapshot()
    incomplete.pop("signal_document_contract_id")
    incomplete["coverage_manifest"].pop("signal_document_contract_id")
    incomplete["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
        incomplete
    )
    with pytest.raises(ValueError, match="boundary is incomplete"):
        validate_live_review_snapshot(incomplete)

    mixed = live_snapshot()
    mixed["coverage_manifest"]["signal_document_contract_id"] = (
        "chanlun-human-assisted-signal-document"
    )
    mixed["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(mixed)
    with pytest.raises(ValueError, match="boundary is incomplete"):
        validate_live_review_snapshot(mixed)

    forged = live_snapshot()
    forged["signals"][0]["higher_timeframe_risk"].pop("market_reason_codes")
    forged["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(forged)
    with pytest.raises(ValueError, match="risk evidence"):
        validate_live_review_snapshot(forged)


def test_live_adapter_rejects_tampered_identity_and_low_stock_coverage() -> None:
    tampered = live_snapshot()
    tampered["signals"][0]["decision_reasons"].append("UNSIGNED_MUTATION")
    with pytest.raises(ValueError, match="boundary is incomplete"):
        validate_live_review_snapshot(tampered)

    insufficient = live_snapshot()
    insufficient["scan_audit"]["coverage_cycle_completion_ratio"] = "0.74"
    insufficient["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
        insufficient
    )
    with pytest.raises(ValueError, match="boundary is incomplete"):
        validate_live_review_snapshot(insufficient)


def test_live_adapter_recomputes_partial_universe_coverage_claims() -> None:
    partial = live_snapshot()
    signal_codes = sorted({str(row["code"]) for row in partial["signals"]})
    completed_codes = sorted((*signal_codes, "SH.600001", "SH.600002"))
    failed_code = "SZ.000002"
    discovered_codes = sorted((*completed_codes, failed_code))
    partial["coverage_manifest"].update(
        discovered_codes=discovered_codes,
        completed_codes=completed_codes,
        failed_codes=[failed_code],
        deferred_frequencies={failed_code: ["d", "30m", "5m", "1m"]},
    )
    partial["scan_audit"].update(
        coverage_cycle_completion_ratio="0.75",
        discovered_symbol_count=4,
        coverage_cycle_attempted_symbol_count=4,
        coverage_cycle_completed_symbol_count=3,
        coverage_cycle_excluded_symbol_count=0,
        coverage_cycle_failed_symbol_count=1,
        coverage_cycle_resolved_symbol_count=3,
        coverage_cycle_resolution_ratio="0.75",
        retry_symbol_count=1,
        next_epoch_retry_symbol_count=1,
        stock_failure_counts={"QMT_DATA_UNAVAILABLE": 1},
        stock_exclusion_counts={},
    )
    partial["errors"] = [
        {
            "error_type": "stock_analysis_error",
            "code": failed_code,
            "reason_code": "QMT_DATA_UNAVAILABLE",
            "failure_class": "UNCLASSIFIED_FAILURE",
            "retry_policy": "NEXT_COVERAGE_CYCLE",
            "deterministic_for_coverage_epoch": False,
            "remote_error_type": "RuntimeError",
            "reason": "QMT data unavailable",
        }
    ]
    partial["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(partial)

    # The frozen review contract explicitly permits 75% coverage, provided
    # every omission is named and scheduled for a later epoch.
    validate_live_review_snapshot(partial)

    for forged in (
        {"coverage_cycle_completion_ratio": "0.99"},
        {"coverage_cycle_completed_symbol_count": 4},
    ):
        value = json.loads(json.dumps(partial))
        value["scan_audit"].update(forged)
        value["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(value)
        with pytest.raises(ValueError, match="boundary is incomplete"):
            validate_live_review_snapshot(value)

    missing_error = json.loads(json.dumps(partial))
    missing_error["errors"] = []
    missing_error["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
        missing_error
    )
    with pytest.raises(ValueError, match="boundary is incomplete"):
        validate_live_review_snapshot(missing_error)


def test_coverage_dispositions_require_authenticated_retry_evidence() -> None:
    snapshot = live_snapshot()
    manifest = copy.deepcopy(snapshot["coverage_manifest"])
    code = str(manifest["completed_codes"][0])
    manifest["failed_codes"] = [code]
    manifest["backoff_frequencies"] = {code: ["d", "30m", "5m", "1m"]}
    runtime_error = {
        "code": code,
        "error_type": "stock_analysis_error",
        "reason_code": "NATIVE_WORKER_TIMEOUT",
        "failure_class": "RUNTIME_FAILURE",
        "retry_policy": "NEXT_REFRESH_AFTER_BACKOFF",
        "deterministic_for_coverage_epoch": False,
        "remote_error_type": "NativeScreeningWorkerTimeout",
        "reason": "native worker made no progress",
    }

    assert coverage_manifest_dispositions_are_consistent(
        manifest,
        [runtime_error],
    )

    for forged_errors in (
        [],
        [{**runtime_error, "failure_class": "MARKET_DATA_REJECTION"}],
        [{**runtime_error, "retry_policy": "NEXT_MARKET_DATA_EPOCH"}],
        [{**runtime_error, "deterministic_for_coverage_epoch": True}],
        [
            {
                key: value
                for key, value in runtime_error.items()
                if key != "remote_error_type"
            }
        ],
    ):
        assert not coverage_manifest_dispositions_are_consistent(
            manifest,
            forged_errors,
        )


def test_live_adapter_authenticates_minimum_history_exclusions() -> None:
    snapshot = live_snapshot()
    signal_codes = sorted({str(row["code"]) for row in snapshot["signals"]})
    completed_codes = sorted((*signal_codes, "SH.600001", "SH.600002", "SH.600003"))
    excluded_code = "SZ.301999"
    discovered_codes = sorted((*completed_codes, excluded_code))
    exclusion = {
        "code": excluded_code,
        "exclusion_type": "stock_analysis_exclusion",
        "eligibility": "INSUFFICIENT_MINIMUM_HISTORY",
        "reason_code": "KLINE_MINIMUM_HISTORY_NOT_MET",
        "retry_policy": "NEXT_MARKET_DATA_EPOCH",
        "deterministic_for_coverage_epoch": True,
        "remote_error_type": "ValueError",
        "reason": "kline frame does not meet minimum history",
    }
    snapshot["coverage_manifest"].update(
        discovered_codes=discovered_codes,
        completed_codes=completed_codes,
        excluded_codes=[excluded_code],
        failed_codes=[],
        exclusions=[exclusion],
        deferred_frequencies={excluded_code: ["d", "30m", "5m", "1m"]},
    )
    snapshot["scan_audit"].update(
        coverage_cycle_completion_ratio="0.8",
        coverage_cycle_resolution_ratio="1",
        discovered_symbol_count=5,
        coverage_cycle_attempted_symbol_count=5,
        coverage_cycle_completed_symbol_count=4,
        coverage_cycle_excluded_symbol_count=1,
        coverage_cycle_failed_symbol_count=0,
        coverage_cycle_resolved_symbol_count=5,
        retry_symbol_count=1,
        next_epoch_retry_symbol_count=1,
        stock_failure_counts={},
        stock_exclusion_counts={"KLINE_MINIMUM_HISTORY_NOT_MET": 1},
    )
    snapshot["errors"] = []
    snapshot["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
        snapshot
    )

    validate_live_review_snapshot(snapshot)

    forged_documents = []
    missing_document = json.loads(json.dumps(snapshot))
    missing_document["coverage_manifest"]["exclusions"] = []
    forged_documents.append(missing_document)

    wrong_reason = json.loads(json.dumps(snapshot))
    wrong_reason["coverage_manifest"]["exclusions"][0]["reason_code"] = (
        "KLINE_FRAME_UNAVAILABLE"
    )
    wrong_reason["scan_audit"]["stock_exclusion_counts"] = {
        "KLINE_FRAME_UNAVAILABLE": 1
    }
    forged_documents.append(wrong_reason)

    forged_resolution = json.loads(json.dumps(snapshot))
    forged_resolution["scan_audit"]["coverage_cycle_resolution_ratio"] = "0.8"
    forged_documents.append(forged_resolution)

    for forged in forged_documents:
        forged["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
            forged
        )
        with pytest.raises(ValueError, match="boundary is incomplete"):
            validate_live_review_snapshot(forged)


def test_live_adapter_recomputes_decision_core_identity() -> None:
    """A self-consistent outer hash cannot bless a forged core contract."""

    tampered = live_snapshot()
    tampered["decision_core"]["policy"]["max_five_minute_setup_age_seconds"] += 1
    tampered["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
        tampered
    )
    with pytest.raises(ValueError, match="boundary is incomplete"):
        validate_live_review_snapshot(tampered)

    self_asserted = live_snapshot()
    forged_id = "sha256:" + "0" * 64
    self_asserted["decision_core_id"] = forged_id
    self_asserted["decision_core"]["contract_id"] = forged_id
    for signal in self_asserted["signals"]:
        signal["decision_core_id"] = forged_id
    self_asserted["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
        self_asserted
    )
    with pytest.raises(ValueError, match="boundary is incomplete"):
        validate_live_review_snapshot(self_asserted)


@pytest.mark.parametrize(
    "reason_code",
    ("no_active_directional_point", "directional_points_expired"),
)
def test_neutral_context_accepts_both_empty_and_expired_point_reasons(
    reason_code: str,
) -> None:
    assert _decision_context_is_consistent(
        {
            "direction": "neutral",
            "disposition": "neutral",
            "hard_block": False,
            "dominant_point_id": None,
            "dominant_point_type": None,
            "reason_codes": [reason_code],
        }
    )


def test_down_context_with_dominant_buy_is_mixed_not_supportive() -> None:
    context = {
        "direction": "down",
        "disposition": "neutral",
        "hard_block": False,
        "dominant_point_id": "sha256:" + "1" * 64,
        "dominant_point_type": "2buy",
        "reason_codes": ["mixed_or_transition_structure"],
    }

    assert _decision_context_is_consistent(context)

    context["disposition"] = "supportive"
    context["reason_codes"] = ["confirmed_buy_structure"]
    assert not _decision_context_is_consistent(context)
