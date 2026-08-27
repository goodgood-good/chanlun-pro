from __future__ import annotations

import copy
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
import sys
import threading
import time

import pytest

import cl_app.services.trading_screening as trading_screening_subject

from chanlun.core.strict_structure.current_events import TerminalSegmentReference
from chanlun.decision_support.fingerprints import sha256_json
from chanlun.decision_support.trading_system.live_human_review import (
    SECTOR_COVERAGE_CONTRACT_ID,
    live_screening_snapshot_content_sha256,
    screening_coverage_epoch_id,
)
from chanlun.decision_support.trading_system.human_assisted_decision import (
    FORMAL_SELECTION_REQUIRED_REASON_CODE,
    HumanAssistedDecisionCore,
)
from chanlun.decision_support.trading_system.human_review_screening import (
    market_symbol_higher_timeframe_review_evidence_from_risk,
)
from chanlun.decision_support.trading_system.engine import (
    SymbolStructureBundle,
)
from chanlun.decision_support.trading_system.a_share_minute_grid import (
    a_share_optional_entry_valid_until,
)
from chanlun.decision_support.trading_system.higher_timeframe_gate import (
    HIGHER_TIMEFRAME_SESSION_EVIDENCE_CONTRACT_ID,
    QMT_SECTOR_NATIVE_DAILY_RESEARCH_SOURCE_MODE,
    HigherTimeframeGateBundle,
    HigherTimeframeGateEvidence,
    HigherTimeframePeriodDiagnostic,
    HigherTimeframeSessionEvidence,
    QmtSectorSameBaseCoverageEvidence,
    sector_native_daily_research_bridge_contract,
    unresolved_higher_timeframe_gates,
)
from chanlun.decision_support.trading_system.models import EntryExecutionBoundary
from chanlun.decision_support.trading_system.lifecycle import (
    structural_point_occurrence_id,
)
from chanlun.decision_support.trading_system.incremental_scan import ScanPlan
from chanlun.decision_support.trading_system.live_review_materialization import (
    live_review_materialization_receipt,
)
from chanlun.decision_support.trading_system.sector_policy import assess_sector
from chanlun.decision_support.trading_system.sector_strength import (
    build_horizontal_sector_strength_batch,
)
from chanlun.decision_support.trading_system.selection import (
    SelectionResearchSnapshot,
    SectorMemberHistory,
)
from chanlun.decision_support.trading_system.qmt_same_base_stream import (
    QmtMinuteSessionIssue,
)
from chanlun.decision_support.trading_system.qmt_higher_timeframe import (
    QMT_HIGHER_TIMEFRAME_WARMUP_CONVERGENCE_PARAMETER_SET_ID,
    QMT_HIGHER_TIMEFRAME_WARMUP_EVIDENCE_CONTRACT_ID,
    QmtHigherTimeframeWarmupEvidence,
)
from chanlun.decision_support.trading_system.warmup_convergence import (
    WARMUP_CONVERGENCE_ENVELOPE_CONTRACT_ID,
    classify_warmup_convergence_envelope,
)
from chanlun.decision_support.trading_system.qmt_native_daily_bridge import (
    QMT_NATIVE_DAILY_CALENDAR_COVERAGE_EVIDENCE_CONTRACT_ID,
    QMT_NATIVE_DAILY_RECONCILIATION_CONTRACT_ID,
    QmtNativeDailyCalendarCoverageEvidence,
    QmtNativeDailyReconciliationEvidence,
)
from tests.trading_system.helpers import (
    AS_OF,
    confirmed_point,
    eligible_sector,
    hostile_sector,
    neutral_context,
    provisional_point,
    supportive_context,
    valid_selection_research,
)
from cl_app.services.trading_screening import (
    SIGNAL_DOCUMENT_CONTRACT_ID,
    TradingScreeningConfig,
    TradingScreeningService,
    _apply_selection_scope,
    _cache_is_valid,
    _full_coverage_refresh_window_open,
    _is_current_selection_signal,
    _next_background_active_start,
    _next_full_coverage_active_start,
    _priority_signal_candidate_codes,
    _priority_monitor_delay_seconds,
    _priority_monitor_compute_window_open,
    _priority_monitor_session_open,
    _rotating_signal_candidate_admission_order,
    _current_session_suspension_can_be_confirmed,
    _current_session_zero_trade_codes,
    _structure_bundle_is_current,
    _structure_bundle_is_current_for_intraday_evidence,
    _take_rotating_priority_batch,
    _take_rule_recheck_batch,
    _take_due_candidate_batch,
    _sector_source_evidence_complete,
)
from cl_app.services.trading_screening_scope import (
    ScreeningScopeAuthorizationError,
)
from cl_app.services.trading_screening_source_migrations import (
    orchestration_source_migration_allowed,
    suspension_evidence_recheck_source_migration_allowed,
)
from cl_app.services.realtime_quotes import (
    AShareInstrumentSessionStatus,
    AShareInstrumentSessionStatusBatch,
    AShareRealtimeQuote,
    AShareRealtimeQuoteBatch,
)
from cl_app.services.trading_notifications import SignalNotificationDispatcher
from cl_app.services.trading_screening_gateway import (
    CachedSectorSnapshot,
    SectorAnalysisExclusion,
    SectorAnalysisFailure,
    SectorAssessmentBatch,
)


def test_stock_decision_outcome_uses_current_executable_five_minute_contract() -> None:
    decision_at = AS_OF + timedelta(days=5)
    historical = SymbolStructureBundle(
        code="SZ.000001",
        as_of=decision_at,
        sector=eligible_sector(),
        thirty_direction="neutral",
        thirty_points=(),
        five_points=(confirmed_point("1buy"),),
        one_points=(),
        opposite_points=(),
    )
    current = replace(
        historical,
        five_points=(confirmed_point("1buy", minutes_after=(5 * 24 * 60) + 295),),
    )
    engine = HumanAssistedDecisionCore(formal_selection_required=False)

    historical_evaluated = engine.evaluate_symbol(historical)
    current_evaluated = engine.evaluate_symbol(current)

    assert historical_evaluated == ()
    assert (
        trading_screening_subject._symbol_stock_decision_outcome(
            historical,
            historical_evaluated,
        )
        == "NO_CURRENT_EXECUTABLE_5M_STRUCTURAL_POINT"
    )
    assert current_evaluated
    assert (
        trading_screening_subject._symbol_stock_decision_outcome(
            current,
            current_evaluated,
        )
        == "CURRENT_5M_STRUCTURAL_SIGNAL_EMITTED"
    )


def _current_terminal_point(point, *, terminal_minutes: int = 30):
    """Attach the exact production lineage required by formal live alerts."""

    return replace(
        point,
        terminal_segment=TerminalSegmentReference(
            role="latest_completed",
            structural_level=point.recursive_level,
            unit_id=f"segment:latest-completed:{point.point_id}",
            source_kind="segment",
            direction="down" if point.side == "buy" else "up",
            state="locked",
            market_start=point.anchor_at - timedelta(minutes=terminal_minutes),
            market_end=point.anchor_at,
            available_at=point.available_at,
        ),
    )


class RecordingMarketData:
    def __init__(self) -> None:
        self.bundle_codes: list[str] = []
        self.bundle_frequency_requests: list[tuple[str, tuple[str, ...]]] = []
        self.name_codes: list[str] = []

    def changed_bars(self, since: datetime | None):
        del since
        return ()

    def active_watchlist(self) -> tuple[str, ...]:
        return ()

    def active_watchlist_scope(
        self,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        requested = self.active_watchlist()
        eligible = self.tradable_instrument_codes(requested)
        return eligible, tuple(code for code in requested if code not in eligible)

    def holdings(self) -> tuple[str, ...]:
        return ()

    def holdings_scope(
        self,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        requested = self.holdings()
        eligible = self.tradable_instrument_codes(requested)
        return eligible, tuple(code for code in requested if code not in eligible)

    def tradable_instrument_codes(
        self,
        codes: tuple[str, ...],
    ) -> tuple[str, ...]:
        return codes

    def screening_instrument_types(
        self,
        codes: tuple[str, ...],
    ) -> dict[str, str]:
        return {
            code: "index_cn" if code == "SH.000001" else "stock_cn" for code in codes
        }

    def symbol_name(self, code: str) -> str | None:
        self.name_codes.append(code)
        return {"SZ.000001": "平安银行"}.get(code)

    def structure_bundle(
        self,
        code: str,
        *,
        as_of: datetime,
        sector,
        frequencies=(),
    ) -> SymbolStructureBundle:
        self.bundle_frequency_requests.append((code, tuple(frequencies)))
        self.bundle_codes.append(code)
        return SymbolStructureBundle(
            code=code,
            as_of=as_of,
            sector=sector,
            thirty_direction="neutral",
            thirty_points=(),
            five_points=(),
            one_points=(),
            opposite_points=(),
        )

    def structure_bundle_with_risk_cutoff(
        self,
        code: str,
        *,
        as_of: datetime,
        sector,
        frequencies=(),
        risk_evidence_cutoff: datetime,
    ) -> SymbolStructureBundle:
        del risk_evidence_cutoff
        return self.structure_bundle(
            code,
            as_of=as_of,
            sector=sector,
            frequencies=frequencies,
        )


class RecordingSectorCatalog:
    def __init__(self, batch: SectorAssessmentBatch | None = None) -> None:
        self.assessment_calls: list[datetime] = []
        self.admitted_scope_calls: list[tuple[str, ...] | None] = []
        self.member_calls = 0
        self.batch = batch or SectorAssessmentBatch(
            assessments=(eligible_sector(),),
            discovered_count=1,
            completed_count=1,
            failure_counts=(),
            errors=(),
        )

    def native_sector_assessments(
        self,
        *,
        as_of: datetime,
        admitted_codes: tuple[str, ...] | None = None,
    ):
        calls = getattr(self, "assessment_calls", None)
        if calls is not None:
            calls.append(as_of)
        scope_calls = getattr(self, "admitted_scope_calls", None)
        if scope_calls is not None:
            scope_calls.append(admitted_codes)
        return self.batch

    def members(self):
        self.member_calls += 1
        return {eligible_sector().sector_id: ("SZ.000001",)}


class MultiMemberSectorCatalog(RecordingSectorCatalog):
    def __init__(self, symbols: tuple[str, ...]) -> None:
        super().__init__()
        self.symbols = symbols

    def members(self):
        self.member_calls += 1
        return {eligible_sector().sector_id: self.symbols}

    def cached_sector_snapshot_for_priority(self, *, as_of: datetime):
        return CachedSectorSnapshot(
            batch=self.batch,
            members=self.members(),
            requested_as_of=as_of,
            current_decision_epoch=True,
            content_sha256="sha256:" + "7" * 64,
        )


class AffinityRecordingSectorCatalog(MultiMemberSectorCatalog):
    def __init__(self, symbols: tuple[str, ...]) -> None:
        super().__init__(symbols)
        self.affinity_calls: list[dict[str, tuple[str, ...]]] = []

    def configure_coverage_sector_affinity(self, *, members_by_sector):
        captured = dict(members_by_sector)
        self.affinity_calls.append(captured)
        return {
            "schema": "test-coverage-sector-affinity",
            "configured": True,
            "symbol_count": sum(len(values) for values in captured.values()),
        }


class EvidenceSectorCatalog(RecordingSectorCatalog):
    def __init__(
        self,
        batch: SectorAssessmentBatch,
        symbols: tuple[str, ...],
    ) -> None:
        super().__init__(batch)
        self.symbols = symbols

    def members(self):
        self.member_calls += 1
        return {eligible_sector().sector_id: self.symbols}

    def cached_sector_snapshot_for_priority(self, *, as_of: datetime):
        return CachedSectorSnapshot(
            batch=self.batch,
            members=self.members(),
            requested_as_of=as_of,
            current_decision_epoch=True,
            content_sha256="sha256:" + "7" * 64,
        )


class HydratingEvidenceSectorCatalog(EvidenceSectorCatalog):
    """Model the native proxy whose atomic member cache is process-local."""

    def __init__(
        self,
        batch: SectorAssessmentBatch,
        symbols: tuple[str, ...],
    ) -> None:
        super().__init__(batch, symbols)
        self.hydrated = False
        self.restore_calls: list[tuple[datetime, str]] = []

    def native_sector_assessments(
        self,
        *,
        as_of: datetime,
        admitted_codes: tuple[str, ...] | None = None,
    ):
        batch = super().native_sector_assessments(
            as_of=as_of,
            admitted_codes=admitted_codes,
        )
        self.hydrated = True
        return batch

    def members(self):
        if not self.hydrated:
            raise RuntimeError("atomic sector snapshot has not been captured")
        return super().members()

    def restore_authenticated_sector_members(
        self,
        *,
        members,
        as_of: datetime,
        catalog_revision: str,
    ) -> None:
        assert members == {eligible_sector().sector_id: self.symbols}
        self.restore_calls.append((as_of, catalog_revision))
        self.hydrated = True


def _evidence_sector_batch(
    symbols: tuple[str, ...],
    *,
    context_revision: str,
) -> SectorAssessmentBatch:
    sector = eligible_sector()
    membership_revision = sha256_json(
        {
            "schema": "test-sector-membership",
            "sector_id": sector.sector_id,
            "symbols": symbols,
        }
    )
    evidence = build_horizontal_sector_strength_batch(
        decision_time=AS_OF,
        benchmark_symbol="SH.000300",
        benchmark_daily=(),
        members_by_sector={
            sector.sector_id: tuple(
                SectorMemberHistory(
                    symbol,
                    AS_OF.date(),
                    "UNEXPLAINED_GAP",
                    (),
                )
                for symbol in symbols
            )
        },
        membership_revision=membership_revision,
    )
    five = replace(
        supportive_context("5m"),
        dominant_point_id=sha256_json(
            {
                "schema": "test-sector-dominant-point",
                "revision": context_revision,
            }
        ),
    )
    assessment = assess_sector(
        sector_id=sector.sector_id,
        sector_name=sector.sector_name,
        market_data_source="qmt-gics3-composite",
        thirty=neutral_context("30m"),
        five=five,
        one=neutral_context("1m"),
        data_complete=True,
    )
    strength = evidence[sector.sector_id]
    assessment = replace(
        assessment,
        horizontal_strength=strength.strength,
        horizontal_rank=strength.rank,
        strength_anchor_session=strength.anchor_session,
        strength_member_count=strength.member_count,
        strength_source_revision=strength.source_revision,
        strength_reason_codes=strength.reason_codes,
    )
    return SectorAssessmentBatch(
        assessments=(assessment,),
        discovered_count=1,
        completed_count=1,
        failure_counts=(),
        errors=(),
        catalog_revision=membership_revision,
        strength_evidence=evidence,
    )


def test_hierarchical_sector_routing_prefers_child_and_falls_back_only_when_unknown() -> (
    None
):
    parent = replace(
        eligible_sector(),
        sector_id="qmt-gics3:parent",
        sector_name="信息技术",
    )
    child = replace(
        eligible_sector(),
        sector_id="qmt-gics4:primary",
        sector_name="信息技术 → 半导体",
    )
    unavailable_child = replace(
        hostile_sector(),
        sector_id="qmt-gics4:unavailable",
        sector_name="信息技术 → 电子设备",
    )
    blocked_child = replace(
        hostile_sector(),
        sector_id="qmt-gics4:blocked",
        sector_name="信息技术 → 软件",
    )

    routing = trading_screening_subject._sector_member_routing(
        assessments=(parent, child, unavailable_child, blocked_child),
        members_by_sector={
            parent.sector_id: (
                "SH.600001",
                "SH.600002",
                "SH.600003",
                "SH.600004",
            ),
            child.sector_id: ("SH.600001",),
            unavailable_child.sector_id: ("SH.600002",),
            blocked_child.sector_id: ("SH.600003",),
        },
        parent_relations=tuple(
            sorted(
                (
                    (child.sector_id, parent.sector_id),
                    (unavailable_child.sector_id, parent.sector_id),
                    (blocked_child.sector_id, parent.sector_id),
                )
            )
        ),
        unavailable_sector_ids=frozenset({unavailable_child.sector_id}),
    )

    assert routing.eligible_sector_by_code["SH.600001"] == child
    assert routing.eligible_sector_by_code["SH.600002"] == parent
    assert "SH.600003" not in routing.eligible_sector_by_code
    assert routing.context_sector_by_code["SH.600003"] == blocked_child
    assert routing.eligible_sector_by_code["SH.600004"] == parent
    assert routing.effective_members_by_sector == {
        parent.sector_id: ("SH.600002", "SH.600004"),
        child.sector_id: ("SH.600001",),
    }
    assert routing.ranked_scan_codes == (
        "SH.600001",
        "SH.600002",
        "SH.600004",
    )
    assert routing.audit["gics4_primary_symbol_count"] == 1
    assert routing.audit["gics3_fallback_symbol_count"] == 2
    assert routing.audit["gics4_structural_blocked_symbol_count"] == 1


def test_ranked_scan_order_round_robins_sector_affinity_groups() -> None:
    first = replace(
        eligible_sector(),
        sector_id="qmt-gics4:first",
        sector_name="第一行业",
    )
    second = replace(
        eligible_sector(),
        sector_id="qmt-gics4:second",
        sector_name="第二行业",
    )
    third = replace(
        eligible_sector(),
        sector_id="qmt-gics3:third",
        sector_name="第三行业",
    )

    ordered = trading_screening_subject._ranked_sector_round_robin_codes(
        {
            "SH.600003": first,
            "SH.600001": first,
            "SH.600004": second,
            "SH.600002": second,
            "SH.600006": third,
            "SH.600005": third,
        },
        {
            first.sector_id: 1,
            second.sector_id: 2,
            third.sector_id: 3,
        },
    )

    assert ordered == (
        "SH.600001",
        "SH.600002",
        "SH.600005",
        "SH.600003",
        "SH.600004",
        "SH.600006",
    )


def test_ranked_scan_order_stripes_the_configured_worker_affinity_slots() -> None:
    worker_count = 3
    sector_ids_by_slot = {index: [] for index in range(worker_count)}
    suffix = 0
    while any(len(values) < 2 for values in sector_ids_by_slot.values()):
        sector_id = f"qmt-gics4:worker-test-{suffix}"
        digest = trading_screening_subject.hashlib.sha256(
            f"sector:{sector_id}".encode("utf-8")
        ).digest()
        slot = int.from_bytes(digest[:8], "big") % worker_count
        if len(sector_ids_by_slot[slot]) < 2:
            sector_ids_by_slot[slot].append(sector_id)
        suffix += 1

    sectors = [
        sector_id
        for slot in range(worker_count)
        for sector_id in sector_ids_by_slot[slot]
    ]
    assessments = {
        f"SH.{600100 + index}": replace(
            eligible_sector(),
            sector_id=sector_id,
            sector_name=sector_id,
        )
        for index, sector_id in enumerate(sectors)
    }
    rank_by_id = {
        assessment.sector_id: index
        for index, assessment in enumerate(assessments.values(), start=1)
    }

    ordered = trading_screening_subject._ranked_sector_round_robin_codes(
        assessments,
        rank_by_id,
        affinity_worker_count=worker_count,
    )
    slots = []
    by_code = assessments
    for code in ordered:
        sector_id = by_code[code].sector_id
        digest = trading_screening_subject.hashlib.sha256(
            f"sector:{sector_id}".encode("utf-8")
        ).digest()
        slots.append(int.from_bytes(digest[:8], "big") % worker_count)

    assert slots == [0, 1, 2, 0, 1, 2]


def test_priority_scan_order_fills_each_wave_from_distinct_worker_slots() -> None:
    worker_count = 2
    sector_ids_by_slot = {index: [] for index in range(worker_count)}
    suffix = 0
    while any(len(values) < 2 for values in sector_ids_by_slot.values()):
        sector_id = f"qmt-gics4:priority-worker-test-{suffix}"
        slot = trading_screening_subject._affinity_worker_slot(
            f"sector:{sector_id}",
            worker_count,
        )
        if len(sector_ids_by_slot[slot]) < 2:
            sector_ids_by_slot[slot].append(sector_id)
        suffix += 1
    sectors = tuple(
        sector_id
        for slot in range(worker_count)
        for sector_id in sector_ids_by_slot[slot]
    )
    codes = tuple(f"SH.{600200 + index}" for index in range(len(sectors)))
    by_code = {
        code: replace(
            eligible_sector(),
            sector_id=sector_id,
            sector_name=sector_id,
        )
        for code, sector_id in zip(codes, sectors)
    }

    ordered = trading_screening_subject._priority_affinity_striped_codes(
        codes,
        sector_by_code=by_code,
        worker_count=worker_count,
    )

    assert [
        trading_screening_subject._affinity_worker_slot(
            f"sector:{by_code[code].sector_id}",
            worker_count,
        )
        for code in ordered
    ] == [0, 1, 0, 1]


def test_candidate_scan_order_stripes_one_large_sector_by_symbol() -> None:
    worker_count = 2
    sector = replace(
        eligible_sector(),
        sector_id="qmt-gics4:candidate-large-sector",
        sector_name="候选大行业",
    )
    codes_by_slot = {index: [] for index in range(worker_count)}
    suffix = 0
    while any(len(values) < 2 for values in codes_by_slot.values()):
        code = f"SH.{601000 + suffix:06d}"
        key = f"sector:{sector.sector_id}|symbol:{code}"
        slot = trading_screening_subject._affinity_worker_slot(key, worker_count)
        if len(codes_by_slot[slot]) < 2:
            codes_by_slot[slot].append(code)
        suffix += 1
    codes = tuple(code for slot in range(worker_count) for code in codes_by_slot[slot])

    ordered = trading_screening_subject._priority_affinity_striped_codes(
        codes,
        sector_by_code={code: sector for code in codes},
        worker_count=worker_count,
        symbol_striping=True,
    )

    assert [
        trading_screening_subject._affinity_worker_slot(
            f"sector:{sector.sector_id}|symbol:{code}",
            worker_count,
        )
        for code in ordered
    ] == [0, 1, 0, 1]


class ConcurrentRecordingMarketData(RecordingMarketData):
    """Expose whether stock structure requests actually overlap in time."""

    def __init__(self) -> None:
        super().__init__()
        self._active = 0
        self.max_active = 0
        self._active_lock = threading.Lock()

    def structure_bundle(
        self,
        code: str,
        *,
        as_of: datetime,
        sector,
        frequencies=(),
    ) -> SymbolStructureBundle:
        with self._active_lock:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        try:
            # Sleeping deliberately releases the GIL, matching the production
            # threads while they wait for isolated worker-process IPC.
            time.sleep(0.03)
            return super().structure_bundle(
                code,
                as_of=as_of,
                sector=sector,
                frequencies=frequencies,
            )
        finally:
            with self._active_lock:
                self._active -= 1


class RecordingEngine(HumanAssistedDecisionCore):
    def __init__(self) -> None:
        self.codes: list[str] = []
        self.bundles: list[SymbolStructureBundle] = []

    def evaluate_symbol(self, bundle: SymbolStructureBundle):
        self.codes.append(bundle.code)
        self.bundles.append(bundle)
        return ()


class RecordingPlanner:
    def __init__(self, symbols: tuple[str, ...] = ("SZ.000001",)) -> None:
        self.calls = 0
        self.symbols = symbols

    def __call__(self, **kwargs) -> ScanPlan:
        self.calls += 1
        assert kwargs["sector_members"] == {eligible_sector().sector_id: ("SZ.000001",)}
        return ScanPlan(
            sectors=(eligible_sector().sector_id,),
            symbols=self.symbols,
            symbol_frequencies=tuple(
                (code, ("1m", "5m", "30m")) for code in self.symbols
            ),
            full_market_history_scan=False,
            background_full_refresh_required=False,
        )


class SequencedPlanner:
    def __init__(self, batches: tuple[tuple[str, ...], ...]) -> None:
        self._batches = list(batches)

    def __call__(self, **_kwargs) -> ScanPlan:
        symbols = self._batches.pop(0) if self._batches else ()
        return ScanPlan(
            sectors=(eligible_sector().sector_id,),
            symbols=symbols,
            symbol_frequencies=tuple((code, ("1m", "5m", "30m")) for code in symbols),
            full_market_history_scan=False,
            background_full_refresh_required=False,
        )


def test_no_signal_scan_keeps_warmup_fail_closed_audit(
    tmp_path: Path,
) -> None:
    symbols = ("SH.601808", "SZ.000698")

    class WarmupAuditMarketData(RecordingMarketData):
        def structure_bundle(
            self,
            code: str,
            *,
            as_of: datetime,
            sector,
            frequencies=(),
        ) -> SymbolStructureBundle:
            base = super().structure_bundle(
                code,
                as_of=as_of,
                sector=sector,
                frequencies=frequencies,
            )
            divergent_frequency = "1m" if code == "SH.601808" else "5m"
            warmup_rows = tuple(
                (
                    frequency,
                    frequency != divergent_frequency,
                    1200 if frequency != "d" else 600,
                    800 if frequency != "d" else 400,
                )
                for frequency in ("d", "30m", "5m", "1m")
            )
            reason_codes = tuple(
                f"{frequency.upper()}:"
                + (
                    "WARMUP_TAIL_DIVERGED"
                    if frequency == divergent_frequency
                    else "WARMUP_TAIL_STABLE"
                )
                for frequency in ("d", "30m", "5m", "1m")
            )
            differences = tuple(
                (
                    frequency,
                    ("WARMUP_ACTIVE_POINT_LANES_CHANGED",)
                    if frequency == divergent_frequency
                    else (),
                )
                for frequency in ("d", "30m", "5m", "1m")
            )
            return replace(
                base,
                warmup_converged=False,
                warmup_reason_codes=reason_codes,
                warmup_by_frequency=warmup_rows,
                warmup_difference_codes_by_frequency=differences,
                enforce_warmup_entry_gate=True,
                analysis_closed_at_by_frequency=tuple(
                    (frequency, as_of) for frequency in ("d", "30m", "5m", "1m")
                ),
            )

    config = TradingScreeningConfig(
        max_symbols_per_refresh=len(symbols),
        stock_worker_count=1,
    )
    cache_path = tmp_path / "snapshot.json"
    service = TradingScreeningService(
        market_data=WarmupAuditMarketData(),
        sector_catalog=MultiMemberSectorCatalog(symbols),
        engine=RecordingEngine(),
        scan_planner=SequencedPlanner((symbols,)),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
        config=config,
    )

    payload = service.refresh_now()
    audit = payload["scan_audit"]

    assert payload["signals"] == []
    assert audit["warmup_sensitive_symbol_count"] == 2
    assert audit["warmup_context_only_sensitive_symbol_count"] == 1
    assert audit["trade_level_warmup_unconverged_symbol_count"] == 1
    assert audit["trade_level_warmup_fail_closed_symbol_count"] == 1
    assert audit["warmup_difference_reason_counts"] == {
        "1m:WARMUP_ACTIVE_POINT_LANES_CHANGED": 1,
        "5m:WARMUP_ACTIVE_POINT_LANES_CHANGED": 1,
    }
    assert audit["stock_decision_outcome_contract_id"] == (
        "chanlun-screening-stock-decision-outcome-v1"
    )
    assert audit["stock_decision_outcome_counts"] == {
        "NO_CURRENT_5M_STRUCTURAL_POINT": 2,
    }
    assert audit["stock_decision_outcomes"] == {
        "SH.601808": "NO_CURRENT_5M_STRUCTURAL_POINT",
        "SZ.000698": "NO_CURRENT_5M_STRUCTURAL_POINT",
    }
    assert [row["code"] for row in audit["warmup_sensitive_symbols"]] == [
        "SH.601808",
        "SZ.000698",
    ]
    assert service.health_snapshot()["warmup_sensitive_symbol_count"] == 2
    assert service.health_snapshot()["trade_level_warmup_fail_closed_symbol_count"] == 1
    assert service.health_snapshot()["stock_decision_outcome_counts"] == {
        "NO_CURRENT_5M_STRUCTURAL_POINT": 2,
    }
    restarted = TradingScreeningService(
        market_data=WarmupAuditMarketData(),
        sector_catalog=MultiMemberSectorCatalog(symbols),
        engine=RecordingEngine(),
        scan_planner=SequencedPlanner(((),)),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
        config=config,
    )
    assert set(restarted._coverage_cycle_warmup_diagnostics) == set(symbols)
    assert restarted._coverage_cycle_stock_decision_outcomes == {
        "SH.601808": "NO_CURRENT_5M_STRUCTURAL_POINT",
        "SZ.000698": "NO_CURRENT_5M_STRUCTURAL_POINT",
    }
    assert restarted.snapshot()["scan_audit"]["warmup_sensitive_symbol_count"] == 2


def test_stock_structure_requests_use_configured_parallel_workers(
    tmp_path: Path,
) -> None:
    symbols = tuple(f"SZ.{index:06d}" for index in range(1, 7))
    market = ConcurrentRecordingMarketData()
    sector_catalog = AffinityRecordingSectorCatalog(symbols)
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=sector_catalog,
        engine=HumanAssistedDecisionCore(),
        scan_planner=SequencedPlanner((symbols,)),
        cache_path=tmp_path / "snapshot.json",
        # 盘后无实时监听到期，完整覆盖可使用配置的全部三个结构进程。
        clock=lambda: AS_OF.replace(hour=15, minute=5),
        notifier=None,
        config=TradingScreeningConfig(
            max_symbols_per_refresh=len(symbols),
            stock_worker_count=3,
        ),
    )

    payload = service.refresh_now()

    assert market.max_active >= 2
    assert set(market.bundle_codes) == set(symbols)
    assert payload["scan_audit"]["completed_symbol_count"] == len(symbols)
    assert payload["scan_audit"]["stock_worker_count"] == 3
    assert sector_catalog.affinity_calls == [{eligible_sector().sector_id: symbols}]
    assert payload["scan_audit"]["coverage_sector_affinity"] == {
        "schema": "test-coverage-sector-affinity",
        "configured": True,
        "symbol_count": len(symbols),
    }


def test_full_coverage_reserves_one_qmt_worker_for_realtime_services(
    tmp_path: Path,
) -> None:
    symbols = tuple(f"SZ.{index:06d}" for index in range(1, 9))
    market = ConcurrentRecordingMarketData()
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=MultiMemberSectorCatalog(symbols),
        engine=HumanAssistedDecisionCore(),
        scan_planner=SequencedPlanner((symbols,)),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(
            max_symbols_per_refresh=len(symbols),
            stock_worker_count=4,
            full_coverage_worker_count=3,
        ),
    )

    payload = service.refresh_now()

    assert market.max_active == 3
    assert payload["scan_audit"]["stock_worker_count"] == 3
    assert payload["scan_audit"]["full_coverage_worker_limit"] == 3


def test_scan_batch_honors_hard_total_limit_without_starving_discovery(
    tmp_path: Path,
) -> None:
    symbols = tuple(f"SZ.{index:06d}" for index in range(1, 9))

    class PriorityMarket(RecordingMarketData):
        def active_watchlist(self) -> tuple[str, ...]:
            return symbols[:4]

    market = PriorityMarket()
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=MultiMemberSectorCatalog(symbols),
        engine=RecordingEngine(),
        scan_planner=SequencedPlanner((symbols,)),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(
            max_symbols_per_refresh=2,
            max_monitor_symbols_per_refresh=4,
            max_total_symbols_per_refresh=3,
        ),
    )

    first = service.refresh_now()
    second = service.refresh_now()

    assert market.bundle_codes == list(symbols[:6])
    assert first["coverage_manifest"]["completed_codes"] == list(symbols[:3])
    assert second["coverage_manifest"]["completed_codes"] == list(symbols[:6])


def test_snapshot_persistence_is_cross_thread_atomic(tmp_path: Path) -> None:
    cache_path = tmp_path / "snapshot.json"
    orphan = tmp_path / ".snapshot.json.1234.interrupted.tmp"
    orphan.write_text("orphan", encoding="utf-8")
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
    )
    writer_count = 12
    barrier = threading.Barrier(writer_count)
    errors: list[BaseException] = []
    errors_lock = threading.Lock()

    def write(index: int) -> None:
        try:
            barrier.wait(timeout=5)
            service._persist_atomic({"writer": index, "payload": "数" * 1000})
        except BaseException as exc:  # pragma: no cover - asserted below
            with errors_lock:
                errors.append(exc)

    threads = [
        threading.Thread(target=write, args=(index,)) for index in range(writer_count)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    persisted = json.loads(cache_path.read_text(encoding="utf-8"))
    assert persisted["writer"] in range(writer_count)
    assert persisted["payload"] == "数" * 1000
    assert not orphan.exists()
    assert not tuple(tmp_path.glob(".snapshot.json.*.tmp"))


def test_large_incomplete_snapshot_checkpoint_is_throttled_but_final_is_not(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache_path = tmp_path / "snapshot.json"
    cache_path.write_text("{}", encoding="utf-8")
    current = [AS_OF]
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=cache_path,
        clock=lambda: current[0],
        notifier=None,
        config=TradingScreeningConfig(
            incomplete_checkpoint_interval_seconds=120,
        ),
    )
    monkeypatch.setattr(
        trading_screening_subject,
        "_LARGE_INCOMPLETE_SNAPSHOT_BYTES",
        1,
    )
    service._last_incomplete_checkpoint_at = AS_OF
    partial = {"coverage_manifest": {"complete": False}}
    complete = {"coverage_manifest": {"complete": True}}

    assert service._incomplete_checkpoint_due(partial) is False
    current[0] = AS_OF + timedelta(seconds=119)
    assert service._incomplete_checkpoint_due(partial) is False
    current[0] = AS_OF + timedelta(seconds=120)
    assert service._incomplete_checkpoint_due(partial) is True
    current[0] = AS_OF
    assert service._incomplete_checkpoint_due(complete) is True


def test_service_rejects_corrupt_primary_before_generation_payload_restore(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "snapshot.json"
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
    )
    expected = service.refresh_now()
    generation_directory = tmp_path / ".snapshot.json.generations"
    generations = tuple(generation_directory.glob("*.json"))

    assert len(generations) == 1
    assert json.loads(generations[0].read_text(encoding="utf-8")) == expected

    cache_path.write_text("{interrupted", encoding="utf-8")
    recovered = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
    )

    assert recovered.snapshot()["snapshot_content_sha256"] is None
    health = recovered.health_snapshot()
    assert health["cache_recovered_from_generation"] is None
    assert health["quarantined_cache_reason"] == (
        "CACHE_SCOPE_PROOF_MISSING_OR_INVALID"
    )
    assert health["cache_generation_count"] == 1
    assert health["cache_generation_error"] is None


def test_same_epoch_incomplete_retry_recovers_last_complete_generation(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "snapshot.json"
    symbols = tuple(f"SZ.{index:06d}" for index in range(1, 11))
    config = TradingScreeningConfig(max_symbols_per_refresh=5)
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=MultiMemberSectorCatalog(symbols),
        engine=RecordingEngine(),
        scan_planner=SequencedPlanner((symbols,)),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
        config=config,
    )

    first = service.refresh_now()
    complete = service.refresh_now()

    assert first["coverage_manifest"]["complete"] is False
    assert complete["coverage_manifest"]["complete"] is True
    for code in symbols:
        service._pending_frequencies[code] = set(
            trading_screening_subject.SCREENING_STRUCTURE_FREQUENCIES
        )
    interrupted_retry = service.refresh_now()
    assert interrupted_retry["coverage_epoch_id"] == complete["coverage_epoch_id"]
    assert interrupted_retry["coverage_manifest"]["complete"] is False

    recovered = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=MultiMemberSectorCatalog(symbols),
        engine=RecordingEngine(),
        scan_planner=SequencedPlanner(((),)),
        cache_path=cache_path,
        clock=lambda: AS_OF + timedelta(minutes=1),
        notifier=None,
        config=config,
    )

    assert (
        recovered.snapshot()["snapshot_content_sha256"]
        == complete["snapshot_content_sha256"]
    )
    assert recovered.health_snapshot()["cache_recovered_from_generation"]


def test_research_revision_change_invalidates_snapshot_and_priority_state(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "snapshot.json"
    priority_path = tmp_path / "trading_priority_monitor_state.json"
    first = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(priority_monitoring_enabled=True),
    )
    published = first.refresh_now()
    first._priority_monitor_last_at = AS_OF
    first._persist_priority_monitor_state()
    persisted_priority = json.loads(priority_path.read_text(encoding="utf-8"))

    research = SelectionResearchSnapshot(
        snapshot_id="research:SZ.000001:20260720",
        symbol="SZ.000001",
        path="INDIVIDUAL_THREE_PROGRAM",
        effective_at=AS_OF,
        known_at=AS_OF,
        valid_until=AS_OF + timedelta(days=30),
        reviewer="研究员",
        signature="signed:research:SZ.000001:20260720",
        official_evidence_ids=("official:SZ.000001:20260720",),
        industry_opportunity_status="PASS",
        fundamental_role="LEADER",
        relative_value_status="UNDERVALUED",
        point_in_time_total_market_cap=Decimal("100000000000"),
        peer_set_id="peer:bank:20260720",
    )
    restarted = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=cache_path,
        selection_research=(research,),
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(priority_monitoring_enabled=True),
    )

    snapshot = restarted.snapshot()
    assert snapshot["scan_state"] == "not_started"
    assert snapshot["snapshot_content_sha256"] is None
    assert (
        snapshot["selection_research_revision"]
        != published["selection_research_revision"]
    )
    assert restarted._priority_monitor_last_at is None
    assert persisted_priority["selection_research_revision"] != (
        restarted._selection_research_revision
    )
    assert restarted.health_snapshot()["quarantined_cache_reason"] == (
        "SELECTION_RESEARCH_REVISION_MISMATCH"
    )


def test_same_epoch_completed_progress_cannot_silently_reset(tmp_path: Path) -> None:
    cache_path = tmp_path / "snapshot.json"
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
    )
    current = service.refresh_now()
    [completed_code] = current["coverage_manifest"]["completed_codes"]
    regressed = json.loads(json.dumps(current))
    manifest = regressed["coverage_manifest"]
    manifest["completed_codes"] = []
    manifest["pending_frequencies"] = {completed_code: ["d", "30m", "5m", "1m"]}
    manifest["complete"] = False
    service._finalize_snapshot_identity(regressed)

    with pytest.raises(ValueError, match="lost completed symbols"):
        service._persist_atomic(regressed)


def test_service_uses_incremental_scan_plan_and_new_engine(tmp_path: Path) -> None:
    market = RecordingMarketData()
    engine = RecordingEngine()
    planner = RecordingPlanner()
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=RecordingSectorCatalog(),
        engine=engine,
        scan_planner=planner,
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
    )

    payload = service.refresh_now()

    assert payload["schema"] == "chanlun-trading-screening"
    assert payload["structure_contract_id"] == "physical-timeframe-recursive"
    assert payload["sector_first"] is True
    assert payload["read_only"] is True
    assert payload["no_order_execution"] is True
    assert market.name_codes == []
    assert payload["screening_policy"] == {
        "latest_per_independent_lane": True,
        "confirmed_and_provisional_lanes_independent": True,
        "recent_confirmed_setup_ledger_retained": True,
        "external_holding_structure_policy": ("UNKNOWN_UNTIL_MANUALLY_CLASSIFIED"),
        "sell_only_higher_timeframe_evidence_policy": (
            "SCHEMA_COMPLETE_UNRESOLVED_WITHOUT_PROVIDER_CALL"
        ),
        "max_five_minute_setup_age_seconds": 345600,
        "sector_catalog_source": "qmt_gics3_gics4_hierarchy",
        "sector_price_source": "qmt_gics_hierarchy_component_composite",
        "sector_taxonomy_levels": ["GICS3", "GICS4"],
        "sector_hierarchy_gate": "GICS3_PARENT_REQUIRED",
        "sector_primary_route": "GICS4_CHILD_WHEN_AVAILABLE",
        "sector_child_unavailable_fallback": "GICS3_PARENT",
        "sector_child_structural_block_fallback": "NONE",
        "sector_composite_provider": "qmt-gics3-composite",
        "sector_composite_adjustment": ("causal-factor-stable-24-member-median"),
        "sector_composite_factor_adjustment_contract": (
            "QMT_RAW_PRICE_DIVISOR_CAUSAL_EX_DATE"
        ),
        "sector_composite_factor_cutoff": "decision_date_only",
        "sector_composite_factor_failure_policy": "fail_closed",
        "sector_composite_structure_price_quantum": "0.000001",
        "sector_composite_member_limit": 24,
        "sector_composite_minimum_member_count": 8,
        "sector_composite_minimum_bar_coverage": "0.60",
        "sector_composite_coverage_denominator": (
            "frozen_deterministic_representative_sample"
        ),
        "sector_composite_calendar_grid_contract": (
            "QMT_SH_TRADING_CALENDAR_CONTIGUOUS_VISIBLE_SUFFIX"
        ),
        "sector_composite_member_mask_contract": (
            "BIT_I_IS_SECTOR_COMPOSITE_MEMBERS_I"
        ),
        "sector_scope": "all_parent_gated_eligible_gics3_and_gics4",
        "stock_scope": "one_effective_sector_per_symbol",
        "sector_frequencies": ["30m", "5m"],
        "sector_higher_timeframe_base_frequency": "5m",
        "sector_thirty_minute_derivation_contract": (
            "SIX_CONTIGUOUS_COMPLETED_5M_COMPOSITE_BARS"
        ),
        "sector_higher_timeframe_frequencies": ["M", "W", "D"],
        "sector_higher_timeframe_membership_provenance": (
            "exact_members_sample_coverage_price_grid_and_path"
        ),
        "stock_structure_frequencies": ["d", "30m", "5m", "1m"],
        "stroke_mode": "strict-cl-k-distance",
        "center_source": "physical_timeframe_recursive_segments",
        "recursive_structure_used": True,
        "stock_structure_request_bars": {
            "d": 1600,
            "30m": 4000,
            "5m": 12000,
            "1m": 12000,
        },
        "stock_structure_qmt_dividend_type": "front_ratio",
        "provisional_point_source": "strict_approaching_ledger",
        "stock_trade_frequency": "5m",
        "stock_segment_difference_frequency": "1m",
        "stock_segment_difference_required_for_trade_signal": False,
        "stock_segment_difference_required_for_precise_execution": True,
        "minimum_market_data_frequency": "1m",
        "qmt_one_minute_grid_revision": (
            "QMT_A_SHARE_END_LABELLED_241_TO_COMPLETED_240_TRADE_AWARE"
        ),
        "tick_data_used": False,
        "selection_universe_source": "qmt_gics3_gics4_current_hierarchy",
        "monitor_instrument_eligibility": ("qmt_native_stock_or_etf_fail_closed"),
        "isolated_structure_instrument_type_contract": (
            "shared_qmt_catalog_explicit_stock_or_etf"
        ),
        "etf_selection_path": "ETF_PROXY_WITHOUT_INDIVIDUAL_SECTOR_GATE",
        "sector_strength_qmt_dividend_type": "front_ratio",
        "sector_strength_adjustment": ("front-ratio-terminal-close-normalized"),
        "sector_strength_price_basis_contract": (
            "QMT_FRONT_RATIO_TERMINAL_CLOSE_NORMALIZATION"
        ),
        "sector_strength_min_member_history_coverage": "1",
        "higher_timeframe_partial_evidence_policy": (
            "preserve_independent_market_sector_symbol_gates_fail_closed"
        ),
        "higher_timeframe_warmup_required_daily_bars": 480,
        "higher_timeframe_warmup_physical_daily_bars": 720,
        "sector_higher_timeframe_physical_five_minute_bars": 34560,
        "higher_timeframe_warmup_required_thirty_minute_bars": 3840,
        "higher_timeframe_warmup_convergence_contract": (
            "drop_oldest_third_compare_mwd_state_mapping_and_ma5"
        ),
        "higher_timeframe_warmup_entry_policy": (
            "fail_closed_on_insufficient_or_diverged"
        ),
        "stock_failure_protocol": "stable_reason_code_epoch_scoped_retry",
    }
    assert payload["screening_policy_id"] == sha256_json(payload["screening_policy"])
    assert (
        payload["coverage_manifest"]["screening_policy_id"]
        == payload["screening_policy_id"]
    )
    assert str(payload["coverage_manifest"]["sector_catalog_revision"]).startswith(
        "sha256:"
    )
    assert payload["scan_audit"]["full_market_history_scan"] is False
    assert planner.calls == 1
    assert market.bundle_codes == ["SZ.000001"]
    assert engine.codes == ["SZ.000001"]


def test_external_holding_does_not_invent_position_structure(
    tmp_path: Path,
) -> None:
    class PhysicalHoldingMarketData(RecordingMarketData):
        def holdings(self) -> tuple[str, ...]:
            return ("SZ.000001",)

        def structure_bundle(self, code: str, **kwargs) -> SymbolStructureBundle:
            return replace(
                super().structure_bundle(code, **kwargs),
                physical_timeframe_recursive=True,
            )

    engine = RecordingEngine()
    service = TradingScreeningService(
        market_data=PhysicalHoldingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=engine,
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
    )

    service.refresh_now()

    assert len(engine.bundles) == 1
    assert engine.bundles[0].held_tower is None
    assert engine.bundles[0].held_level is None


def test_qmt_catalog_revision_change_starts_a_new_coverage_epoch(
    tmp_path: Path,
) -> None:
    first_revision = "sha256:" + "1" * 64
    second_revision = "sha256:" + "2" * 64
    catalog = RecordingSectorCatalog(
        SectorAssessmentBatch(
            assessments=(eligible_sector(),),
            discovered_count=1,
            completed_count=1,
            failure_counts=(),
            errors=(),
            catalog_revision=first_revision,
        )
    )
    clock_value = [AS_OF]
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=catalog,
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: clock_value[0],
        notifier=None,
    )

    first = service.refresh_now()
    catalog.batch = replace(catalog.batch, catalog_revision=second_revision)
    clock_value[0] = AS_OF + timedelta(minutes=10)
    second = service.refresh_now()

    assert first["coverage_manifest"]["sector_catalog_revision"] == (first_revision)
    assert second["coverage_manifest"]["sector_catalog_revision"] == (second_revision)
    assert second["coverage_epoch_id"] != first["coverage_epoch_id"]


def test_replacement_epoch_forces_every_current_eligible_sector_member(
    tmp_path: Path,
) -> None:
    """A new coverage ledger may not inherit the old incremental cursor.

    Even when the incremental planner reports no changed bars, changing the
    authenticated sector-catalog identity must replay every member in the
    currently eligible QMT sector scope before the new manifest can complete.
    """

    symbols = ("SZ.000001", "SZ.000002", "SZ.000003", "SZ.000004")
    first_revision = "sha256:" + "3" * 64
    second_revision = "sha256:" + "4" * 64
    catalog = MultiMemberSectorCatalog(symbols)
    catalog.batch = replace(catalog.batch, catalog_revision=first_revision)
    market = RecordingMarketData()
    clock_value = [AS_OF]
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=catalog,
        engine=RecordingEngine(),
        scan_planner=SequencedPlanner((symbols, ())),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: clock_value[0],
        notifier=None,
    )

    first = service.refresh_now()
    catalog.batch = replace(catalog.batch, catalog_revision=second_revision)
    clock_value[0] = AS_OF + timedelta(minutes=10)
    second = service.refresh_now()

    assert first["coverage_manifest"]["discovered_codes"] == list(symbols)
    assert second["coverage_epoch_id"] != first["coverage_epoch_id"]
    assert second["coverage_manifest"]["discovered_codes"] == list(symbols)
    assert second["coverage_manifest"]["completed_codes"] == list(symbols)
    assert second["coverage_manifest"]["complete"] is True
    assert market.bundle_codes == [*symbols, *symbols]


def test_sector_infrastructure_failures_below_gate_keep_previous_snapshot(
    tmp_path: Path,
) -> None:
    catalog = RecordingSectorCatalog()
    clock_value = [AS_OF]
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=catalog,
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: clock_value[0],
        notifier=None,
    )
    previous = service.refresh_now()
    successful = tuple(
        replace(eligible_sector(), sector_id=f"TDX.88030{index}") for index in range(7)
    )
    failures = tuple(
        SectorAnalysisFailure(
            sector_id=f"TDX.88039{index}",
            code=f"SH.88039{index}",
            error_type="sector_price_basis_unavailable",
            reason="industry index price basis unavailable",
        )
        for index in range(3)
    )
    catalog.batch = SectorAssessmentBatch(
        assessments=successful,
        discovered_count=10,
        completed_count=7,
        failure_counts=(("sector_price_basis_unavailable", 3),),
        errors=failures,
    )

    clock_value[0] = AS_OF + timedelta(minutes=10)
    payload = service.refresh_now()

    assert payload["scan_state"] == "incomplete_not_published"
    assert payload["sectors"] == previous["sectors"]
    assert payload["signals"] == previous["signals"]
    assert payload["scan_audit"]["sector_completion_ratio"] == "0.7"
    assert payload["data_quality"]["failure_codes"] == [
        "sector_scan_completion_below_threshold"
    ]


def test_business_ineligible_sectors_count_as_completed_and_can_publish_empty(
    tmp_path: Path,
) -> None:
    batch = SectorAssessmentBatch(
        assessments=(hostile_sector(),),
        discovered_count=1,
        completed_count=1,
        failure_counts=(),
        errors=(),
    )

    def empty_plan(**_kwargs) -> ScanPlan:
        return ScanPlan(
            sectors=(),
            symbols=(),
            symbol_frequencies=(),
            full_market_history_scan=False,
            background_full_refresh_required=False,
        )

    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(batch),
        engine=RecordingEngine(),
        scan_planner=empty_plan,
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
    )

    payload = service.refresh_now()

    assert payload["scan_state"] == "complete"
    assert payload["scan_audit"]["sector_completion_ratio"] == "1"
    assert payload["scan_audit"]["selected_sector_count"] == 0


def _sector_exclusion_batch() -> SectorAssessmentBatch:
    completed = tuple(
        replace(
            eligible_sector(),
            sector_id=f"qmt-gics3:eligible-{index}",
            sector_name=f"合格行业 {index}",
        )
        for index in range(4)
    )
    exclusion = SectorAnalysisExclusion(
        sector_id="qmt-gics3:small",
        code="GICS3小板块",
        reason_code="sector_member_coverage_insufficient",
        reason="catalog_members=7; universe_members=7; required=8",
        detail_code="sector_constituent_count_below_minimum",
        catalog_member_count=7,
        universe_member_count=7,
        required_member_count=8,
    )
    excluded_assessment = replace(
        hostile_sector(),
        sector_id=exclusion.sector_id,
        sector_name="小板块",
        reason_codes=(exclusion.reason_code, exclusion.detail_code),
    )
    return SectorAssessmentBatch(
        assessments=(*completed, excluded_assessment),
        discovered_count=5,
        completed_count=4,
        failure_counts=(),
        errors=(),
        exclusion_counts=(("sector_member_coverage_insufficient", 1),),
        exclusions=(exclusion,),
    )


def _empty_scan_plan(**_kwargs) -> ScanPlan:
    return ScanPlan(
        sectors=(),
        symbols=(),
        symbol_frequencies=(),
        full_market_history_scan=False,
        background_full_refresh_required=False,
    )


def test_member_history_diagnostics_are_required_by_current_cache_contract(
    tmp_path: Path,
) -> None:
    sector = eligible_sector()
    membership_revision = "sha256:" + "7" * 64
    evidence = build_horizontal_sector_strength_batch(
        decision_time=AS_OF,
        benchmark_symbol="SH.000300",
        benchmark_daily=(),
        members_by_sector={
            sector.sector_id: (
                SectorMemberHistory(
                    "SH.600000",
                    AS_OF.date(),
                    "UNEXPLAINED_GAP",
                    (),
                ),
            )
        },
        membership_revision=membership_revision,
    )
    strength = evidence[sector.sector_id]
    assessment = replace(
        sector,
        horizontal_strength=strength.strength,
        horizontal_rank=strength.rank,
        strength_anchor_session=strength.anchor_session,
        strength_member_count=strength.member_count,
        strength_source_revision=strength.source_revision,
        strength_reason_codes=strength.reason_codes,
    )
    batch = SectorAssessmentBatch(
        assessments=(assessment,),
        discovered_count=1,
        completed_count=1,
        failure_counts=(),
        errors=(),
        catalog_revision=membership_revision,
        strength_evidence=evidence,
    )
    cache_path = tmp_path / "snapshot.json"
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(batch),
        engine=RecordingEngine(),
        scan_planner=_empty_scan_plan,
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
    )

    payload = service.refresh_now()
    diagnostics = payload["sector_member_history_diagnostics"]

    assert diagnostics["evidence_revision"] == evidence.evidence_revision
    assert diagnostics["unique_symbol_status_counts"] == {
        "COMPLETE": 0,
        "NEW_LISTING": 0,
        "SUSPENDED": 0,
        "UNEXPLAINED_GAP": 1,
    }
    assert diagnostics["unexplained_gap_symbols"] == ["SH.600000"]
    assert service.health_snapshot()["sector_member_history_diagnostics"] == diagnostics
    assert _sector_source_evidence_complete(payload) is True

    forged = json.loads(json.dumps(payload))
    forged["sector_member_history_diagnostics"]["unique_symbol_status_counts"][
        "UNEXPLAINED_GAP"
    ] = 0
    forged["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(forged)
    assert _sector_source_evidence_complete(forged) is False

    noncurrent = json.loads(json.dumps(payload))
    noncurrent.pop("sector_member_history_diagnostics")
    noncurrent["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
        noncurrent
    )
    cache_path.write_text(
        json.dumps(noncurrent, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    restored_service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(batch),
        engine=RecordingEngine(),
        scan_planner=_empty_scan_plan,
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
    )
    restored = restored_service.snapshot()

    assert restored["scan_state"] == "not_started"
    assert restored["coverage_epoch_id"] is None
    assert restored["sector_member_history_diagnostics"] is None
    health = restored_service.health_snapshot()
    assert health["quarantined_cache_reason"] == (
        "CACHE_SCOPE_PROOF_MISSING_OR_INVALID"
    )


def test_sector_eligibility_exclusion_resolves_without_quality_failure(
    tmp_path: Path,
) -> None:
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(_sector_exclusion_batch()),
        engine=RecordingEngine(),
        scan_planner=_empty_scan_plan,
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
    )

    payload = service.refresh_now()

    assert payload["scan_state"] == "complete"
    assert payload["sector_coverage_contract_id"] == (SECTOR_COVERAGE_CONTRACT_ID)
    assert payload["errors"] == []
    assert len(payload["sector_exclusions"]) == 1
    assert payload["sector_exclusions"][0]["retry_policy"] == (
        "NEXT_SECTOR_CATALOG_REVISION"
    )
    assert payload["data_quality"] == {
        "complete": True,
        "stale": False,
        "failure_codes": [],
    }
    audit = payload["scan_audit"]
    assert audit["sector_discovered_count"] == 5
    assert audit["sector_completed_count"] == 4
    assert audit["sector_excluded_count"] == 1
    assert audit["sector_failed_count"] == 0
    assert audit["sector_resolved_count"] == 5
    assert audit["sector_completion_ratio"] == "0.8"
    assert audit["sector_resolution_ratio"] == "1"
    assert audit["sector_failure_counts"] == {}
    assert audit["sector_exclusion_counts"] == {
        "sector_member_coverage_insufficient": 1,
    }
    health = service.health_snapshot()
    assert health["sector_excluded_count"] == 1
    assert health["sector_failed_count"] == 0
    assert health["sector_resolution_ratio"] == "1"
    assert health["sector_exclusions"] == payload["sector_exclusions"]


def test_old_sector_failure_contract_is_rejected_without_migration(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "snapshot.json"
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(_sector_exclusion_batch()),
        engine=RecordingEngine(),
        scan_planner=_empty_scan_plan,
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(
            full_coverage_refresh_enabled=True,
            large_scope_authorized=True,
        ),
    )
    current = service.refresh_now()
    noncurrent = json.loads(json.dumps(current))
    [exclusion] = noncurrent.pop("sector_exclusions")
    noncurrent.pop("sector_coverage_contract_id")
    noncurrent["errors"] = [
        {
            "sector_id": exclusion["sector_id"],
            "code": exclusion["code"],
            "error_type": exclusion["reason_code"],
            "reason": exclusion["reason"],
            "detail_code": exclusion["detail_code"],
            "catalog_member_count": exclusion["catalog_member_count"],
            "universe_member_count": exclusion["universe_member_count"],
        }
    ]
    noncurrent_audit = noncurrent["scan_audit"]
    for field in (
        "sector_excluded_count",
        "sector_resolved_count",
        "sector_resolution_ratio",
        "sector_exclusion_counts",
    ):
        noncurrent_audit.pop(field)
    noncurrent_audit["sector_failed_count"] = 1
    noncurrent_audit["sector_failure_counts"] = {
        "sector_member_coverage_insufficient": 1,
    }
    noncurrent["data_quality"] = {
        "complete": False,
        "stale": False,
        "failure_codes": ["sector_scan_partial"],
    }
    noncurrent["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
        noncurrent
    )
    cache_path.write_text(
        json.dumps(noncurrent, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )

    restored_service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(_sector_exclusion_batch()),
        engine=RecordingEngine(),
        scan_planner=_empty_scan_plan,
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(
            full_coverage_refresh_enabled=True,
            large_scope_authorized=True,
        ),
    )
    restored = restored_service.snapshot()

    assert restored["scan_state"] == "not_started"
    assert restored["coverage_epoch_id"] is None
    assert restored["sector_exclusions"] == []
    persisted = json.loads(cache_path.read_text(encoding="utf-8"))
    assert persisted == noncurrent
    health = restored_service.health_snapshot()
    assert health["quarantined_cache_reason"] == "CURRENT_CACHE_CONTRACT_INVALID"


def test_rehashed_sector_disposition_forgery_is_not_restored(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "snapshot.json"
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(_sector_exclusion_batch()),
        engine=RecordingEngine(),
        scan_planner=_empty_scan_plan,
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
    )
    forged = service.refresh_now()
    forged["scan_audit"]["sector_excluded_count"] = 0
    forged["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(forged)
    cache_path.write_text(
        json.dumps(forged, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )

    restored = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(_sector_exclusion_batch()),
        engine=RecordingEngine(),
        scan_planner=_empty_scan_plan,
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
    ).snapshot()

    assert restored["available"] is False
    assert restored["scan_state"] == "not_started"
    assert restored["sector_exclusions"] == []


def test_service_scans_every_eligible_sector_without_a_top_n_cutoff(
    tmp_path: Path,
) -> None:
    assessments = tuple(
        replace(
            eligible_sector(),
            sector_id=f"qmt-gics3:{index:02d}",
            sector_name=f"QMT 行业 {index:02d}",
        )
        for index in range(12)
    )

    class AllEligibleCatalog(RecordingSectorCatalog):
        def __init__(self) -> None:
            self.batch = SectorAssessmentBatch(
                assessments=assessments,
                discovered_count=len(assessments),
                completed_count=len(assessments),
                failure_counts=(),
                errors=(),
            )

        def members(self):
            return {
                assessment.sector_id: (f"SH.60{index:04d}",)
                for index, assessment in enumerate(assessments)
            }

    captured: dict[str, object] = {}

    def empty_plan(**kwargs) -> ScanPlan:
        captured.update(kwargs)
        return ScanPlan(
            sectors=(),
            symbols=(),
            symbol_frequencies=(),
            full_market_history_scan=False,
            background_full_refresh_required=False,
        )

    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=AllEligibleCatalog(),
        engine=RecordingEngine(),
        scan_planner=empty_plan,
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
    )

    payload = service.refresh_now()

    assert payload["scan_audit"]["selected_sector_count"] == 12
    assert len(captured["sector_members"]) == 12
    assert set(captured["known_sector_ids"]) == {
        assessment.sector_id for assessment in assessments
    }
    assert [sector["rank"] for sector in payload["sectors"]] == list(range(1, 13))


def test_cache_with_another_schema_is_rejected(tmp_path: Path) -> None:
    cache_path = tmp_path / "snapshot.json"
    cache_path.write_text(
        json.dumps(
            {
                "schema": "chanlun-early-screening",
                "algorithm_id": "chanlun-original-low-drawdown",
                "read_only": True,
                "no_order_execution": True,
                "sectors": [],
                "signals": [{"signal_id": "noncurrent"}],
                "data_quality": {},
            }
        ),
        encoding="utf-8",
    )

    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
    )

    snapshot = service.snapshot()
    assert snapshot["schema"] == "chanlun-trading-screening"
    assert snapshot["structure_contract_id"] == "physical-timeframe-recursive"
    assert snapshot["scan_state"] == "not_started"
    assert snapshot["signals"] == []


def test_bounded_cache_rejects_missing_scope_proof_before_payload_read(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "snapshot.json"
    cache_path.write_text(
        "{this large legacy payload must not be parsed", encoding="utf-8"
    )
    original_read_text = Path.read_text

    def guarded_read_text(path: Path, *args, **kwargs):
        if path == cache_path:
            raise AssertionError(
                "bounded restore opened the payload before scope admission"
            )
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)
    config = TradingScreeningConfig(admitted_universe_codes=("SZ.000001",))

    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
        config=config,
    )

    assert service.snapshot()["scan_state"] == "not_started"
    assert service.health_snapshot()["quarantined_cache_reason"] == (
        "CACHE_SCOPE_PROOF_MISSING_OR_INVALID"
    )


def test_bounded_cache_rejects_stale_scope_proof_before_replaced_payload_read(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "snapshot.json"
    config = TradingScreeningConfig(admitted_universe_codes=("SZ.000001",))
    first = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
        config=config,
    )
    payload = first.snapshot()
    first._finalize_snapshot_identity(payload)
    first._persist_atomic(payload, cache_valid=False)
    assert first._cache_scope_sidecar_path(cache_path).is_file()

    cache_path.write_text("{replacement-is-not-the-proven-payload", encoding="utf-8")
    original_read_text = Path.read_text

    def guarded_read_text(path: Path, *args, **kwargs):
        if path == cache_path:
            raise AssertionError("stale sidecar admitted a replaced payload")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)
    restarted = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
        config=config,
    )

    assert restarted.snapshot()["scan_state"] == "not_started"


def test_bounded_writer_does_not_attest_full_market_payload(tmp_path: Path) -> None:
    cache_path = tmp_path / "snapshot.json"
    config = TradingScreeningConfig(admitted_universe_codes=("SZ.000001",))
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
        config=config,
    )
    payload = service.snapshot()
    service._finalize_snapshot_identity(payload)
    payload["screening_scope_mode"] = "FULL_MARKET"

    service._persist_atomic(payload, cache_valid=False)

    assert not service._cache_scope_sidecar_path(cache_path).exists()


def test_explicit_full_market_cache_keeps_direct_payload_restore_behavior(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "snapshot.json"
    cache_path.write_text("{}", encoding="utf-8")
    original_read_text = Path.read_text
    payload_reads = 0

    def recording_read_text(path: Path, *args, **kwargs):
        nonlocal payload_reads
        if path == cache_path:
            payload_reads += 1
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", recording_read_text)
    TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(
            full_coverage_refresh_enabled=True,
            large_scope_authorized=True,
        ),
    )

    assert payload_reads == 1


def test_full_market_snapshot_scope_ignores_sector_source_error_codes(
    tmp_path: Path,
) -> None:
    config = TradingScreeningConfig(
        full_coverage_refresh_enabled=True,
        large_scope_authorized=True,
    )
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
        config=config,
    )
    payload = service.snapshot()
    payload["coverage_manifest"]["discovered_codes"] = ["SZ.000001"]
    payload["errors"] = [
        {
            "phase": "sector_analysis",
            "code": "GICS3sector-source-key",
            "reason_code": "sector_catalog_failed",
            "details": {"symbol": "SH.600000"},
        }
    ]

    service._finalize_snapshot_identity(payload)

    assert payload["admitted_universe_codes"] == ["SZ.000001", "SH.600000"]
    assert trading_screening_subject._restored_snapshot_scope_is_valid(
        payload,
        config,
    )


def test_full_market_restart_rejects_bounded_generation_and_continuity(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "snapshot.json"
    bounded = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(priority_monitoring_enabled=True),
    )
    bounded_snapshot = bounded.refresh_now()
    generation_directory = tmp_path / ".snapshot.json.generations"
    generation_paths = tuple(generation_directory.glob("*.json"))

    assert bounded_snapshot["screening_scope_mode"] == "VALIDATION_COHORT"
    assert len(generation_paths) == 1
    assert bounded._cache_scope_sidecar_path(generation_paths[0]).is_file()
    cache_path.unlink()
    bounded._cache_scope_sidecar_path(cache_path).unlink()

    restarted = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(
            priority_monitoring_enabled=True,
            full_coverage_refresh_enabled=True,
            large_scope_authorized=True,
        ),
    )

    assert restarted.snapshot()["scan_state"] == "not_started"
    health = restarted.health_snapshot()
    assert health["cache_recovered_from_generation"] is None
    assert health["preselection_continuity_active"] is False


def test_full_market_restart_restores_full_market_generation(tmp_path: Path) -> None:
    cache_path = tmp_path / "snapshot.json"
    config = TradingScreeningConfig(
        priority_monitoring_enabled=True,
        full_coverage_refresh_enabled=True,
        large_scope_authorized=True,
    )
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
        config=config,
    )
    expected = service.refresh_now()
    cache_path.unlink()

    restarted = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
        config=config,
    )

    assert (
        restarted.snapshot()["snapshot_content_sha256"]
        == expected["snapshot_content_sha256"]
    )
    assert restarted.health_snapshot()["cache_recovered_from_generation"]


def test_startup_sector_cache_receives_exact_bounded_admission_before_restore(
    tmp_path: Path,
) -> None:
    admitted = ("SZ.000001", "SH.600000")

    class ScopedSectorCatalog(RecordingSectorCatalog):
        def __init__(self) -> None:
            super().__init__()
            self.scope_calls: list[dict[str, object]] = []

        def configure_sector_cache_restore_scope(self, **kwargs) -> None:
            self.scope_calls.append(dict(kwargs))

        def cached_sector_snapshot_for_priority(self, *, as_of: datetime):
            assert self.scope_calls
            return None

    catalog = ScopedSectorCatalog()
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=catalog,
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(
            admitted_universe_codes=admitted,
        ),
    )

    assert service._load_presentation_cached_sector_snapshot(observed_at=AS_OF) is None
    assert catalog.scope_calls == [
        {
            "scope_mode": "VALIDATION_COHORT",
            "max_symbols": 12,
            "admitted_codes": admitted,
        }
    ]


def test_new_validation_epoch_scans_the_complete_exact_cohort(
    tmp_path: Path,
) -> None:
    admitted = ("SZ.000001", "SZ.000002", "SZ.000003")
    market = RecordingMarketData()
    planner = RecordingPlanner((admitted[0],))
    catalog = RecordingSectorCatalog()
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=catalog,
        engine=RecordingEngine(),
        scan_planner=planner,
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(admitted_universe_codes=admitted),
    )

    snapshot = service.refresh_now()

    assert planner.calls == 1
    assert catalog.admitted_scope_calls == [admitted]
    assert market.bundle_codes == list(admitted)
    assert snapshot["coverage_manifest"]["discovered_codes"] == list(admitted)
    assert snapshot["coverage_manifest"]["completed_codes"] == list(admitted)
    assert snapshot["scan_audit"]["planned_symbol_count"] == len(admitted)


def test_cache_from_previous_partial_member_policy_is_rejected(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "snapshot.json"
    first = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
    )
    first.refresh_now()
    foreign = json.loads(cache_path.read_text(encoding="utf-8"))
    foreign["screening_policy"]["sector_strength_min_member_history_coverage"] = "0.80"
    foreign["screening_policy_id"] = sha256_json(foreign["screening_policy"])
    foreign["coverage_manifest"]["screening_policy_id"] = foreign["screening_policy_id"]
    first._finalize_snapshot_identity(foreign)
    cache_path.write_text(json.dumps(foreign), encoding="utf-8")

    restarted = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
    )

    assert restarted.snapshot()["scan_state"] == "not_started"


def test_cache_from_previous_decision_core_is_quarantined(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "snapshot.json"
    first = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
    )
    previous = first.refresh_now()

    class ReplacementEngine(RecordingEngine):
        pass

    restarted = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=ReplacementEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(full_coverage_refresh_enabled=False),
    )

    assert restarted.snapshot()["scan_state"] == "not_started"
    health = restarted.health_snapshot()
    assert (
        health["quarantined_cache_decision_core_id"] == (previous["decision_core_id"])
    )
    assert health["quarantined_cache_reason"] == ("DECISION_CORE_IDENTITY_MISMATCH")
    assert "screening_snapshot_unavailable" not in health["reasons"]


def test_cache_from_previous_decision_source_is_recomputed_not_reused(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "snapshot.json"
    first = TradingScreeningService(
        market_data=ActionableMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=HumanAssistedDecisionCore(formal_selection_required=False),
        scan_planner=RecordingPlanner(),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
    )
    published = first.refresh_now()
    assert [row["code"] for row in published["signals"]] == ["SZ.000001"]

    previous_source = "sha256:" + "9" * 64
    persisted = json.loads(cache_path.read_text(encoding="utf-8"))
    persisted["decision_source_snapshot_id"] = previous_source
    persisted["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
        persisted
    )
    cache_path.write_text(json.dumps(persisted), encoding="utf-8")

    restarted = TradingScreeningService(
        market_data=ActionableMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=HumanAssistedDecisionCore(formal_selection_required=False),
        scan_planner=RecordingPlanner(),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(
            full_coverage_refresh_enabled=False,
            priority_monitoring_enabled=True,
        ),
    )

    assert restarted.snapshot()["scan_state"] == "not_started"
    assert restarted.snapshot()["signals"] == []
    assert restarted._decision_rule_recheck_pending_codes == set()
    health = restarted.health_snapshot()
    assert health["quarantined_cache_reason"] == (
        "CACHE_SCOPE_PROOF_MISSING_OR_INVALID"
    )

    auto_recovery = TradingScreeningService(
        market_data=ActionableMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=HumanAssistedDecisionCore(formal_selection_required=False),
        scan_planner=RecordingPlanner(),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(priority_monitoring_enabled=True),
    )
    recovery_health = auto_recovery.health_snapshot()
    assert recovery_health["full_coverage_force_active"] is False
    assert recovery_health["full_coverage_auto_recovery_active"] is False
    assert recovery_health["full_coverage_auto_recovery_reason"] is None


def _previous_runtime_policy_source_snapshot(
    current: dict[str, object],
) -> dict[str, object]:
    previous = copy.deepcopy(current)
    runtime_row = next(
        row
        for row in previous["files"]
        if row["path"]
        == "web/chanlun_chart/cl_app/services/trading_screening_runtime_policy.py"
    )
    runtime_row["sha256"] = "sha256:" + "1" * 64
    previous["aggregate_sha256"] = sha256_json(
        {"schema": previous["schema"], "files": previous["files"]}
    )
    return previous


def _previous_suspension_evidence_source_snapshot(
    current: dict[str, object],
) -> dict[str, object]:
    previous = copy.deepcopy(current)
    screening_row = next(
        row
        for row in previous["files"]
        if row["path"] == "web/chanlun_chart/cl_app/services/trading_screening.py"
    )
    assert screening_row["sha256"] == (
        "sha256:401efa0ccbda18ec6bc203fbcac93a92ce6131dba602c70373e218918182e6e5"
    )
    screening_row["sha256"] = (
        "sha256:117e1e518f6c4417385e72f2ad9a911147192eb413543b7610550f1bbaebf8e3"
    )
    previous["aggregate_sha256"] = sha256_json(
        {"schema": previous["schema"], "files": previous["files"]}
    )
    return previous


def _reviewed_review_availability_source_snapshots(
    template: dict[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    current = copy.deepcopy(template)
    review_row = next(
        row
        for row in current["files"]
        if row["path"]
        == "src/chanlun/decision_support/trading_system/live_human_review.py"
    )
    review_row["sha256"] = (
        "sha256:4b5223d73c250f293940556ec858622b4e44fc8762fb2ff9e8893320dbb0bb56"
    )
    current["aggregate_sha256"] = sha256_json(
        {"schema": current["schema"], "files": current["files"]}
    )
    previous = copy.deepcopy(current)
    review_row = next(
        row
        for row in previous["files"]
        if row["path"]
        == "src/chanlun/decision_support/trading_system/live_human_review.py"
    )
    review_row["sha256"] = (
        "sha256:4e4ace9302d304a00373e01e659bb097677f8f3c9db5dfeb6bc57836215e8b84"
    )
    previous["aggregate_sha256"] = sha256_json(
        {"schema": previous["schema"], "files": previous["files"]}
    )
    return previous, current


def _previous_closed_session_bootstrap_source_snapshot(
    current: dict[str, object],
) -> dict[str, object]:
    previous = copy.deepcopy(current)
    screening_row = next(
        row
        for row in previous["files"]
        if row["path"] == "web/chanlun_chart/cl_app/services/trading_screening.py"
    )
    assert screening_row["sha256"] == (
        "sha256:401efa0ccbda18ec6bc203fbcac93a92ce6131dba602c70373e218918182e6e5"
    )
    screening_row["sha256"] = (
        "sha256:98b8373179fcb2c2ab772bc58975f832fc79c86c46880e4f8f34becf899a646f"
    )
    previous["aggregate_sha256"] = sha256_json(
        {"schema": previous["schema"], "files": previous["files"]}
    )
    return previous


def test_review_availability_source_migration_reuses_decision_snapshot(
    tmp_path: Path,
) -> None:
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
    )
    current = service._decision_source_snapshot
    assert isinstance(current, dict)
    previous, reviewed_current = _reviewed_review_availability_source_snapshots(current)
    previous_id = previous["aggregate_sha256"]
    current_id = reviewed_current["aggregate_sha256"]

    assert orchestration_source_migration_allowed(
        cached_decision_source_snapshot_id=previous_id,
        current_decision_source_snapshot_id=current_id,
        cached_decision_source_snapshot=previous,
        current_decision_source_snapshot=reviewed_current,
    )
    assert not suspension_evidence_recheck_source_migration_allowed(
        cached_decision_source_snapshot_id=previous_id,
        current_decision_source_snapshot_id=current_id,
        cached_decision_source_snapshot=previous,
        current_decision_source_snapshot=reviewed_current,
    )


def test_context_risk_role_source_change_requires_decision_recompute(
    tmp_path: Path,
) -> None:
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
    )
    current = service._decision_source_snapshot
    current_id = service._decision_source_snapshot_id
    assert isinstance(current, dict)
    assert isinstance(current_id, str)
    cached = copy.deepcopy(current)
    review_row = next(
        row
        for row in cached["files"]
        if row["path"]
        == "src/chanlun/decision_support/trading_system/live_human_review.py"
    )
    current_review_row = next(
        row
        for row in current["files"]
        if row["path"]
        == "src/chanlun/decision_support/trading_system/live_human_review.py"
    )
    assert current_review_row["sha256"] == (
        "sha256:7c04f3e18286adf36121ed88e04e0c9878ae7665c40e5f6914d647cc4051e615"
    )
    review_row["sha256"] = (
        "sha256:4b5223d73c250f293940556ec858622b4e44fc8762fb2ff9e8893320dbb0bb56"
    )
    cached["aggregate_sha256"] = sha256_json(
        {"schema": cached["schema"], "files": cached["files"]}
    )

    assert not orchestration_source_migration_allowed(
        cached_decision_source_snapshot_id=cached["aggregate_sha256"],
        current_decision_source_snapshot_id=current_id,
        cached_decision_source_snapshot=cached,
        current_decision_source_snapshot=current,
    )


def test_closed_session_bootstrap_source_migration_requires_no_stock_recheck(
    tmp_path: Path,
) -> None:
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
    )
    current = service._decision_source_snapshot
    current_id = service._decision_source_snapshot_id
    assert isinstance(current, dict)
    assert isinstance(current_id, str)
    previous = _previous_closed_session_bootstrap_source_snapshot(current)
    previous_id = previous["aggregate_sha256"]

    assert orchestration_source_migration_allowed(
        cached_decision_source_snapshot_id=previous_id,
        current_decision_source_snapshot_id=current_id,
        cached_decision_source_snapshot=previous,
        current_decision_source_snapshot=current,
    )
    assert not suspension_evidence_recheck_source_migration_allowed(
        cached_decision_source_snapshot_id=previous_id,
        current_decision_source_snapshot_id=current_id,
        cached_decision_source_snapshot=previous,
        current_decision_source_snapshot=current,
    )


def test_reviewed_orchestration_source_migration_reuses_complete_snapshot(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "snapshot.json"
    market = ActionableMarketData()
    first = TradingScreeningService(
        market_data=market,
        sector_catalog=RecordingSectorCatalog(),
        engine=HumanAssistedDecisionCore(formal_selection_required=False),
        scan_planner=RecordingPlanner(),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
    )
    published = first.refresh_now()
    current_source_id = first._decision_source_snapshot_id
    assert isinstance(current_source_id, str)
    persisted = json.loads(cache_path.read_text(encoding="utf-8"))
    previous_source = _previous_runtime_policy_source_snapshot(
        persisted["decision_source_snapshot"]
    )
    previous_source_id = previous_source["aggregate_sha256"]
    persisted["decision_source_snapshot_id"] = previous_source_id
    persisted["decision_source_snapshot"] = previous_source
    persisted["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
        persisted
    )
    legacy_snapshot_sha256 = persisted["snapshot_content_sha256"]
    cache_path.write_text(json.dumps(persisted), encoding="utf-8")
    first._persist_cache_scope_sidecar(cache_path, persisted)
    current_generations = first._generation_paths()
    assert len(current_generations) == 1
    for generation in current_generations:
        first._cache_scope_sidecar_path(generation).unlink(missing_ok=True)
        generation.unlink()
    legacy_generation = first._cache_generation_directory() / (
        legacy_snapshot_sha256.removeprefix("sha256:") + ".json"
    )
    legacy_generation.write_text(json.dumps(persisted), encoding="utf-8")
    first._persist_cache_scope_sidecar(legacy_generation, persisted)

    restarted_market = RecordingMarketData()
    restarted = TradingScreeningService(
        market_data=restarted_market,
        sector_catalog=RecordingSectorCatalog(),
        engine=HumanAssistedDecisionCore(formal_selection_required=False),
        scan_planner=RecordingPlanner(),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
    )

    restored = restarted.snapshot()
    health = restarted.health_snapshot()
    assert restarted_market.bundle_codes == []
    assert restored["available"] is True
    assert restored["scan_state"] == "complete"
    assert restored["signals"] == published["signals"]
    assert restored["decision_source_snapshot_id"] == current_source_id
    assert restored["snapshot_content_sha256"] != legacy_snapshot_sha256
    assert health["cache_decision_source_migrated_from"] == previous_source_id
    assert health["cache_decision_source_migration_persist_error"] is None
    on_disk = json.loads(cache_path.read_text(encoding="utf-8"))
    assert on_disk["decision_source_snapshot_id"] == current_source_id
    assert on_disk["snapshot_content_sha256"] == restored["snapshot_content_sha256"]
    assert not legacy_generation.exists()
    current_generations = restarted._generation_paths()
    assert len(current_generations) == 1
    assert current_generations[0].stem == restored[
        "snapshot_content_sha256"
    ].removeprefix("sha256:")

    # A physically missing main pointer must be able to recover the same
    # reviewed transition from the immutable generation instead of launching
    # a market-wide rebuild.
    for generation in current_generations:
        restarted._cache_scope_sidecar_path(generation).unlink(missing_ok=True)
        generation.unlink()
    legacy_generation.write_text(json.dumps(persisted), encoding="utf-8")
    restarted._persist_cache_scope_sidecar(legacy_generation, persisted)
    restarted._cache_scope_sidecar_path(cache_path).unlink(missing_ok=True)
    cache_path.unlink()
    generation_market = RecordingMarketData()
    recovered_from_generation = TradingScreeningService(
        market_data=generation_market,
        sector_catalog=RecordingSectorCatalog(),
        engine=HumanAssistedDecisionCore(formal_selection_required=False),
        scan_planner=RecordingPlanner(),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
    )
    generation_snapshot = recovered_from_generation.snapshot()
    generation_health = recovered_from_generation.health_snapshot()
    assert generation_market.bundle_codes == []
    assert generation_snapshot["available"] is True
    assert generation_snapshot["decision_source_snapshot_id"] == current_source_id
    assert generation_health["cache_recovered_from_generation"] == str(
        legacy_generation
    )
    assert (
        generation_health["cache_decision_source_migrated_from"] == previous_source_id
    )


def test_suspension_evidence_source_migration_rechecks_only_old_exclusions(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "snapshot.json"
    symbols = tuple(f"SZ.{index:06d}" for index in range(1, 6))
    first = TradingScreeningService(
        market_data=CurrentSessionSuspendedMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(symbols),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
    )
    published = first.refresh_now()
    assert published["coverage_manifest"]["excluded_codes"] == ["SZ.000002"]

    persisted = json.loads(cache_path.read_text(encoding="utf-8"))
    current_source = persisted["decision_source_snapshot"]
    current_source_id = persisted["decision_source_snapshot_id"]
    previous_source = _previous_suspension_evidence_source_snapshot(current_source)
    previous_source_id = previous_source["aggregate_sha256"]
    assert suspension_evidence_recheck_source_migration_allowed(
        cached_decision_source_snapshot_id=previous_source_id,
        current_decision_source_snapshot_id=current_source_id,
        cached_decision_source_snapshot=previous_source,
        current_decision_source_snapshot=current_source,
    )
    assert not suspension_evidence_recheck_source_migration_allowed(
        cached_decision_source_snapshot_id=current_source_id,
        current_decision_source_snapshot_id=previous_source_id,
        cached_decision_source_snapshot=current_source,
        current_decision_source_snapshot=previous_source,
    )
    persisted["decision_source_snapshot_id"] = previous_source_id
    persisted["decision_source_snapshot"] = previous_source
    persisted["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
        persisted
    )
    cache_path.write_text(json.dumps(persisted), encoding="utf-8")
    first._persist_cache_scope_sidecar(cache_path, persisted)
    for generation in first._generation_paths():
        first._cache_scope_sidecar_path(generation).unlink(missing_ok=True)
        generation.unlink()

    migrated_market = PositiveStatusTradingMarketData()
    migrated_planner = RecordingPlanner(())
    migrated = TradingScreeningService(
        market_data=migrated_market,
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=migrated_planner,
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
    )
    migrated_health = migrated.health_snapshot()
    assert migrated_market.bundle_codes == []
    assert migrated.snapshot()["available"] is True
    assert migrated_health["cache_decision_source_recheck_codes"] == ["SZ.000002"]
    assert migrated_health["cache_decision_source_recheck_pending_codes"] == [
        "SZ.000002"
    ]
    on_disk = json.loads(cache_path.read_text(encoding="utf-8"))
    assert on_disk["source_migration_suspension_evidence_recheck_codes"] == [
        "SZ.000002"
    ]

    durable_market = PositiveStatusTradingMarketData()
    durable_planner = RecordingPlanner(())
    durable = TradingScreeningService(
        market_data=durable_market,
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=durable_planner,
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
    )
    assert durable_market.bundle_codes == []
    assert (
        durable.health_snapshot()["cache_decision_source_recheck_pending_code_count"]
        == 1
    )

    refreshed = durable.refresh_now()
    refreshed_health = durable.health_snapshot()

    assert durable_planner.calls == 0
    assert durable_market.bundle_codes == ["SZ.000002"]
    assert refreshed["coverage_manifest"]["completed_codes"] == list(symbols)
    assert refreshed["coverage_manifest"]["excluded_codes"] == []
    assert "source_migration_suspension_evidence_recheck_codes" not in refreshed
    assert refreshed_health["cache_decision_source_recheck_code_count"] == 1
    assert refreshed_health["cache_decision_source_recheck_pending_code_count"] == 0


def test_reviewed_source_migration_rejects_incomplete_snapshot_without_mutation(
    tmp_path: Path,
) -> None:
    service = TradingScreeningService(
        market_data=ActionableMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=HumanAssistedDecisionCore(formal_selection_required=False),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
    )
    legacy = service.refresh_now()
    previous_source = _previous_runtime_policy_source_snapshot(
        legacy["decision_source_snapshot"]
    )
    legacy["decision_source_snapshot_id"] = previous_source["aggregate_sha256"]
    legacy["decision_source_snapshot"] = previous_source
    legacy["scan_state"] = "in_progress"
    legacy["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(legacy)
    original = copy.deepcopy(legacy)

    assert service._operationally_migrated_cache(legacy) is None
    assert legacy == original


def test_previous_close_preselection_continuity_keeps_current_notifications_live(
    tmp_path: Path,
) -> None:
    symbols = ("SZ.000001", "SZ.000002")
    cache_path = tmp_path / "snapshot.json"
    batch = _evidence_sector_batch(symbols, context_revision="source-transition")
    first = TradingScreeningService(
        market_data=ActionableMarketData(),
        sector_catalog=EvidenceSectorCatalog(batch, symbols),
        engine=HumanAssistedDecisionCore(formal_selection_required=False),
        scan_planner=SequencedPlanner((symbols,)),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(
            priority_monitoring_enabled=True,
            full_coverage_refresh_enabled=True,
            large_scope_authorized=True,
        ),
    )
    published = first.refresh_now()
    assert published["scan_state"] == "complete"
    assert {row["code"] for row in published["signals"]} == set(symbols)

    persisted = json.loads(cache_path.read_text(encoding="utf-8"))
    previous_source = persisted["decision_source_snapshot"]
    changed_source = next(
        row
        for row in previous_source["files"]
        if row["path"] == "web/chanlun_chart/cl_app/services/trading_screening.py"
    )
    changed_source["sha256"] = "sha256:" + "9" * 64
    previous_source["aggregate_sha256"] = sha256_json(
        {
            "schema": previous_source["schema"],
            "files": previous_source["files"],
        }
    )
    persisted["decision_source_snapshot_id"] = previous_source["aggregate_sha256"]
    persisted["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
        persisted
    )
    cache_path.write_text(json.dumps(persisted), encoding="utf-8")

    now = [AS_OF + timedelta(hours=18, minutes=35)]

    class CurrentSessionActionableMarket(ActionableMarketData):
        def structure_bundle(
            self,
            code: str,
            *,
            as_of: datetime,
            sector,
            frequencies=(),
        ) -> SymbolStructureBundle:
            del frequencies
            self.bundle_codes.append(code)
            return SymbolStructureBundle(
                code=code,
                as_of=as_of,
                sector=sector,
                thirty_direction="neutral",
                thirty_points=(),
                five_points=(confirmed_point("2buy", code=code, minutes_after=1410),),
                one_points=(),
                opposite_points=(),
            )

    class RecordingNotifier:
        def __init__(self) -> None:
            self.calls: list[tuple[dict[str, object], dict[str, object]]] = []

        def dispatch_changes(self, previous, current) -> None:
            self.calls.append((dict(previous), dict(current)))

    market = CurrentSessionActionableMarket()
    catalog = HydratingEvidenceSectorCatalog(batch, symbols)
    notifier = RecordingNotifier()
    restarted = TradingScreeningService(
        market_data=market,
        sector_catalog=catalog,
        engine=HumanAssistedDecisionCore(formal_selection_required=False),
        scan_planner=SequencedPlanner((symbols,)),
        cache_path=cache_path,
        clock=lambda: now[0],
        notifier=notifier,
        config=TradingScreeningConfig(
            priority_monitoring_enabled=True,
            full_coverage_refresh_enabled=True,
            large_scope_authorized=True,
        ),
    )

    assert restarted.snapshot()["signals"] == []
    before = restarted.health_snapshot()
    assert before["preselection_continuity_active"] is True
    assert before["preselection_continuity_target_session"] == (
        now[0].date().isoformat()
    )
    assert before["preselection_continuity_signal_code_count"] == len(symbols)
    assert before["preselection_continuity_supportive_code_count"] == len(symbols)
    assert before["full_coverage_force_active"] is False
    assert before["full_coverage_refresh_window_open"] is False
    assert before["full_coverage_deferred_for_preselection_continuity"] is True

    restarted.refresh_now(priority_only=True)

    assert set(market.bundle_codes) == set(symbols)
    assert catalog.assessment_calls == []
    assert len(catalog.restore_calls) == 1
    assert notifier.calls
    authoritative_codes = notifier.calls[-1][1]["notification_authoritative_codes"]
    assert set(authoritative_codes) == set(symbols)
    presentation = restarted.presentation_snapshot()
    assert any(
        row.get("code") in symbols
        and "PRESELECTION_CONTINUITY_RECHECK" in row.get("selection_sources", ())
        for row in presentation["signals"]
    )
    health = restarted.health_snapshot()
    assert health["priority_monitor_sector_source_mode"] == ("PRESELECTION_CONTINUITY")
    assert health["candidate_monitor_five_minute"]["universe_count"] == len(symbols)
    assert restarted.snapshot()["signals"] == []

    # The full rebuild resumes at the existing post-close window; continuity
    # protects only the live/pre-open candidate workers, not the daily rebuild.
    now[0] = now[0].replace(hour=15, minute=5)
    after_close = restarted.health_snapshot()
    assert after_close["full_coverage_deferred_for_preselection_continuity"] is False
    assert after_close["full_coverage_force_active"] is True
    assert after_close["full_coverage_refresh_window_open"] is True


def test_preselection_continuity_rejects_wrong_session_and_tampered_main_cache(
    tmp_path: Path,
) -> None:
    symbols = ("SZ.000001",)
    batch = _evidence_sector_batch(symbols, context_revision="continuity-rejection")

    wrong_session_path = tmp_path / "wrong-session" / "snapshot.json"
    wrong_session_path.parent.mkdir()
    wrong_session = TradingScreeningService(
        market_data=ActionableMarketData(),
        sector_catalog=EvidenceSectorCatalog(batch, symbols),
        engine=HumanAssistedDecisionCore(formal_selection_required=False),
        scan_planner=SequencedPlanner((symbols,)),
        cache_path=wrong_session_path,
        clock=lambda: AS_OF,
        notifier=None,
    )
    wrong_session.refresh_now()
    persisted = json.loads(wrong_session_path.read_text(encoding="utf-8"))
    persisted["decision_source_snapshot_id"] = "sha256:" + "8" * 64
    persisted["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
        persisted
    )
    wrong_session_path.write_text(json.dumps(persisted), encoding="utf-8")
    misaligned = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=HydratingEvidenceSectorCatalog(batch, symbols),
        engine=HumanAssistedDecisionCore(formal_selection_required=False),
        scan_planner=SequencedPlanner((symbols,)),
        cache_path=wrong_session_path,
        clock=lambda: AS_OF + timedelta(days=2),
        notifier=None,
        config=TradingScreeningConfig(priority_monitoring_enabled=True),
    )
    assert misaligned.health_snapshot()["preselection_continuity_active"] is False

    tampered_path = tmp_path / "tampered" / "snapshot.json"
    tampered_path.parent.mkdir()
    tampered = TradingScreeningService(
        market_data=ActionableMarketData(),
        sector_catalog=EvidenceSectorCatalog(batch, symbols),
        engine=HumanAssistedDecisionCore(formal_selection_required=False),
        scan_planner=SequencedPlanner((symbols,)),
        cache_path=tampered_path,
        clock=lambda: AS_OF,
        notifier=None,
    )
    tampered.refresh_now()
    forged = json.loads(tampered_path.read_text(encoding="utf-8"))
    forged["decision_source_snapshot_id"] = "sha256:" + "7" * 64
    # Deliberately retain the old content hash.  A valid immutable generation
    # must not disguise a parseable, tampered main cache.
    tampered_path.write_text(json.dumps(forged), encoding="utf-8")
    rejected = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=HydratingEvidenceSectorCatalog(batch, symbols),
        engine=HumanAssistedDecisionCore(formal_selection_required=False),
        scan_planner=SequencedPlanner((symbols,)),
        cache_path=tampered_path,
        clock=lambda: AS_OF + timedelta(hours=18, minutes=35),
        notifier=None,
        config=TradingScreeningConfig(priority_monitoring_enabled=True),
    )
    assert rejected.health_snapshot()["preselection_continuity_active"] is False


def test_completed_continuity_recheck_leaves_recurring_candidate_rotation(
    tmp_path: Path,
) -> None:
    market = RecordingMarketData()
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF.replace(hour=14, minute=58),
        notifier=None,
        config=TradingScreeningConfig(
            full_coverage_refresh_enabled=False,
            priority_monitoring_enabled=True,
        ),
    )

    # This code has already left the one-off recheck queue and has no current
    # signal or supportive-sector route.  The immutable continuity provenance
    # may still contain it, but it must not consume every later 5m rotation.
    assert service._decision_rule_recheck_pending_codes == set()
    service._run_priority_monitor(
        previous=service.snapshot(),
        observed_at=AS_OF.replace(hour=14, minute=58),
        preselection_continuity_codes=("SZ.000002",),
    )

    assert market.bundle_codes == []
    assert (
        service.health_snapshot()["candidate_monitor_five_minute"]["universe_count"]
        == 0
    )


def test_current_unavailable_checkpoint_rejects_stale_generation_scope_proofs(
    tmp_path: Path,
) -> None:
    symbols = ("SZ.000001",)
    cache_path = tmp_path / "snapshot.json"
    batch = _evidence_sector_batch(symbols, context_revision="generation-transition")
    config = TradingScreeningConfig(priority_monitoring_enabled=True)
    first = TradingScreeningService(
        market_data=ActionableMarketData(),
        sector_catalog=EvidenceSectorCatalog(batch, symbols),
        engine=HumanAssistedDecisionCore(formal_selection_required=False),
        scan_planner=SequencedPlanner((symbols,)),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
        config=config,
    )
    published = first.refresh_now()
    generation_dir = cache_path.with_name(f".{cache_path.name}.generations")
    generation_paths = tuple(generation_dir.glob("*.json"))
    assert generation_paths
    for path in generation_paths:
        generation = json.loads(path.read_text(encoding="utf-8"))
        generation["decision_source_snapshot_id"] = "sha256:" + "6" * 64
        generation["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
            generation
        )
        path.write_text(json.dumps(generation), encoding="utf-8")

    checkpoint = trading_screening_subject._initial_snapshot(
        config,
        selection_research_revision=first._selection_research_revision,
        decision_source_snapshot_id=first._decision_source_snapshot_id,
    )
    checkpoint["decision_core_id"] = first._decision_core_id
    checkpoint["decision_core"] = published["decision_core"]
    checkpoint["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
        checkpoint
    )
    cache_path.write_text(json.dumps(checkpoint), encoding="utf-8")

    restarted = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=HydratingEvidenceSectorCatalog(batch, symbols),
        engine=HumanAssistedDecisionCore(formal_selection_required=False),
        scan_planner=SequencedPlanner((symbols,)),
        cache_path=cache_path,
        clock=lambda: AS_OF + timedelta(hours=18, minutes=35),
        notifier=None,
        config=config,
    )

    health = restarted.health_snapshot()
    assert restarted.snapshot()["scan_state"] == "not_started"
    assert health["preselection_continuity_active"] is False
    assert health["preselection_continuity_source_name"] is None


def test_previous_core_snapshot_seeds_bounded_rule_recheck_without_reusing_results(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "snapshot.json"
    old = TradingScreeningService(
        market_data=ActionableMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=HumanAssistedDecisionCore(formal_selection_required=False),
        scan_planner=RecordingPlanner(),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
    )
    published = old.refresh_now()
    assert [row["code"] for row in published["signals"]] == ["SZ.000001"]

    class ReplacementEngine(RecordingEngine):
        def __init__(self) -> None:
            super().__init__()
            self._delegate = HumanAssistedDecisionCore()

        def evaluate_symbol(self, bundle: SymbolStructureBundle):
            self.codes.append(bundle.code)
            self.bundles.append(bundle)
            return self._delegate.evaluate_symbol(bundle)

    restarted = TradingScreeningService(
        market_data=ActionableMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=ReplacementEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=cache_path,
        clock=lambda: AS_OF.replace(hour=14, minute=58),
        notifier=None,
        config=TradingScreeningConfig(
            full_coverage_refresh_enabled=False,
            priority_monitoring_enabled=True,
        ),
    )

    assert restarted.snapshot()["signals"] == []
    assert restarted._decision_rule_recheck_pending_codes == {"SZ.000001"}
    health = restarted.health_snapshot()
    assert health["decision_rule_recheck_pending_count"] == 1
    assert health["decision_rule_recheck_status"] == "pending"

    restarted._run_priority_monitor(
        previous=restarted.snapshot(), observed_at=AS_OF.replace(hour=14, minute=58)
    )

    assert restarted._decision_rule_recheck_pending_codes == set()
    assert restarted.snapshot()["signals"] == []
    presentation = restarted.presentation_snapshot()
    assert presentation["candidate_live_overlay"]["live"] is True
    assert presentation["candidate_live_overlay"]["signal_count"] >= 1
    assert any(
        row.get("code") == "SZ.000001"
        and "DECISION_RULE_RECHECK" in row.get("selection_sources", ())
        and row.get("observation_lane") == "CANDIDATE_CURRENT_5M"
        for row in presentation["signals"]
    )
    persisted = json.loads(
        (tmp_path / "trading_priority_monitor_state.json").read_text(encoding="utf-8")
    )
    assert persisted["decision_rule_recheck_pending_codes"] == []
    assert (
        persisted["decision_rule_recheck_source_snapshot_sha256"]
        == (published["snapshot_content_sha256"])
    )


def test_complete_current_core_snapshot_supersedes_old_rule_recheck_backlog(
    tmp_path: Path,
) -> None:
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(priority_monitoring_enabled=True),
    )
    service._decision_rule_recheck_source_snapshot_sha256 = "sha256:" + "6" * 64
    service._decision_rule_recheck_source_core_id = "sha256:" + "5" * 64
    service._decision_rule_recheck_pending_codes.update(("SZ.000001", "SZ.000002"))
    service._preselection_continuity_source_snapshot_sha256 = "sha256:" + "4" * 64
    service._preselection_continuity_signal_codes = ("SZ.000001",)

    published = service.refresh_now()

    assert published["decision_core_id"] == service._decision_core_id
    assert published["coverage_manifest"]["complete"] is True
    assert published["coverage_manifest"]["failed_codes"] == []
    assert service._decision_rule_recheck_pending_codes == set()
    assert service.health_snapshot()["preselection_continuity_active"] is False
    assert service._preselection_continuity_signal_codes == ()
    persisted = json.loads(
        (tmp_path / "trading_priority_monitor_state.json").read_text(encoding="utf-8")
    )
    assert persisted["decision_rule_recheck_pending_codes"] == []


def test_partial_current_core_checkpoint_retires_finalized_rule_rechecks(
    tmp_path: Path,
) -> None:
    symbols = ("SZ.000001", "SZ.000002", "SZ.000003")
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=MultiMemberSectorCatalog(symbols),
        engine=RecordingEngine(),
        scan_planner=SequencedPlanner((symbols,)),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(
            max_symbols_per_refresh=1,
            priority_monitoring_enabled=False,
        ),
    )
    service._decision_rule_recheck_source_snapshot_sha256 = "sha256:" + "6" * 64
    service._decision_rule_recheck_source_core_id = "sha256:" + "5" * 64
    service._decision_rule_recheck_pending_codes.update(symbols)
    service._preselection_continuity_source_snapshot_sha256 = "sha256:" + "4" * 64
    service._preselection_continuity_signal_codes = symbols

    checkpoint = service.refresh_now()

    assert checkpoint["coverage_manifest"]["complete"] is False
    assert checkpoint["coverage_manifest"]["completed_codes"] == [symbols[0]]
    assert service._decision_rule_recheck_pending_codes == set(symbols[1:])
    assert service.health_snapshot()["preselection_continuity_active"] is True


def test_disabled_full_coverage_priority_monitor_never_builds_sector_snapshot(
    tmp_path: Path,
) -> None:
    """暂停全量覆盖后，优先通道没有缓存也必须继续个股复查。"""

    class WatchlistMarket(RecordingMarketData):
        def active_watchlist(self) -> tuple[str, ...]:
            return ("SZ.000001",)

    class NoRebuildCatalog(RecordingSectorCatalog):
        def native_sector_assessments(
            self,
            *,
            as_of: datetime,
            admitted_codes=None,
        ):
            del admitted_codes
            raise AssertionError(f"盘中不得重建板块快照：{as_of.isoformat()}")

    market = WatchlistMarket()
    engine = RecordingEngine()
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=NoRebuildCatalog(),
        engine=engine,
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF.replace(hour=14, minute=58),
        notifier=None,
        config=TradingScreeningConfig(
            full_coverage_refresh_enabled=False,
            priority_monitoring_enabled=True,
        ),
    )

    service._run_priority_monitor(
        previous=service.snapshot(),
        observed_at=AS_OF.replace(hour=14, minute=58),
    )

    assert market.bundle_codes == ["SZ.000001"]
    assert engine.bundles[0].sector.sector_id == "unclassified"
    assert engine.bundles[0].sector.hard_block is True
    health = service.health_snapshot()
    assert health["priority_monitor_sector_source_mode"] == (
        "UNCLASSIFIED_SECTOR_FAIL_CLOSED"
    )
    assert health["priority_monitor_last_error_count"] == 0


def test_stale_priority_sector_cache_preserves_members_but_blocks_buy_scope(
    tmp_path: Path,
) -> None:
    """旧板块事实可用于定位标的，但不能扩大支持板块的买入发现范围。"""

    code = "SZ.000001"
    cached_at = AS_OF.replace(hour=14, minute=50)
    observed_at = cached_at + timedelta(minutes=8)
    supportive = replace(eligible_sector(), regime="supportive")
    batch = SectorAssessmentBatch(
        assessments=(supportive,),
        discovered_count=1,
        completed_count=1,
        failure_counts=(),
        errors=(),
    )

    class CachedCatalog(RecordingSectorCatalog):
        def cached_sector_snapshot_for_priority(self, *, as_of: datetime):
            assert as_of == observed_at
            return CachedSectorSnapshot(
                batch=batch,
                members={supportive.sector_id: (code,)},
                requested_as_of=cached_at,
                current_decision_epoch=False,
                content_sha256="sha256:" + "7" * 64,
            )

        def native_sector_assessments(
            self,
            *,
            as_of: datetime,
            admitted_codes=None,
        ):
            del admitted_codes
            raise AssertionError(f"盘中不得重建板块快照：{as_of.isoformat()}")

    market = RecordingMarketData()
    engine = RecordingEngine()
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=CachedCatalog(),
        engine=engine,
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: observed_at,
        notifier=None,
        config=TradingScreeningConfig(
            full_coverage_refresh_enabled=False,
            priority_monitoring_enabled=True,
        ),
    )
    service._decision_rule_recheck_pending_codes.add(code)

    service._run_priority_monitor(
        previous=service.snapshot(),
        observed_at=observed_at,
    )

    assert market.bundle_codes == [code]
    sector = engine.bundles[0].sector
    assert sector.sector_id == supportive.sector_id
    assert sector.hard_block is True
    assert "priority_sector_snapshot_stale" in sector.reason_codes
    assert "QMT_SECTOR_TRIGGER" not in engine.bundles[0].selection_sources
    assert "DECISION_RULE_RECHECK" in engine.bundles[0].selection_sources
    health = service.health_snapshot()
    assert health["priority_monitor_sector_source_mode"] == (
        "STALE_CACHED_SECTOR_SNAPSHOT_FAIL_CLOSED"
    )


def test_rule_recheck_progress_is_restored_without_reseeding_completed_codes(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "snapshot.json"
    old = TradingScreeningService(
        market_data=ActionableMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=HumanAssistedDecisionCore(formal_selection_required=False),
        scan_planner=RecordingPlanner(),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
    )
    published = old.refresh_now()
    assert [row["code"] for row in published["signals"]] == ["SZ.000001"]

    class ReplacementEngine(RecordingEngine):
        pass

    first = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=ReplacementEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(
            full_coverage_refresh_enabled=False,
            priority_monitoring_enabled=True,
        ),
    )
    first._decision_rule_recheck_pending_codes.clear()
    first._persist_priority_monitor_state()

    restarted = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=ReplacementEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(
            full_coverage_refresh_enabled=False,
            priority_monitoring_enabled=True,
        ),
    )

    assert restarted._decision_rule_recheck_pending_codes == set()
    assert restarted.health_snapshot()["decision_rule_recheck_status"] == "complete"


def test_cache_from_previous_coverage_state_contract_is_rejected(
    tmp_path: Path,
) -> None:
    """A state-machine migration must rebuild, not restore old retry queues."""

    cache_path = tmp_path / "snapshot.json"
    first = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
    )
    first.refresh_now()
    noncurrent = json.loads(cache_path.read_text(encoding="utf-8"))
    noncurrent["coverage_manifest"].pop("coverage_state_contract_id")
    first._finalize_snapshot_identity(noncurrent)
    cache_path.write_text(json.dumps(noncurrent), encoding="utf-8")

    restarted = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
    )

    assert restarted.snapshot()["scan_state"] == "not_started"
    assert restarted.snapshot()["coverage_epoch_id"] is None


def test_noncurrent_signal_document_contract_replays_full_current_universe(
    tmp_path: Path,
) -> None:
    """An output-schema upgrade may not leave old/new signal rows mixed.

    The previous complete snapshot remains readable during migration, but the
    old coverage epoch is superseded and every current planned symbol enters a
    resumable queue.  This is an output-evidence migration only: decision and
    screening-policy identities remain unchanged.
    """

    cache_path = tmp_path / "snapshot.json"
    symbols = ("SZ.000001", "SZ.000002", "SZ.000003")
    first = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=SequencedPlanner((symbols,)),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
    )
    current = first.refresh_now()
    decision_core_id = current["decision_core_id"]
    screening_policy_id = current["screening_policy_id"]

    noncurrent = json.loads(cache_path.read_text(encoding="utf-8"))
    noncurrent["signal_document_contract_id"] = "chanlun-human-assisted-signal-document"
    noncurrent["coverage_manifest"]["signal_document_contract_id"] = (
        "chanlun-human-assisted-signal-document"
    )
    noncurrent_epoch = sha256_json({"schema": "noncurrent-signal-document"})
    noncurrent["coverage_epoch_id"] = noncurrent_epoch
    noncurrent["coverage_manifest"]["coverage_epoch_id"] = noncurrent_epoch
    first._finalize_snapshot_identity(noncurrent)
    cache_path.write_text(json.dumps(noncurrent), encoding="utf-8")

    planner_calls: list[object] = []

    def full_plan(**kwargs) -> ScanPlan:
        planner_calls.append(kwargs["previous"])
        return ScanPlan(
            sectors=(eligible_sector().sector_id,),
            symbols=symbols,
            symbol_frequencies=tuple(
                (code, ("1m", "5m", "30m", "d")) for code in symbols
            ),
            full_market_history_scan=False,
            background_full_refresh_required=True,
        )

    restarted = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=full_plan,
        cache_path=cache_path,
        clock=lambda: AS_OF + timedelta(minutes=10),
        notifier=None,
        config=TradingScreeningConfig(max_symbols_per_refresh=1),
    )

    migrated = restarted.refresh_now()

    assert planner_calls and planner_calls[0].initialized is False
    assert migrated["signal_document_contract_id"] == (SIGNAL_DOCUMENT_CONTRACT_ID)
    assert migrated["coverage_manifest"]["signal_document_contract_id"] == (
        SIGNAL_DOCUMENT_CONTRACT_ID
    )
    assert migrated["coverage_epoch_id"] != noncurrent_epoch
    assert migrated["coverage_manifest"]["complete"] is False
    assert set(migrated["coverage_manifest"]["pending_frequencies"]) == {
        "SZ.000002",
        "SZ.000003",
    }
    assert migrated["decision_core_id"] == decision_core_id
    assert migrated["screening_policy_id"] == screening_policy_id

    restarted.refresh_now()
    completed = restarted.refresh_now()
    assert completed["coverage_manifest"]["complete"] is True
    assert completed["coverage_manifest"]["completed_codes"] == list(symbols)


def test_incomplete_noncurrent_contract_does_not_restore_its_pending_epoch(
    tmp_path: Path,
) -> None:
    """A schema migration must restart even when the old queue is unfinished.

    Restoring the old pending queue used to skip ``_begin_coverage_cycle``:
    the service then emitted the current contract while retaining the previous
    contract's epoch ID and already-completed symbol set.
    """

    cache_path = tmp_path / "snapshot.json"
    symbols = ("SZ.000001", "SZ.000002", "SZ.000003")
    first = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(symbols),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(max_symbols_per_refresh=1),
    )
    incomplete = first.refresh_now()
    assert incomplete["coverage_manifest"]["complete"] is False
    assert incomplete["coverage_manifest"]["completed_codes"] == ["SZ.000001"]

    noncurrent = json.loads(cache_path.read_text(encoding="utf-8"))
    noncurrent_contract = "chanlun-human-assisted-signal-document"
    noncurrent_epoch = sha256_json({"schema": "noncurrent-incomplete-epoch"})
    noncurrent["signal_document_contract_id"] = noncurrent_contract
    noncurrent["coverage_epoch_id"] = noncurrent_epoch
    noncurrent["coverage_manifest"]["signal_document_contract_id"] = noncurrent_contract
    noncurrent["coverage_manifest"]["coverage_epoch_id"] = noncurrent_epoch
    first._finalize_snapshot_identity(noncurrent)
    cache_path.write_text(json.dumps(noncurrent), encoding="utf-8")

    planner_cursors: list[object] = []

    def full_plan(**kwargs) -> ScanPlan:
        planner_cursors.append(kwargs["previous"])
        return ScanPlan(
            sectors=(eligible_sector().sector_id,),
            symbols=symbols,
            symbol_frequencies=tuple(
                (code, ("1m", "5m", "30m", "d")) for code in symbols
            ),
            full_market_history_scan=False,
            background_full_refresh_required=True,
        )

    market = RecordingMarketData()
    restarted = TradingScreeningService(
        market_data=market,
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=full_plan,
        cache_path=cache_path,
        clock=lambda: AS_OF + timedelta(minutes=10),
        notifier=None,
        config=TradingScreeningConfig(max_symbols_per_refresh=1),
    )
    migrated = restarted.refresh_now()

    assert planner_cursors and planner_cursors[0].initialized is False
    assert market.bundle_codes == ["SZ.000001"]
    assert migrated["coverage_epoch_id"] != noncurrent_epoch
    assert migrated["coverage_manifest"]["batch_count"] == 1
    assert migrated["coverage_manifest"]["completed_codes"] == ["SZ.000001"]
    assert set(migrated["coverage_manifest"]["pending_frequencies"]) == {
        "SZ.000002",
        "SZ.000003",
    }


def test_old_signal_contract_is_rebuilt_without_reusing_signal_rows(
    tmp_path: Path,
) -> None:
    """A replacement epoch may publish only signals recomputed in that epoch.

    Same-epoch incremental batches deliberately retain current signals for
    unscanned symbols.  Carrying that rule across a signal-document migration,
    however, labels old evidence with the new contract until the full queue
    drains.  The incomplete snapshot is not forward-publishable, but its page
    must still be honest about which symbols the replacement epoch has scanned.
    """

    cache_path = tmp_path / "snapshot.json"
    symbols = ("SZ.000001", "SZ.000002", "SZ.000003")
    catalog = MultiMemberSectorCatalog(symbols)

    def full_plan(**_kwargs) -> ScanPlan:
        return ScanPlan(
            sectors=(eligible_sector().sector_id,),
            symbols=symbols,
            symbol_frequencies=tuple(
                (code, ("1m", "5m", "30m", "d")) for code in symbols
            ),
            full_market_history_scan=False,
            background_full_refresh_required=True,
        )

    first = TradingScreeningService(
        market_data=ActionableMarketData(),
        sector_catalog=catalog,
        engine=HumanAssistedDecisionCore(),
        scan_planner=full_plan,
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(max_symbols_per_refresh=2),
    )
    current = first.refresh_now()
    assert {row["code"] for row in current["signals"]} == {
        "SZ.000001",
        "SZ.000002",
    }

    noncurrent = json.loads(cache_path.read_text(encoding="utf-8"))
    unscanned_noncurrent_signal = dict(noncurrent["signals"][0])
    unscanned_noncurrent_signal["code"] = "SZ.000003"
    unscanned_noncurrent_signal["signal_id"] = "noncurrent-signal:SZ.000003"
    unscanned_noncurrent_signal["name"] = "noncurrent-only"
    unscanned_noncurrent_signal["lifecycle_stage"] = "closed"
    noncurrent["signals"].append(unscanned_noncurrent_signal)
    noncurrent_contract = "chanlun-human-assisted-signal-document"
    noncurrent_epoch = sha256_json({"schema": "noncurrent-signals-epoch"})
    noncurrent["signal_document_contract_id"] = noncurrent_contract
    noncurrent["coverage_epoch_id"] = noncurrent_epoch
    noncurrent["coverage_manifest"]["signal_document_contract_id"] = noncurrent_contract
    noncurrent["coverage_manifest"]["coverage_epoch_id"] = noncurrent_epoch
    first._finalize_snapshot_identity(noncurrent)
    cache_path.write_text(json.dumps(noncurrent), encoding="utf-8")

    restarted = TradingScreeningService(
        market_data=ActionableMarketData(),
        sector_catalog=MultiMemberSectorCatalog(symbols),
        engine=HumanAssistedDecisionCore(),
        scan_planner=full_plan,
        cache_path=cache_path,
        clock=lambda: AS_OF + timedelta(minutes=10),
        notifier=None,
        config=TradingScreeningConfig(max_symbols_per_refresh=1),
    )
    migrated = restarted.refresh_now()
    completed_codes = set(migrated["coverage_manifest"]["completed_codes"])
    signal_codes = {str(row["code"]) for row in migrated["signals"]}

    assert completed_codes == {"SZ.000001"}
    assert signal_codes.issubset(completed_codes)
    assert "SZ.000003" not in signal_codes


def test_cache_without_sector_source_evidence_replays_current_universe(
    tmp_path: Path,
) -> None:
    """A current-looking cache may not bypass a sector-evidence migration."""

    cache_path = tmp_path / "snapshot.json"
    first = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
    )
    first.refresh_now()

    noncurrent = json.loads(cache_path.read_text(encoding="utf-8"))
    assert noncurrent["signal_document_contract_id"] == SIGNAL_DOCUMENT_CONTRACT_ID
    noncurrent.pop("sector_strength_evidence")
    noncurrent.pop("sector_strength_evidence_revision")
    for sector in noncurrent["sectors"]:
        sector.pop("strength_source_revision")
    first._finalize_snapshot_identity(noncurrent)
    cache_path.write_text(json.dumps(noncurrent), encoding="utf-8")

    previous_cursors: list[object] = []

    def full_plan(**kwargs) -> ScanPlan:
        previous_cursors.append(kwargs["previous"])
        return ScanPlan(
            sectors=(eligible_sector().sector_id,),
            symbols=("SZ.000001",),
            symbol_frequencies=(("SZ.000001", ("1m", "5m", "30m", "d")),),
            full_market_history_scan=False,
            background_full_refresh_required=False,
        )

    restarted = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=full_plan,
        cache_path=cache_path,
        clock=lambda: AS_OF + timedelta(minutes=10),
        notifier=None,
    )
    migrated = restarted.refresh_now()

    assert previous_cursors and previous_cursors[0].initialized is False
    assert migrated["coverage_manifest"]["complete"] is True
    assert all("strength_source_revision" in sector for sector in migrated["sectors"])
    assert "sector_strength_evidence" in migrated
    assert "sector_strength_evidence_revision" in migrated


def test_incomplete_warmup_signal_contract_is_rejected_without_conversion(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "snapshot.json"
    first = TradingScreeningService(
        market_data=ApproachingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=HumanAssistedDecisionCore(),
        scan_planner=RecordingPlanner(),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(
            full_coverage_refresh_enabled=True,
            large_scope_authorized=True,
        ),
    )
    first.refresh_now()

    noncurrent = json.loads(cache_path.read_text(encoding="utf-8"))
    [signal] = noncurrent["signals"]
    signal["selection_sources"] = ["QMT_SECTOR_TRIGGER"]
    signal["sector_triggered"] = True
    signal["monitor_only"] = False
    signal["decision_reasons"] = [
        reason
        for reason in signal["decision_reasons"]
        if reason != FORMAL_SELECTION_REQUIRED_REASON_CODE
    ]
    signal["warmup"].pop("difference_codes_by_frequency", None)
    noncurrent_contract = "chanlun-human-assisted-signal-document"
    noncurrent["signal_document_contract_id"] = noncurrent_contract
    noncurrent["coverage_manifest"]["signal_document_contract_id"] = noncurrent_contract
    manifest = noncurrent["coverage_manifest"]
    noncurrent_epoch_id = screening_coverage_epoch_id(
        market_data_as_of=datetime.fromisoformat(manifest["market_data_as_of"]),
        universe_revision=manifest["universe_revision"],
        sector_catalog_revision=manifest["sector_catalog_revision"],
        sector_strength_evidence_revision=manifest["sector_strength_evidence_revision"],
        decision_core_id=noncurrent["decision_core_id"],
        screening_policy_id=manifest["screening_policy_id"],
        structure_contract_id=noncurrent["structure_contract_id"],
        parameter_set_id=noncurrent["parameter_set_id"],
        signal_document_contract_id=noncurrent_contract,
    )
    noncurrent["coverage_epoch_id"] = noncurrent_epoch_id
    manifest["coverage_epoch_id"] = noncurrent_epoch_id
    first._finalize_snapshot_identity(noncurrent)
    cache_path.write_text(json.dumps(noncurrent), encoding="utf-8")

    restarted_market = ApproachingMarketData()
    restarted = TradingScreeningService(
        market_data=restarted_market,
        sector_catalog=RecordingSectorCatalog(),
        engine=HumanAssistedDecisionCore(),
        scan_planner=RecordingPlanner(),
        cache_path=cache_path,
        clock=lambda: AS_OF + timedelta(minutes=1),
        notifier=None,
        config=TradingScreeningConfig(
            full_coverage_refresh_enabled=True,
            large_scope_authorized=True,
        ),
    )
    migrated = restarted.snapshot()

    assert migrated["scan_state"] == "not_started"
    assert migrated["signals"] == []
    assert migrated["coverage_epoch_id"] is None
    assert migrated["signal_document_contract_id"] == SIGNAL_DOCUMENT_CONTRACT_ID
    assert restarted.health_snapshot()["quarantined_cache_reason"] == (
        "CURRENT_CACHE_CONTRACT_INVALID"
    )
    assert restarted_market.bundle_codes == []


def test_missing_decision_identity_contract_is_rejected_without_conversion(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "snapshot.json"
    first = TradingScreeningService(
        market_data=ApproachingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=HumanAssistedDecisionCore(),
        scan_planner=RecordingPlanner(),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(
            full_coverage_refresh_enabled=True,
            large_scope_authorized=True,
        ),
    )
    first.refresh_now()

    noncurrent = json.loads(cache_path.read_text(encoding="utf-8"))
    noncurrent_contract = "chanlun-human-assisted-signal-document"
    for signal in noncurrent["signals"]:
        signal.pop("decision_document_schema", None)
        signal.pop("decision_document_id", None)
    noncurrent["signal_document_contract_id"] = noncurrent_contract
    manifest = noncurrent["coverage_manifest"]
    manifest["signal_document_contract_id"] = noncurrent_contract
    noncurrent_epoch_id = screening_coverage_epoch_id(
        market_data_as_of=datetime.fromisoformat(manifest["market_data_as_of"]),
        universe_revision=manifest["universe_revision"],
        sector_catalog_revision=manifest["sector_catalog_revision"],
        sector_strength_evidence_revision=manifest["sector_strength_evidence_revision"],
        decision_core_id=noncurrent["decision_core_id"],
        screening_policy_id=manifest["screening_policy_id"],
        structure_contract_id=noncurrent["structure_contract_id"],
        parameter_set_id=noncurrent["parameter_set_id"],
        signal_document_contract_id=noncurrent_contract,
    )
    noncurrent["coverage_epoch_id"] = noncurrent_epoch_id
    manifest["coverage_epoch_id"] = noncurrent_epoch_id
    first._finalize_snapshot_identity(noncurrent)
    cache_path.write_text(json.dumps(noncurrent), encoding="utf-8")

    restarted_market = ApproachingMarketData()
    restarted = TradingScreeningService(
        market_data=restarted_market,
        sector_catalog=RecordingSectorCatalog(),
        engine=HumanAssistedDecisionCore(),
        scan_planner=RecordingPlanner(),
        cache_path=cache_path,
        clock=lambda: AS_OF + timedelta(minutes=1),
        notifier=None,
        config=TradingScreeningConfig(
            full_coverage_refresh_enabled=True,
            large_scope_authorized=True,
        ),
    )
    migrated = restarted.snapshot()

    assert migrated["scan_state"] == "not_started"
    assert migrated["coverage_manifest"]["complete"] is False
    assert migrated["signals"] == []
    assert migrated["signal_document_contract_id"] == SIGNAL_DOCUMENT_CONTRACT_ID
    assert migrated["coverage_epoch_id"] is None
    assert restarted.health_snapshot()["quarantined_cache_reason"] == (
        "CURRENT_CACHE_CONTRACT_INVALID"
    )
    assert restarted_market.bundle_codes == []


def test_incomplete_noncurrent_queue_is_not_resumed_by_current_runtime(
    tmp_path: Path,
) -> None:
    symbols = ("SZ.000001", "SZ.000002", "SZ.000003")
    cache_path = tmp_path / "snapshot.json"
    first = TradingScreeningService(
        market_data=ActionableMarketData(),
        sector_catalog=MultiMemberSectorCatalog(symbols),
        engine=HumanAssistedDecisionCore(),
        scan_planner=SequencedPlanner((symbols,)),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(max_symbols_per_refresh=1),
    )
    partial = first.refresh_now()
    assert partial["coverage_manifest"]["complete"] is False
    assert set(partial["coverage_manifest"]["pending_frequencies"]) == set(symbols[1:])

    noncurrent = json.loads(cache_path.read_text(encoding="utf-8"))
    noncurrent_contract = "chanlun-human-assisted-signal-document"
    for signal in noncurrent["signals"]:
        signal.pop("decision_document_schema", None)
        signal.pop("decision_document_id", None)
    noncurrent["signal_document_contract_id"] = noncurrent_contract
    manifest = noncurrent["coverage_manifest"]
    manifest["signal_document_contract_id"] = noncurrent_contract
    noncurrent_epoch_id = screening_coverage_epoch_id(
        market_data_as_of=datetime.fromisoformat(manifest["market_data_as_of"]),
        universe_revision=manifest["universe_revision"],
        sector_catalog_revision=manifest["sector_catalog_revision"],
        sector_strength_evidence_revision=manifest["sector_strength_evidence_revision"],
        decision_core_id=noncurrent["decision_core_id"],
        screening_policy_id=manifest["screening_policy_id"],
        structure_contract_id=noncurrent["structure_contract_id"],
        parameter_set_id=noncurrent["parameter_set_id"],
        signal_document_contract_id=noncurrent_contract,
    )
    noncurrent["coverage_epoch_id"] = noncurrent_epoch_id
    manifest["coverage_epoch_id"] = noncurrent_epoch_id
    first._finalize_snapshot_identity(noncurrent)
    cache_path.write_text(json.dumps(noncurrent), encoding="utf-8")

    restarted_market = ActionableMarketData()
    restarted = TradingScreeningService(
        market_data=restarted_market,
        sector_catalog=MultiMemberSectorCatalog(symbols),
        engine=HumanAssistedDecisionCore(),
        scan_planner=SequencedPlanner((symbols,)),
        cache_path=cache_path,
        clock=lambda: AS_OF + timedelta(minutes=1),
        notifier=None,
        config=TradingScreeningConfig(max_symbols_per_refresh=1),
    )
    rejected = restarted.snapshot()

    assert rejected["scan_state"] == "not_started"
    assert rejected["signals"] == []
    assert rejected["coverage_epoch_id"] is None
    assert restarted._pending_frequencies == {}
    assert restarted_market.bundle_codes == []

    rebuilt = restarted.refresh_now()
    assert set(rebuilt["coverage_manifest"]["pending_frequencies"]) == set(symbols[1:])
    assert restarted_market.bundle_codes == ["SZ.000001"]


def test_cache_with_tampered_semantic_content_is_rejected(tmp_path: Path) -> None:
    cache_path = tmp_path / "snapshot.json"
    first = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
    )
    first.refresh_now()
    tampered = json.loads(cache_path.read_text(encoding="utf-8"))
    tampered["scan_state"] = "incomplete_not_published"
    cache_path.write_text(json.dumps(tampered), encoding="utf-8")

    restarted = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
    )

    assert restarted.snapshot()["scan_state"] == "not_started"


class StaleMarketData(RecordingMarketData):
    def structure_bundle(self, code: str, *, as_of: datetime, sector, frequencies=()):
        return super().structure_bundle(
            code,
            as_of=as_of - timedelta(hours=2),
            sector=sector,
            frequencies=frequencies,
        )


@pytest.mark.parametrize(
    ("observed_at", "bundle_as_of", "expected"),
    (
        (
            datetime(2026, 7, 20, 18, 30, tzinfo=AS_OF.tzinfo),
            datetime(2026, 7, 20, 15, 0, tzinfo=AS_OF.tzinfo),
            True,
        ),
        (
            datetime(2026, 7, 21, 8, 50, tzinfo=AS_OF.tzinfo),
            datetime(2026, 7, 20, 15, 0, tzinfo=AS_OF.tzinfo),
            True,
        ),
        (
            datetime(2026, 7, 25, 18, 30, tzinfo=AS_OF.tzinfo),
            datetime(2026, 7, 24, 15, 0, tzinfo=AS_OF.tzinfo),
            True,
        ),
        (
            datetime(2026, 7, 27, 10, 0, tzinfo=AS_OF.tzinfo),
            datetime(2026, 7, 24, 15, 0, tzinfo=AS_OF.tzinfo),
            False,
        ),
    ),
)
def test_structure_freshness_uses_completed_market_time(
    observed_at: datetime,
    bundle_as_of: datetime,
    expected: bool,
) -> None:
    assert (
        _structure_bundle_is_current(
            observed_at=observed_at,
            bundle_as_of=bundle_as_of,
            max_age_seconds=3600,
        )
        is expected
    )


def test_five_minute_candidate_freshness_respects_session_break_bar_boundary() -> None:
    previous_close = datetime(2026, 7, 17, 15, 0, tzinfo=AS_OF.tzinfo)
    lunch_close = datetime(2026, 7, 20, 11, 30, tzinfo=AS_OF.tzinfo)

    assert _structure_bundle_is_current(
        observed_at=datetime(2026, 7, 20, 9, 31, 10, tzinfo=AS_OF.tzinfo),
        bundle_as_of=previous_close,
        max_age_seconds=60,
        expected_frequency="5m",
    )
    assert _structure_bundle_is_current(
        observed_at=datetime(2026, 7, 20, 13, 4, 40, tzinfo=AS_OF.tzinfo),
        bundle_as_of=lunch_close,
        max_age_seconds=60,
        expected_frequency="5m",
    )
    assert not _structure_bundle_is_current(
        observed_at=datetime(2026, 7, 20, 13, 4, 40, tzinfo=AS_OF.tzinfo),
        bundle_as_of=lunch_close,
        max_age_seconds=60,
    )
    assert not _structure_bundle_is_current(
        observed_at=datetime(2026, 7, 20, 13, 5, 40, tzinfo=AS_OF.tzinfo),
        bundle_as_of=lunch_close,
        max_age_seconds=60,
        expected_frequency="5m",
    )
    assert _structure_bundle_is_current(
        observed_at=datetime(2026, 7, 20, 13, 5, 40, tzinfo=AS_OF.tzinfo),
        bundle_as_of=datetime(2026, 7, 20, 13, 5, tzinfo=AS_OF.tzinfo),
        max_age_seconds=60,
        expected_frequency="5m",
    )


def test_full_scan_freshness_uses_independent_materialized_frequency_times(
    tmp_path: Path,
) -> None:
    lunch_observed_at = datetime(2026, 7, 20, 13, 3, 39, tzinfo=AS_OF.tzinfo)
    lunch_close = datetime(2026, 7, 20, 11, 30, tzinfo=AS_OF.tzinfo)
    afternoon_observed_at = datetime(2026, 7, 20, 13, 6, tzinfo=AS_OF.tzinfo)
    afternoon_close = datetime(2026, 7, 20, 13, 5, tzinfo=AS_OF.tzinfo)

    class LunchBoundaryMarketData(RecordingMarketData):
        def __init__(
            self,
            closed_at_by_frequency: tuple[tuple[str, datetime], ...],
        ) -> None:
            super().__init__()
            self.closed_at_by_frequency = closed_at_by_frequency

        def structure_bundle(
            self,
            code: str,
            *,
            as_of: datetime,
            sector,
            frequencies=(),
        ) -> SymbolStructureBundle:
            bundle = super().structure_bundle(
                code,
                as_of=as_of,
                sector=sector,
                frequencies=frequencies,
            )
            return replace(
                bundle,
                as_of=max(
                    closed_at for _frequency, closed_at in self.closed_at_by_frequency
                ),
                warmup_by_frequency=tuple(
                    (frequency, True, 100, 100)
                    for frequency, _closed_at in self.closed_at_by_frequency
                ),
                analysis_closed_at_by_frequency=self.closed_at_by_frequency,
            )

    def refresh(
        closed_at_by_frequency: tuple[tuple[str, datetime], ...],
        cache_name: str,
        *,
        observed_at: datetime,
    ) -> tuple[dict[str, object], LunchBoundaryMarketData]:
        market = LunchBoundaryMarketData(closed_at_by_frequency)
        service = TradingScreeningService(
            market_data=market,
            sector_catalog=RecordingSectorCatalog(),
            engine=RecordingEngine(),
            scan_planner=RecordingPlanner(),
            cache_path=tmp_path / cache_name,
            clock=lambda: observed_at,
            notifier=None,
            config=TradingScreeningConfig(
                max_structure_age_seconds=60,
                priority_monitoring_enabled=False,
            ),
        )
        return service.refresh_now(), market

    five_only, five_market = refresh(
        (("5m", lunch_close),),
        "five-only.json",
        observed_at=lunch_observed_at,
    )
    stale_precision, precision_market = refresh(
        (("5m", lunch_close), ("1m", lunch_close)),
        "stale-precision.json",
        observed_at=lunch_observed_at,
    )
    stale_trade, _stale_trade_market = refresh(
        (("5m", lunch_close), ("1m", afternoon_close)),
        "stale-trade.json",
        observed_at=afternoon_observed_at,
    )
    stale_precision_only, _stale_precision_only_market = refresh(
        (("5m", afternoon_close), ("1m", lunch_close)),
        "stale-precision-only.json",
        observed_at=afternoon_observed_at,
    )

    assert five_market.bundle_frequency_requests == [
        ("SZ.000001", ("1m", "5m", "30m", "d"))
    ]
    assert precision_market.bundle_frequency_requests == [
        ("SZ.000001", ("1m", "5m", "30m", "d")),
        ("SZ.000001", ("1m", "5m", "30m", "d")),
    ]
    assert five_only["scan_audit"]["completed_symbol_count"] == 1
    assert five_only["scan_audit"]["stock_failure_counts"] == {}
    for stale in (stale_precision, stale_trade, stale_precision_only):
        assert stale["scan_audit"]["completed_symbol_count"] == 0
        assert stale["scan_audit"]["stock_failure_counts"] == {
            "STRUCTURE_BUNDLE_STALE": 1
        }


def test_intraday_freshness_rejects_future_or_ambiguous_legacy_bundle_time() -> None:
    observed_at = datetime(2026, 7, 20, 13, 6, tzinfo=AS_OF.tzinfo)
    five_close = datetime(2026, 7, 20, 13, 5, tzinfo=AS_OF.tzinfo)
    base = SymbolStructureBundle(
        code="SZ.000001",
        as_of=five_close,
        sector=eligible_sector(),
        thirty_direction="neutral",
        thirty_points=(),
        five_points=(),
        one_points=(),
        opposite_points=(),
        warmup_by_frequency=(("5m", True, 100, 100),),
    )

    assert _structure_bundle_is_current_for_intraday_evidence(
        base,
        observed_at=observed_at,
        max_age_seconds=60,
        requested_frequencies=("5m",),
    )
    assert not _structure_bundle_is_current_for_intraday_evidence(
        replace(
            base,
            as_of=observed_at + timedelta(minutes=4),
            analysis_closed_at_by_frequency=(("5m", five_close),),
        ),
        observed_at=observed_at,
        max_age_seconds=60,
        requested_frequencies=("5m",),
    )
    assert not _structure_bundle_is_current_for_intraday_evidence(
        replace(
            base,
            warmup_by_frequency=(
                ("5m", True, 100, 100),
                ("1m", True, 100, 100),
            ),
        ),
        observed_at=observed_at,
        max_age_seconds=60,
        requested_frequencies=("5m", "1m"),
    )


def test_priority_lunch_freshness_accepts_a_materialized_five_minute_empty_branch(
    tmp_path: Path,
) -> None:
    observed_at = [datetime(2026, 7, 20, 11, 30, tzinfo=AS_OF.tzinfo)]
    lunch_close = observed_at[0]

    class LunchPriorityMarketData(RecordingMarketData):
        def active_watchlist(self) -> tuple[str, ...]:
            return ("SZ.000001",)

        def structure_bundle(
            self,
            code: str,
            *,
            as_of: datetime,
            sector,
            frequencies=(),
        ) -> SymbolStructureBundle:
            bundle = super().structure_bundle(
                code,
                as_of=as_of,
                sector=sector,
                frequencies=frequencies,
            )
            return replace(
                bundle,
                as_of=lunch_close,
                warmup_by_frequency=(("5m", True, 100, 100),),
                analysis_closed_at_by_frequency=(("5m", lunch_close),),
            )

    market = LunchPriorityMarketData()
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: observed_at[0],
        notifier=None,
        config=TradingScreeningConfig(
            max_structure_age_seconds=60,
            priority_monitoring_enabled=True,
            priority_monitor_interval_seconds=60,
        ),
    )

    first = service.refresh_now()
    first_request_count = len(market.bundle_frequency_requests)
    observed_at[0] = datetime(2026, 7, 20, 13, 3, 39, tzinfo=AS_OF.tzinfo)
    service.refresh_now(priority_only=True)

    assert first["scan_audit"]["completed_symbol_count"] == 1
    assert market.bundle_frequency_requests[first_request_count:] == [
        ("SZ.000001", ("30m", "5m", "1m"))
    ]
    assert service.health_snapshot()["priority_monitor_last_error_count"] == 0


def test_suspension_confirmation_requires_complete_trading_session() -> None:
    session = date(2026, 7, 20)

    assert not _current_session_suspension_can_be_confirmed(
        session=session,
        market_data_as_of=datetime(2026, 7, 20, 10, 30, tzinfo=AS_OF.tzinfo),
    )
    assert _current_session_suspension_can_be_confirmed(
        session=session,
        market_data_as_of=datetime(2026, 7, 20, 15, 0, tzinfo=AS_OF.tzinfo),
    )
    assert _current_session_suspension_can_be_confirmed(
        session=session,
        market_data_as_of=datetime(2026, 7, 21, 8, 45, tzinfo=AS_OF.tzinfo),
    )


def test_zero_trade_quote_evidence_is_narrow_and_read_only() -> None:
    requested_codes = ("SH.513100", "SZ.000001")
    suspended = AShareRealtimeQuote(
        code="SH.513100",
        last=2.264,
        buy1=0.0,
        sell1=0.0,
        high=0.0,
        low=0.0,
        open=0.0,
        volume=0.0,
        rate=0.0,
    )
    trading = AShareRealtimeQuote(
        code="SZ.000001",
        last=12.0,
        buy1=11.99,
        sell1=12.01,
        high=12.1,
        low=11.8,
        open=11.9,
        volume=100.0,
        rate=0.5,
    )
    batch = AShareRealtimeQuoteBatch(
        requested_codes=requested_codes,
        market_open=True,
        quotes=(suspended, trading),
        tick_data_used=True,
    )

    assert _current_session_zero_trade_codes(
        batch,
        requested_codes=requested_codes,
    ) == frozenset({"SH.513100"})
    assert not _current_session_zero_trade_codes(
        object(),
        requested_codes=requested_codes,
    )


def test_stale_structure_data_fails_closed(tmp_path: Path) -> None:
    service = TradingScreeningService(
        market_data=StaleMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(max_structure_age_seconds=60),
    )

    payload = service.refresh_now()

    assert payload["available"] is False
    assert payload["scan_state"] == "incomplete_not_published"
    assert payload["data_quality"] == {
        "complete": False,
        "stale": True,
        "failure_codes": ["scan_completion_below_threshold"],
    }


def test_transient_stale_structure_retries_only_the_exact_symbol(
    tmp_path: Path,
) -> None:
    symbols = ("SZ.000001", "SZ.000002")

    class StaleOnceMarketData(RecordingMarketData):
        def __init__(self) -> None:
            super().__init__()
            self.attempts_by_code: dict[str, int] = {}

        def structure_bundle(
            self,
            code: str,
            *,
            as_of: datetime,
            sector,
            frequencies=(),
        ) -> SymbolStructureBundle:
            attempt = self.attempts_by_code.get(code, 0) + 1
            self.attempts_by_code[code] = attempt
            effective_as_of = (
                as_of - timedelta(hours=2)
                if code == symbols[0] and attempt == 1
                else as_of
            )
            return super().structure_bundle(
                code,
                as_of=effective_as_of,
                sector=sector,
                frequencies=frequencies,
            )

    def planner(**_kwargs) -> ScanPlan:
        return ScanPlan(
            sectors=(eligible_sector().sector_id,),
            symbols=symbols,
            symbol_frequencies=tuple((code, ("1m", "5m", "30m")) for code in symbols),
            full_market_history_scan=False,
            background_full_refresh_required=False,
        )

    market = StaleOnceMarketData()
    engine = RecordingEngine()
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=MultiMemberSectorCatalog(symbols),
        engine=engine,
        scan_planner=planner,
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(
            max_structure_age_seconds=60,
            priority_monitoring_enabled=False,
            stock_worker_count=1,
        ),
    )

    payload = service.refresh_now()

    assert market.bundle_codes == ["SZ.000001", "SZ.000001", "SZ.000002"]
    assert market.attempts_by_code == {"SZ.000001": 2, "SZ.000002": 1}
    assert engine.codes == ["SZ.000001", "SZ.000002"]
    assert payload["scan_audit"]["completed_symbol_count"] == 2
    assert payload["scan_audit"]["stock_failure_counts"] == {}


class PartiallyFailingMarketData(RecordingMarketData):
    def structure_bundle(self, code: str, *, as_of: datetime, sector, frequencies=()):
        if code == "SZ.000002":
            raise RuntimeError("fixture failure")
        return super().structure_bundle(
            code,
            as_of=as_of,
            sector=sector,
            frequencies=frequencies,
        )


class UnavailableKlineMarketData(RecordingMarketData):
    def structure_bundle(self, code: str, *, as_of: datetime, sector, frequencies=()):
        if code == "SZ.000002":
            raise ValueError("kline frame is unavailable")
        return super().structure_bundle(
            code,
            as_of=as_of,
            sector=sector,
            frequencies=frequencies,
        )


class MinimumHistoryMarketData(RecordingMarketData):
    def structure_bundle(self, code: str, *, as_of: datetime, sector, frequencies=()):
        if code == "SZ.000002":
            raise ValueError("kline frame does not meet minimum history")
        return super().structure_bundle(
            code,
            as_of=as_of,
            sector=sector,
            frequencies=frequencies,
        )


class CurrentSessionSuspendedMarketData(RecordingMarketData):
    suspended_code = "SZ.000002"

    def current_session_instrument_statuses(
        self,
        codes: tuple[str, ...],
        *,
        session: date,
    ) -> AShareInstrumentSessionStatusBatch:
        return AShareInstrumentSessionStatusBatch(
            requested_codes=tuple(sorted(set(codes))),
            session=session,
            facts=(
                AShareInstrumentSessionStatus(
                    code=self.suspended_code,
                    trading_day=session,
                    instrument_name="万科A",
                    instrument_status=2,
                    is_trading=False,
                ),
            ),
        )

    def structure_bundle(self, code: str, *, as_of: datetime, sector, frequencies=()):
        bundle = super().structure_bundle(
            code,
            as_of=as_of,
            sector=sector,
            frequencies=frequencies,
        )
        if code != self.suspended_code:
            return bundle
        previous_close = as_of - timedelta(days=3)
        return replace(
            bundle,
            as_of=previous_close,
            analysis_closed_at_by_frequency=(
                ("5m", previous_close),
                ("1m", previous_close),
            ),
        )


class PositiveStatusTradingMarketData(RecordingMarketData):
    status_code = "SZ.000002"

    def __init__(self, *, stale_one_minute: bool = False) -> None:
        super().__init__()
        self.stale_one_minute = stale_one_minute

    def current_session_instrument_statuses(
        self,
        codes: tuple[str, ...],
        *,
        session: date,
    ) -> AShareInstrumentSessionStatusBatch:
        return AShareInstrumentSessionStatusBatch(
            requested_codes=tuple(sorted(set(codes))),
            session=session,
            facts=(
                AShareInstrumentSessionStatus(
                    code=self.status_code,
                    trading_day=session,
                    instrument_name="跨境ETF",
                    instrument_status=1,
                    is_trading=False,
                ),
            ),
        )

    def structure_bundle(self, code: str, *, as_of: datetime, sector, frequencies=()):
        bundle = super().structure_bundle(
            code,
            as_of=as_of,
            sector=sector,
            frequencies=frequencies,
        )
        if code != self.status_code:
            return bundle
        one_minute_closed_at = (
            as_of - timedelta(days=7) if self.stale_one_minute else as_of
        )
        return replace(
            bundle,
            analysis_closed_at_by_frequency=(
                ("5m", as_of),
                ("1m", one_minute_closed_at),
            ),
        )


class LateBatchFailureMarketData(RecordingMarketData):
    def structure_bundle(self, code: str, *, as_of: datetime, sector, frequencies=()):
        if code == "SZ.000006":
            raise RuntimeError("late fixture failure")
        return super().structure_bundle(
            code,
            as_of=as_of,
            sector=sector,
            frequencies=frequencies,
        )


def test_cumulative_coverage_gate_is_not_batch_order_dependent(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "snapshot.json"
    first_service = TradingScreeningService(
        market_data=LateBatchFailureMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(
            tuple(f"SZ.{index:06d}" for index in range(1, 7))
        ),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(
            max_symbols_per_refresh=3,
            full_coverage_refresh_enabled=True,
            large_scope_authorized=True,
        ),
    )

    first = first_service.refresh_now()
    # Reproduce the persisted state written by the older per-batch gate: it
    # retained the last usable result but marked the scan as unpublished.  A
    # restart must be able to resume the same attested coverage epoch.
    degraded = json.loads(cache_path.read_text(encoding="utf-8"))
    degraded["scan_state"] = "incomplete_not_published"
    first_service._finalize_snapshot_identity(degraded)
    cache_path.write_text(json.dumps(degraded), encoding="utf-8")

    restarted = TradingScreeningService(
        market_data=LateBatchFailureMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(
            tuple(f"SZ.{index:06d}" for index in range(1, 7))
        ),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(
            max_symbols_per_refresh=3,
            full_coverage_refresh_enabled=True,
            large_scope_authorized=True,
        ),
    )
    assert restarted.snapshot()["scan_state"] == "incomplete_not_published"
    second = restarted.refresh_now()

    assert first["scan_state"] == "in_progress"
    assert first["coverage_manifest"]["complete"] is False
    assert second["scan_state"] == "complete"
    assert second["scan_audit"]["completion_ratio"] == str(Decimal(2) / Decimal(3))
    assert second["scan_audit"]["coverage_cycle_completion_ratio"] == str(
        Decimal(5) / Decimal(6)
    )
    assert second["coverage_manifest"]["complete"] is True
    assert second["coverage_manifest"]["failed_codes"] == ["SZ.000006"]
    assert second["data_quality"]["failure_codes"] == ["stock_scan_partial"]


def test_low_completion_batch_advances_remaining_coverage_instead_of_spinning(
    tmp_path: Path,
) -> None:
    """A clustered deterministic failure batch must not monopolize the queue.

    Sorted production symbols can cluster unavailable or invalid market facts.
    Requeueing the whole low-completion batch means the same deterministic
    failures are selected every refresh and later valid symbols are never
    visited.  Terminal failures belong in the next-market-epoch queue while
    the untouched part of the current coverage plan keeps draining.
    """

    class ClusteredFailureMarket(RecordingMarketData):
        def structure_bundle(
            self,
            code: str,
            *,
            as_of: datetime,
            sector,
            frequencies=(),
        ):
            if code in {"SZ.000001", "SZ.000002"}:
                self.bundle_codes.append(code)
                raise ValueError("kline frame is unavailable")
            return super().structure_bundle(
                code,
                as_of=as_of,
                sector=sector,
                frequencies=frequencies,
            )

    symbols = tuple(f"SZ.{index:06d}" for index in range(1, 11))
    sector_batch = _evidence_sector_batch(
        symbols,
        context_revision="low-completion-restart",
    )
    market = ClusteredFailureMarket()
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=EvidenceSectorCatalog(sector_batch, symbols),
        engine=RecordingEngine(),
        scan_planner=SequencedPlanner((symbols,)),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(max_symbols_per_refresh=7),
    )

    first = service.refresh_now()

    assert first["scan_state"] == "incomplete_not_published", first.get("errors")
    assert market.bundle_codes == list(symbols[:7])
    assert set(first["coverage_manifest"]["pending_frequencies"]) == set(symbols[7:])
    assert set(first["coverage_manifest"]["deferred_frequencies"]) == {
        "SZ.000001",
        "SZ.000002",
    }
    assert first["scan_audit"]["discovered_symbol_count"] == 10
    assert first["scan_audit"]["coverage_cycle_attempted_symbol_count"] == 7
    assert first["scan_audit"]["coverage_cycle_completed_symbol_count"] == 5
    assert first["scan_audit"]["coverage_cycle_failed_symbol_count"] == 2
    assert first["scan_audit"]["coverage_cycle_started_at"] == AS_OF.isoformat()
    assert (
        first["sector_strength_evidence_revision"]
        == (first["coverage_manifest"]["sector_strength_evidence_revision"])
    )
    assert first["sectors"]

    # Content hashing alone must not bless the exact inconsistent shape that
    # caused the production restart failure.
    inconsistent = json.loads(json.dumps(first))
    inconsistent["sectors"] = []
    inconsistent["sector_strength_evidence"] = None
    inconsistent["sector_strength_evidence_revision"] = None
    inconsistent["sector_member_history_diagnostics"] = None
    service._finalize_snapshot_identity(inconsistent)
    assert _sector_source_evidence_complete(inconsistent)
    assert not _cache_is_valid(
        inconsistent,
        service._config,
        service._decision_core_id,
        service._selection_research_revision,
        service._decision_source_snapshot_id,
    )

    # A low-completion checkpoint must carry enough frozen sector evidence to
    # restore the cumulative ledger after a process restart. Continuing only
    # in memory hides a production failure where a fresh process starts a new
    # ledger with the same epoch ID and loses already-completed symbols.
    restarted = TradingScreeningService(
        market_data=market,
        sector_catalog=EvidenceSectorCatalog(sector_batch, symbols),
        engine=RecordingEngine(),
        scan_planner=SequencedPlanner((symbols,)),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(max_symbols_per_refresh=7),
    )
    assert restarted._coverage_cycle_completed_codes == set(symbols[2:7])

    second = restarted.refresh_now()

    assert market.bundle_codes == list(symbols)
    assert second["scan_state"] == "complete"
    assert second["coverage_manifest"]["complete"] is True
    assert second["coverage_manifest"]["pending_frequencies"] == {}
    assert second["coverage_manifest"]["completed_codes"] == list(symbols[2:])
    assert second["coverage_manifest"]["failed_codes"] == list(symbols[:2])
    assert second["scan_audit"]["coverage_cycle_completion_ratio"] == "0.8"


def test_new_universe_epoch_discards_retry_codes_outside_current_scope(
    tmp_path: Path,
) -> None:
    """A restarted epoch may retry current members, never removed members."""

    sector_id = eligible_sector().sector_id

    class MembershipCatalog(RecordingSectorCatalog):
        def __init__(self, members: tuple[str, ...]) -> None:
            super().__init__()
            self.current_members = members

        def members(self):
            self.member_calls += 1
            return {sector_id: self.current_members}

    def planner_for(symbols: tuple[str, ...]):
        def planner(**kwargs) -> ScanPlan:
            assert kwargs["sector_members"] == {sector_id: symbols}
            return ScanPlan(
                sectors=(sector_id,),
                symbols=symbols,
                symbol_frequencies=tuple(
                    (code, ("1m", "5m", "30m")) for code in symbols
                ),
                full_market_history_scan=False,
                background_full_refresh_required=False,
            )

        return planner

    old_symbols = tuple(f"SZ.{index:06d}" for index in range(1, 6))
    removed = old_symbols[0]
    current_symbols = old_symbols[1:]
    cache_path = tmp_path / "snapshot.json"
    old_market = RecordingMarketData()
    original = old_market.structure_bundle

    def fail_removed(code, **kwargs):
        if code == removed:
            old_market.bundle_codes.append(code)
            raise ValueError("kline frame is unavailable")
        return original(code, **kwargs)

    old_market.structure_bundle = fail_removed
    old_service = TradingScreeningService(
        market_data=old_market,
        sector_catalog=MembershipCatalog(old_symbols),
        engine=RecordingEngine(),
        scan_planner=planner_for(old_symbols),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
    )
    old_snapshot = old_service.refresh_now()
    old_epoch = old_snapshot["coverage_epoch_id"]
    assert old_snapshot["coverage_manifest"]["complete"] is True
    assert removed in old_snapshot["coverage_manifest"]["deferred_frequencies"]

    new_market = RecordingMarketData()
    restarted = TradingScreeningService(
        market_data=new_market,
        sector_catalog=MembershipCatalog(current_symbols),
        engine=RecordingEngine(),
        scan_planner=planner_for(current_symbols),
        cache_path=cache_path,
        clock=lambda: AS_OF + timedelta(minutes=10),
        notifier=None,
    )
    current = restarted.refresh_now()

    assert current["coverage_epoch_id"] != old_epoch
    assert new_market.bundle_codes == list(current_symbols)
    manifest = current["coverage_manifest"]
    assert manifest["complete"] is True
    assert manifest["discovered_codes"] == list(current_symbols)
    assert manifest["completed_codes"] == list(current_symbols)
    assert manifest["failed_codes"] == []
    assert manifest["pending_frequencies"] == {}
    assert manifest["backoff_frequencies"] == {}
    assert manifest["deferred_frequencies"] == {}
    assert manifest["discarded_out_of_scope_retry_codes"] == [removed]


def test_incomplete_scan_does_not_publish_partial_signals(tmp_path: Path) -> None:
    service = TradingScreeningService(
        market_data=PartiallyFailingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(("SZ.000001", "SZ.000002")),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
    )

    payload = service.refresh_now()

    assert payload["available"] is False
    assert payload["signals"] == []
    assert payload["scan_state"] == "incomplete_not_published"
    assert payload["errors"][0]["code"] == "SZ.000002"


def test_published_partial_stock_scan_has_stable_failure_protocol(
    tmp_path: Path,
) -> None:
    service = TradingScreeningService(
        market_data=PartiallyFailingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(
            (
                "SZ.000001",
                "SZ.000002",
                "SZ.000003",
                "SZ.000004",
                "SZ.000005",
            )
        ),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
    )

    payload = service.refresh_now()

    assert payload["scan_state"] == "complete"
    assert payload["scan_audit"]["completion_ratio"] == "0.8"
    assert payload["data_quality"] == {
        "complete": False,
        "stale": False,
        "failure_codes": ["stock_scan_partial"],
    }
    assert payload["errors"] == [
        {
            "code": "SZ.000002",
            "error_type": "stock_analysis_error",
            "reason_code": "STOCK_ANALYSIS_UNCLASSIFIED",
            "failure_class": "UNCLASSIFIED_FAILURE",
            "retry_policy": "NEXT_COVERAGE_CYCLE",
            "deterministic_for_coverage_epoch": False,
            "remote_error_type": "RuntimeError",
            "reason": "fixture failure",
        }
    ]
    assert payload["scan_audit"]["stock_failure_counts"] == {
        "STOCK_ANALYSIS_UNCLASSIFIED": 1
    }


def test_restart_retries_only_non_deterministic_deferred_stock_failures(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "snapshot.json"
    symbols = tuple(f"SZ.{index:06d}" for index in range(1, 6))
    first_service = TradingScreeningService(
        market_data=PartiallyFailingMarketData(),
        sector_catalog=MultiMemberSectorCatalog(symbols),
        engine=RecordingEngine(),
        scan_planner=SequencedPlanner((symbols,)),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
    )

    failed = first_service.refresh_now()

    assert failed["coverage_manifest"]["failed_codes"] == ["SZ.000002"]
    assert set(failed["coverage_manifest"]["deferred_frequencies"]) == {"SZ.000002"}

    recovered_market = RecordingMarketData()
    restarted = TradingScreeningService(
        market_data=recovered_market,
        sector_catalog=MultiMemberSectorCatalog(symbols),
        engine=RecordingEngine(),
        scan_planner=SequencedPlanner(((),)),
        cache_path=cache_path,
        clock=lambda: AS_OF + timedelta(minutes=1),
        notifier=None,
    )

    assert set(restarted._pending_frequencies) == {"SZ.000002"}
    assert restarted._backoff_frequencies == {}
    assert "SZ.000002" not in restarted._deferred_frequencies

    recovered = restarted.refresh_now()

    assert recovered_market.bundle_codes == ["SZ.000002"]
    assert recovered["coverage_manifest"]["failed_codes"] == []
    assert recovered["coverage_manifest"]["backoff_frequencies"] == {}
    assert recovered["coverage_manifest"]["deferred_frequencies"] == {}
    assert recovered["errors"] == []


def test_market_data_rejection_has_stable_reason_and_epoch_retry_policy(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "snapshot.json"
    service = TradingScreeningService(
        market_data=UnavailableKlineMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(
            tuple(f"SZ.{index:06d}" for index in range(1, 6))
        ),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
    )

    payload = service.refresh_now()

    assert payload["scan_state"] == "complete"
    assert payload["errors"] == [
        {
            "code": "SZ.000002",
            "error_type": "stock_analysis_error",
            "reason_code": "KLINE_FRAME_UNAVAILABLE",
            "failure_class": "MARKET_DATA_REJECTION",
            "retry_policy": "NEXT_MARKET_DATA_EPOCH",
            "deterministic_for_coverage_epoch": True,
            "remote_error_type": "ValueError",
            "reason": "kline frame is unavailable",
        }
    ]
    assert payload["scan_audit"]["stock_failure_counts"] == {
        "KLINE_FRAME_UNAVAILABLE": 1
    }

    restarted = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=SequencedPlanner(((),)),
        cache_path=cache_path,
        clock=lambda: AS_OF + timedelta(minutes=1),
        notifier=None,
    )

    assert restarted._backoff_frequencies == {}
    assert set(restarted._deferred_frequencies) == {"SZ.000002"}


def test_minimum_history_rejection_is_audited_as_epoch_exclusion(
    tmp_path: Path,
) -> None:
    """Short-history listings are neither successes nor operational failures."""

    cache_path = tmp_path / "snapshot.json"
    symbols = tuple(f"SZ.{index:06d}" for index in range(1, 6))
    service = TradingScreeningService(
        market_data=MinimumHistoryMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(symbols),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
    )

    payload = service.refresh_now()
    manifest = payload["coverage_manifest"]
    audit = payload["scan_audit"]

    assert payload["scan_state"] == "complete"
    assert payload["errors"] == []
    assert payload["data_quality"] == {
        "complete": True,
        "stale": False,
        "failure_codes": [],
    }
    assert manifest["completed_codes"] == list(symbols[:1] + symbols[2:])
    assert manifest["excluded_codes"] == ["SZ.000002"]
    assert manifest["failed_codes"] == []
    assert manifest["deferred_frequencies"] == {"SZ.000002": ["d", "30m", "5m", "1m"]}
    assert manifest["exclusions"] == [
        {
            "code": "SZ.000002",
            "exclusion_type": "stock_analysis_exclusion",
            "eligibility": "INSUFFICIENT_MINIMUM_HISTORY",
            "reason_code": "KLINE_MINIMUM_HISTORY_NOT_MET",
            "retry_policy": "NEXT_MARKET_DATA_EPOCH",
            "deterministic_for_coverage_epoch": True,
            "remote_error_type": "ValueError",
            "reason": "kline frame does not meet minimum history",
        }
    ]
    assert audit["coverage_cycle_attempted_symbol_count"] == 5
    assert audit["coverage_cycle_completed_symbol_count"] == 4
    assert audit["coverage_cycle_excluded_symbol_count"] == 1
    assert audit["coverage_cycle_failed_symbol_count"] == 0
    assert audit["coverage_cycle_resolved_symbol_count"] == 5
    assert audit["coverage_cycle_completion_ratio"] == "0.8"
    assert audit["coverage_cycle_resolution_ratio"] == "1"
    assert audit["stock_failure_counts"] == {}
    assert audit["stock_exclusion_counts"] == {"KLINE_MINIMUM_HISTORY_NOT_MET": 1}
    health = service.health_snapshot()
    assert health["coverage_cycle_excluded_symbol_count"] == 1
    assert health["coverage_cycle_resolved_symbol_count"] == 5
    assert health["coverage_cycle_resolution_ratio"] == "1"
    assert health["coverage_excluded_codes"] == ["SZ.000002"]
    assert health["coverage_exclusions"] == manifest["exclusions"]

    # A restart restores the exact terminal disposition and must not silently
    # turn the excluded symbol into completed coverage.
    restored = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(()),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
    ).snapshot()
    assert restored["coverage_manifest"] == manifest
    assert restored["snapshot_content_sha256"] == payload["snapshot_content_sha256"]


def test_current_session_suspension_requires_stale_five_minute_trade_evidence(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "snapshot.json"
    symbols = tuple(f"SZ.{index:06d}" for index in range(1, 6))
    market = CurrentSessionSuspendedMarketData()
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(symbols),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
    )

    payload = service.refresh_now()
    manifest = payload["coverage_manifest"]
    audit = payload["scan_audit"]

    assert market.bundle_codes.count(market.suspended_code) == 2
    assert payload["scan_state"] == "complete"
    assert payload["errors"] == []
    assert payload["data_quality"] == {
        "complete": True,
        "stale": False,
        "failure_codes": [],
    }
    assert manifest["completed_codes"] == list(symbols[:1] + symbols[2:])
    assert manifest["excluded_codes"] == [market.suspended_code]
    assert manifest["failed_codes"] == []
    assert manifest["exclusions"] == [
        {
            "code": market.suspended_code,
            "exclusion_type": "stock_analysis_exclusion",
            "eligibility": "CURRENT_SESSION_SUSPENDED",
            "reason_code": "CURRENT_SESSION_SUSPENDED",
            "retry_policy": "NEXT_MARKET_DATA_EPOCH",
            "deterministic_for_coverage_epoch": True,
            "remote_error_type": "ValueError",
            "reason": "current_session_suspended",
        }
    ]
    assert audit["stock_instrument_status_probe_status"] == "completed"
    assert audit["stock_instrument_status_probe_error"] is None
    assert audit["stock_instrument_status_suspension_hint_count"] == 1
    assert audit["stock_current_session_suspended_code_count"] == 1
    assert audit["coverage_cycle_completed_symbol_count"] == 4
    assert audit["coverage_cycle_excluded_symbol_count"] == 1
    assert audit["coverage_cycle_failed_symbol_count"] == 0
    assert audit["coverage_cycle_resolution_ratio"] == "1"
    assert audit["stock_exclusion_counts"] == {"CURRENT_SESSION_SUSPENDED": 1}

    restored = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(()),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
    )
    assert restored._snapshot_rebuild_required is False
    assert restored._coverage_cycle_completed_codes == set(symbols[:1] + symbols[2:])
    assert restored._coverage_cycle_excluded_codes == {market.suspended_code}
    assert restored._coverage_cycle_failed_codes == set()
    assert restored._coverage_cycle_exclusions == {
        market.suspended_code: manifest["exclusions"][0]
    }


def test_intraday_status_hint_without_trade_waits_instead_of_suspending(
    tmp_path: Path,
) -> None:
    observed_at = datetime(2026, 7, 20, 10, 0, tzinfo=AS_OF.tzinfo)
    symbols = ("SZ.000001", "SZ.000002")
    market = CurrentSessionSuspendedMarketData()
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(symbols),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: observed_at,
        notifier=None,
    )

    payload = service.refresh_now()
    manifest = payload["coverage_manifest"]
    audit = payload["scan_audit"]
    error = payload["errors"][0]

    assert market.bundle_codes.count(market.suspended_code) == 2
    assert manifest["completed_codes"] == ["SZ.000001"]
    assert manifest["excluded_codes"] == []
    assert manifest["failed_codes"] == [market.suspended_code]
    assert manifest["exclusions"] == []
    assert error["code"] == market.suspended_code
    assert error["reason_code"] == "CURRENT_SESSION_FIRST_TRADE_PENDING"
    assert error["failure_class"] == "MARKET_DATA_PENDING"
    assert error["retry_policy"] == "NEXT_REFRESH_AFTER_BACKOFF"
    assert error["deterministic_for_coverage_epoch"] is False
    assert audit["stock_current_session_suspended_code_count"] == 0
    assert audit["stock_exclusion_counts"] == {}
    assert audit["stock_failure_counts"] == {"CURRENT_SESSION_FIRST_TRADE_PENDING": 1}
    assert audit["backoff_retry_symbol_count"] == 1


def test_positive_status_with_current_five_minute_trade_is_not_suspended(
    tmp_path: Path,
) -> None:
    symbols = ("SZ.000001", "SZ.000002")
    market = PositiveStatusTradingMarketData()
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(symbols),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
    )

    payload = service.refresh_now()
    audit = payload["scan_audit"]

    assert payload["coverage_manifest"]["completed_codes"] == list(symbols)
    assert payload["coverage_manifest"]["excluded_codes"] == []
    assert market.bundle_codes.count(market.status_code) == 1
    assert audit["stock_instrument_status_suspension_hint_count"] == 1
    assert audit["stock_current_session_suspended_code_count"] == 0
    assert audit["stock_exclusion_counts"] == {}


def test_current_five_minute_with_stale_one_minute_is_not_suspension(
    tmp_path: Path,
) -> None:
    symbols = ("SZ.000001", "SZ.000002")
    market = PositiveStatusTradingMarketData(stale_one_minute=True)
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(symbols),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
    )

    payload = service.refresh_now()
    audit = payload["scan_audit"]
    manifest = payload["coverage_manifest"]
    error = payload["errors"][0]

    assert market.bundle_codes.count(market.status_code) == 2
    assert manifest["excluded_codes"] == []
    assert manifest["failed_codes"] == [market.status_code]
    assert error["code"] == market.status_code
    assert error["reason_code"] == "STRUCTURE_BUNDLE_STALE"
    assert audit["stock_instrument_status_suspension_hint_count"] == 1
    assert audit["stock_current_session_suspended_code_count"] == 0
    assert audit["stock_exclusion_counts"] == {}
    assert audit["stock_failure_counts"] == {"STRUCTURE_BUNDLE_STALE": 1}


def test_runtime_symbol_failure_retries_after_paced_refresh_in_same_epoch(
    tmp_path: Path,
) -> None:
    class NativeScreeningWorkerUnavailable(RuntimeError):
        pass

    market = RecordingMarketData()
    original = market.structure_bundle
    failed_once = False

    def structure_bundle(code, **kwargs):
        nonlocal failed_once
        if code == "SZ.000002" and not failed_once:
            failed_once = True
            market.bundle_codes.append(code)
            raise NativeScreeningWorkerUnavailable("worker restarted")
        return original(code, **kwargs)

    market.structure_bundle = structure_bundle
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(
            tuple(f"SZ.{index:06d}" for index in range(1, 6))
        ),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
    )

    first = service.refresh_now()

    assert first["scan_audit"]["coverage_cycle_complete"] is False
    assert first["last_batch_state"] == "complete"
    assert first["full_coverage_state"] == "in_progress"
    assert first["scan_audit"]["immediate_pending_symbol_count"] == 0
    assert first["scan_audit"]["pending_symbol_count"] == 1
    assert first["scan_audit"]["backoff_retry_symbol_count"] == 1
    assert first["scan_audit"]["next_epoch_retry_symbol_count"] == 0
    assert first["errors"][0]["retry_policy"] == "NEXT_REFRESH_AFTER_BACKOFF"

    # A process restart must restore the backoff queue from the manifest. Keep
    # the fixture free of newly discovered bars so only that queue is retried.
    restarted = TradingScreeningService(
        market_data=market,
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(()),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
    )
    second = restarted.refresh_now()

    assert second["coverage_epoch_id"] == first["coverage_epoch_id"]
    assert second["scan_audit"]["coverage_cycle_complete"] is True
    assert second["last_batch_state"] == "complete"
    assert second["full_coverage_state"] == "complete"
    assert (
        second["scan_audit"]["coverage_cycle_runtime_baseline_finalized_symbol_count"]
        == 4
    )
    assert second["scan_audit"]["coverage_cycle_runtime_finalized_symbol_count"] == 1
    assert second["scan_audit"]["pending_symbol_count"] == 0
    assert second["scan_audit"]["backoff_retry_symbol_count"] == 0
    assert second["scan_audit"]["coverage_cycle_completed_symbol_count"] == 5
    assert second["scan_audit"]["coverage_cycle_failed_symbol_count"] == 0
    assert second["errors"] == []
    assert market.bundle_codes.count("SZ.000002") == 2


class FailingSectorCatalog(RecordingSectorCatalog):
    def native_sector_assessments(
        self,
        *,
        as_of: datetime,
        admitted_codes=None,
    ):
        del as_of, admitted_codes
        raise RuntimeError("native sector feed unavailable")


def test_refresh_failure_is_published_as_stale_read_only_state(
    tmp_path: Path,
) -> None:
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=FailingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
    )

    payload = service.refresh_now()

    assert payload["available"] is False
    assert payload["scan_state"] == "refresh_failed"
    assert payload["read_only"] is True
    assert payload["data_quality"] == {
        "complete": False,
        "stale": True,
        "failure_codes": ["refresh_failed"],
    }
    assert payload["errors"] == [
        {"error": "RuntimeError", "reason": "native sector feed unavailable"}
    ]


def test_refresh_failure_retains_an_existing_complete_snapshot(
    tmp_path: Path,
) -> None:
    """A revalidation outage must not destroy the last publishable screen.

    The returned value remains an explicit operational failure so the
    background health loop can fail readiness and retry.  The service state
    and atomic cache, however, must continue serving the independently hashed
    complete epoch instead of replacing it with a synthetic error document.
    """

    class ToggleFailingSectorCatalog(RecordingSectorCatalog):
        def __init__(self) -> None:
            super().__init__()
            self.fail = False

        def native_sector_assessments(
            self,
            *,
            as_of: datetime,
            admitted_codes=None,
        ):
            if self.fail:
                raise RuntimeError("same-epoch sector transport failure")
            return super().native_sector_assessments(
                as_of=as_of,
                admitted_codes=admitted_codes,
            )

    cache_path = tmp_path / "snapshot.json"
    catalog = ToggleFailingSectorCatalog()
    clock_value = [AS_OF]
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=catalog,
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=cache_path,
        clock=lambda: clock_value[0],
        notifier=None,
    )
    first = service.refresh_now()
    assert first["coverage_manifest"]["complete"] is True

    catalog.fail = True
    # A complete epoch is revalidated only at the deliberate post-close
    # boundary; ordinary same-epoch monitoring keeps its frozen evidence.
    clock_value[0] = AS_OF + timedelta(minutes=10)
    failure = service.refresh_now()

    assert failure["scan_state"] == "refresh_failed"
    assert failure["errors"] == [
        {
            "error": "RuntimeError",
            "reason": "same-epoch sector transport failure",
        }
    ]
    assert service.snapshot() == first
    assert json.loads(cache_path.read_text(encoding="utf-8")) == first
    service._record_background_result(failure)
    failed_health = service.health_snapshot()
    assert failed_health["last_error"] == (
        "RuntimeError: same-epoch sector transport failure"
    )
    assert "screening_background_error" not in failed_health["reasons"]
    assert (
        "screening_background_error"
        in failed_health["selection_operational_reason_codes"]
    )

    catalog.fail = False
    recovered = service.refresh_now()
    service._record_background_result(recovered)
    assert recovered == first
    assert service.snapshot() == first
    assert service.health_snapshot()["last_error"] is None


def test_snapshot_has_six_independent_point_channels_and_keeps_neutral_sector(
    tmp_path: Path,
) -> None:
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
    )

    payload = service.refresh_now()

    assert payload["counts_by_point_type"] == {
        "1buy": 0,
        "2buy": 0,
        "3buy": 0,
        "1sell": 0,
        "2sell": 0,
        "3sell": 0,
    }
    assert payload["sectors"][0]["regime"] == "neutral"
    assert payload["sectors"][0]["eligible"] is True


def _all_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value).union(*(_all_keys(item) for item in value.values()), set())
    if isinstance(value, list):
        return set().union(*(_all_keys(item) for item in value), set())
    return set()


def test_snapshot_has_no_fixed_progress_or_probability_field(tmp_path: Path) -> None:
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
    )

    payload = service.refresh_now()

    assert {"progress", "probability", "score_probability"}.isdisjoint(
        _all_keys(payload)
    )


class ActionableMarketData(RecordingMarketData):
    def structure_bundle(
        self,
        code: str,
        *,
        as_of: datetime,
        sector,
        frequencies=(),
    ) -> SymbolStructureBundle:
        del frequencies
        self.bundle_codes.append(code)
        setup = _current_terminal_point(
            confirmed_point("2buy", code=code, minutes_after=295)
        )
        trigger = _current_terminal_point(
            confirmed_point(
                "1buy",
                code=code,
                frequency="1m",
                minutes_after=294,
                available_minutes_after=2,
            ),
            terminal_minutes=1,
        )
        return SymbolStructureBundle(
            code=code,
            as_of=as_of,
            sector=sector,
            thirty_direction="neutral",
            thirty_points=(),
            five_points=(setup,),
            one_points=(trigger,),
            opposite_points=(),
        )


def test_retained_coverage_rows_preserve_disabled_formal_research_policy(
    tmp_path: Path,
) -> None:
    symbols = ("SZ.000001", "SZ.000002")
    service = TradingScreeningService(
        market_data=ActionableMarketData(),
        sector_catalog=MultiMemberSectorCatalog(symbols),
        engine=HumanAssistedDecisionCore(formal_selection_required=False),
        scan_planner=SequencedPlanner((symbols,)),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(max_symbols_per_refresh=1),
    )

    first = service.refresh_now()
    assert first["scan_state"] == "in_progress"
    assert [row["code"] for row in first["signals"]] == [symbols[0]]
    assert first["signals"][0]["formal_selection_required"] is False
    assert (
        FORMAL_SELECTION_REQUIRED_REASON_CODE
        not in first["signals"][0]["decision_reasons"]
    )

    second = service.refresh_now()
    assert second["scan_state"] == "complete"
    assert {row["code"] for row in second["signals"]} == set(symbols)
    assert all(row["formal_selection_required"] is False for row in second["signals"])
    assert all(
        FORMAL_SELECTION_REQUIRED_REASON_CODE not in row["decision_reasons"]
        for row in second["signals"]
    )


class SessionIssueMarketData(ActionableMarketData):
    def structure_bundle(self, code: str, **kwargs) -> SymbolStructureBundle:
        bundle = super().structure_bundle(code, **kwargs)
        issue = QmtMinuteSessionIssue(
            session=date(2026, 7, 17),
            code="QMT_ONE_MINUTE_EXPECTED_SESSION_MISSING",
            observed_rows=0,
            detail="trading-calendar session is absent from the QMT 1m prefix",
        )
        market = HigherTimeframeGateEvidence(
            subject="SH.000300",
            observed_at=bundle.as_of,
            monthly="NONE",
            weekly="NONE",
            daily="NONE",
            gate="GREEN",
            grade="RESEARCH_ONLY",
            snapshot_id="market:test",
            source_revision="market:test",
            period_diagnostics=tuple(
                HigherTimeframePeriodDiagnostic(
                    period=period,
                    state="NONE",
                    completed_bar_count=count,
                    evidence_bar_end=None,
                    active_top_interval=None,
                    mapping_unique=True,
                    mapped_center_id=None,
                    mapping_candidate_ids=(),
                    blocker_codes=(),
                    warning_codes=(),
                    source_revision=sha256_json(
                        {
                            "schema": "test-higher-timeframe-period",
                            "subject": "SH.000300",
                            "period": period,
                        }
                    ),
                )
                for period, count in (("M", 12), ("W", 51), ("D", 243))
            ),
            session_evidence=HigherTimeframeSessionEvidence.exact(),
        )
        symbol = unresolved_higher_timeframe_gates(
            symbol=code,
            observed_at=bundle.as_of,
            reason_code="QMT_ONE_MINUTE_EXPECTED_SESSION_MISSING",
            session_evidence=HigherTimeframeSessionEvidence.exact((issue,)),
        ).symbol
        return replace(
            bundle,
            higher_timeframe_gates=HigherTimeframeGateBundle(
                market=market,
                sector=replace(
                    market,
                    subject=bundle.sector.sector_id,
                    snapshot_id="sector:test",
                    source_revision="sector:test",
                ),
                symbol=symbol,
            ),
            enforce_higher_timeframe_entry_gate=True,
        )


class NativeDailyAheadMarketData(ActionableMarketData):
    def structure_bundle(self, code: str, **kwargs) -> SymbolStructureBundle:
        bundle = super().structure_bundle(code, **kwargs)
        unresolved = unresolved_higher_timeframe_gates(
            symbol=code,
            observed_at=bundle.as_of,
            reason_code="QMT_NATIVE_DAILY_AHEAD_OF_ONE_MINUTE_BASE",
            sector_subject=bundle.sector.sector_id,
        )
        return replace(
            bundle,
            higher_timeframe_gates=unresolved,
            enforce_higher_timeframe_entry_gate=True,
        )


class WarmupIssueMarketData(ActionableMarketData):
    def structure_bundle(self, code: str, **kwargs) -> SymbolStructureBundle:
        bundle = super().structure_bundle(code, **kwargs)
        warmup = QmtHigherTimeframeWarmupEvidence(
            required_daily_bar_count=480,
            full_daily_bar_count=120,
            suffix_daily_bar_count=0,
            converged=False,
            reason_code=("QMT_HIGHER_TIMEFRAME_WARMUP_HISTORY_INSUFFICIENT"),
            full_signature="sha256:" + "4" * 64,
            suffix_signature=None,
        )
        convergence = classify_warmup_convergence_envelope(
            frequency="d",
            as_of=bundle.as_of,
            parameter_set_id=(QMT_HIGHER_TIMEFRAME_WARMUP_CONVERGENCE_PARAMETER_SET_ID),
            observations=(),
        )
        unresolved = unresolved_higher_timeframe_gates(
            symbol=code,
            observed_at=bundle.as_of,
            reason_code=warmup.reason_code,
            sector_subject=bundle.sector.sector_id,
        )
        assert unresolved.sector is not None
        return replace(
            bundle,
            higher_timeframe_gates=HigherTimeframeGateBundle(
                market=replace(
                    unresolved.market,
                    warmup_evidence=warmup,
                    warmup_convergence_evidence=convergence,
                ),
                sector=replace(
                    unresolved.sector,
                    warmup_evidence=warmup,
                    warmup_convergence_evidence=convergence,
                ),
                symbol=replace(
                    unresolved.symbol,
                    warmup_evidence=warmup,
                    warmup_convergence_evidence=convergence,
                ),
            ),
            enforce_higher_timeframe_entry_gate=True,
        )


class CausalCutoffMarketData(RecordingMarketData):
    """Model 1m precision newer than the frozen sector/MWD cutoff."""

    def __init__(self, *, signal_at: datetime, risk_cutoff: datetime) -> None:
        super().__init__()
        self.signal_at = signal_at
        self.risk_cutoff = risk_cutoff
        self.cutoff_calls: list[datetime] = []

    def structure_bundle_with_risk_cutoff(
        self,
        code: str,
        *,
        as_of: datetime,
        sector,
        frequencies=(),
        risk_evidence_cutoff: datetime,
    ) -> SymbolStructureBundle:
        del frequencies
        assert as_of == self.signal_at
        assert risk_evidence_cutoff == self.risk_cutoff
        self.bundle_codes.append(code)
        self.cutoff_calls.append(risk_evidence_cutoff)
        warmup = QmtHigherTimeframeWarmupEvidence(
            required_daily_bar_count=480,
            full_daily_bar_count=120,
            suffix_daily_bar_count=0,
            converged=False,
            reason_code="QMT_HIGHER_TIMEFRAME_WARMUP_HISTORY_INSUFFICIENT",
            full_signature="sha256:" + "8" * 64,
            suffix_signature=None,
        )
        convergence = classify_warmup_convergence_envelope(
            frequency="d",
            as_of=risk_evidence_cutoff,
            parameter_set_id=(QMT_HIGHER_TIMEFRAME_WARMUP_CONVERGENCE_PARAMETER_SET_ID),
            observations=(),
        )
        unresolved = unresolved_higher_timeframe_gates(
            symbol=code,
            observed_at=risk_evidence_cutoff,
            reason_code=warmup.reason_code,
            sector_subject=sector.sector_id,
        )
        assert unresolved.sector is not None
        gates = HigherTimeframeGateBundle(
            market=replace(
                unresolved.market,
                warmup_evidence=warmup,
                warmup_convergence_evidence=convergence,
            ),
            sector=replace(
                unresolved.sector,
                warmup_evidence=warmup,
                warmup_convergence_evidence=convergence,
            ),
            symbol=replace(
                unresolved.symbol,
                warmup_evidence=warmup,
                warmup_convergence_evidence=convergence,
            ),
        )
        return SymbolStructureBundle(
            code=code,
            as_of=self.signal_at,
            sector=sector,
            thirty_direction="neutral",
            thirty_points=(),
            five_points=(confirmed_point("2buy", code=code, minutes_after=295),),
            one_points=(
                confirmed_point(
                    "1buy",
                    code=code,
                    frequency="1m",
                    minutes_after=300,
                ),
            ),
            opposite_points=(),
            higher_timeframe_gates=gates,
            enforce_higher_timeframe_entry_gate=True,
        )


class SectorNativeDailyResearchMarketData(ActionableMarketData):
    """One valid GREEN-to-AMBER sector research bridge for page QA."""

    def structure_bundle(self, code: str, **kwargs) -> SymbolStructureBundle:
        bundle = super().structure_bundle(code, **kwargs)
        unresolved = unresolved_higher_timeframe_gates(
            symbol=code,
            observed_at=bundle.as_of,
            reason_code="QMT_NATIVE_DAILY_TEST_UNRESOLVED",
        )
        strict_warmup = QmtHigherTimeframeWarmupEvidence(
            required_daily_bar_count=480,
            full_daily_bar_count=120,
            suffix_daily_bar_count=0,
            converged=False,
            reason_code="QMT_HIGHER_TIMEFRAME_WARMUP_HISTORY_INSUFFICIENT",
            full_signature="sha256:" + "6" * 64,
            suffix_signature=None,
        )
        selected_warmup = QmtHigherTimeframeWarmupEvidence(
            required_daily_bar_count=480,
            full_daily_bar_count=520,
            suffix_daily_bar_count=520 - 520 // 3,
            converged=True,
            reason_code="QMT_HIGHER_TIMEFRAME_WARMUP_TAIL_STABLE",
            full_signature="sha256:" + "7" * 64,
            suffix_signature="sha256:" + "7" * 64,
        )
        strict_coverage = QmtSectorSameBaseCoverageEvidence(
            observed_at=bundle.as_of,
            calendar_first_session=date(2023, 5, 4),
            first_visible_bar_at=bundle.as_of.replace(hour=9, minute=35),
            last_visible_bar_at=bundle.as_of,
            first_completed_session=date(2026, 1, 2),
            last_completed_session=bundle.as_of.date(),
            visible_five_minute_bar_count=120 * 48,
            completed_daily_bar_count=120,
            required_daily_bar_count=480,
            remaining_daily_bar_count=360,
            missing_leading_calendar_session_count=360,
            warmup_converged=False,
            warmup_reason_code=("QMT_HIGHER_TIMEFRAME_WARMUP_HISTORY_INSUFFICIENT"),
            boundary_status="VISIBLE_PREFIX_STARTS_AFTER_REQUESTED_WARMUP",
            physical_source_boundary_status=(
                "REQUESTED_REPLAY_LEFT_BOUNDARY_CLIPS_EARLIER_QMT_HISTORY"
            ),
            physical_source_requested_start_at=bundle.as_of.replace(
                hour=9,
                minute=35,
            ),
            physical_source_required_contributor_start_at=bundle.as_of.replace(
                hour=9,
                minute=35,
            ),
            physical_source_representative_member_count=24,
            physical_source_available_member_count=23,
            physical_source_required_contributor_count=15,
            physical_source_inventory_revision="sha256:" + "9" * 64,
        )
        blocker = "QMT_SECTOR_NATIVE_DAILY_AND_5M_UNRECONCILED_RESEARCH_BRIDGE"
        diagnostics = tuple(
            HigherTimeframePeriodDiagnostic(
                period=period,
                state="NONE",
                completed_bar_count=count,
                evidence_bar_end=None,
                active_top_interval=None,
                mapping_unique=True,
                mapped_center_id=None,
                mapping_candidate_ids=(),
                blocker_codes=(),
                warning_codes=(),
                source_revision=sha256_json(
                    {
                        "schema": "test-sector-native-daily-period",
                        "period": period,
                    }
                ),
            )
            for period, count in (("M", 24), ("W", 104), ("D", 520))
        )
        sector = HigherTimeframeGateEvidence(
            subject=bundle.sector.sector_id,
            observed_at=bundle.as_of,
            monthly="NONE",
            weekly="NONE",
            daily="NONE",
            # The raw states are GREEN; the unreconciled research contract
            # deliberately caps that result to AMBER.
            gate="AMBER",
            grade="RESEARCH_ONLY",
            snapshot_id=sha256_json({"schema": "test-sector-native-daily-gate"}),
            source_revision=sha256_json({"schema": "test-sector-native-daily-source"}),
            reason_codes=(blocker,),
            period_diagnostics=diagnostics,
            session_evidence=HigherTimeframeSessionEvidence.exact(),
            warmup_evidence=selected_warmup,
            sector_source_mode=QMT_SECTOR_NATIVE_DAILY_RESEARCH_SOURCE_MODE,
            sector_strict_same_base_warmup_evidence=strict_warmup,
            sector_strict_same_base_source_coverage_evidence=strict_coverage,
            sector_research_bridge_parameter_set_id=str(
                sector_native_daily_research_bridge_contract()["parameter_set_id"]
            ),
        )
        return replace(
            bundle,
            higher_timeframe_gates=HigherTimeframeGateBundle(
                market=unresolved.market,
                sector=sector,
                symbol=unresolved.symbol,
            ),
            enforce_higher_timeframe_entry_gate=True,
        )


def _native_daily_evidence(symbol: str) -> QmtNativeDailyReconciliationEvidence:
    return QmtNativeDailyReconciliationEvidence(
        symbol=symbol,
        observed_at=AS_OF,
        native_daily_bar_count=600,
        one_minute_daily_bar_count=240,
        overlap_session_count=240,
        first_overlap_session="2025-08-01",
        last_overlap_session=AS_OF.date().isoformat(),
        native_daily_content_revision="sha256:" + "1" * 64,
        one_minute_base_revision="sha256:" + "2" * 64,
        price_basis_revision="sha256:" + "3" * 64,
        trading_calendar_revision="sha256:" + "4" * 64,
        price_tolerance_quanta=1,
        price_difference_identities=(),
        max_observed_price_difference_quanta=0,
        reconciled_source_revision="sha256:" + "5" * 64,
    )


def _native_daily_calendar_evidence(
    symbol: str,
    *,
    missing: tuple[date, ...] = (),
) -> QmtNativeDailyCalendarCoverageEvidence:
    return QmtNativeDailyCalendarCoverageEvidence(
        symbol=symbol,
        observed_at=AS_OF,
        native_first_session=date(2024, 1, 2),
        native_last_session=AS_OF.date(),
        calendar_first_session=date(2024, 1, 2),
        calendar_last_session=AS_OF.date(),
        native_daily_bar_count=600 - len(missing),
        expected_calendar_session_count=600,
        native_only_sessions=(),
        unexplained_calendar_only_sessions=missing,
        trading_calendar_revision="sha256:" + "4" * 64,
        status=("EXACT" if not missing else "UNEXPLAINED_CALENDAR_SESSION_MISSING"),
    )


class NativeDailyEvidenceMarketData(ActionableMarketData):
    def structure_bundle(self, code: str, **kwargs) -> SymbolStructureBundle:
        bundle = super().structure_bundle(code, **kwargs)
        unresolved = unresolved_higher_timeframe_gates(
            symbol=code,
            observed_at=bundle.as_of,
            reason_code="QMT_NATIVE_DAILY_TEST_UNRESOLVED",
            sector_subject=bundle.sector.sector_id,
        )
        assert unresolved.sector is not None
        return replace(
            bundle,
            higher_timeframe_gates=HigherTimeframeGateBundle(
                market=replace(
                    unresolved.market,
                    native_daily_reconciliation_evidence=(
                        _native_daily_evidence("SH.000300")
                    ),
                    native_daily_calendar_coverage_evidence=(
                        _native_daily_calendar_evidence("SH.000300")
                    ),
                ),
                sector=unresolved.sector,
                symbol=replace(
                    unresolved.symbol,
                    native_daily_reconciliation_evidence=(_native_daily_evidence(code)),
                    native_daily_calendar_coverage_evidence=(
                        _native_daily_calendar_evidence(code)
                    ),
                ),
            ),
            enforce_higher_timeframe_entry_gate=True,
        )


class NativeDailyCalendarGapMarketData(ActionableMarketData):
    def structure_bundle(self, code: str, **kwargs) -> SymbolStructureBundle:
        bundle = super().structure_bundle(code, **kwargs)
        base = unresolved_higher_timeframe_gates(
            symbol=code,
            observed_at=bundle.as_of,
            reason_code="QMT_NATIVE_DAILY_TEST_UNRESOLVED",
            sector_subject=bundle.sector.sector_id,
        )
        gap = _native_daily_calendar_evidence(
            code,
            missing=(date(2025, 8, 15),),
        )
        symbol_failure = unresolved_higher_timeframe_gates(
            symbol=code,
            observed_at=bundle.as_of,
            reason_code="QMT_NATIVE_DAILY_TRADING_CALENDAR_MISMATCH",
            symbol_native_daily_calendar_coverage_evidence=gap,
        ).symbol
        assert base.sector is not None
        return replace(
            bundle,
            higher_timeframe_gates=HigherTimeframeGateBundle(
                market=replace(
                    base.market,
                    native_daily_reconciliation_evidence=(
                        _native_daily_evidence("SH.000300")
                    ),
                    native_daily_calendar_coverage_evidence=(
                        _native_daily_calendar_evidence("SH.000300")
                    ),
                ),
                sector=base.sector,
                symbol=symbol_failure,
            ),
            enforce_higher_timeframe_entry_gate=True,
        )


class ApproachingMarketData(RecordingMarketData):
    def structure_bundle(
        self,
        code: str,
        *,
        as_of: datetime,
        sector,
        frequencies=(),
    ) -> SymbolStructureBundle:
        del frequencies
        self.bundle_codes.append(code)
        return SymbolStructureBundle(
            code=code,
            as_of=as_of,
            sector=sector,
            thirty_direction="neutral",
            thirty_points=(),
            five_points=(provisional_point("2buy", code=code),),
            one_points=(),
            opposite_points=(),
        )


def test_snapshot_serializes_approaching_point_without_inventing_confirmation(
    tmp_path: Path,
) -> None:
    service = TradingScreeningService(
        market_data=ApproachingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=HumanAssistedDecisionCore(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
    )

    payload = service.refresh_now()

    assert payload["scan_state"] == "complete"
    assert len(payload["signals"]) == 1
    signal = payload["signals"][0]
    assert signal["lifecycle_stage"] == "approaching"
    assert signal["name"] == "平安银行"
    assert signal["setup_5m"]["status"] == "provisional"
    assert signal["setup_5m"]["confirmed_at"] is None
    assert signal["entry_allowed"] is False
    assert signal["selection_sources"] == ["QMT_SECTOR_ELIGIBLE_SCOPE"]
    assert signal["sector_triggered"] is False
    assert signal["monitor_only"] is True


def test_supportive_sector_is_the_only_native_sector_trigger(
    tmp_path: Path,
) -> None:
    supportive = replace(
        eligible_sector(),
        regime="supportive",
        rank_components=(("thirty_support", 40),),
        reason_codes=("test_supportive",),
    )
    batch = SectorAssessmentBatch(
        assessments=(supportive,),
        discovered_count=1,
        completed_count=1,
        failure_counts=(),
        errors=(),
    )
    catalog = RecordingSectorCatalog(batch)

    payload = TradingScreeningService(
        market_data=ApproachingMarketData(),
        sector_catalog=catalog,
        engine=HumanAssistedDecisionCore(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
    ).refresh_now()

    [signal] = payload["signals"]
    assert signal["selection_sources"] == ["QMT_SECTOR_TRIGGER"]
    assert signal["sector_triggered"] is True
    assert signal["monitor_only"] is True
    assert signal["formal_selection"]["status"] == "UNRESOLVED"
    assert FORMAL_SELECTION_REQUIRED_REASON_CODE in signal["decision_reasons"]


def test_live_service_applies_visible_research_to_formal_buy_entry(
    tmp_path: Path,
) -> None:
    supportive = replace(
        eligible_sector(),
        regime="supportive",
        rank_components=(("thirty_support", 40),),
        reason_codes=("test_supportive",),
    )
    catalog = RecordingSectorCatalog(
        SectorAssessmentBatch(
            assessments=(supportive,),
            discovered_count=1,
            completed_count=1,
            failure_counts=(),
            errors=(),
        )
    )

    payload = TradingScreeningService(
        market_data=ApproachingMarketData(),
        sector_catalog=catalog,
        engine=HumanAssistedDecisionCore(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        selection_research=(valid_selection_research(),),
        clock=lambda: AS_OF,
        notifier=None,
    ).refresh_now()

    [signal] = payload["signals"]
    assert signal["selection_research"]["snapshot_id"] == (
        valid_selection_research().snapshot_id
    )
    assert signal["formal_selection"]["status"] == "PASS"
    assert signal["sector_triggered"] is True
    assert signal["monitor_only"] is False
    assert FORMAL_SELECTION_REQUIRED_REASON_CODE not in signal["decision_reasons"]


def test_snapshot_exposes_exact_session_gap_without_blocking_five_minute_signal(
    tmp_path: Path,
) -> None:
    service = TradingScreeningService(
        market_data=SessionIssueMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=HumanAssistedDecisionCore(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
    )

    [signal] = service.refresh_now()["signals"]
    risk = signal["higher_timeframe_risk"]

    assert signal["entry_allowed"] is True
    assert risk["data_integrity_hard_block_reason_codes"] == []
    assert (
        "QMT_ONE_MINUTE_EXPECTED_SESSION_MISSING"
        in signal["execution_profile"]["advisory_reason_codes"]
    )
    assert (
        "QMT_ONE_MINUTE_EXPECTED_SESSION_MISSING"
        not in signal["execution_profile"]["hard_block_reason_codes"]
    )
    assert risk["session_evidence_contract_id"] == (
        HIGHER_TIMEFRAME_SESSION_EVIDENCE_CONTRACT_ID
    )
    assert risk["market_session_evidence"] == {
        "contract_id": HIGHER_TIMEFRAME_SESSION_EVIDENCE_CONTRACT_ID,
        "status": "EXACT",
        "issue_count": 0,
        "issues": [],
        "entry_disposition": "NO_SESSION_BLOCKER",
    }
    assert risk["symbol_session_evidence"]["entry_disposition"] == ("FAIL_CLOSED")
    assert risk["symbol_session_evidence"]["issues"] == [
        {
            "session": "2026-07-17",
            "code": "QMT_ONE_MINUTE_EXPECTED_SESSION_MISSING",
            "observed_rows": 0,
            "classification": "UNCLASSIFIED_EXPECTED_SESSION_ABSENCE",
            "detail": ("trading-calendar session is absent from the QMT 1m prefix"),
            "historical_trade_status_proven": False,
            "entry_disposition": "FAIL_CLOSED",
        }
    ]


def test_snapshot_binds_mwd_warmup_evidence(
    tmp_path: Path,
) -> None:
    service = TradingScreeningService(
        market_data=WarmupIssueMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=HumanAssistedDecisionCore(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
    )

    payload = service.refresh_now()
    [signal] = payload["signals"]
    risk = signal["higher_timeframe_risk"]

    assert signal["entry_allowed"] is True
    assert risk["data_integrity_hard_block_reason_codes"] == []
    assert "WARMUP_CONVERGENCE_GATE_FAILED" not in signal["decision_reasons"]
    assert risk["warmup_evidence_contract_id"] == (
        QMT_HIGHER_TIMEFRAME_WARMUP_EVIDENCE_CONTRACT_ID
    )
    assert risk["market_warmup_evidence"] == {
        "contract_id": QMT_HIGHER_TIMEFRAME_WARMUP_EVIDENCE_CONTRACT_ID,
        "required_daily_bar_count": 480,
        "full_daily_bar_count": 120,
        "suffix_daily_bar_count": 0,
        "converged": False,
        "reason_code": ("QMT_HIGHER_TIMEFRAME_WARMUP_HISTORY_INSUFFICIENT"),
        "full_signature": "sha256:" + "4" * 64,
        "suffix_signature": None,
        "entry_disposition": "FAIL_CLOSED",
    }
    assert risk["warmup_convergence_contract_id"] == (
        WARMUP_CONVERGENCE_ENVELOPE_CONTRACT_ID
    )
    assert risk["market_warmup_convergence_evidence"]["status"] == (
        "INSUFFICIENT_PREFIXES"
    )
    assert risk["sector_warmup_convergence_evidence"]["active_gate_unchanged"] is True
    assert risk["symbol_warmup_convergence_evidence"]["diagnostic_only"] is True


def test_newer_one_minute_signal_uses_frozen_mwd_cutoff_and_is_self_consistent(
    tmp_path: Path,
) -> None:
    """A 15:00 trigger may retain sector/MWD evidence frozen at 14:55."""

    risk_cutoff = AS_OF - timedelta(minutes=5)

    def context(frequency: str):
        return replace(
            neutral_context(frequency),
            observed_at=risk_cutoff,
        )

    assessment = replace(
        eligible_sector(),
        thirty_context=context("30m"),
        five_context=context("5m"),
        one_context=context("1m"),
    )
    catalog = RecordingSectorCatalog(
        SectorAssessmentBatch(
            assessments=(assessment,),
            discovered_count=1,
            completed_count=1,
            failure_counts=(),
            errors=(),
        )
    )
    market_data = CausalCutoffMarketData(
        signal_at=AS_OF,
        risk_cutoff=risk_cutoff,
    )
    service = TradingScreeningService(
        market_data=market_data,
        sector_catalog=catalog,
        engine=HumanAssistedDecisionCore(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
    )

    payload = service.refresh_now()

    assert payload["market_data_as_of"] == risk_cutoff.isoformat()
    assert market_data.cutoff_calls == [risk_cutoff]
    [signal] = payload["signals"]
    assert signal["observed_at"] == AS_OF.isoformat()
    risk = signal["higher_timeframe_risk"]
    for subject in ("market", "sector", "symbol"):
        assert (
            risk[f"{subject}_warmup_convergence_evidence"]["as_of"]
            == risk_cutoff.isoformat()
        )


def test_presentation_snapshot_compacts_audit_only_evidence(
    tmp_path: Path,
) -> None:
    service = TradingScreeningService(
        market_data=WarmupIssueMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=HumanAssistedDecisionCore(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
    )

    full = service.refresh_now()
    presentation = service.presentation_snapshot()
    [full_signal] = full["signals"]
    [visible_signal] = presentation["signals"]
    full_risk = full_signal["higher_timeframe_risk"]
    visible_risk = visible_signal["higher_timeframe_risk"]

    assert presentation["presentation_schema"] == (
        "chanlun-trading-screening-presentation"
    )
    assert str(presentation["presentation_revision"]).startswith("sha256:")
    assert presentation["sector_catalog_overlay"]["source"] == ("PUBLISHED_SNAPSHOT")
    assert presentation["sector_catalog_overlay"]["decision_authoritative"] is True
    assert (
        presentation["source_snapshot_content_sha256"]
        == (full["snapshot_content_sha256"])
    )
    assert (
        visible_signal["decision_document_id"] == (full_signal["decision_document_id"])
    )
    assert visible_signal["presentation_projection"] is True
    assert visible_signal["full_audit_evidence_embedded"] is False
    assert "market_warmup_convergence_evidence" in full_risk
    assert "market_warmup_convergence_evidence" not in visible_risk
    assert "market_warmup_evidence" not in visible_risk
    assert visible_risk["market_gate"] == full_risk["market_gate"]
    assert visible_risk["market_reason_codes"] == full_risk["market_reason_codes"]
    assert visible_signal["chart_urls"] == full_signal["chart_urls"]
    for field in ("sector_id", "sector_name", "eligible", "hard_block", "reason_codes"):
        assert visible_signal["sector"][field] == full_signal["sector"][field]
    assert visible_signal["setup_5m"]["status"] == (full_signal["setup_5m"]["status"])
    assert (
        visible_signal["setup_5m"]["formation_state"]
        == (full_signal["setup_5m"]["formation_state"])
    )
    assert (
        visible_signal["setup_5m"]["lock_state"]
        == (full_signal["setup_5m"]["lock_state"])
    )
    assert (
        visible_signal["setup_5m"]["actionable"]
        == (full_signal["setup_5m"]["actionable"])
    )
    for field in (
        "point_id",
        "point_type",
        "side",
        "source_frequency",
        "recursive_level",
        "anchor_at",
        "confirmed_at",
        "available_at",
    ):
        assert visible_signal["setup_5m"][field] == full_signal["setup_5m"][field]
    assert visible_signal["warmup"]["converged"] == (full_signal["warmup"]["converged"])
    assert "decision_core_id" not in visible_signal
    full_size = len(json.dumps(full, ensure_ascii=False))
    visible_size = len(json.dumps(presentation, ensure_ascii=False))
    assert visible_size < full_size * 0.9

    visible_signal["code"] = "MUTATED"
    again = service.presentation_snapshot()
    assert again["signals"][0]["code"] == "SZ.000001"
    assert service.snapshot()["signals"][0]["code"] == "SZ.000001"
    first_reference = service.presentation_snapshot_reference()
    second_reference = service.presentation_snapshot_reference()
    assert first_reference is second_reference
    assert first_reference is not again


def test_presentation_snapshot_excludes_invalidated_rows_from_current_selection(
    tmp_path: Path,
) -> None:
    service = TradingScreeningService(
        market_data=WarmupIssueMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=HumanAssistedDecisionCore(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
    )

    full = service.refresh_now()
    assert len(full["signals"]) == 1
    with service._state_lock:
        service._snapshot["signals"][0]["lifecycle_stage"] = "invalidated"
        service._presentation_cache = None
        service._presentation_cache_sha256 = None

    presentation = service.presentation_snapshot()

    assert service.snapshot()["signals"][0]["lifecycle_stage"] == "invalidated"
    assert presentation["signals"] == []
    assert presentation["counts_by_stage"] == {}
    assert all(count == 0 for count in presentation["counts_by_point_type"].values())


def test_presentation_projection_preserves_structure_occurrence_identity() -> None:
    setup_available_at = AS_OF.replace(hour=10, minute=5).isoformat()
    trigger_available_at = AS_OF.replace(hour=10, minute=6).isoformat()
    projected = trading_screening_subject._presentation_signal_document(
        {
            "signal_id": "sha256:" + "1" * 64,
            "code": "SZ.000001",
            "point_type": "1buy",
            "side": "buy",
            "lifecycle_stage": "triggered",
            "setup_5m": {
                "status": "confirmed",
                "point_id": "setup-point",
                "point_type": "1buy",
                "side": "buy",
                "source_frequency": "5m",
                "recursive_level": 0,
                "anchor_at": setup_available_at,
                "confirmed_at": setup_available_at,
                "available_at": setup_available_at,
                "evidence_codes": [],
                "missing_conditions": [],
            },
            "segment_difference_1m": {
                "status": "confirmed",
                "point_id": "segment-point",
                "point_type": "1buy",
                "side": "buy",
                "source_frequency": "1m",
                "recursive_level": 0,
                "anchor_at": trigger_available_at,
                "confirmed_at": trigger_available_at,
                "available_at": trigger_available_at,
                "divergence_kind": "trend",
                "evidence_codes": ["strict_segment_difference"],
                "missing_conditions": [],
            },
        }
    )

    assert projected["setup_5m"]["point_id"] == "setup-point"
    assert projected["setup_5m"]["source_frequency"] == "5m"
    assert projected["setup_5m"]["available_at"] == setup_available_at
    assert projected["segment_difference_1m"] == {
        "point_id": "segment-point",
        "status": "confirmed",
        "point_type": "1buy",
        "side": "buy",
        "source_frequency": "1m",
        "recursive_level": 0,
        "anchor_at": trigger_available_at,
        "confirmed_at": trigger_available_at,
        "available_at": trigger_available_at,
        "divergence_kind": "trend",
        "evidence_codes": ["strict_segment_difference"],
        "missing_conditions": [],
    }


def test_current_selection_rejects_terminal_lineage_stage_contradictions() -> None:
    base = {
        "code": "SZ.000001",
        "point_type": "3buy",
        "lifecycle_stage": "approaching",
        "setup_5m": {
            "status": "provisional",
            "point_type": "3buy",
            "evidence_codes": [
                "provisional_center_completion",
                "core_boundary_held",
            ],
            "missing_conditions": ["terminal_unit_locked"],
            "terminal_segment_role": "latest_unfinished",
            "terminal_segment_state": "forming",
        },
    }

    assert _is_current_selection_signal(base) is True
    assert _is_current_selection_signal({**base, "lifecycle_stage": "formed"}) is False
    completed = {
        **base,
        "lifecycle_stage": "formed",
        "setup_5m": {
            **base["setup_5m"],
            "terminal_segment_role": "latest_completed",
            "terminal_segment_state": "formed",
        },
    }
    assert _is_current_selection_signal(completed) is False
    assert (
        _is_current_selection_signal({**completed, "lifecycle_stage": "approaching"})
        is False
    )
    confirmed = {
        **completed,
        "lifecycle_stage": "triggered",
        "setup_5m": {
            **completed["setup_5m"],
            "status": "confirmed",
        },
    }
    assert _is_current_selection_signal(confirmed) is True


def test_presentation_snapshot_exposes_current_sector_catalog_during_first_scan(
    tmp_path: Path,
) -> None:
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
    )
    service._coverage_cycle_sector_batch = SectorAssessmentBatch(
        assessments=(eligible_sector(),),
        discovered_count=1,
        completed_count=1,
        failure_counts=(),
        errors=(),
    )

    presentation = service.presentation_snapshot()

    assert service.snapshot()["available"] is False
    assert service.snapshot()["sectors"] == []
    assert [row["sector_id"] for row in presentation["sectors"]] == [
        eligible_sector().sector_id
    ]
    assert presentation["sector_catalog_overlay"] == {
        "schema": "chanlun-sector-catalog-page-overlay",
        "active": True,
        "source": "CURRENT_COVERAGE_CYCLE",
        "provisional": True,
        "decision_authoritative": False,
        "display_only": True,
        "sector_count": 1,
        "completion_ratio": "1",
        "archival_snapshot_unchanged": True,
    }


def test_presentation_snapshot_restores_validated_cached_sector_catalog(
    tmp_path: Path,
) -> None:
    batch = SectorAssessmentBatch(
        assessments=(eligible_sector(),),
        discovered_count=1,
        completed_count=1,
        failure_counts=(),
        errors=(),
    )

    class CachedCatalog(RecordingSectorCatalog):
        def __init__(self) -> None:
            super().__init__()
            self.cache_calls = 0

        def cached_sector_snapshot_for_priority(self, *, as_of: datetime):
            self.cache_calls += 1
            return CachedSectorSnapshot(
                batch=batch,
                members={eligible_sector().sector_id: ("SZ.000001",)},
                requested_as_of=as_of - timedelta(minutes=5),
                current_decision_epoch=False,
                content_sha256="sha256:" + "7" * 64,
            )

    catalog = CachedCatalog()
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=catalog,
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
    )

    first = service.presentation_snapshot()
    second = service.presentation_snapshot()

    assert [row["sector_id"] for row in first["sectors"]] == [
        eligible_sector().sector_id
    ]
    assert first["sector_catalog_overlay"]["source"] == "CACHED_SECTOR_SNAPSHOT"
    assert first["sector_catalog_overlay"]["decision_authoritative"] is False
    assert second["sectors"] == first["sectors"]
    assert catalog.cache_calls == 1


def test_rebuilding_invalidated_snapshot_keeps_runtime_ready(
    tmp_path: Path,
) -> None:
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(
            full_coverage_refresh_enabled=True,
            large_scope_authorized=True,
        ),
    )
    service.refresh_now()
    with service._state_lock:
        invalidated = dict(service._snapshot)
        invalidated["scan_state"] = "coverage_epoch_invalidated"
        invalidated["last_batch_state"] = "coverage_epoch_invalidated"
        invalidated["snapshot_content_sha256"] = None
        service._snapshot = invalidated
        service._validated_snapshot_sha256 = None
    with service._background_lock:
        service._background_thread = threading.current_thread()
        service._background_heartbeat_at = AS_OF
        service._background_refresh_started_at = AS_OF

    health = service.health_snapshot()

    assert health["snapshot_rebuild_in_progress"] is True
    assert health["runtime_ready"] is True
    assert health["selection_ready"] is False
    assert health["reasons"] == []


def test_initial_snapshot_build_reports_rebuild_in_progress(
    tmp_path: Path,
) -> None:
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(
            full_coverage_refresh_enabled=True,
            large_scope_authorized=True,
        ),
    )
    with service._background_lock:
        service._background_thread = threading.current_thread()
        service._background_heartbeat_at = AS_OF
        service._background_refresh_started_at = AS_OF

    health = service.health_snapshot()

    assert health["scan_state"] == "not_started"
    assert health["snapshot_available"] is False
    assert health["snapshot_rebuild_in_progress"] is True
    assert health["runtime_ready"] is True
    assert health["selection_ready"] is False
    assert health["reasons"] == []


def test_presentation_projection_preserves_explicit_etf_proxy_sector_contract() -> None:
    projected = trading_screening_subject._presentation_signal_document(
        {
            "signal_id": "sha256:" + "1" * 64,
            "code": "SH.513100",
            "name": "纳指ETF国泰",
            "point_type": "3buy",
            "side": "buy",
            "lifecycle_stage": "observed",
            "selection_path": "ETF_PROXY",
            "sector": {
                "sector_id": "etf-proxy:SH.513100",
                "sector_name": "ETF代理",
                "eligible": True,
                "hard_block": False,
                "reason_codes": ["ETF_PROXY_SECTOR_NOT_REQUIRED"],
            },
            "higher_timeframe_risk": {
                "market_gate": "GREEN",
                "sector_gate": "UNRESOLVED",
                "symbol_gate": "GREEN",
                "sector_reason_codes": [
                    "QMT_SECTOR_HIGHER_TIMEFRAME_INPUT_UNAVAILABLE"
                ],
            },
        }
    )

    assert projected["selection_path"] == "ETF_PROXY"
    assert projected["sector"] == {
        "sector_id": "etf-proxy:SH.513100",
        "sector_name": "ETF代理",
        "eligible": True,
        "hard_block": False,
        "reason_codes": ["ETF_PROXY_SECTOR_NOT_REQUIRED"],
    }


def test_presentation_projection_preserves_legacy_sell_only_gate_declaration() -> None:
    projected = trading_screening_subject._presentation_signal_document(
        {
            "signal_id": "sha256:" + "2" * 64,
            "code": "SH.600080",
            "name": "金花股份",
            "point_type": "1sell",
            "side": "sell",
            "lifecycle_stage": "formed",
            "selection_path": "INDIVIDUAL_THREE_PROGRAM",
            "entry_allowed": False,
            "technical_entry_allowed": False,
            "exit_allowed": True,
            "higher_timeframe_risk": {
                "market_gate": "UNRESOLVED",
                "sector_gate": "UNRESOLVED",
                "symbol_gate": "UNRESOLVED",
                "new_entry_requires_all_green": False,
                "market_reason_codes": ["HIGHER_TIMEFRAME_GATE_NOT_ATTACHED"],
                "sector_reason_codes": ["HIGHER_TIMEFRAME_SECTOR_GATE_NOT_ATTACHED"],
                "symbol_reason_codes": ["HIGHER_TIMEFRAME_GATE_NOT_ATTACHED"],
                "reason_codes": [
                    "HIGHER_TIMEFRAME_GATE_NOT_ATTACHED",
                    "HIGHER_TIMEFRAME_SECTOR_GATE_NOT_ATTACHED",
                ],
            },
        }
    )

    assert projected["point_type"] == "1sell"
    assert projected["side"] == "sell"
    assert projected["selection_path"] == "INDIVIDUAL_THREE_PROGRAM"
    assert projected["entry_allowed"] is False
    assert projected["technical_entry_allowed"] is False
    assert projected["higher_timeframe_risk"] == {
        "market_gate": "UNRESOLVED",
        "sector_gate": "UNRESOLVED",
        "symbol_gate": "UNRESOLVED",
        "new_entry_requires_all_green": False,
        "reason_codes": [
            "HIGHER_TIMEFRAME_GATE_NOT_ATTACHED",
            "HIGHER_TIMEFRAME_SECTOR_GATE_NOT_ATTACHED",
        ],
        "market_reason_codes": ["HIGHER_TIMEFRAME_GATE_NOT_ATTACHED"],
        "sector_reason_codes": ["HIGHER_TIMEFRAME_SECTOR_GATE_NOT_ATTACHED"],
        "symbol_reason_codes": ["HIGHER_TIMEFRAME_GATE_NOT_ATTACHED"],
    }


def test_snapshot_binds_native_daily_reconciliation(
    tmp_path: Path,
) -> None:
    service = TradingScreeningService(
        market_data=NativeDailyEvidenceMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=HumanAssistedDecisionCore(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
    )

    payload = service.refresh_now()
    [signal] = payload["signals"]
    risk = signal["higher_timeframe_risk"]

    assert risk["native_daily_reconciliation_contract_id"] == (
        QMT_NATIVE_DAILY_RECONCILIATION_CONTRACT_ID
    )
    assert risk["market_native_daily_reconciliation_evidence"]["symbol"] == (
        "SH.000300"
    )
    assert risk["sector_native_daily_reconciliation_evidence"] is None
    assert risk["symbol_native_daily_reconciliation_evidence"]["symbol"] == (
        "SZ.000001"
    )
    assert (
        risk["symbol_native_daily_reconciliation_evidence"]["intraday_role"]
        == "ONE_MINUTE_DERIVED_30M_AND_DAILY_TAIL"
    )
    assert risk["native_daily_calendar_coverage_contract_id"] == (
        QMT_NATIVE_DAILY_CALENDAR_COVERAGE_EVIDENCE_CONTRACT_ID
    )
    assert risk["market_native_daily_calendar_coverage_evidence"]["status"] == "EXACT"
    assert risk["sector_native_daily_calendar_coverage_evidence"] is None
    assert (
        risk["symbol_native_daily_calendar_coverage_evidence"]["entry_disposition"]
        == "NO_CALENDAR_BLOCKER"
    )


def test_native_daily_ahead_snapshot_fails_closed(
    tmp_path: Path,
) -> None:
    service = TradingScreeningService(
        market_data=NativeDailyAheadMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=HumanAssistedDecisionCore(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
    )

    payload = service.refresh_now()

    assert payload["coverage_manifest"]["complete"] is True
    assert payload["signals"][0]["higher_timeframe_risk"]["market_gate"] == (
        "UNRESOLVED"
    )
    assert payload["signals"][0]["entry_allowed"] is False


def test_snapshot_binds_unexplained_native_daily_gap_without_claiming_suspension(
    tmp_path: Path,
) -> None:
    service = TradingScreeningService(
        market_data=NativeDailyCalendarGapMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=HumanAssistedDecisionCore(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
    )

    payload = service.refresh_now()
    [signal] = payload["signals"]
    risk = signal["higher_timeframe_risk"]
    coverage = risk["symbol_native_daily_calendar_coverage_evidence"]

    assert signal["entry_allowed"] is False
    assert risk["symbol_gate"] == "UNRESOLVED"
    assert coverage["status"] == "UNEXPLAINED_CALENDAR_SESSION_MISSING"
    assert coverage["unexplained_calendar_only_sessions"] == ["2025-08-15"]
    assert coverage["missing_session_interpretation"] == (
        "UNEXPLAINED_NEVER_INFERRED_AS_SUSPENSION"
    )
    assert coverage["point_in_time_status_evidence_present"] is False
    assert coverage["entry_disposition"] == "FAIL_CLOSED"
    review = market_symbol_higher_timeframe_review_evidence_from_risk(
        risk,
        symbol="SZ.000001",
        observed_at=AS_OF,
    )
    support = review.symbol_evidence.source_support
    assert support is not None
    assert support.native_daily_calendar_coverage_evidence is not None
    assert support.native_daily_calendar_coverage_evidence.status == (
        "UNEXPLAINED_CALENDAR_SESSION_MISSING"
    )


def test_snapshot_authenticates_sector_native_daily_research_cap(
    tmp_path: Path,
) -> None:
    service = TradingScreeningService(
        market_data=SectorNativeDailyResearchMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=HumanAssistedDecisionCore(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
    )

    payload = service.refresh_now()
    [signal] = payload["signals"]
    risk = signal["higher_timeframe_risk"]

    assert risk["sector_gate"] == "AMBER"
    assert risk["sector_higher_timeframe_source_mode"] == (
        QMT_SECTOR_NATIVE_DAILY_RESEARCH_SOURCE_MODE
    )
    assert risk["sector_strict_same_5m_warmup_evidence"]["reason_code"] == (
        "QMT_HIGHER_TIMEFRAME_WARMUP_HISTORY_INSUFFICIENT"
    )
    assert (
        risk["sector_strict_same_5m_source_coverage_evidence"][
            "remaining_daily_bar_count"
        ]
        == 360
    )
    assert (
        risk["sector_research_bridge_parameter_set_id"]
        == (sector_native_daily_research_bridge_contract()["parameter_set_id"])
    )


def test_priority_monitor_continuation_preserves_sector_source_provenance(
    tmp_path: Path,
) -> None:
    service = TradingScreeningService(
        market_data=SectorNativeDailyResearchMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=HumanAssistedDecisionCore(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
    )
    [signal] = service.refresh_now()["signals"]

    continuation = trading_screening_subject._priority_monitor_continuation_document(
        signal
    )
    presentation_risk = trading_screening_subject._presentation_signal_document(signal)[
        "higher_timeframe_risk"
    ]
    continuation_risk = continuation["higher_timeframe_risk"]
    sector_source_fields = (
        "sector_higher_timeframe_source_mode",
        "sector_strict_same_5m_warmup_evidence",
        "sector_strict_same_5m_source_coverage_evidence",
        "sector_research_bridge_parameter_set_id",
    )

    assert {field: continuation_risk[field] for field in sector_source_fields} == {
        field: presentation_risk[field] for field in sector_source_fields
    }
    assert (
        trading_screening_subject.validate_signal_decision_document(continuation)
        == signal["decision_document_id"]
    )
    assert continuation["monitor_continuation"] is True
    assert continuation["presentation_projection"] is False

    code = str(signal["code"])
    service._record_priority_monitor_result(
        observed_at=AS_OF,
        codes=(),
        errors=(),
        documents=(signal,),
        successful_codes=(code,),
        lanes_by_code={
            code: trading_screening_subject.CANDIDATE_MONITOR_LANE_5M,
        },
        five_universe=(code,),
        thirty_universe=(),
        five_codes=(code,),
        successful_five_codes=(code,),
    )
    [live_overlay] = service.presentation_snapshot()["signals"]
    overlay_risk = live_overlay["higher_timeframe_risk"]
    assert {field: overlay_risk[field] for field in sector_source_fields} == {
        field: presentation_risk[field] for field in sector_source_fields
    }


def test_signal_identity_survives_service_restart(tmp_path: Path) -> None:
    cache_path = tmp_path / "snapshot.json"
    first_service = TradingScreeningService(
        market_data=ActionableMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=HumanAssistedDecisionCore(),
        scan_planner=RecordingPlanner(),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
    )
    first = first_service.refresh_now()
    second_service = TradingScreeningService(
        market_data=ActionableMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=HumanAssistedDecisionCore(),
        scan_planner=RecordingPlanner(),
        cache_path=cache_path,
        clock=lambda: AS_OF + timedelta(minutes=1),
        notifier=None,
    )

    second = second_service.refresh_now()

    assert len(first["signals"]) == len(second["signals"]) == 1
    assert first["signals"][0]["signal_id"] == second["signals"][0]["signal_id"]
    assert first["signals"][0]["lifecycle_stage"] == "triggered"
    # A process restart with the same frozen market cutoff must not advance a
    # lifecycle merely because wall-clock time passed.
    assert second["signals"][0]["lifecycle_stage"] == "triggered"
    assert second["signals"][0]["chart_urls"] == {
        "d": "/?market=a&code=SZ.000001&layout=single&intervals=D",
        "30m": "/?market=a&code=SZ.000001&layout=single&intervals=30",
        "5m": "/?market=a&code=SZ.000001&layout=single&intervals=5",
        "1m": "/?market=a&code=SZ.000001&layout=single&intervals=1",
    }


def test_confirmed_signal_serializes_causal_and_price_basis_evidence(
    tmp_path: Path,
) -> None:
    service = TradingScreeningService(
        market_data=ActionableMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=HumanAssistedDecisionCore(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
    )

    [signal] = service.refresh_now()["signals"]

    assert (
        signal["setup_5m"]["available_at"]
        == confirmed_point("2buy", minutes_after=295).available_at.isoformat()
    )
    assert signal["setup_5m"]["price_basis_revision"] == "test-raw"
    assert signal["setup_5m"]["tower"] == "formal"
    assert (
        signal["segment_difference_1m"]["available_at"]
        == confirmed_point(
            "1buy", frequency="1m", minutes_after=296
        ).available_at.isoformat()
    )


class AdmissionOrderAssertingNotifier:
    def __init__(self, cache_path: Path) -> None:
        self.cache_path = cache_path
        self.calls = 0
        self.cache_existed_during_dispatch: bool | None = None

    def dispatch_changes(self, previous, current) -> None:
        del previous, current
        self.cache_existed_during_dispatch = self.cache_path.exists()
        self.calls += 1


def test_notifier_admission_precedes_snapshot_checkpoint(tmp_path: Path) -> None:
    cache_path = tmp_path / "snapshot.json"
    notifier = AdmissionOrderAssertingNotifier(cache_path)
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=notifier,
    )

    snapshot = service.refresh_now()

    assert notifier.calls == 1
    assert notifier.cache_existed_during_dispatch is False
    assert json.loads(cache_path.read_text(encoding="utf-8")) == snapshot


def test_incomplete_frozen_coverage_never_emits_realtime_notification(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "snapshot.json"
    notifier = AdmissionOrderAssertingNotifier(cache_path)
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=MultiMemberSectorCatalog(
            ("SZ.000001", "SZ.000002", "SZ.000003")
        ),
        engine=RecordingEngine(),
        scan_planner=SequencedPlanner((("SZ.000001", "SZ.000002", "SZ.000003"),)),
        cache_path=cache_path,
        clock=lambda: AS_OF.replace(hour=14, minute=58),
        notifier=notifier,
        config=TradingScreeningConfig(max_symbols_per_refresh=1),
    )

    snapshot = service.refresh_now()

    assert snapshot["coverage_manifest"]["complete"] is False
    assert snapshot["notification_context"] == {
        "schema": "chanlun-realtime-notification-context",
        "realtime_eligible": False,
        "reason_code": "COVERAGE_IN_PROGRESS",
        "source": "FROZEN_COVERAGE",
        "observed_at": AS_OF.replace(hour=14, minute=58).isoformat(),
        "market_data_as_of": AS_OF.replace(hour=14, minute=58).isoformat(),
        "market_data_age_seconds": 0.0,
        "max_age_seconds": 180,
        "uses_completed_minute_bars_only": True,
    }
    assert notifier.calls == 0


def test_complete_but_stale_coverage_never_emits_realtime_notification(
    tmp_path: Path,
) -> None:
    cutoff = AS_OF.replace(hour=14, minute=30)
    observed_at = AS_OF.replace(hour=14, minute=58)

    stale_sector = replace(
        eligible_sector(),
        thirty_context=replace(neutral_context("30m"), observed_at=cutoff),
        five_context=replace(neutral_context("5m"), observed_at=cutoff),
        one_context=replace(neutral_context("1m"), observed_at=cutoff),
    )
    stale_batch = SectorAssessmentBatch(
        assessments=(stale_sector,),
        discovered_count=1,
        completed_count=1,
        failure_counts=(),
        errors=(),
    )

    cache_path = tmp_path / "snapshot.json"
    notifier = AdmissionOrderAssertingNotifier(cache_path)
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(stale_batch),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=cache_path,
        clock=lambda: observed_at,
        notifier=notifier,
    )

    snapshot = service.refresh_now()

    assert snapshot["coverage_manifest"]["complete"] is True
    assert snapshot["notification_context"]["realtime_eligible"] is False
    assert (
        snapshot["notification_context"]["reason_code"]
        == "FROZEN_COVERAGE_CUTOFF_STALE"
    )
    assert notifier.calls == 0


def test_service_batches_discovered_symbols_without_losing_pending_scope(
    tmp_path: Path,
) -> None:
    market = RecordingMarketData()
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=SequencedPlanner((("SZ.000001", "SZ.000002", "SZ.000003"), ())),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(max_symbols_per_refresh=2),
    )

    first = service.refresh_now()
    second = service.refresh_now()

    assert market.bundle_codes == ["SZ.000001", "SZ.000002", "SZ.000003"]
    assert first["scan_audit"]["discovered_symbol_count"] == 3
    assert first["scan_audit"]["pending_symbol_count"] == 1
    assert second["scan_audit"]["pending_symbol_count"] == 0


def test_priority_monitor_uses_current_bars_while_coverage_epoch_stays_frozen(
    tmp_path: Path,
) -> None:
    """A long coverage queue may not delay owned/watchlisted observations."""

    symbols = ("SZ.000001", "SZ.000002", "SZ.000003", "SZ.000004")
    observed_at = [AS_OF.replace(hour=14, minute=58)]

    class WatchlistMarket(RecordingMarketData):
        def __init__(self) -> None:
            super().__init__()
            self.bundle_observations: list[tuple[str, datetime]] = []

        def active_watchlist(self) -> tuple[str, ...]:
            return (symbols[0],)

        def structure_bundle(self, code: str, **kwargs) -> SymbolStructureBundle:
            self.bundle_observations.append((code, kwargs["as_of"]))
            return super().structure_bundle(code, **kwargs)

    supportive = replace(eligible_sector(), regime="supportive")
    catalog = MultiMemberSectorCatalog(symbols)
    catalog.batch = SectorAssessmentBatch(
        assessments=(supportive,),
        discovered_count=1,
        completed_count=1,
        failure_counts=(),
        errors=(),
    )
    market = WatchlistMarket()
    state_path = tmp_path / "trading_priority_monitor_state.json"
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=catalog,
        engine=RecordingEngine(),
        scan_planner=SequencedPlanner((symbols,)),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: observed_at[0],
        notifier=None,
        config=TradingScreeningConfig(
            max_symbols_per_refresh=1,
            priority_monitoring_enabled=True,
            max_five_minute_candidate_symbols_per_refresh=2,
            max_thirty_minute_candidate_symbols_per_refresh=2,
            priority_monitor_interval_seconds=60,
        ),
    )

    first = service.refresh_now()
    frozen_as_of = observed_at[0]
    assert first["coverage_manifest"]["completed_codes"] == list(symbols[:2])
    assert set(first["coverage_manifest"]["pending_frequencies"]) == set(symbols[2:])

    observed_at[0] += timedelta(minutes=1)
    second = service.refresh_now()

    assert market.bundle_observations == [
        # The 5m/30m candidate lanes start immediately with an otherwise
        # unscanned supportive-sector member, without changing coverage.
        (symbols[2], frozen_as_of),
        (symbols[0], frozen_as_of),
        (symbols[1], frozen_as_of),
        # One minute later the explicit watchlist is always in the current 1m
        # lane, while the archival queue keeps its original frozen cutoff.
        (symbols[0], observed_at[0]),
        (symbols[2], frozen_as_of),
    ]
    # Sector selection stays frozen for the coverage/preselection epoch;
    # only stock bars are observed again one minute later.
    assert catalog.assessment_calls == [frozen_as_of]
    assert second["as_of"] == frozen_as_of.isoformat()
    assert second["coverage_manifest"]["completed_codes"] == list(symbols[:3])
    assert set(second["coverage_manifest"]["pending_frequencies"]) == {symbols[3]}
    priority_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert priority_state["last_at"] == observed_at[0].isoformat()
    assert priority_state["last_codes"] == [symbols[0]]
    assert priority_state["candidate_monitor_contract_id"] == (
        "bar-cadence-live-candidate-monitor-v5-validation-liveness"
    )
    assert priority_state["screening_policy_id"] == second["screening_policy_id"]
    assert priority_state["decision_core_id"] == service._decision_core_id
    assert priority_state["sector_source_mode"] == "FROZEN_COVERAGE_EPOCH"
    assert priority_state["sector_as_of"] == frozen_as_of.isoformat()
    assert priority_state["sector_coverage_epoch_id"] == first["coverage_epoch_id"]
    health = service.health_snapshot()
    assert health["priority_monitor_ready"] is True
    assert health["priority_monitor_status"] == "verified"
    assert health["priority_monitor_last_codes"] == [symbols[0]]
    assert health["candidate_monitor_status"] == "warming"
    assert health["priority_monitor_sector_source_mode"] == ("FROZEN_COVERAGE_EPOCH")
    assert health["priority_monitor_sector_as_of"] == frozen_as_of.isoformat()
    assert (
        health["priority_monitor_sector_coverage_epoch_id"]
        == first["coverage_epoch_id"]
    )
    assert health["priority_monitor_due"] is False


def test_priority_state_capture_and_write_are_serialized_in_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(priority_monitoring_enabled=True),
    )
    worker_count = 4
    start_gate = threading.Barrier(worker_count)
    counter_lock = threading.Lock()
    active = 0
    maximum_active = 0
    call_count = 0

    def persist_serialized() -> None:
        nonlocal active, maximum_active, call_count
        with counter_lock:
            active += 1
            maximum_active = max(maximum_active, active)
        time.sleep(0.02)
        with counter_lock:
            active -= 1
            call_count += 1

    monkeypatch.setattr(
        service,
        "_persist_priority_monitor_state_serialized",
        persist_serialized,
    )

    def persist() -> None:
        start_gate.wait()
        service._persist_priority_monitor_state()

    threads = tuple(threading.Thread(target=persist) for _ in range(worker_count))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert all(not thread.is_alive() for thread in threads)
    assert call_count == worker_count
    assert maximum_active == 1


def test_priority_only_refresh_does_not_consume_archival_coverage_queue(
    tmp_path: Path,
) -> None:
    symbols = ("SZ.000001", "SZ.000002", "SZ.000003", "SZ.000004")
    observed_at = [AS_OF.replace(hour=14, minute=58)]

    class RestartRoutingMarket(RecordingMarketData):
        def __init__(self) -> None:
            super().__init__()
            self.routing_ready = True

        def structure_bundle(self, *args, **kwargs) -> SymbolStructureBundle:
            if not self.routing_ready:
                raise RuntimeError("authenticated sector routing was not restored")
            return super().structure_bundle(*args, **kwargs)

    market = RestartRoutingMarket()

    class RestorableCatalog(MultiMemberSectorCatalog):
        def __init__(self) -> None:
            super().__init__(symbols)
            self.restore_calls: list[tuple[datetime, str]] = []

        def restore_authenticated_sector_members(
            self,
            *,
            members,
            as_of: datetime,
            catalog_revision: str,
        ) -> None:
            assert members == {eligible_sector().sector_id: symbols}
            self.restore_calls.append((as_of, catalog_revision))
            market.routing_ready = True

    catalog = RestorableCatalog()
    supportive = replace(eligible_sector(), regime="supportive")
    catalog.batch = SectorAssessmentBatch(
        assessments=(supportive,),
        discovered_count=1,
        completed_count=1,
        failure_counts=(),
        errors=(),
    )
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=catalog,
        engine=RecordingEngine(),
        scan_planner=SequencedPlanner((symbols,)),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: observed_at[0],
        notifier=None,
        config=TradingScreeningConfig(
            max_symbols_per_refresh=1,
            priority_monitoring_enabled=True,
            max_five_minute_candidate_symbols_per_refresh=2,
            max_thirty_minute_candidate_symbols_per_refresh=2,
            priority_monitor_interval_seconds=60,
        ),
    )

    first = service.refresh_now()
    first_manifest = first["coverage_manifest"]
    first_scanned_at = first["scanned_at"]
    first_bundle_count = len(market.bundle_codes)
    first_sector_read_count = len(catalog.assessment_calls)
    market.routing_ready = False
    service._coverage_cycle_sector_runtime_hydrated = False

    observed_at[0] += timedelta(minutes=1)
    second = service.refresh_now(priority_only=True)

    assert second["snapshot_content_sha256"] == first["snapshot_content_sha256"]
    assert second["coverage_manifest"] == first_manifest
    assert second["scanned_at"] == first_scanned_at
    assert len(catalog.assessment_calls) == first_sector_read_count
    assert len(catalog.restore_calls) == 1
    assert catalog.restore_calls[0][0] == observed_at[0] - timedelta(minutes=1)
    assert catalog.restore_calls[0][1].startswith("sha256:")
    assert len(market.bundle_codes) == first_bundle_count + 1
    health = service.health_snapshot()
    assert health["priority_monitor_last_at"] == observed_at[0].isoformat()
    assert health["priority_monitor_last_error_count"] == 0


def test_restarted_priority_monitor_requires_immediate_runtime_verification(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "snapshot.json"
    first_at = AS_OF.replace(hour=14, minute=58)
    first = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(("SZ.000001",)),
        cache_path=cache_path,
        clock=lambda: first_at,
        notifier=None,
        config=TradingScreeningConfig(priority_monitoring_enabled=True),
    )
    first.refresh_now()

    restart_at = first_at + timedelta(seconds=30)
    restarted = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(("SZ.000001",)),
        cache_path=cache_path,
        clock=lambda: restart_at,
        notifier=None,
        config=TradingScreeningConfig(priority_monitoring_enabled=True),
    )

    assert restarted._priority_monitor_last_at == first_at
    assert restarted._priority_monitor_due(restart_at) is True
    health = restarted.health_snapshot()
    assert health["priority_monitor_runtime_verified"] is False
    assert health["priority_monitor_status"] == "awaiting_runtime_verification"
    assert health["priority_monitor_reason_codes"] == [
        "PRIORITY_MONITOR_RUNTIME_UNVERIFIED"
    ]


def test_restarted_priority_monitor_restores_continuation_signal_documents(
    tmp_path: Path,
) -> None:
    """精简实时状态保留决策身份，并与纯页面投影隔离后跨重启恢复。"""

    cache_path = tmp_path / "snapshot.json"
    observed_at = AS_OF.replace(hour=14, minute=58)
    code = "SZ.000001"
    signal_id = "priority-signal:SZ.000001"
    config = TradingScreeningConfig(priority_monitoring_enabled=True)
    first = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner((code,)),
        cache_path=cache_path,
        clock=lambda: observed_at,
        notifier=None,
        config=config,
    )
    first._record_priority_monitor_result(
        observed_at=observed_at,
        codes=(code,),
        errors=(),
        documents=(
            {
                "signal_id": signal_id,
                "code": code,
                "point_type": "1buy",
                "lifecycle_stage": "armed",
                "observed_at": observed_at.isoformat(),
            },
        ),
        successful_codes=(code,),
        lanes_by_code={
            code: trading_screening_subject.CANDIDATE_MONITOR_LANE_1M,
        },
        five_universe=(code,),
        thirty_universe=(code,),
        five_codes=(code,),
        thirty_codes=(code,),
        successful_five_codes=(code,),
        successful_thirty_codes=(code,),
    )
    first._persist_priority_monitor_state()

    state_path = tmp_path / "trading_priority_monitor_state.json"
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    [persisted_document] = persisted["latest_documents"]
    assert persisted_document["monitor_continuation"] is True
    assert persisted_document["presentation_projection"] is False
    assert persisted_document["full_audit_evidence_embedded"] is False

    restarted = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner((code,)),
        cache_path=cache_path,
        clock=lambda: observed_at + timedelta(seconds=30),
        notifier=None,
        config=config,
    )

    assert restarted._priority_monitor_last_at == observed_at
    assert restarted._candidate_monitor_five_universe == (code,)
    assert restarted._candidate_monitor_thirty_universe == (code,)
    assert restarted._candidate_monitor_five_last_success_at == {code: observed_at}
    assert restarted._candidate_monitor_thirty_last_success_at == {code: observed_at}
    assert restarted._priority_monitor_code_observations == {
        code: (
            observed_at,
            trading_screening_subject.CANDIDATE_MONITOR_LANE_1M,
        )
    }
    assert set(restarted._priority_monitor_latest_documents) == {signal_id}
    restored = restarted._priority_monitor_latest_documents[signal_id]
    assert restored["monitor_continuation"] is True
    assert restored["presentation_projection"] is False
    assert restored["observation_lane"] == "PRIORITY_CURRENT_1M"
    assert restored["monitor_observed_at"] == observed_at.isoformat()
    assert restored["realtime_observation"] is True
    assert restarted._priority_monitor_runtime_verified is False


def test_priority_monitor_continuation_survives_preopen_restart_and_notifies_locator(
    tmp_path: Path,
) -> None:
    code = "SZ.000001"
    preopen_at = AS_OF + timedelta(hours=17, minutes=45)
    open_at = AS_OF + timedelta(hours=18, minutes=31)

    class CrossSessionLocatorMarket(RecordingMarketData):
        def structure_bundle(
            self,
            code: str,
            *,
            as_of: datetime,
            sector,
            frequencies=(),
        ) -> SymbolStructureBundle:
            self.bundle_frequency_requests.append((code, tuple(frequencies)))
            self.bundle_codes.append(code)
            setup = _current_terminal_point(
                confirmed_point("2buy", code=code, minutes_after=295)
            )
            trigger = (
                None
                if as_of < open_at
                else _current_terminal_point(
                    confirmed_point(
                        "1buy",
                        code=code,
                        frequency="1m",
                        minutes_after=294,
                        available_minutes_after=1117,
                    ),
                    terminal_minutes=1,
                )
            )
            boundaries = (
                ()
                if trigger is None
                else (
                    EntryExecutionBoundary(
                        symbol=code,
                        setup_occurrence_id=structural_point_occurrence_id(setup),
                        point_id=trigger.point_id,
                        source_frequency="1m",
                        confirmation_bar_closed_at=trigger.available_at,
                        raw_open=Decimal("9.95"),
                        raw_high=Decimal("10.05"),
                        raw_low=Decimal("9.90"),
                        raw_close=Decimal("10.00"),
                        raw_volume=Decimal("10000"),
                        entry_valid_until=a_share_optional_entry_valid_until(
                            trigger.available_at
                        ),
                        raw_price_basis_revision="test-raw",
                    ),
                )
            )
            return SymbolStructureBundle(
                code=code,
                as_of=as_of,
                sector=sector,
                thirty_direction="neutral",
                thirty_points=(),
                five_points=(setup,),
                one_points=(() if trigger is None else (trigger,)),
                opposite_points=(),
                warmup_converged=True,
                physical_timeframe_recursive=True,
                entry_execution_boundaries=boundaries,
            )

    cache_path = tmp_path / "snapshot.json"
    clock = [AS_OF]
    first_market = CrossSessionLocatorMarket()
    config = TradingScreeningConfig(
        max_symbols_per_refresh=1,
        priority_monitoring_enabled=True,
        max_five_minute_candidate_symbols_per_refresh=1,
        max_thirty_minute_candidate_symbols_per_refresh=1,
        admitted_universe_codes=(code,),
    )
    first = TradingScreeningService(
        market_data=first_market,
        sector_catalog=MultiMemberSectorCatalog((code,)),
        engine=HumanAssistedDecisionCore(formal_selection_required=False),
        scan_planner=RecordingPlanner((code,)),
        cache_path=cache_path,
        clock=lambda: clock[0],
        notifier=None,
        config=config,
    )
    snapshot = first.refresh_now()
    [original] = snapshot["signals"]
    assert original["lifecycle_stage"] == "triggered"
    assert original["segment_difference_1m"] is None

    clock[0] = preopen_at
    first._run_priority_monitor(previous=snapshot, observed_at=preopen_at)
    [continuation] = first._priority_monitor_latest_documents.values()
    assert continuation["monitor_continuation"] is True
    assert continuation["decision_core_id"] == first._decision_core_id
    assert continuation["setup_id"] == original["setup_id"]
    assert continuation["setup_5m"]["anchor_at"] == original["setup_5m"]["anchor_at"]
    assert continuation["setup_5m"]["source_frequency"] == "5m"
    assert continuation["setup_5m"]["recursive_level"] == 0

    class RecordingSender:
        def __init__(self) -> None:
            self.messages: list[tuple[str, list[str]]] = []

        def send(self, title, lines) -> bool:
            self.messages.append((title, list(lines)))
            return True

    sender = RecordingSender()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "notification_state.json",
        clock=lambda: open_at,
    )
    restarted_market = CrossSessionLocatorMarket()
    restarted = TradingScreeningService(
        market_data=restarted_market,
        sector_catalog=MultiMemberSectorCatalog((code,)),
        engine=HumanAssistedDecisionCore(formal_selection_required=False),
        scan_planner=RecordingPlanner((code,)),
        cache_path=cache_path,
        clock=lambda: open_at,
        notifier=dispatcher,
        config=config,
    )
    assert restarted._priority_monitor_latest_documents

    restarted._run_priority_monitor(previous=snapshot, observed_at=open_at)

    assert any(
        requested_code == code and "1m" in frequencies
        for requested_code, frequencies in restarted_market.bundle_frequency_requests
    )
    assert restarted.health_snapshot()["priority_monitor_immediate_universe_count"] == 1
    assert len(sender.messages) == 1, (
        json.dumps(dispatcher.health_snapshot(), ensure_ascii=False, indent=2),
        json.dumps(
            restarted._priority_monitor_latest_documents,
            ensure_ascii=False,
            indent=2,
        ),
    )
    visible = restarted.presentation_snapshot()["signals"]
    assert visible and visible[0]["presentation_projection"] is True
    assert "decision_core_id" not in visible[0]


def test_previous_core_priority_state_seeds_codes_without_restoring_conclusions(
    tmp_path: Path,
) -> None:
    """规则升级时只迁移旧实时命中代码，旧结论必须全部隔离。"""

    cache_path = tmp_path / "snapshot.json"
    observed_at = AS_OF.replace(hour=14, minute=58)
    old = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=cache_path,
        clock=lambda: observed_at,
        notifier=None,
        config=TradingScreeningConfig(priority_monitoring_enabled=True),
    )
    old._record_priority_monitor_result(
        observed_at=observed_at,
        codes=("SZ.000001", "US.AAPL"),
        errors=(),
        documents=(
            {
                "signal_id": "old:SZ.000001",
                "code": "SZ.000001",
                "point_type": "3buy",
                "lifecycle_stage": "confirmed",
                "observed_at": observed_at.isoformat(),
            },
            {
                "signal_id": "old:US.AAPL",
                "code": "US.AAPL",
                "point_type": "3buy",
                "lifecycle_stage": "confirmed",
                "observed_at": observed_at.isoformat(),
            },
        ),
        successful_codes=("SZ.000001", "US.AAPL"),
        lanes_by_code={
            "SZ.000001": trading_screening_subject.CANDIDATE_MONITOR_LANE_1M,
            "US.AAPL": trading_screening_subject.CANDIDATE_MONITOR_LANE_1M,
        },
    )
    old._persist_priority_monitor_state()
    state_path = tmp_path / "trading_priority_monitor_state.json"
    persisted = json.loads(state_path.read_text(encoding="utf-8"))

    class ReplacementEngine(RecordingEngine):
        pass

    restarted = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=ReplacementEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=cache_path,
        clock=lambda: observed_at + timedelta(minutes=1),
        notifier=None,
        config=TradingScreeningConfig(
            full_coverage_refresh_enabled=False,
            priority_monitoring_enabled=True,
        ),
    )

    assert restarted._decision_rule_recheck_pending_codes == {"SZ.000001"}
    assert (
        restarted._decision_rule_recheck_source_snapshot_sha256
        == (persisted["content_sha256"])
    )
    assert restarted._decision_rule_recheck_source_core_id == old._decision_core_id
    assert restarted._priority_monitor_latest_documents == {}
    assert restarted._priority_monitor_signal_stages == {}
    assert restarted._priority_monitor_last_at is None
    assert restarted._candidate_monitor_five_universe == ()
    health = restarted.health_snapshot()
    assert health["quarantined_priority_monitor_decision_core_id"] == (
        old._decision_core_id
    )
    assert health["quarantined_priority_monitor_reason"] == (
        "DECISION_CORE_IDENTITY_MISMATCH"
    )
    assert health["quarantined_priority_monitor_recheck_code_count"] == 1
    assert health["decision_rule_recheck_status"] == "pending"


def test_tampered_previous_core_priority_state_cannot_seed_recheck(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "snapshot.json"
    observed_at = AS_OF.replace(hour=14, minute=58)
    old = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=cache_path,
        clock=lambda: observed_at,
        notifier=None,
        config=TradingScreeningConfig(priority_monitoring_enabled=True),
    )
    old._record_priority_monitor_result(
        observed_at=observed_at,
        codes=("SZ.000001",),
        errors=(),
        documents=(
            {
                "signal_id": "old:SZ.000001",
                "code": "SZ.000001",
                "point_type": "3buy",
                "lifecycle_stage": "confirmed",
                "observed_at": observed_at.isoformat(),
            },
        ),
        successful_codes=("SZ.000001",),
        lanes_by_code={
            "SZ.000001": trading_screening_subject.CANDIDATE_MONITOR_LANE_1M,
        },
    )
    old._persist_priority_monitor_state()
    state_path = tmp_path / "trading_priority_monitor_state.json"
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    persisted["latest_documents"][0]["code"] = "SH.600000"
    state_path.write_text(json.dumps(persisted), encoding="utf-8")

    class ReplacementEngine(RecordingEngine):
        pass

    restarted = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=ReplacementEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=cache_path,
        clock=lambda: observed_at + timedelta(minutes=1),
        notifier=None,
        config=TradingScreeningConfig(
            full_coverage_refresh_enabled=False,
            priority_monitoring_enabled=True,
        ),
    )

    assert restarted._decision_rule_recheck_pending_codes == set()
    assert (
        restarted.health_snapshot()["quarantined_priority_monitor_decision_core_id"]
        is None
    )


def test_candidate_scheduler_covers_a_five_minute_universe_once_per_cadence() -> None:
    universe = tuple(f"SZ.{value:06d}" for value in range(10))
    observed_at = AS_OF.replace(hour=10, minute=0)
    last_success_at: dict[str, datetime] = {}

    batches = []
    for minute in range(5):
        current = observed_at + timedelta(minutes=minute)
        batch = _take_due_candidate_batch(
            universe,
            last_success_at=last_success_at,
            observed_at=current,
            target_seconds=300,
            monitor_interval_seconds=60,
            max_symbols=2,
            previous_monitor_at=(
                None if minute == 0 else current - timedelta(minutes=1)
            ),
        )
        batches.append(batch)
        last_success_at.update({code: current for code in batch})

    assert batches == [
        universe[0:2],
        universe[2:4],
        universe[4:6],
        universe[6:8],
        universe[8:10],
    ]
    assert set(last_success_at) == set(universe)


def test_candidate_scheduler_drains_lumpy_deadline_before_capacity_gap() -> None:
    universe = tuple(f"SZ.{value:06d}" for value in range(1, 21))
    observed_at = AS_OF.replace(hour=10, minute=0, second=2, microsecond=0)
    last_success_at = {code: observed_at - timedelta(seconds=239) for code in universe}

    # All twenty rows have the same deadline.  Only twelve fit in the next
    # physical wave, so eight must use otherwise-idle capacity now even though
    # their nominal five-minute target has not arrived yet.
    first = _take_due_candidate_batch(
        universe,
        last_success_at=last_success_at,
        observed_at=observed_at,
        target_seconds=300,
        monitor_interval_seconds=60,
        max_symbols=12,
        execution_grace_seconds=50,
        previous_monitor_at=observed_at - timedelta(seconds=60),
    )
    assert first == universe[:8]
    last_success_at.update({code: observed_at for code in first})

    next_observed_at = observed_at + timedelta(seconds=60)
    second = _take_due_candidate_batch(
        universe,
        last_success_at=last_success_at,
        observed_at=next_observed_at,
        target_seconds=300,
        monitor_interval_seconds=60,
        max_symbols=12,
        execution_grace_seconds=50,
        previous_monitor_at=observed_at,
    )
    assert second == universe[8:]


def test_candidate_scheduler_admits_one_second_cadence_jitter() -> None:
    code = "SZ.000001"
    observed_at = AS_OF.replace(hour=10, minute=0, second=2, microsecond=0)

    batch = _take_due_candidate_batch(
        (code,),
        last_success_at={code: observed_at - timedelta(seconds=299)},
        observed_at=observed_at,
        target_seconds=300,
        monitor_interval_seconds=60,
        max_symbols=12,
        execution_grace_seconds=50,
        previous_monitor_at=observed_at - timedelta(seconds=60),
    )

    assert batch == (code,)


def test_candidate_cadence_is_independent_from_transport_retry_ttl() -> None:
    valid = TradingScreeningConfig(
        five_minute_candidate_target_seconds=570,
        candidate_monitor_time_budget_seconds=50,
    )
    assert valid.five_minute_candidate_target_seconds == 570


def test_candidate_cadence_excludes_lunch_and_closed_days() -> None:
    code = "SZ.000001"
    morning_last = AS_OF.replace(hour=11, minute=30, second=0, microsecond=0)

    before_next_bar = _take_due_candidate_batch(
        (code,),
        last_success_at={code: morning_last},
        observed_at=AS_OF.replace(hour=13, minute=4, second=0, microsecond=0),
        target_seconds=300,
        monitor_interval_seconds=60,
        max_symbols=1,
    )
    at_next_bar = _take_due_candidate_batch(
        (code,),
        last_success_at={code: morning_last},
        observed_at=AS_OF.replace(hour=13, minute=5, second=0, microsecond=0),
        target_seconds=300,
        monitor_interval_seconds=60,
        max_symbols=1,
    )
    assert before_next_bar == ()
    assert at_next_bar == (code,)

    friday_last = datetime(2026, 7, 24, 15, 0, tzinfo=AS_OF.tzinfo)
    monday_open = datetime(2026, 7, 27, 9, 31, tzinfo=AS_OF.tzinfo)
    assert (
        _take_due_candidate_batch(
            (code,),
            last_success_at={code: friday_last},
            observed_at=monday_open,
            target_seconds=300,
            monitor_interval_seconds=60,
            max_symbols=1,
        )
        == ()
    )


def test_candidate_coverage_reports_trading_session_age() -> None:
    code = "SZ.000001"
    last_at = AS_OF.replace(hour=11, minute=30, second=0, microsecond=0)
    coverage = trading_screening_subject._candidate_lane_coverage(
        (code,),
        last_success_at={code: last_at},
        observed_at=AS_OF.replace(hour=13, minute=4, second=0, microsecond=0),
        target_seconds=300,
        execution_grace_seconds=50,
    )

    assert coverage["age_basis"] == "A_SHARE_COMPLETED_MINUTE_SESSION_SECONDS"
    assert coverage["oldest_observation_age_seconds"] == 240.0
    assert coverage["ready"] is True
    assert coverage["overdue_count"] == 0


def test_candidate_scheduler_exposes_hard_capacity_instead_of_overclaiming() -> None:
    universe = tuple(f"SZ.{value:06d}" for value in range(20))

    batch = _take_due_candidate_batch(
        universe,
        last_success_at={},
        observed_at=AS_OF,
        target_seconds=300,
        monitor_interval_seconds=60,
        max_symbols=3,
    )

    # Four symbols per minute would be required for a five-minute SLA.  The
    # scheduler honors its hard cap; health reports the insufficiency.
    assert batch == universe[:3]


def test_candidate_scheduler_preserves_lifecycle_priority_for_equal_due_times() -> None:
    # The caller places triggered/formed/approaching codes in this order.  A
    # cold cache has no timestamps with which to distinguish them, so the
    # scheduler must not silently replace that priority with lexical code order.
    universe = (
        "SZ.300003",
        "SZ.000002",
        "SZ.000001",
        *(f"SZ.{value:06d}" for value in range(4, 11)),
    )

    batch = _take_due_candidate_batch(
        universe,
        last_success_at={},
        observed_at=AS_OF,
        target_seconds=300,
        monitor_interval_seconds=60,
        max_symbols=2,
    )

    assert batch == universe[:2]


def test_candidate_scheduler_does_not_starve_due_rows_behind_rotated_cold_rows() -> (
    None
):
    observed_at = AS_OF.replace(hour=10, minute=0, second=2, microsecond=0)
    cold = tuple(f"COLD_{value:02d}" for value in range(31))
    due = tuple(f"DUE_{value:02d}" for value in range(4))
    fresh = tuple(f"FRESH_{value:02d}" for value in range(25))
    universe = cold + due + fresh
    last_success_at = {
        **{code: observed_at - timedelta(seconds=301) for code in due},
        **{code: observed_at for code in fresh},
    }

    batch = _take_due_candidate_batch(
        universe,
        last_success_at=last_success_at,
        observed_at=observed_at,
        target_seconds=300,
        monitor_interval_seconds=60,
        max_symbols=12,
        execution_grace_seconds=50,
        previous_monitor_at=observed_at - timedelta(seconds=60),
    )

    # A large discovery-window rotation must not consume the whole physical
    # wave before retained current setups whose cadence is already due.
    assert batch == due + cold[:8]


def test_candidate_scheduler_rotated_window_stays_inside_execution_grace() -> None:
    started_at = AS_OF.replace(hour=10, minute=0, second=2, microsecond=0)
    cold = tuple(f"COLD_{value:02d}" for value in range(31))
    retained = tuple(f"RETAINED_{value:02d}" for value in range(29))
    universe = cold + retained
    last_success_at = {
        code: started_at - timedelta(minutes=max(0, 4 - index // 6))
        for index, code in enumerate(retained)
    }

    for minute in range(6):
        observed_at = started_at + timedelta(minutes=minute)
        batch = _take_due_candidate_batch(
            universe,
            last_success_at=last_success_at,
            observed_at=observed_at,
            target_seconds=300,
            monitor_interval_seconds=60,
            max_symbols=12,
            execution_grace_seconds=50,
            previous_monitor_at=(
                None if minute == 0 else observed_at - timedelta(minutes=1)
            ),
        )
        last_success_at.update({code: observed_at for code in batch})
        coverage = trading_screening_subject._candidate_lane_coverage(
            universe,
            last_success_at=last_success_at,
            observed_at=observed_at,
            target_seconds=300,
            execution_grace_seconds=50,
        )
        assert coverage["overdue_count"] == 0

    assert set(cold) <= set(last_success_at)


def test_candidate_batch_groups_by_sector_without_changing_due_membership() -> None:
    sector_a = replace(eligible_sector(), sector_id="qmt-gics3:a")
    sector_b = replace(eligible_sector(), sector_id="qmt-gics3:b")
    codes = (
        "SZ.000001",
        "SZ.000002",
        "SZ.000003",
        "SZ.000004",
        "SZ.000005",
    )
    sector_by_code = {
        codes[0]: sector_a,
        codes[1]: sector_b,
        codes[2]: sector_a,
        codes[3]: sector_b,
        # codes[4] deliberately has no authenticated membership.
    }

    grouped = trading_screening_subject._group_candidate_batch_by_sector(
        codes,
        sector_by_code=sector_by_code,
    )

    assert grouped == (codes[0], codes[2], codes[1], codes[3], codes[4])
    assert set(grouped) == set(codes)


def test_priority_burst_uses_all_workers_before_formal_five_minute_lane() -> None:
    production = TradingScreeningConfig(stock_worker_count=3)
    single_worker = TradingScreeningConfig(stock_worker_count=1)

    assert production.effective_priority_worker_count == 3
    assert production.effective_candidate_worker_count == 2
    assert single_worker.effective_priority_worker_count == 1
    assert single_worker.effective_candidate_worker_count == 1


def test_candidate_coverage_includes_bounded_execution_grace() -> None:
    code = "SZ.000001"
    baseline = AS_OF.replace(hour=10, minute=0, second=0, microsecond=0)
    within_budget = trading_screening_subject._candidate_lane_coverage(
        (code,),
        last_success_at={code: baseline},
        observed_at=baseline + timedelta(seconds=350),
        target_seconds=300,
        execution_grace_seconds=50,
    )
    overdue = trading_screening_subject._candidate_lane_coverage(
        (code,),
        last_success_at={code: baseline},
        observed_at=baseline + timedelta(seconds=351),
        target_seconds=300,
        execution_grace_seconds=50,
    )

    assert within_budget["ready"] is True
    assert within_budget["maximum_age_seconds"] == 350.0
    assert overdue["ready"] is False
    assert overdue["overdue_count"] == 1


def test_new_candidate_scope_is_warming_instead_of_inheriting_service_age(
    tmp_path: Path,
) -> None:
    observed_at = AS_OF.replace(hour=14, minute=58)
    code = "SZ.000001"
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: observed_at,
        notifier=None,
        config=TradingScreeningConfig(priority_monitoring_enabled=True),
    )
    service._priority_monitor_runtime_verified = True
    service._priority_monitor_last_at = observed_at
    service._candidate_monitor_started_at = observed_at - timedelta(hours=2)
    service._candidate_monitor_five_universe = (code,)
    service._candidate_monitor_thirty_universe = (code,)

    health = service.health_snapshot()

    assert health["candidate_monitor_status"] == "warming"
    assert health["candidate_monitor_reason_codes"] == ["CANDIDATE_MONITOR_WARMING"]
    assert health["candidate_monitor_five_minute"]["missing_count"] == 1
    assert health["candidate_monitor_thirty_minute"]["missing_count"] == 1


def test_candidate_warmup_does_not_degrade_completed_batch_notifications(
    tmp_path: Path,
) -> None:
    class VerifiedNotifier:
        def health_snapshot(self) -> dict[str, object]:
            return {
                "configured": True,
                "operationally_verified": True,
                "status": "verified",
                "reason_code": "DELIVERY_SUCCESS_PROVEN",
                "delivered_event_count": 1,
            }

    observed_at = AS_OF.replace(hour=14, minute=58)
    code = "SZ.000001"
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: observed_at,
        notifier=VerifiedNotifier(),
        config=TradingScreeningConfig(priority_monitoring_enabled=True),
    )
    service._priority_monitor_runtime_verified = True
    service._priority_monitor_last_at = observed_at
    service._candidate_monitor_started_at = observed_at
    service._candidate_monitor_five_universe = (code,)
    service._candidate_monitor_thirty_universe = (code,)

    health = service.health_snapshot()

    assert health["candidate_monitor_ready"] is False
    assert health["candidate_monitor_status"] == "warming"
    assert health["candidate_monitor_reason_codes"] == ["CANDIDATE_MONITOR_WARMING"]
    assert health["realtime_alert_ready"] is True
    assert health["realtime_alert_status"] == "ready"
    assert health["realtime_alert_reason_code"] == "READY"


def test_candidate_cadence_overdue_still_degrades_realtime_alert(
    tmp_path: Path,
) -> None:
    class VerifiedNotifier:
        def health_snapshot(self) -> dict[str, object]:
            return {
                "configured": True,
                "operationally_verified": True,
                "status": "verified",
                "reason_code": "DELIVERY_SUCCESS_PROVEN",
                "delivered_event_count": 1,
            }

    observed_at = AS_OF.replace(hour=14, minute=58, second=0, microsecond=0)
    code = "SZ.000001"
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: observed_at,
        notifier=VerifiedNotifier(),
        config=TradingScreeningConfig(priority_monitoring_enabled=True),
    )
    service._priority_monitor_runtime_verified = True
    service._priority_monitor_last_at = observed_at
    service._candidate_monitor_started_at = observed_at - timedelta(minutes=10)
    service._candidate_monitor_five_universe = (code,)
    service._candidate_monitor_thirty_universe = (code,)
    service._candidate_monitor_five_last_success_at = {
        code: observed_at - timedelta(seconds=351)
    }
    service._candidate_monitor_thirty_last_success_at = {code: observed_at}

    health = service.health_snapshot()

    assert health["candidate_monitor_ready"] is False
    assert health["candidate_monitor_status"] == "cadence_overdue"
    assert health["realtime_alert_ready"] is False
    assert health["realtime_alert_status"] == "candidate_monitor_degraded"
    assert health["realtime_alert_reason_code"] == ("CANDIDATE_MONITOR_CADENCE_OVERDUE")


def test_deferred_candidate_with_fresh_observation_does_not_claim_capacity_failure(
    tmp_path: Path,
) -> None:
    observed_at = AS_OF.replace(hour=14, minute=58, second=0, microsecond=0)
    code = "SZ.000001"
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: observed_at,
        notifier=None,
        config=TradingScreeningConfig(priority_monitoring_enabled=True),
    )
    service._priority_monitor_runtime_verified = True
    service._priority_monitor_last_at = observed_at
    service._candidate_monitor_started_at = observed_at - timedelta(minutes=1)
    service._candidate_monitor_five_universe = (code,)
    service._candidate_monitor_thirty_universe = (code,)
    service._candidate_monitor_five_last_success_at = {code: observed_at}
    service._candidate_monitor_thirty_last_success_at = {code: observed_at}
    service._candidate_monitor_last_deferred_codes = (code,)

    health = service.health_snapshot()

    assert health["candidate_monitor_last_run_status"] == "deferred"
    assert health["candidate_monitor_observed_capacity_sufficient"] is None
    assert health["candidate_monitor_capacity_sufficient"] is True
    assert health["candidate_monitor_status"] == "verified"
    assert health["candidate_monitor_reason_codes"] == []


def test_measured_throughput_reports_capacity_failure_before_cadence_is_overdue(
    tmp_path: Path,
) -> None:
    observed_at = AS_OF.replace(hour=14, minute=58, second=0, microsecond=0)
    codes = tuple(f"SZ.{value:06d}" for value in range(1, 301))
    attempted = codes[:20]
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: observed_at,
        notifier=None,
        config=TradingScreeningConfig(
            priority_monitoring_enabled=True,
            large_scope_authorized=True,
            max_admitted_universe_symbols=len(codes),
            admitted_universe_codes=codes,
        ),
    )
    service._priority_monitor_runtime_verified = True
    service._priority_monitor_last_at = observed_at
    service._priority_monitor_last_round_elapsed_seconds = 50.0
    service._candidate_monitor_started_at = observed_at - timedelta(minutes=1)
    service._candidate_monitor_five_universe = codes
    service._candidate_monitor_thirty_universe = codes
    service._candidate_monitor_five_last_success_at = {
        code: observed_at for code in codes
    }
    service._candidate_monitor_thirty_last_success_at = {
        code: observed_at for code in codes
    }
    service._candidate_monitor_last_five_codes = attempted
    service._candidate_monitor_last_thirty_codes = attempted
    service._candidate_monitor_last_deferred_codes = codes[20:]

    health = service.health_snapshot()

    assert health["candidate_monitor_five_minute"]["overdue_count"] == 0
    assert health["candidate_monitor_observed_symbols_per_second"] == 0.4
    assert health["candidate_monitor_required_symbols_per_second"] > 1
    assert health["candidate_monitor_observed_capacity_sufficient"] is False
    assert health["candidate_monitor_status"] == "capacity_insufficient"


def test_rule_migration_rechecks_only_current_buy_candidates(
    tmp_path: Path,
) -> None:
    observed_at = AS_OF.replace(hour=14, minute=58, second=0, microsecond=0)
    codes = tuple(f"SZ.{value:06d}" for value in range(1, 5))
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: observed_at,
        notifier=None,
        config=TradingScreeningConfig(
            priority_monitoring_enabled=True,
            admitted_universe_codes=codes,
        ),
    )
    snapshot = {
        "snapshot_content_sha256": "sha256:" + "7" * 64,
        "market_data_as_of": observed_at.isoformat(),
        "signals": [
            {
                "code": "SZ.000001",
                "side": "buy",
                "point_type": "1buy",
                "lifecycle_stage": "triggered",
                "setup_5m": {
                    "anchor_at": (observed_at - timedelta(minutes=5)).isoformat(),
                    "available_at": (observed_at - timedelta(minutes=5)).isoformat(),
                },
            },
            {
                "code": "SZ.000002",
                "side": "buy",
                "point_type": "2buy",
                "lifecycle_stage": "approaching",
                "setup_5m": {
                    "available_at": (observed_at - timedelta(minutes=30)).isoformat()
                },
            },
            {
                "code": "SZ.000003",
                "side": "buy",
                "point_type": "3buy",
                "lifecycle_stage": "triggered",
                "setup_5m": {
                    "anchor_at": (observed_at - timedelta(minutes=11)).isoformat(),
                    "available_at": (observed_at - timedelta(minutes=11)).isoformat(),
                },
            },
            {
                "code": "SZ.000004",
                "side": "sell",
                "point_type": "1sell",
                "lifecycle_stage": "triggered",
                "setup_5m": {
                    "available_at": (observed_at - timedelta(minutes=5)).isoformat()
                },
            },
        ],
    }

    service._seed_decision_rule_recheck(
        snapshot,
        cached_core_id="sha256:" + "6" * 64,
    )

    assert service._decision_rule_recheck_pending_codes == {
        "SZ.000001",
        "SZ.000002",
        "SZ.000003",
    }


def test_deferred_new_candidate_is_warming_without_capacity_failure(
    tmp_path: Path,
) -> None:
    observed_at = AS_OF.replace(hour=14, minute=58, second=0, microsecond=0)
    code = "SZ.000001"
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: observed_at,
        notifier=None,
        config=TradingScreeningConfig(priority_monitoring_enabled=True),
    )
    service._priority_monitor_runtime_verified = True
    service._priority_monitor_last_at = observed_at
    service._candidate_monitor_started_at = observed_at
    service._candidate_monitor_five_universe = (code,)
    service._candidate_monitor_thirty_universe = (code,)
    service._candidate_monitor_last_deferred_codes = (code,)

    health = service.health_snapshot()

    assert health["candidate_monitor_observed_capacity_sufficient"] is None
    assert health["candidate_monitor_capacity_sufficient"] is True
    assert health["candidate_monitor_status"] == "warming"
    assert health["candidate_monitor_reason_codes"] == ["CANDIDATE_MONITOR_WARMING"]


def test_deferred_candidate_that_is_overdue_reports_observed_capacity_failure(
    tmp_path: Path,
) -> None:
    observed_at = AS_OF.replace(hour=14, minute=58, second=0, microsecond=0)
    code = "SZ.000001"
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: observed_at,
        notifier=None,
        config=TradingScreeningConfig(priority_monitoring_enabled=True),
    )
    service._priority_monitor_runtime_verified = True
    service._priority_monitor_last_at = observed_at
    service._candidate_monitor_started_at = observed_at - timedelta(minutes=10)
    service._candidate_monitor_five_universe = (code,)
    service._candidate_monitor_thirty_universe = (code,)
    service._candidate_monitor_five_last_success_at = {
        code: observed_at - timedelta(seconds=351)
    }
    service._candidate_monitor_thirty_last_success_at = {code: observed_at}
    service._candidate_monitor_last_deferred_codes = (code,)

    health = service.health_snapshot()

    assert health["candidate_monitor_five_minute"]["overdue_count"] == 1
    assert health["candidate_monitor_observed_capacity_sufficient"] is False
    assert health["candidate_monitor_capacity_sufficient"] is False
    assert health["candidate_monitor_status"] == "capacity_insufficient"
    assert health["candidate_monitor_reason_codes"] == [
        "CANDIDATE_MONITOR_OBSERVED_CAPACITY_INSUFFICIENT"
    ]


def test_rule_recheck_scheduler_drains_with_fixed_remaining_capacity() -> None:
    pending = tuple(f"SZ.{value:06d}" for value in range(10))
    completed: set[str] = set()
    batches: list[tuple[str, ...]] = []

    while len(completed) < len(pending):
        current_pending = tuple(code for code in pending if code not in completed)
        batch = _take_rule_recheck_batch(
            current_pending,
            scheduled_codes=(),
            max_symbols=3,
        )
        batches.append(batch)
        completed.update(batch)

    assert tuple(len(batch) for batch in batches) == (3, 3, 3, 1)
    assert completed == set(pending)


def test_rule_recheck_scheduler_reserves_capacity_for_regular_candidates() -> None:
    batch = _take_rule_recheck_batch(
        ("SZ.000003", "SZ.000002", "SZ.000001"),
        scheduled_codes=("SZ.000001", "SZ.000009"),
        max_symbols=3,
    )

    assert batch == ("SZ.000002",)


def test_priority_monitor_rejects_oversized_mandatory_scope_before_market_data(
    tmp_path: Path,
) -> None:
    class OversizedMandatoryMarket(RecordingMarketData):
        def __init__(self) -> None:
            super().__init__()
            self.quote_calls = 0

        def active_watchlist(self) -> tuple[str, ...]:
            return tuple(f"SZ.{value:06d}" for value in range(1, 14))

        def priority_realtime_ticks(self, _codes: tuple[str, ...]):
            self.quote_calls += 1
            raise AssertionError("scope gate must run before realtime quotes")

    market = OversizedMandatoryMarket()
    catalog = RecordingSectorCatalog()
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=catalog,
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(priority_monitoring_enabled=True),
    )

    with pytest.raises(
        ScreeningScopeAuthorizationError,
        match="mandatory screening universe has 13 symbols",
    ):
        service._run_priority_monitor(previous=service.snapshot(), observed_at=AS_OF)

    assert market.quote_calls == 0
    assert market.bundle_codes == []
    assert catalog.assessment_calls == []


def test_validation_scope_is_explicit_in_health_and_page_presentation(
    tmp_path: Path,
) -> None:
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(priority_monitoring_enabled=True),
    )

    health = service.health_snapshot()
    assert health["screening_scope_mode"] == "VALIDATION_COHORT"
    assert health["validation_cohort_size"] == 12
    assert health["effective_monitor_universe_limit"] == 12
    assert health["max_admitted_universe_symbols"] == 20
    assert health["large_scope_authorized"] is False
    assert health["full_coverage_refresh_enabled"] is False

    assert service.presentation_snapshot()["screening_scope"] == {
        "schema": "chanlun-screening-scope-v1",
        "mode": "VALIDATION_COHORT",
        "validation_cohort_size": 12,
        "effective_monitor_universe_limit": 12,
        "configured_max_admitted_universe_symbols": 20,
        "large_scope_authorized": False,
        "full_coverage_enabled": False,
    }


def test_archive_mandatory_subjects_share_the_bounded_admission_gate(
    tmp_path: Path,
) -> None:
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(),
    )

    assert service.admit_archive_universe_codes(("SZ.000001",)) == ("SZ.000001",)
    with pytest.raises(
        ScreeningScopeAuthorizationError,
        match="mandatory screening universe has 13 symbols",
    ):
        service.admit_archive_universe_codes(
            tuple(f"SZ.{value:06d}" for value in range(1, 14))
        )

    full = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "full.json",
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(
            full_coverage_refresh_enabled=True,
            large_scope_authorized=True,
        ),
    )
    assert full.admit_archive_universe_codes(("SZ.000001",)) is None


def test_validation_restart_rejects_broad_snapshot_queues_and_status(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "snapshot.json"
    symbols = tuple(f"SZ.{value:06d}" for value in range(1, 14))
    full_config = TradingScreeningConfig(
        full_coverage_refresh_enabled=True,
        large_scope_authorized=True,
        max_admitted_universe_symbols=20,
        max_symbols_per_refresh=13,
    )
    producer = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=MultiMemberSectorCatalog(symbols),
        engine=RecordingEngine(),
        scan_planner=SequencedPlanner((symbols,)),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
        config=full_config,
    )

    broad = producer.refresh_now()

    assert len(broad["coverage_manifest"]["discovered_codes"]) == 13
    assert broad["scan_audit"]["pending_symbol_count"] == 1
    bounded = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=MultiMemberSectorCatalog(symbols),
        engine=RecordingEngine(),
        scan_planner=SequencedPlanner(((),)),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(),
    )

    assert bounded.snapshot()["scan_state"] == "not_started"
    assert bounded._pending_frequencies == {}
    assert bounded.presentation_snapshot()["signals"] == []
    assert bounded.health_snapshot()["pending_symbol_count"] == 0

    # The same immutable checkpoint remains valid under explicitly authorized
    # full-market semantics.
    full = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=MultiMemberSectorCatalog(symbols),
        engine=RecordingEngine(),
        scan_planner=SequencedPlanner(((),)),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
        config=full_config,
    )
    assert full.snapshot()["scan_state"] == broad["scan_state"]
    assert set(full._pending_frequencies) == {symbols[-1]}


def test_validation_restart_rejects_broad_compact_signal_archive(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "snapshot.json"
    observed_at = AS_OF.replace(hour=14, minute=58)
    codes = tuple(f"SZ.{value:06d}" for value in range(1, 14))
    producer = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=cache_path,
        clock=lambda: observed_at,
        notifier=None,
        config=TradingScreeningConfig(
            priority_monitoring_enabled=True,
            full_coverage_refresh_enabled=True,
            large_scope_authorized=True,
            max_admitted_universe_symbols=20,
        ),
    )
    producer._record_priority_monitor_result(
        observed_at=observed_at,
        codes=codes,
        errors=(),
        documents=tuple(
            {
                "signal_id": f"old:{code}",
                "code": code,
                "point_type": "1buy",
                "lifecycle_stage": "armed",
                "observed_at": observed_at.isoformat(),
            }
            for code in codes
        ),
        successful_codes=codes,
        lanes_by_code={
            code: trading_screening_subject.CANDIDATE_MONITOR_LANE_1M for code in codes
        },
    )
    producer._persist_priority_monitor_state()

    bounded = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=cache_path,
        clock=lambda: observed_at + timedelta(seconds=30),
        notifier=None,
        config=TradingScreeningConfig(priority_monitoring_enabled=True),
    )

    assert bounded._priority_monitor_latest_documents == {}
    assert bounded._priority_monitor_signal_codes == {}
    assert bounded.presentation_snapshot()["signals"] == []

    full = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=cache_path,
        clock=lambda: observed_at + timedelta(seconds=30),
        notifier=None,
        config=TradingScreeningConfig(
            priority_monitoring_enabled=True,
            full_coverage_refresh_enabled=True,
            large_scope_authorized=True,
            max_admitted_universe_symbols=20,
        ),
    )
    assert len(full._priority_monitor_latest_documents) == len(codes)


def test_validation_restart_rejects_compact_state_without_scope_proof(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "snapshot.json"
    observed_at = AS_OF.replace(hour=14, minute=58)
    code = "SZ.000001"
    producer = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=cache_path,
        clock=lambda: observed_at,
        notifier=None,
        config=TradingScreeningConfig(priority_monitoring_enabled=True),
    )
    producer._record_priority_monitor_result(
        observed_at=observed_at,
        codes=(code,),
        errors=(),
        documents=(
            {
                "signal_id": f"old:{code}",
                "code": code,
                "point_type": "1buy",
                "lifecycle_stage": "armed",
                "observed_at": observed_at.isoformat(),
            },
        ),
        successful_codes=(code,),
        lanes_by_code={
            code: trading_screening_subject.CANDIDATE_MONITOR_LANE_1M,
        },
    )
    producer._persist_priority_monitor_state()
    state_path = tmp_path / "trading_priority_monitor_state.json"
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    persisted.pop("screening_scope_mode")
    persisted.pop("effective_monitor_universe_limit")
    persisted.pop("admitted_universe_codes")
    persisted["content_sha256"] = producer._priority_monitor_state_sha256(persisted)
    state_path.write_text(json.dumps(persisted), encoding="utf-8")

    bounded = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=cache_path,
        clock=lambda: observed_at + timedelta(seconds=30),
        notifier=None,
        config=TradingScreeningConfig(priority_monitoring_enabled=True),
    )
    assert bounded._priority_monitor_latest_documents == {}
    assert bounded._priority_monitor_signal_codes == {}

    full = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=cache_path,
        clock=lambda: observed_at + timedelta(seconds=30),
        notifier=None,
        config=TradingScreeningConfig(
            priority_monitoring_enabled=True,
            full_coverage_refresh_enabled=True,
            large_scope_authorized=True,
        ),
    )
    assert set(full._priority_monitor_latest_documents) == {f"old:{code}"}


def test_priority_monitor_admits_only_one_physical_rule_recheck_wave(
    tmp_path: Path,
) -> None:
    pending = tuple(f"SZ.{value:06d}" for value in range(1, 11))
    market = RecordingMarketData()
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(
            full_coverage_refresh_enabled=False,
            priority_monitoring_enabled=True,
            stock_worker_count=3,
            max_five_minute_candidate_symbols_per_refresh=256,
            max_admitted_universe_symbols=256,
            large_scope_authorized=True,
        ),
    )
    service._decision_rule_recheck_source_snapshot_sha256 = "sha256:" + "6" * 64
    service._decision_rule_recheck_source_core_id = "sha256:" + "5" * 64
    service._decision_rule_recheck_pending_codes.update(pending)

    service._run_priority_monitor(previous=service.snapshot(), observed_at=AS_OF)

    # 三个结构分片中第一个预留给 1m；迁移积压每分钟最多使用另外两个物理分片
    # 的一个波次，不能依照 256 个逻辑上限占满整轮预算。
    assert market.bundle_codes == list(pending[:2])
    assert service._decision_rule_recheck_pending_codes == set(pending[2:])
    health = service.health_snapshot()
    assert health["decision_rule_recheck_last_attempted_codes"] == list(pending[:2])
    assert health["decision_rule_recheck_last_deferred_codes"] == list(pending[2:])


def test_rule_recheck_scheduler_rotates_past_persistent_failures() -> None:
    pending = tuple(f"SZ.{value:06d}" for value in range(6))

    first = _take_rule_recheck_batch(
        pending,
        scheduled_codes=(),
        max_symbols=3,
    )
    second = _take_rule_recheck_batch(
        pending,
        scheduled_codes=(),
        previous_codes=first,
        max_symbols=3,
    )

    assert first == pending[:3]
    assert second == pending[3:]


def test_candidate_health_reports_insufficient_configured_cadence_capacity(
    tmp_path: Path,
) -> None:
    class VerifiedNotifier:
        def dispatch_changes(self, _previous, _current) -> None:
            return None

        def health_snapshot(self) -> dict[str, object]:
            return {
                "configured": True,
                "operationally_verified": True,
                "status": "verified",
                "reason_code": "DELIVERY_SUCCESS_PROVEN",
                "delivered_event_count": 1,
            }

    symbols = tuple(f"SZ.{value:06d}" for value in range(1, 21))
    catalog = MultiMemberSectorCatalog(symbols)
    catalog.batch = SectorAssessmentBatch(
        assessments=(replace(eligible_sector(), regime="supportive"),),
        discovered_count=1,
        completed_count=1,
        failure_counts=(),
        errors=(),
    )
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=catalog,
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=VerifiedNotifier(),
        config=TradingScreeningConfig(
            priority_monitoring_enabled=True,
            max_five_minute_candidate_symbols_per_refresh=3,
            max_thirty_minute_candidate_symbols_per_refresh=3,
            validation_cohort_size=20,
        ),
    )

    service._run_priority_monitor(
        previous={
            "signals": [
                {
                    "signal_id": f"formed:{code}",
                    "code": code,
                    "point_type": "2buy",
                    "lifecycle_stage": "formed",
                }
                for code in symbols
            ]
        },
        observed_at=AS_OF,
    )

    health = service.health_snapshot()
    assert health["candidate_monitor_capacity_sufficient"] is False
    assert health["candidate_monitor_status"] == "capacity_insufficient"
    assert health["candidate_monitor_reason_codes"] == [
        "CANDIDATE_MONITOR_CONFIGURED_CAPACITY_INSUFFICIENT"
    ]
    assert health["candidate_monitor_five_minute"]["required_symbols_per_refresh"] == 4
    assert health["candidate_monitor_five_minute"]["last_batch_count"] == 3
    assert health["priority_monitor_ready"] is True
    assert health["realtime_alert_ready"] is False
    assert health["realtime_alert_status"] == "candidate_monitor_degraded"
    assert health["realtime_alert_reason_code"] == (
        "CANDIDATE_MONITOR_CONFIGURED_CAPACITY_INSUFFICIENT"
    )


def test_realtime_alert_requires_successful_delivery_proof(tmp_path: Path) -> None:
    class UnverifiedNotifier:
        def health_snapshot(self) -> dict[str, object]:
            return {
                "configured": True,
                "operationally_verified": False,
                "status": "awaiting_first_delivery",
                "reason_code": "NO_NOTIFICATION_EVENT_DUE_OR_DELIVERED",
                "delivered_event_count": 0,
            }

    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF.replace(hour=14, minute=59),
        notifier=UnverifiedNotifier(),
        config=TradingScreeningConfig(priority_monitoring_enabled=True),
    )
    service._priority_monitor_runtime_verified = True
    service._priority_monitor_last_at = AS_OF.replace(hour=14, minute=59)
    service._candidate_monitor_started_at = AS_OF.replace(hour=14, minute=59)

    health = service.health_snapshot()

    assert health["priority_monitor_ready"] is True
    assert health["candidate_monitor_ready"] is True
    assert health["notification_operationally_verified"] is False
    assert health["realtime_alert_ready"] is False
    assert health["realtime_alert_status"] == "notification_unverified"
    assert health["realtime_alert_reason_code"] == (
        "NO_NOTIFICATION_EVENT_DUE_OR_DELIVERED"
    )


def test_realtime_alert_without_due_event_is_not_degraded_after_close(
    tmp_path: Path,
) -> None:
    class AwaitingFirstDeliveryNotifier:
        def health_snapshot(self) -> dict[str, object]:
            return {
                "configured": True,
                "operationally_verified": False,
                "status": "awaiting_first_delivery",
                "reason_code": "NO_NOTIFICATION_EVENT_DUE_OR_DELIVERED",
                "delivered_event_count": 0,
            }

    closed_sunday = AS_OF + timedelta(days=6)
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: closed_sunday,
        notifier=AwaitingFirstDeliveryNotifier(),
        config=TradingScreeningConfig(priority_monitoring_enabled=True),
    )

    health = service.health_snapshot()

    assert health["priority_monitor_session_open"] is False
    assert health["notification_operationally_verified"] is False
    assert health["realtime_alert_ready"] is True
    assert health["realtime_alert_status"] == "not_due"
    assert health["realtime_alert_reason_code"] == "NON_TRADING_SESSION_NOT_DUE"


def test_validation_cohort_uses_idle_capacity_for_live_five_minute_probes(
    tmp_path: Path,
) -> None:
    symbols = tuple(f"SZ.{value:06d}" for value in range(1, 4))
    observed_at = [AS_OF.replace(hour=14, minute=56, second=0, microsecond=0)]
    market = RecordingMarketData()
    engine = RecordingEngine()
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=MultiMemberSectorCatalog(symbols),
        engine=engine,
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: observed_at[0],
        notifier=None,
        config=TradingScreeningConfig(
            priority_monitoring_enabled=True,
            admitted_universe_codes=symbols,
        ),
    )

    for _ in symbols:
        service._run_priority_monitor(
            previous=service.snapshot(),
            observed_at=observed_at[0],
        )
        observed_at[0] += timedelta(minutes=1)

    assert market.bundle_codes == list(symbols)
    assert {
        code: set(frequencies) for code, frequencies in market.bundle_frequency_requests
    } == {code: {"5m", "30m"} for code in symbols}
    assert all(
        bundle.selection_sources == ("QMT_SECTOR_ELIGIBLE_SCOPE",)
        for bundle in engine.bundles
    )
    health = service.health_snapshot()
    assert health["candidate_monitor_validation_probe_pool_count"] == len(symbols)
    assert health["candidate_monitor_validation_probe_admitted_count"] == len(symbols)
    assert health["candidate_monitor_validation_probe_deferred_count"] == 0
    assert health["candidate_monitor_active"] is True
    assert health["candidate_monitor_status"] == "verified"
    assert health["candidate_monitor_five_minute"]["universe_count"] == len(symbols)
    assert health["candidate_monitor_five_minute"]["scope"] == (
        "OWNED_WATCHED_EXISTING_SUPPORTIVE_AND_VALIDATION_COHORT"
    )
    assert health["priority_monitor_locator_pool_count"] == 0
    assert health["priority_monitor_locator_runtime_status"] == "not_required"


def test_large_scope_without_candidates_is_explicitly_idle_not_vacuously_verified(
    tmp_path: Path,
) -> None:
    class VerifiedNotifier:
        def health_snapshot(self) -> dict[str, object]:
            return {
                "configured": True,
                "operationally_verified": True,
                "status": "verified",
                "reason_code": "DELIVERY_SUCCESS_PROVEN",
                "delivered_event_count": 1,
            }

    symbols = tuple(f"SZ.{value:06d}" for value in range(1, 4))
    observed_at = AS_OF.replace(hour=14, minute=58, second=0, microsecond=0)
    market = RecordingMarketData()
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=MultiMemberSectorCatalog(symbols),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: observed_at,
        notifier=VerifiedNotifier(),
        config=TradingScreeningConfig(
            priority_monitoring_enabled=True,
            large_scope_authorized=True,
            admitted_universe_codes=symbols,
        ),
    )

    service._run_priority_monitor(
        previous=service.snapshot(),
        observed_at=observed_at,
    )

    assert market.bundle_codes == []
    health = service.health_snapshot()
    assert health["candidate_monitor_validation_probe_pool_count"] == 0
    assert health["candidate_monitor_active"] is False
    assert health["candidate_monitor_ready"] is True
    assert health["candidate_monitor_status"] == "idle_no_candidates"
    assert health["candidate_monitor_reason_codes"] == [
        "CANDIDATE_MONITOR_NO_ELIGIBLE_UNIVERSE"
    ]
    assert health["candidate_monitor_five_minute"]["coverage_ratio"] == "1"
    assert health["realtime_alert_ready"] is True
    assert health["realtime_alert_active"] is False
    assert health["realtime_alert_status"] == "ready_idle"
    assert health["realtime_alert_reason_code"] == (
        "CANDIDATE_MONITOR_NO_ELIGIBLE_UNIVERSE"
    )


def test_priority_monitor_uses_bar_cadence_lanes_and_merges_frequency_work(
    tmp_path: Path,
) -> None:
    symbols = tuple(f"SZ.{value:06d}" for value in range(1, 5))
    observed_at = AS_OF.replace(hour=14, minute=58)
    market = RecordingMarketData()
    catalog = MultiMemberSectorCatalog(symbols)
    catalog.batch = SectorAssessmentBatch(
        assessments=(replace(eligible_sector(), regime="supportive"),),
        discovered_count=1,
        completed_count=1,
        failure_counts=(),
        errors=(),
    )
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=catalog,
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: observed_at,
        notifier=None,
        config=TradingScreeningConfig(
            priority_monitoring_enabled=True,
            stock_worker_count=1,
            max_five_minute_candidate_symbols_per_refresh=8,
            max_thirty_minute_candidate_symbols_per_refresh=8,
            admitted_universe_codes=symbols,
        ),
    )
    previous = {
        "signals": [
            {
                "signal_id": "triggered-buy",
                "code": symbols[0],
                "point_type": "1buy",
                "lifecycle_stage": "triggered",
                "setup_5m": {
                    "anchor_at": (observed_at - timedelta(minutes=5)).isoformat(),
                    "available_at": (observed_at - timedelta(minutes=5)).isoformat(),
                    "confirmed_at": (observed_at - timedelta(minutes=5)).isoformat(),
                },
            },
            {
                "signal_id": "formed-buy",
                "code": symbols[1],
                "point_type": "2buy",
                "lifecycle_stage": "formed",
            },
        ]
    }
    previous["signals"][1]["setup_5m"] = {
        "terminal_segment_available_at": (
            observed_at - timedelta(minutes=11)
        ).isoformat(),
        "formation_state": "confirmed",
    }

    service._run_priority_monitor(previous=previous, observed_at=observed_at)
    service._run_priority_monitor(
        previous=previous,
        observed_at=observed_at + timedelta(minutes=1),
    )

    assert market.bundle_frequency_requests == [
        (symbols[0], ("30m", "5m", "1m")),
        (symbols[1], ("30m", "5m")),
    ]
    service._clock = lambda: observed_at + timedelta(minutes=1)
    health = service.health_snapshot()
    assert health["priority_monitor_last_codes"] == []
    assert health["candidate_monitor_status"] == "warming"
    # 已消失的旧信号不会复活，但支持性板块发现范围仍继续按 5m 节奏轮转。
    assert health["candidate_monitor_five_minute"]["current_count"] == 2
    assert health["candidate_monitor_five_minute"]["missing_count"] == 2
    assert health["candidate_monitor_five_minute"]["last_batch_codes"] == [symbols[1]]
    assert health["candidate_monitor_thirty_minute"]["last_batch_codes"] == [symbols[1]]
    restarted = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=catalog,
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: observed_at + timedelta(minutes=1, seconds=30),
        notifier=None,
        config=service._config,
    )
    assert set(restarted._candidate_monitor_five_last_success_at) == set(symbols[:2])
    assert set(restarted._candidate_monitor_thirty_last_success_at) == set(symbols[:2])
    assert restarted._priority_monitor_runtime_verified is False


def test_live_supportive_discovery_is_bounded_by_sector_rank(
    tmp_path: Path,
) -> None:
    high_code = "SZ.000001"
    low_code = "SZ.000002"
    base = eligible_sector()
    high = replace(
        base,
        sector_id="qmt-gics3:high",
        sector_name="高排名支持行业",
        regime="supportive",
        rank_components=(("structural_strength", 100),),
    )
    low = replace(
        base,
        sector_id="qmt-gics3:low",
        sector_name="低排名支持行业",
        regime="supportive",
        rank_components=(("structural_strength", 90),),
    )

    class RankedSupportiveCatalog(RecordingSectorCatalog):
        def __init__(self) -> None:
            super().__init__(
                SectorAssessmentBatch(
                    assessments=(high, low),
                    discovered_count=2,
                    completed_count=2,
                    failure_counts=(),
                    errors=(),
                )
            )

        def members(self):
            self.member_calls += 1
            return {
                high.sector_id: (high_code,),
                low.sector_id: (low_code,),
            }

        def cached_sector_snapshot_for_priority(self, *, as_of: datetime):
            return CachedSectorSnapshot(
                batch=self.batch,
                members=self.members(),
                requested_as_of=as_of,
                current_decision_epoch=True,
                content_sha256="sha256:" + "7" * 64,
            )

    market = RecordingMarketData()
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=RankedSupportiveCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(
            priority_monitoring_enabled=True,
            supportive_discovery_max_sector_rank=1,
        ),
    )

    service._run_priority_monitor(previous={"signals": []}, observed_at=AS_OF)

    assert high_code in market.bundle_codes
    assert low_code not in market.bundle_codes
    health = service.health_snapshot()
    assert health["candidate_monitor_five_minute"]["universe_count"] == 1
    assert health["candidate_monitor_supportive_eligible_count"] == 1
    assert health["candidate_monitor_supportive_admitted_count"] == 1


def test_supportive_discovery_admission_uses_full_cadence_capacity(
    tmp_path: Path,
) -> None:
    codes = tuple(f"SZ.{value:06d}" for value in range(1, 7))
    catalog = MultiMemberSectorCatalog(codes)
    catalog.batch = SectorAssessmentBatch(
        assessments=(replace(eligible_sector(), regime="supportive"),),
        discovered_count=1,
        completed_count=1,
        failure_counts=(),
        errors=(),
    )
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=catalog,
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(
            priority_monitoring_enabled=True,
            max_five_minute_candidate_symbols_per_refresh=1,
            max_thirty_minute_candidate_symbols_per_refresh=1,
            admitted_universe_codes=codes,
        ),
    )

    service._run_priority_monitor(previous={"signals": []}, observed_at=AS_OF)

    health = service.health_snapshot()
    assert health["candidate_monitor_supportive_eligible_count"] == 6
    assert health["candidate_monitor_supportive_capacity"] == 5
    assert health["candidate_monitor_supportive_admitted_count"] == 5
    assert health["candidate_monitor_five_minute"]["universe_count"] == 5


def test_cold_five_minute_batch_coalesces_missing_thirty_minute_context(
    tmp_path: Path,
) -> None:
    symbols = tuple(f"SZ.{value:06d}" for value in range(1, 11))
    catalog = MultiMemberSectorCatalog(symbols)
    catalog.batch = SectorAssessmentBatch(
        assessments=(replace(eligible_sector(), regime="supportive"),),
        discovered_count=1,
        completed_count=1,
        failure_counts=(),
        errors=(),
    )
    market = RecordingMarketData()
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=catalog,
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(
            priority_monitoring_enabled=True,
            stock_worker_count=1,
            max_five_minute_candidate_symbols_per_refresh=2,
            max_thirty_minute_candidate_symbols_per_refresh=3,
        ),
    )

    service._run_priority_monitor(previous=service.snapshot(), observed_at=AS_OF)

    assert market.bundle_frequency_requests == [
        (symbols[0], ("30m", "5m")),
        (symbols[1], ("30m", "5m")),
    ]
    health = service.health_snapshot()
    assert health["candidate_monitor_five_minute"]["last_batch_codes"] == list(
        symbols[:2]
    )
    assert health["candidate_monitor_thirty_minute"]["last_batch_codes"] == list(
        symbols[:2]
    )
    assert health["candidate_monitor_thirty_minute"]["current_count"] == 2


def test_priority_phase_finishes_before_candidate_phase_uses_remaining_budget(
    tmp_path: Path,
) -> None:
    symbols = ("SZ.000001", "SZ.000002", "SZ.000003", "SZ.000004")
    events: list[tuple[str, object]] = []
    candidate_started = threading.Event()
    priority_observed_candidate = []

    class PhaseMarket(RecordingMarketData):
        def prepare_priority_local_history(
            self,
            *,
            frequency_requests: tuple[tuple[str, tuple[str, ...]], ...],
            as_of: datetime,
        ) -> dict[str, object]:
            events.append(("prepare_priority", frequency_requests))
            return {
                "schema": "chanlun-screening-local-history-preparation",
                "as_of": as_of.isoformat(),
                "prepared_frequencies_by_code": dict(frequency_requests),
                "batch_download_available": True,
            }

        def prepare_candidate_local_history_until(
            self,
            *,
            frequency_requests: tuple[tuple[str, tuple[str, ...]], ...],
            as_of: datetime,
            deadline_monotonic: float,
        ) -> dict[str, object]:
            del deadline_monotonic
            events.append(("prepare_candidate", frequency_requests))
            return {
                "schema": "chanlun-screening-local-history-preparation",
                "as_of": as_of.isoformat(),
                "prepared_frequencies_by_code": dict(frequency_requests),
                "batch_download_available": True,
            }

        def priority_structure_bundle_with_risk_cutoff_until(
            self,
            code: str,
            *,
            deadline_monotonic: float,
            **kwargs,
        ) -> SymbolStructureBundle:
            assert deadline_monotonic > time.perf_counter()
            events.append(("evaluate_priority", code))
            priority_observed_candidate.append(candidate_started.is_set())
            return RecordingMarketData.structure_bundle_with_risk_cutoff(
                self, code, **kwargs
            )

        def candidate_structure_bundle_with_risk_cutoff_until(
            self,
            code: str,
            *,
            deadline_monotonic: float,
            **kwargs,
        ) -> SymbolStructureBundle:
            del deadline_monotonic
            events.append(("evaluate_candidate", code))
            candidate_started.set()
            time.sleep(0.05)
            return RecordingMarketData.structure_bundle_with_risk_cutoff(
                self, code, **kwargs
            )

        def prepare_local_history(self, **_kwargs) -> dict[str, object]:
            raise AssertionError("分钟监听必须使用显式优先/候选资源通道")

        def structure_bundle_with_risk_cutoff(
            self, code: str, **_kwargs
        ) -> SymbolStructureBundle:
            raise AssertionError(f"{code} 未使用显式资源通道")

    market = PhaseMarket()
    catalog = MultiMemberSectorCatalog(symbols)
    catalog.batch = SectorAssessmentBatch(
        assessments=(replace(eligible_sector(), regime="supportive"),),
        discovered_count=1,
        completed_count=1,
        failure_counts=(),
        errors=(),
    )
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=catalog,
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(
            priority_monitoring_enabled=True,
            stock_worker_count=3,
            max_five_minute_candidate_symbols_per_refresh=8,
            max_thirty_minute_candidate_symbols_per_refresh=8,
            admitted_universe_codes=symbols,
        ),
    )
    previous = {
        "signals": [
            {
                "signal_id": "triggered-buy",
                "code": symbols[0],
                "point_type": "1buy",
                "lifecycle_stage": "triggered",
                "setup_5m": {
                    "anchor_at": (AS_OF - timedelta(minutes=5)).isoformat(),
                    "available_at": (AS_OF - timedelta(minutes=5)).isoformat(),
                    "confirmed_at": (AS_OF - timedelta(minutes=5)).isoformat(),
                },
            },
            {
                "signal_id": "formed-buy",
                "code": symbols[1],
                "point_type": "2buy",
                "lifecycle_stage": "formed",
            },
            {
                "signal_id": "formed-sell",
                "code": symbols[2],
                "point_type": "2sell",
                "lifecycle_stage": "formed",
            },
            {
                "signal_id": "formed-third-buy",
                "code": symbols[3],
                "point_type": "3buy",
                "lifecycle_stage": "formed",
            },
        ]
    }
    # 让 1m 优先标的在 5m/30m 节奏上尚未到期；跨过五个调度间隔后，
    # 其余三只候选会形成多个波次，但只能在优先波次发布后开始。
    service._candidate_monitor_five_last_success_at = {symbols[0]: AS_OF}
    service._candidate_monitor_thirty_last_success_at = {symbols[0]: AS_OF}
    service._priority_monitor_last_at = AS_OF - timedelta(minutes=5)

    service._run_priority_monitor(previous=previous, observed_at=AS_OF)

    assert priority_observed_candidate == [False]
    assert events.index(("evaluate_priority", symbols[0])) < next(
        index for index, event in enumerate(events) if event[0] == "prepare_candidate"
    )
    assert set(events) == {
        (
            "prepare_priority",
            ((symbols[0], ("d", "30m", "5m", "1m")),),
        ),
        ("evaluate_priority", symbols[0]),
        (
            "prepare_candidate",
            (
                (symbols[1], ("5m",)),
                (symbols[3], ("5m",)),
            ),
        ),
        ("evaluate_candidate", symbols[1]),
        ("prepare_candidate", ((symbols[2], ("5m",)),)),
        ("evaluate_candidate", symbols[2]),
        ("evaluate_candidate", symbols[3]),
    }


def test_candidate_time_budget_defers_unstarted_codes_without_marking_success(
    tmp_path: Path,
) -> None:
    """低频预算只能延后工作，不能把未完成标的伪装成已观察。"""

    codes = ("SZ.000001", "SZ.000002", "SZ.000003")

    class BudgetMarket(RecordingMarketData):
        def prepare_local_history(self, **_kwargs) -> dict[str, object]:
            return {}

        def prepare_local_history_until(self, **_kwargs) -> dict[str, object]:
            return {}

        def structure_bundle_with_risk_cutoff_until(
            self,
            code: str,
            *,
            deadline_monotonic: float,
            **kwargs,
        ) -> SymbolStructureBundle:
            del deadline_monotonic
            time.sleep(0.07)
            return super().structure_bundle_with_risk_cutoff(code, **kwargs)

    market = BudgetMarket()
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=MultiMemberSectorCatalog(codes),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(
            priority_monitoring_enabled=True,
            stock_worker_count=1,
            candidate_monitor_time_budget_seconds=0.05,
            max_five_minute_candidate_symbols_per_refresh=3,
        ),
    )
    service._decision_rule_recheck_source_snapshot_sha256 = "sha256:" + "6" * 64
    service._decision_rule_recheck_source_core_id = "sha256:" + "5" * 64
    service._decision_rule_recheck_pending_codes.update(codes)

    service._run_priority_monitor(previous=service.snapshot(), observed_at=AS_OF)

    assert market.bundle_codes == [codes[0]]
    assert service._decision_rule_recheck_pending_codes == set(codes[1:])
    # 规则迁移复查使用 5m 数据，但不冒充常规候选池的五分钟 SLA 覆盖。
    assert service._candidate_monitor_five_last_success_at == {}
    health = service.health_snapshot()
    assert health["candidate_monitor_five_minute"]["universe_count"] == 0
    assert health["candidate_monitor_five_minute"]["last_batch_codes"] == []
    assert health["candidate_monitor_last_deferred_count"] == 0
    assert health["decision_rule_recheck_last_attempted_codes"] == [codes[0]]
    assert health["decision_rule_recheck_last_deferred_codes"] == list(codes[1:])

    restarted = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=MultiMemberSectorCatalog(codes),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
        config=service._config,
    )
    assert restarted._candidate_monitor_last_deferred_codes == ()
    assert restarted._decision_rule_recheck_last_deferred_codes == codes[1:]


def test_candidate_phase_cannot_open_a_second_budget_after_priority_deadline(
    tmp_path: Path,
) -> None:
    codes = ("SZ.000001", "SZ.000002")

    class SharedDeadlineMarket(RecordingMarketData):
        def priority_structure_bundle_with_risk_cutoff_until(
            self,
            code: str,
            *,
            deadline_monotonic: float,
            **kwargs,
        ) -> SymbolStructureBundle:
            del deadline_monotonic
            time.sleep(0.07)
            return super().structure_bundle_with_risk_cutoff(code, **kwargs)

        def candidate_structure_bundle_with_risk_cutoff_until(
            self,
            code: str,
            *,
            deadline_monotonic: float,
            **kwargs,
        ) -> SymbolStructureBundle:
            raise AssertionError(f"候选 {code} 不得在 1m 已耗尽本轮截止时间后启动")

    market = SharedDeadlineMarket()
    catalog = MultiMemberSectorCatalog(codes)
    catalog.batch = SectorAssessmentBatch(
        assessments=(replace(eligible_sector(), regime="supportive"),),
        discovered_count=1,
        completed_count=1,
        failure_counts=(),
        errors=(),
    )
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=catalog,
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(
            priority_monitoring_enabled=True,
            stock_worker_count=1,
            priority_monitor_time_budget_seconds=0.05,
            candidate_monitor_time_budget_seconds=1.0,
            max_monitor_symbols_per_refresh=2,
            max_five_minute_candidate_symbols_per_refresh=2,
            admitted_universe_codes=codes,
        ),
    )
    previous = {
        "signals": [
            {
                "signal_id": "triggered-buy",
                "code": codes[0],
                "point_type": "1buy",
                "lifecycle_stage": "triggered",
                "setup_5m": {
                    "anchor_at": AS_OF.isoformat(),
                    "available_at": AS_OF.isoformat(),
                    "confirmed_at": AS_OF.isoformat(),
                },
            },
            {
                "signal_id": "formed-buy",
                "code": codes[1],
                "point_type": "2buy",
                "lifecycle_stage": "formed",
            },
        ]
    }
    service._candidate_monitor_five_last_success_at = {codes[0]: AS_OF}
    service._candidate_monitor_thirty_last_success_at = {codes[0]: AS_OF}
    service._priority_monitor_last_at = AS_OF - timedelta(minutes=5)

    service._run_priority_monitor(previous=previous, observed_at=AS_OF)

    assert market.bundle_codes == [codes[0]]
    assert service.health_snapshot()["candidate_monitor_last_deferred_codes"] == [
        codes[1]
    ]


def test_closed_startup_candidate_catchup_uses_its_independent_budget(
    tmp_path: Path,
) -> None:
    closed_at = AS_OF.replace(year=2026, month=8, day=16, hour=10, minute=0)
    codes = ("SZ.000001", "SZ.000002")

    class ClosedStartupMarket(RecordingMarketData):
        def priority_structure_bundle_with_risk_cutoff_until(
            self,
            code: str,
            *,
            deadline_monotonic: float,
            **kwargs,
        ) -> SymbolStructureBundle:
            del deadline_monotonic
            time.sleep(0.07)
            return super().structure_bundle_with_risk_cutoff(code, **kwargs)

    market = ClosedStartupMarket()
    catalog = MultiMemberSectorCatalog(codes)
    catalog.batch = SectorAssessmentBatch(
        assessments=(replace(eligible_sector(), regime="supportive"),),
        discovered_count=1,
        completed_count=1,
        failure_counts=(),
        errors=(),
    )
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=catalog,
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: closed_at,
        notifier=None,
        config=TradingScreeningConfig(
            priority_monitoring_enabled=True,
            stock_worker_count=1,
            priority_monitor_time_budget_seconds=0.05,
            candidate_monitor_time_budget_seconds=1.0,
            max_monitor_symbols_per_refresh=2,
            max_five_minute_candidate_symbols_per_refresh=2,
            admitted_universe_codes=codes,
        ),
    )
    previous = {
        "signals": [
            {
                "signal_id": "triggered-buy",
                "code": codes[0],
                "point_type": "1buy",
                "lifecycle_stage": "triggered",
                "setup_5m": {
                    "anchor_at": closed_at.isoformat(),
                    "available_at": closed_at.isoformat(),
                    "confirmed_at": closed_at.isoformat(),
                },
            },
            {
                "signal_id": "formed-buy",
                "code": codes[1],
                "point_type": "2buy",
                "lifecycle_stage": "formed",
            },
        ]
    }
    service._candidate_monitor_five_last_success_at = {codes[0]: closed_at}
    service._candidate_monitor_thirty_last_success_at = {codes[0]: closed_at}

    service._run_priority_monitor(
        previous=previous,
        observed_at=closed_at,
        force_startup_bootstrap=True,
    )

    assert market.bundle_codes == list(codes)
    assert service.health_snapshot()["candidate_monitor_last_deferred_codes"] == []


def test_candidate_budget_starts_when_candidate_lane_is_admitted(
    tmp_path: Path,
) -> None:
    code = "SZ.000001"

    class SlowPrioritySetupMarket(RecordingMarketData):
        def active_watchlist_scope(
            self,
        ) -> tuple[tuple[str, ...], tuple[str, ...]]:
            # Sector/scope preparation belongs to the priority round.  It must
            # not consume the candidate lane's own execution budget before the
            # first candidate wave can even be admitted.
            time.sleep(0.07)
            return (), ()

    market = SlowPrioritySetupMarket()
    catalog = MultiMemberSectorCatalog((code,))
    catalog.batch = SectorAssessmentBatch(
        assessments=(replace(eligible_sector(), regime="supportive"),),
        discovered_count=1,
        completed_count=1,
        failure_counts=(),
        errors=(),
    )
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=catalog,
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(
            priority_monitoring_enabled=True,
            stock_worker_count=1,
            candidate_monitor_time_budget_seconds=0.05,
            max_five_minute_candidate_symbols_per_refresh=1,
            max_thirty_minute_candidate_symbols_per_refresh=1,
        ),
    )

    service._run_priority_monitor(previous=service.snapshot(), observed_at=AS_OF)

    assert market.bundle_codes == [code]
    assert service.health_snapshot()["candidate_monitor_last_deferred_codes"] == []


def test_priority_time_budget_reports_unstarted_mandatory_codes(
    tmp_path: Path,
) -> None:
    codes = ("SZ.000001", "SZ.000002", "SZ.000003")

    class BudgetPriorityMarket(RecordingMarketData):
        def active_watchlist(self) -> tuple[str, ...]:
            return codes

        def priority_structure_bundle_with_risk_cutoff_until(
            self,
            code: str,
            *,
            deadline_monotonic: float,
            **kwargs,
        ) -> SymbolStructureBundle:
            del deadline_monotonic
            time.sleep(0.07)
            return super().structure_bundle_with_risk_cutoff(code, **kwargs)

    market = BudgetPriorityMarket()
    catalog = MultiMemberSectorCatalog(codes)
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=catalog,
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(
            priority_monitoring_enabled=True,
            stock_worker_count=1,
            max_monitor_symbols_per_refresh=3,
            priority_monitor_time_budget_seconds=0.05,
        ),
    )
    previous = {
        "signals": [
            {
                "signal_id": f"triggered-{code}",
                "code": code,
                "point_type": "1buy",
                "lifecycle_stage": "triggered",
            }
            for code in codes
        ]
    }

    service._run_priority_monitor(previous=previous, observed_at=AS_OF)

    health = service.health_snapshot()
    assert market.bundle_codes == [codes[0]]
    assert health["priority_monitor_last_codes"] == [codes[0]]
    assert health["priority_monitor_last_failure_reason_counts"] == {
        "PRIORITY_MONITOR_TIME_BUDGET_EXHAUSTED": 1,
    }


def test_priority_monitor_waits_for_first_trade_without_marking_suspended(
    tmp_path: Path,
) -> None:
    observed_at = datetime(2026, 7, 27, 10, 0, tzinfo=AS_OF.tzinfo)
    previous_close = datetime(2026, 7, 24, 15, 0, tzinfo=AS_OF.tzinfo)

    class FirstTradePendingPriorityMarket(RecordingMarketData):
        def __init__(self) -> None:
            super().__init__()
            self.trading = False

        def active_watchlist(self) -> tuple[str, ...]:
            return ("SH.513100",)

        def priority_realtime_ticks(
            self,
            codes: tuple[str, ...],
        ) -> AShareRealtimeQuoteBatch:
            assert codes == ("SH.513100",)
            return AShareRealtimeQuoteBatch(
                requested_codes=codes,
                market_open=True,
                quotes=(
                    AShareRealtimeQuote(
                        code="SH.513100",
                        last=2.264,
                        buy1=2.263 if self.trading else 0.0,
                        sell1=2.264 if self.trading else 0.0,
                        high=2.27 if self.trading else 0.0,
                        low=2.25 if self.trading else 0.0,
                        open=2.26 if self.trading else 0.0,
                        volume=1000.0 if self.trading else 0.0,
                        rate=0.0,
                    ),
                ),
                tick_data_used=True,
            )

        def realtime_ticks(
            self,
            codes: tuple[str, ...],
        ) -> AShareRealtimeQuoteBatch:
            del codes
            raise AssertionError("mandatory quote must use the priority lane")

        def priority_current_session_instrument_statuses(
            self,
            codes: tuple[str, ...],
            *,
            session: date,
        ) -> AShareInstrumentSessionStatusBatch:
            assert codes == ("SH.513100",)
            return AShareInstrumentSessionStatusBatch(
                requested_codes=codes,
                session=session,
                facts=(
                    AShareInstrumentSessionStatus(
                        code="SH.513100",
                        trading_day=session,
                        instrument_name="纳指ETF",
                        instrument_status=2,
                        is_trading=False,
                    ),
                ),
            )

        def structure_bundle_with_risk_cutoff(
            self,
            code: str,
            *,
            as_of: datetime,
            sector,
            frequencies=(),
            risk_evidence_cutoff: datetime,
        ) -> SymbolStructureBundle:
            del risk_evidence_cutoff
            return self.structure_bundle(
                code,
                as_of=as_of if self.trading else previous_close,
                sector=sector,
                frequencies=frequencies,
            )

    market = FirstTradePendingPriorityMarket()
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=MultiMemberSectorCatalog(("SH.513100",)),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: observed_at,
        notifier=None,
        config=TradingScreeningConfig(
            priority_monitoring_enabled=True,
            max_monitor_symbols_per_refresh=1,
            max_structure_age_seconds=3600,
            admitted_universe_codes=("SH.513100",),
        ),
    )

    service._run_priority_monitor(
        previous=service.snapshot(),
        observed_at=observed_at,
    )

    health = service.health_snapshot()
    assert market.bundle_codes == []
    assert health["priority_monitor_last_error_count"] == 0
    assert health["priority_monitor_last_codes"] == []
    assert health["priority_monitor_mandatory_count"] == 1
    assert health["priority_monitor_scheduled_count"] == 0
    assert health["priority_monitor_current_session_zero_trade_code_count"] == 1
    assert health["priority_monitor_current_session_zero_trade_codes"] == ["SH.513100"]
    assert health["priority_monitor_current_session_suspended_code_count"] == 0
    assert health["priority_monitor_current_session_suspended_codes"] == []
    assert health["priority_monitor_instrument_status_probe_status"] == (
        "awaiting_first_trade"
    )
    assert health["priority_monitor_instrument_status_probe_error"] is None
    assert health["priority_monitor_zero_trade_quote_status"] == ("verified_zero_trade")
    assert health["priority_monitor_zero_trade_quote_error"] is None
    assert health["priority_monitor_zero_trade_quote_diagnostics"] == [
        {
            "code": "SH.513100",
            "present": True,
            "last_positive": True,
            "nonzero_fields": [],
            "malformed": False,
        }
    ]

    market.trading = True
    service._run_priority_monitor(
        previous=service.snapshot(),
        observed_at=observed_at + timedelta(minutes=31),
    )

    resumed = service.health_snapshot()
    assert market.bundle_codes == ["SH.513100"]
    assert resumed["priority_monitor_scheduled_count"] == 1
    assert resumed["priority_monitor_last_codes"] == ["SH.513100"]
    assert resumed["priority_monitor_current_session_suspended_codes"] == []
    assert resumed["priority_monitor_instrument_status_probe_status"] == (
        "verified_no_suspension"
    )


def test_awaiting_first_trade_symbol_does_not_consume_live_capacity(
    tmp_path: Path,
) -> None:
    codes = ("SH.513100", "SZ.000001")

    class MixedStatusPriorityMarket(RecordingMarketData):
        def active_watchlist(self) -> tuple[str, ...]:
            return codes

        def priority_realtime_ticks(
            self,
            requested: tuple[str, ...],
        ) -> AShareRealtimeQuoteBatch:
            assert requested == codes
            return AShareRealtimeQuoteBatch(
                requested_codes=requested,
                market_open=True,
                quotes=(
                    AShareRealtimeQuote(
                        code="SH.513100",
                        last=2.264,
                        buy1=0.0,
                        sell1=0.0,
                        high=0.0,
                        low=0.0,
                        open=0.0,
                        volume=0.0,
                        rate=0.0,
                    ),
                    AShareRealtimeQuote(
                        code="SZ.000001",
                        last=10.0,
                        buy1=9.99,
                        sell1=10.01,
                        high=10.1,
                        low=9.9,
                        open=10.0,
                        volume=1000.0,
                        rate=0.0,
                    ),
                ),
                tick_data_used=True,
            )

        def priority_current_session_instrument_statuses(
            self,
            requested: tuple[str, ...],
            *,
            session: date,
        ) -> AShareInstrumentSessionStatusBatch:
            assert requested == codes
            return AShareInstrumentSessionStatusBatch(
                requested_codes=requested,
                session=session,
                facts=(
                    AShareInstrumentSessionStatus(
                        code="SH.513100",
                        trading_day=session,
                        instrument_name="纳指ETF",
                        instrument_status=14,
                        is_trading=False,
                    ),
                ),
            )

    market = MixedStatusPriorityMarket()
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=MultiMemberSectorCatalog(codes),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(
            priority_monitoring_enabled=True,
            max_monitor_symbols_per_refresh=1,
            admitted_universe_codes=codes,
        ),
    )

    service._run_priority_monitor(
        previous=service.snapshot(),
        observed_at=AS_OF,
    )

    health = service.health_snapshot()
    assert market.bundle_codes == ["SZ.000001"]
    assert health["priority_monitor_mandatory_count"] == 2
    assert health["priority_monitor_scheduled_count"] == 1
    assert health["priority_monitor_last_codes"] == ["SZ.000001"]
    assert health["priority_monitor_last_error_count"] == 0
    assert health["priority_monitor_current_session_suspended_codes"] == []
    assert health["priority_monitor_instrument_status_probe_status"] == (
        "awaiting_first_trade"
    )


def test_positive_instrument_status_does_not_exclude_actively_trading_etf(
    tmp_path: Path,
) -> None:
    code = "SH.513100"

    class ActiveEtfPriorityMarket(RecordingMarketData):
        def active_watchlist(self) -> tuple[str, ...]:
            return (code,)

        def priority_realtime_ticks(
            self,
            requested: tuple[str, ...],
        ) -> AShareRealtimeQuoteBatch:
            return AShareRealtimeQuoteBatch(
                requested_codes=requested,
                market_open=True,
                quotes=(
                    AShareRealtimeQuote(
                        code=code,
                        last=2.191,
                        buy1=2.190,
                        sell1=2.191,
                        high=2.198,
                        low=2.187,
                        open=2.195,
                        volume=762383.0,
                        rate=-0.95,
                    ),
                ),
                tick_data_used=True,
            )

        def priority_current_session_instrument_statuses(
            self,
            requested: tuple[str, ...],
            *,
            session: date,
        ) -> AShareInstrumentSessionStatusBatch:
            return AShareInstrumentSessionStatusBatch(
                requested_codes=requested,
                session=session,
                facts=(
                    AShareInstrumentSessionStatus(
                        code=code,
                        trading_day=session,
                        instrument_name="纳指ETF",
                        instrument_status=17,
                        is_trading=False,
                    ),
                ),
            )

    market = ActiveEtfPriorityMarket()
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=MultiMemberSectorCatalog((code,)),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(
            priority_monitoring_enabled=True,
            max_monitor_symbols_per_refresh=1,
        ),
    )

    service._run_priority_monitor(
        previous=service.snapshot(),
        observed_at=AS_OF,
    )

    health = service.health_snapshot()
    assert market.bundle_codes == [code]
    assert health["priority_monitor_scheduled_count"] == 1
    assert health["priority_monitor_current_session_suspended_codes"] == []
    assert health["priority_monitor_instrument_status_probe_status"] == (
        "verified_no_suspension"
    )
    assert health["priority_monitor_last_error_count"] == 0


def test_candidate_waiting_for_first_trade_retries_on_next_five_minute_epoch(
    tmp_path: Path,
) -> None:
    code = "SZ.002084"
    observed_at = datetime(2026, 7, 27, 10, 0, tzinfo=AS_OF.tzinfo)
    previous_close = datetime(2026, 7, 24, 15, 0, tzinfo=AS_OF.tzinfo)

    class FirstTradePendingCandidateMarket(RecordingMarketData):
        def __init__(self) -> None:
            super().__init__()
            self.trading = False

        def priority_realtime_ticks(
            self,
            requested: tuple[str, ...],
        ) -> AShareRealtimeQuoteBatch:
            assert requested == (code,)
            return AShareRealtimeQuoteBatch(
                requested_codes=requested,
                market_open=True,
                quotes=(
                    AShareRealtimeQuote(
                        code=code,
                        last=3.59,
                        buy1=3.58 if self.trading else 0.0,
                        sell1=3.59 if self.trading else 0.0,
                        high=3.61 if self.trading else 0.0,
                        low=3.55 if self.trading else 0.0,
                        open=3.57 if self.trading else 0.0,
                        volume=1000.0 if self.trading else 0.0,
                        rate=0.0,
                    ),
                ),
                tick_data_used=True,
            )

        def priority_current_session_instrument_statuses(
            self,
            requested: tuple[str, ...],
            *,
            session: date,
        ) -> AShareInstrumentSessionStatusBatch:
            assert requested == (code,)
            return AShareInstrumentSessionStatusBatch(
                requested_codes=requested,
                session=session,
                facts=(
                    AShareInstrumentSessionStatus(
                        code=code,
                        trading_day=session,
                        instrument_name="海鸥住工",
                        instrument_status=4,
                        is_trading=False,
                    ),
                ),
            )

        def structure_bundle_with_risk_cutoff(
            self,
            requested_code: str,
            *,
            as_of: datetime,
            sector,
            frequencies=(),
            risk_evidence_cutoff: datetime,
        ) -> SymbolStructureBundle:
            del risk_evidence_cutoff
            return self.structure_bundle(
                requested_code,
                as_of=as_of if self.trading else previous_close,
                sector=sector,
                frequencies=frequencies,
            )

    market = FirstTradePendingCandidateMarket()
    supportive_batch = SectorAssessmentBatch(
        assessments=(replace(eligible_sector(), regime="supportive"),),
        discovered_count=1,
        completed_count=1,
        failure_counts=(),
        errors=(),
    )
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=EvidenceSectorCatalog(supportive_batch, (code,)),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: observed_at,
        notifier=None,
        config=TradingScreeningConfig(
            priority_monitoring_enabled=True,
            admitted_universe_codes=(code,),
        ),
    )

    service._run_priority_monitor(
        previous=service.snapshot(),
        observed_at=observed_at,
    )

    health = service.health_snapshot()
    assert market.bundle_codes == [code]
    assert health["candidate_monitor_last_error_count"] == 0
    assert health["candidate_monitor_five_minute"]["universe_count"] == 0
    assert health["candidate_monitor_current_session_suspended_codes"] == []
    assert health["candidate_monitor_suspension_probe_status"] == (
        "awaiting_first_trade"
    )
    assert health["candidate_monitor_symbol_exclusion_codes"] == [code]
    assert health["candidate_monitor_symbol_exclusion_reason_counts"] == {
        "CURRENT_SESSION_FIRST_TRADE_PENDING": 1
    }

    market.trading = True
    service._run_priority_monitor(
        previous=service.snapshot(),
        observed_at=observed_at + timedelta(minutes=6),
    )

    assert market.bundle_codes == [code, code]
    assert (
        service.health_snapshot()[
            "candidate_monitor_current_session_suspended_code_count"
        ]
        == 0
    )
    assert service.health_snapshot()["candidate_monitor_symbol_exclusion_codes"] == []


def test_deterministic_candidate_rejection_is_epoch_scoped_not_lane_failure(
    tmp_path: Path,
) -> None:
    code = "SZ.000001"
    observed_at = AS_OF.replace(hour=14, minute=58, second=0, microsecond=0)

    class InsufficientHistoryCandidateMarket(RecordingMarketData):
        def structure_bundle_with_risk_cutoff(
            self,
            requested_code: str,
            **_kwargs,
        ) -> SymbolStructureBundle:
            self.bundle_codes.append(requested_code)
            raise ValueError("kline frame does not meet minimum history")

    supportive_batch = SectorAssessmentBatch(
        assessments=(replace(eligible_sector(), regime="supportive"),),
        discovered_count=1,
        completed_count=1,
        failure_counts=(),
        errors=(),
    )
    market = InsufficientHistoryCandidateMarket()
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=EvidenceSectorCatalog(supportive_batch, (code,)),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: observed_at,
        notifier=None,
        config=TradingScreeningConfig(
            priority_monitoring_enabled=True,
            admitted_universe_codes=(code,),
        ),
    )

    service._run_priority_monitor(previous=service.snapshot(), observed_at=observed_at)
    health = service.health_snapshot()

    assert market.bundle_codes == [code]
    assert health["candidate_monitor_last_error_count"] == 0
    assert health["candidate_monitor_symbol_exclusion_count"] == 1
    assert health["candidate_monitor_symbol_exclusion_codes"] == [code]
    assert health["candidate_monitor_symbol_exclusion_reason_counts"] == {
        "KLINE_MINIMUM_HISTORY_NOT_MET": 1
    }
    assert health["candidate_monitor_status"] == "idle_no_candidates"
    assert health["candidate_monitor_ready"] is True
    assert health["candidate_monitor_observed_capacity_sufficient"] is True
    with service._state_lock:
        snapshot_with_old_signal = dict(service._snapshot)
        snapshot_with_old_signal["signals"] = [
            {
                "signal_id": "old-candidate-signal",
                "code": code,
                "point_type": "1buy",
                "lifecycle_stage": "triggered",
            }
        ]
        snapshot_with_old_signal["snapshot_content_sha256"] = "sha256:" + "9" * 64
        service._snapshot = snapshot_with_old_signal
    presentation = service.presentation_snapshot()
    assert presentation["candidate_live_overlay"]["symbol_exclusion_count"] == 1
    assert presentation["candidate_live_overlay"]["symbol_exclusion_codes"] == [code]
    assert all(row.get("code") != code for row in presentation["signals"])

    # 14:58 and 14:59 both observe the completed 14:55 five-minute bar.  The
    # deterministic rejection is not hot-looped until a new 15:00 fact exists.
    service._run_priority_monitor(
        previous=service.snapshot(),
        observed_at=observed_at + timedelta(minutes=1),
    )
    assert market.bundle_codes == [code]

    service._run_priority_monitor(
        previous=service.snapshot(),
        observed_at=observed_at + timedelta(minutes=2),
    )
    assert market.bundle_codes == [code, code]


def test_optional_segment_rotation_beyond_locator_sla_is_capacity_failure(
    tmp_path: Path,
) -> None:
    codes = ("SZ.000001", "SZ.000002", "SZ.000003")
    market = RecordingMarketData()
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=MultiMemberSectorCatalog(codes),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(
            priority_monitoring_enabled=True,
            max_monitor_symbols_per_refresh=2,
            admitted_universe_codes=codes,
        ),
    )
    previous = {
        "signals": [
            {
                "signal_id": f"triggered-{code}",
                "code": code,
                "point_type": "1buy",
                "lifecycle_stage": "triggered",
                "setup_5m": {
                    "anchor_at": AS_OF.isoformat(),
                    "available_at": AS_OF.isoformat(),
                    "confirmed_at": AS_OF.isoformat(),
                },
            }
            for code in codes
        ]
    }

    service._run_priority_monitor(previous=previous, observed_at=AS_OF)

    health = service.health_snapshot()
    assert market.bundle_codes == list(codes[:2])
    assert health["priority_monitor_configured_rotation_seconds"] == 120
    assert health["priority_monitor_locator_sla_seconds"] == 60
    assert health["priority_monitor_locator_capacity_sufficient"] is False
    assert health["priority_monitor_locator_deferred_codes"] == [codes[2]]
    assert health["priority_monitor_last_failure_reason_counts"] == {
        "ONE_MINUTE_LOCATOR_CONFIGURED_CAPACITY_INSUFFICIENT": 1,
    }
    assert health["priority_monitor_ready"] is False


def test_large_scope_priority_wave_covers_48_current_locators(
    tmp_path: Path,
) -> None:
    codes = tuple(f"SZ.{index:06d}" for index in range(1, 49))
    market = RecordingMarketData()
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=MultiMemberSectorCatalog(codes),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(
            priority_monitoring_enabled=True,
            stock_worker_count=1,
            large_scope_authorized=True,
            max_monitor_symbols_per_refresh=48,
            max_admitted_universe_symbols=60,
        ),
    )
    previous = {
        "signals": [
            {
                "signal_id": f"triggered-{code}",
                "code": code,
                "side": "buy",
                "point_type": "1buy",
                "lifecycle_stage": "triggered",
                "setup_5m": {
                    "anchor_at": AS_OF.isoformat(),
                    "available_at": AS_OF.isoformat(),
                    "confirmed_at": AS_OF.isoformat(),
                },
            }
            for code in codes
        ]
    }

    service._run_priority_monitor(previous=previous, observed_at=AS_OF)

    health = service.health_snapshot()
    assert market.bundle_codes == list(codes)
    assert health["priority_monitor_locator_pool_count"] == 48
    assert health["priority_monitor_locator_admission_deferred_count"] == 0
    assert health["priority_monitor_immediate_universe_count"] == 48
    assert health["priority_monitor_scheduled_count"] == 48
    assert health["priority_monitor_configured_rotation_seconds"] == 60
    assert health["priority_monitor_locator_capacity_sufficient"] is True
    assert health["priority_monitor_locator_runtime_verified"] is True
    assert health["priority_monitor_ready"] is True


def test_priority_locator_wave_uses_all_configured_workers(
    tmp_path: Path,
) -> None:
    codes = tuple(f"SZ.{index:06d}" for index in range(1, 9))

    class ConcurrentPriorityMarket(RecordingMarketData):
        def __init__(self) -> None:
            super().__init__()
            self.active = 0
            self.max_active = 0
            self.lock = threading.Lock()

        def priority_structure_bundle_with_risk_cutoff_until(
            self,
            code: str,
            *,
            deadline_monotonic: float,
            **kwargs,
        ) -> SymbolStructureBundle:
            assert deadline_monotonic > time.perf_counter()
            with self.lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            try:
                time.sleep(0.04)
                return super().structure_bundle_with_risk_cutoff(code, **kwargs)
            finally:
                with self.lock:
                    self.active -= 1

    market = ConcurrentPriorityMarket()
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=MultiMemberSectorCatalog(codes),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(
            priority_monitoring_enabled=True,
            stock_worker_count=4,
            max_monitor_symbols_per_refresh=len(codes),
            admitted_universe_codes=codes,
        ),
    )
    previous = {
        "signals": [
            {
                "signal_id": f"triggered-{code}",
                "code": code,
                "side": "buy",
                "point_type": "1buy",
                "lifecycle_stage": "triggered",
                "setup_5m": {
                    "anchor_at": AS_OF.isoformat(),
                    "available_at": AS_OF.isoformat(),
                    "confirmed_at": AS_OF.isoformat(),
                },
            }
            for code in codes
        ]
    }

    service._run_priority_monitor(previous=previous, observed_at=AS_OF)

    assert market.max_active == 4
    assert set(market.bundle_codes) == set(codes)
    assert (
        service.health_snapshot()["priority_monitor_locator_runtime_verified"] is True
    )


def test_optional_segment_deadline_miss_is_visible_and_fails_closed(
    tmp_path: Path,
) -> None:
    codes = ("SZ.000001", "SZ.000002", "SZ.000003")

    class SlowLocatorMarket(RecordingMarketData):
        def priority_structure_bundle_with_risk_cutoff_until(
            self,
            code: str,
            *,
            deadline_monotonic: float,
            **kwargs,
        ) -> SymbolStructureBundle:
            del deadline_monotonic
            time.sleep(0.07)
            return super().structure_bundle_with_risk_cutoff(code, **kwargs)

    service = TradingScreeningService(
        market_data=SlowLocatorMarket(),
        sector_catalog=MultiMemberSectorCatalog(codes),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(
            priority_monitoring_enabled=True,
            stock_worker_count=1,
            max_monitor_symbols_per_refresh=3,
            priority_monitor_time_budget_seconds=0.05,
            admitted_universe_codes=codes,
        ),
    )
    previous = {
        "signals": [
            {
                "signal_id": f"triggered-{code}",
                "code": code,
                "point_type": "1buy",
                "lifecycle_stage": "triggered",
                "setup_5m": {
                    "anchor_at": AS_OF.isoformat(),
                    "available_at": AS_OF.isoformat(),
                    "confirmed_at": AS_OF.isoformat(),
                },
            }
            for code in codes
        ]
    }

    service._run_priority_monitor(previous=previous, observed_at=AS_OF)

    health = service.health_snapshot()
    assert health["priority_monitor_last_codes"] == [codes[0]]
    assert health["priority_monitor_locator_deferred_codes"] == list(codes[1:])
    assert health["priority_monitor_last_failure_reason_counts"] == {
        "ONE_MINUTE_LOCATOR_TIME_BUDGET_EXHAUSTED": 1,
    }
    assert health["priority_monitor_ready"] is False


def test_priority_locator_admits_final_wave_proven_to_fit_before_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codes = ("SZ.000001", "SZ.000002", "SZ.000003")

    class MonotonicClock:
        def __init__(self) -> None:
            self.value = 0.0

        def perf_counter(self) -> float:
            return self.value

        def advance(self, seconds: float) -> None:
            self.value += seconds

    monotonic = MonotonicClock()

    class FinalWaveMarket(RecordingMarketData):
        def priority_structure_bundle_with_risk_cutoff_until(
            self,
            code: str,
            *,
            deadline_monotonic: float,
            **kwargs,
        ) -> SymbolStructureBundle:
            assert deadline_monotonic == pytest.approx(0.95)
            monotonic.advance(0.3)
            return super().structure_bundle_with_risk_cutoff(code, **kwargs)

    monkeypatch.setattr(trading_screening_subject, "time", monotonic)
    market = FinalWaveMarket()
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=MultiMemberSectorCatalog(codes),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(
            priority_monitoring_enabled=True,
            stock_worker_count=1,
            max_monitor_symbols_per_refresh=3,
            priority_monitor_time_budget_seconds=0.95,
            admitted_universe_codes=codes,
        ),
    )
    previous = {
        "signals": [
            {
                "signal_id": f"triggered-{code}",
                "code": code,
                "point_type": "1buy",
                "lifecycle_stage": "triggered",
                "setup_5m": {
                    "anchor_at": AS_OF.isoformat(),
                    "available_at": AS_OF.isoformat(),
                    "confirmed_at": AS_OF.isoformat(),
                },
            }
            for code in codes
        ]
    }

    service._run_priority_monitor(previous=previous, observed_at=AS_OF)

    health = service.health_snapshot()
    assert market.bundle_codes == list(codes)
    assert health["priority_monitor_locator_last_scheduled_count"] == 3
    assert health["priority_monitor_locator_last_attempted_count"] == 3
    assert health["priority_monitor_locator_last_completed_count"] == 3
    assert health["priority_monitor_locator_deferred_codes"] == []
    assert health["priority_monitor_locator_runtime_verified"] is True


def test_closed_session_bounded_bootstrap_is_not_reported_as_degraded(
    tmp_path: Path,
) -> None:
    closed_at = AS_OF.replace(year=2026, month=8, day=16, hour=10, minute=0)
    codes = ("SZ.000001", "SZ.000002", "SZ.000003")

    class BudgetPriorityMarket(RecordingMarketData):
        def priority_structure_bundle_with_risk_cutoff_until(
            self,
            code: str,
            *,
            deadline_monotonic: float,
            **kwargs,
        ) -> SymbolStructureBundle:
            del deadline_monotonic
            time.sleep(0.07)
            return super().structure_bundle_with_risk_cutoff(code, **kwargs)

    market = BudgetPriorityMarket()
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=MultiMemberSectorCatalog(codes),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: closed_at,
        notifier=None,
        config=TradingScreeningConfig(
            priority_monitoring_enabled=True,
            stock_worker_count=1,
            max_monitor_symbols_per_refresh=3,
            priority_monitor_time_budget_seconds=0.05,
        ),
    )
    previous = {
        "signals": [
            {
                "signal_id": f"armed-{code}",
                "code": code,
                "point_type": "1buy",
                "lifecycle_stage": "armed",
            }
            for code in codes
        ]
    }

    service._run_priority_monitor(
        previous=previous,
        observed_at=closed_at,
        force_startup_bootstrap=True,
    )

    health = service.health_snapshot()
    assert market.bundle_codes == [codes[0]]
    assert health["priority_monitor_status"] == "not_due"
    assert health["priority_monitor_ready"] is True
    assert health["priority_monitor_last_error_count"] == 0


def test_formal_thirty_minute_candidate_runs_before_rule_recheck_backlog(
    tmp_path: Path,
) -> None:
    """一次性旧规则复核不能饿死正式 30m 候选。"""

    formal_code = "SZ.000001"
    recheck_codes = ("SZ.000002", "SZ.000003")

    class BudgetMarket(RecordingMarketData):
        def prepare_local_history(self, **_kwargs) -> dict[str, object]:
            return {}

        def prepare_local_history_until(self, **_kwargs) -> dict[str, object]:
            return {}

        def structure_bundle_with_risk_cutoff_until(
            self,
            code: str,
            *,
            deadline_monotonic: float,
            **kwargs,
        ) -> SymbolStructureBundle:
            del deadline_monotonic
            time.sleep(0.07)
            return super().structure_bundle_with_risk_cutoff(code, **kwargs)

    market = BudgetMarket()
    catalog = MultiMemberSectorCatalog((formal_code,))
    catalog.batch = SectorAssessmentBatch(
        assessments=(replace(eligible_sector(), regime="supportive"),),
        discovered_count=1,
        completed_count=1,
        failure_counts=(),
        errors=(),
    )
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=catalog,
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(
            priority_monitoring_enabled=True,
            stock_worker_count=1,
            candidate_monitor_time_budget_seconds=0.05,
            max_five_minute_candidate_symbols_per_refresh=3,
            max_thirty_minute_candidate_symbols_per_refresh=1,
        ),
    )
    service._decision_rule_recheck_source_snapshot_sha256 = "sha256:" + "6" * 64
    service._decision_rule_recheck_source_core_id = "sha256:" + "5" * 64
    service._decision_rule_recheck_pending_codes.update(recheck_codes)

    service._run_priority_monitor(previous=service.snapshot(), observed_at=AS_OF)

    assert market.bundle_codes == [formal_code]
    assert service._decision_rule_recheck_pending_codes == set(recheck_codes)
    health = service.health_snapshot()
    assert health["candidate_monitor_last_deferred_codes"] == []
    assert health["candidate_monitor_thirty_minute"]["last_batch_codes"] == [
        formal_code
    ]
    assert health["decision_rule_recheck_last_attempted_codes"] == []
    assert health["decision_rule_recheck_last_deferred_codes"] == list(recheck_codes)


def test_continuity_recheck_backlog_does_not_inflate_formal_candidate_sla(
    tmp_path: Path,
) -> None:
    """A deployment recheck queue must not become the recurring 5m universe."""

    formal_code = "SZ.000001"
    recheck_codes = ("SZ.000002", "SZ.000003", "SZ.000004")
    market = RecordingMarketData()
    catalog = MultiMemberSectorCatalog((formal_code,))
    catalog.batch = SectorAssessmentBatch(
        assessments=(replace(eligible_sector(), regime="supportive"),),
        discovered_count=1,
        completed_count=1,
        failure_counts=(),
        errors=(),
    )
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=catalog,
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(
            priority_monitoring_enabled=True,
            stock_worker_count=3,
        ),
    )
    service._decision_rule_recheck_source_snapshot_sha256 = "sha256:" + "6" * 64
    service._decision_rule_recheck_source_core_id = "sha256:" + "5" * 64
    service._decision_rule_recheck_pending_codes.update(recheck_codes)

    service._run_priority_monitor(
        previous=service.snapshot(),
        observed_at=AS_OF,
        preselection_continuity_codes=recheck_codes,
    )

    health = service.health_snapshot()
    assert health["candidate_monitor_five_minute"]["universe_count"] == 1
    assert health["candidate_monitor_thirty_minute"]["universe_count"] == 1
    assert health["candidate_monitor_five_minute"]["last_batch_codes"] == [formal_code]
    assert formal_code in market.bundle_codes
    assert health["decision_rule_recheck_pending_count"] == 1


def test_priority_signal_candidates_treat_buy_and_sell_symmetrically() -> None:
    rows = (
        {
            "code": "SELL_ONLY",
            "point_type": "3sell",
            "lifecycle_stage": "triggered",
        },
        {
            "code": "BUY_APPROACHING",
            "point_type": "3buy",
            "lifecycle_stage": "approaching",
        },
        {
            "code": "BUY_ARMED",
            "point_type": "1buy",
            "lifecycle_stage": "armed",
        },
        {
            "code": "BUY_EXECUTABLE",
            "point_type": "2buy",
            "lifecycle_stage": "executable",
        },
        {
            "code": "BUY_APPROACHING_B",
            "point_type": "3buy",
            "lifecycle_stage": "approaching",
        },
        {
            "code": "WATCHED_BUY",
            "point_type": "2buy",
            "lifecycle_stage": "triggered",
        },
    )
    candidates = _priority_signal_candidate_codes(
        rows,
        excluded_codes=frozenset({"WATCHED_BUY"}),
    )

    assert candidates == (
        "BUY_EXECUTABLE",
        "SELL_ONLY",
        "BUY_ARMED",
        "BUY_APPROACHING",
        "BUY_APPROACHING_B",
    )
    urgent = _priority_signal_candidate_codes(
        rows,
        excluded_codes=frozenset({"WATCHED_BUY"}),
        allowed_stages=frozenset({"armed", "triggered", "executable", "active"}),
    )
    assert urgent == ("BUY_EXECUTABLE", "SELL_ONLY", "BUY_ARMED")


def test_signal_candidate_admission_pins_current_setups_and_rotates_completed_window() -> (
    None
):
    observed_at = AS_OF.replace(hour=10, minute=0)
    universe = ("ACTIVE", "A", "B", "C", "D")

    incomplete = _rotating_signal_candidate_admission_order(
        universe,
        pinned_codes=("ACTIVE",),
        previous_universe=("ACTIVE", "A", "B"),
        last_success_at={"A": observed_at},
    )
    completed = _rotating_signal_candidate_admission_order(
        universe,
        pinned_codes=("ACTIVE",),
        previous_universe=("ACTIVE", "A", "B"),
        last_success_at={"A": observed_at, "B": observed_at},
    )

    assert incomplete == ("ACTIVE", "A", "B", "C", "D")
    assert completed == ("ACTIVE", "C", "D", "A", "B")


def test_signal_candidate_admission_rotates_completed_pinned_overflow() -> None:
    observed_at = AS_OF.replace(hour=10, minute=0)
    universe = ("PIN_A", "PIN_B", "PIN_C", "PIN_D", "DISCOVERY")

    incomplete = _rotating_signal_candidate_admission_order(
        universe,
        pinned_codes=("PIN_A", "PIN_B", "PIN_C", "PIN_D"),
        previous_universe=("PIN_A", "PIN_B"),
        last_success_at={"PIN_A": observed_at},
    )
    completed = _rotating_signal_candidate_admission_order(
        universe,
        pinned_codes=("PIN_A", "PIN_B", "PIN_C", "PIN_D"),
        previous_universe=("PIN_A", "PIN_B"),
        last_success_at={"PIN_A": observed_at, "PIN_B": observed_at},
    )

    assert incomplete == ("PIN_A", "PIN_B", "PIN_C", "PIN_D", "DISCOVERY")
    assert completed == ("PIN_C", "PIN_D", "PIN_A", "PIN_B", "DISCOVERY")


def test_segment_monitor_keeps_current_five_minute_setups_in_locator_rotation(
    tmp_path: Path,
) -> None:
    observed_at = AS_OF.replace(hour=14, minute=58)
    fresh_code = "SZ.000001"
    stale_code = "SZ.000002"
    forming_code = "SZ.000003"
    rearmed_code = "SZ.000004"
    current_locator_code = "SZ.000005"
    codes = (
        fresh_code,
        stale_code,
        forming_code,
        rearmed_code,
        current_locator_code,
    )
    market = RecordingMarketData()
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=MultiMemberSectorCatalog(codes),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: observed_at,
        notifier=None,
        config=TradingScreeningConfig(
            priority_monitoring_enabled=True,
            stock_worker_count=1,
            max_five_minute_candidate_symbols_per_refresh=8,
            max_thirty_minute_candidate_symbols_per_refresh=8,
            admitted_universe_codes=codes,
        ),
    )
    previous = {
        "signals": [
            *(
                {
                    "signal_id": f"triggered-{code}",
                    "code": code,
                    "point_type": "1buy",
                    "lifecycle_stage": "triggered",
                    "setup_5m": {
                        "anchor_at": available_at.isoformat(),
                        "available_at": available_at.isoformat(),
                        "confirmed_at": available_at.isoformat(),
                    },
                }
                for code, available_at in (
                    (fresh_code, observed_at - timedelta(minutes=5)),
                    (stale_code, observed_at - timedelta(minutes=11)),
                )
            ),
            {
                "signal_id": f"approaching-{forming_code}",
                "code": forming_code,
                "point_type": "2buy",
                "lifecycle_stage": "approaching",
                "setup_5m": {
                    "available_at": (observed_at - timedelta(minutes=30)).isoformat(),
                },
            },
            *(
                {
                    "signal_id": f"triggered-{code}",
                    "code": code,
                    "side": "buy",
                    "point_type": "2buy",
                    "lifecycle_stage": "triggered",
                    "setup_5m": {
                        "anchor_at": (observed_at - timedelta(minutes=5)).isoformat(),
                        "available_at": (
                            observed_at - timedelta(minutes=5)
                        ).isoformat(),
                        "confirmed_at": (
                            observed_at - timedelta(minutes=5)
                        ).isoformat(),
                    },
                    "segment_difference_1m": {
                        "point_id": f"segment-{code}",
                    },
                    "entry_execution_boundary": {
                        "entry_valid_until": valid_until.isoformat(),
                    },
                }
                for code, valid_until in (
                    (rearmed_code, observed_at),
                    (current_locator_code, observed_at + timedelta(minutes=1)),
                )
            ),
        ]
    }

    service._run_priority_monitor(previous=previous, observed_at=observed_at)

    requests = dict(market.bundle_frequency_requests)
    assert "1m" in requests[fresh_code]
    assert "1m" in requests[stale_code]
    assert "1m" not in requests.get(rearmed_code, ())
    assert "1m" not in requests.get(current_locator_code, ())
    health = service.health_snapshot()
    assert health["candidate_monitor_five_minute"]["universe_count"] == 5
    assert health["priority_monitor_immediate_universe_count"] == 2
    assert "priority_monitor_rearmed_segment_universe_count" not in health
    assert "priority_monitor_expired_segment_universe_count" not in health
    assert "priority_monitor_segment_difference_universe_count" not in health
    assert health["priority_monitor_scheduled_count"] == 2


def test_validation_scope_caps_old_snapshot_locator_universe(
    tmp_path: Path,
) -> None:
    observed_at = AS_OF.replace(hour=14, minute=58)
    codes = tuple(f"SZ.{index:06d}" for index in range(1, 35))
    market = RecordingMarketData()
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=MultiMemberSectorCatalog(codes),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: observed_at,
        notifier=None,
        config=TradingScreeningConfig(
            priority_monitoring_enabled=True,
            stock_worker_count=1,
        ),
    )
    previous = {
        "signals": [
            {
                "signal_id": f"triggered-{code}",
                "code": code,
                "side": "buy",
                "point_type": "1buy",
                "lifecycle_stage": "triggered",
                "setup_5m": {
                    "anchor_at": observed_at.isoformat(),
                    "available_at": observed_at.isoformat(),
                    "confirmed_at": observed_at.isoformat(),
                },
            }
            for code in codes
        ]
    }

    service._run_priority_monitor(previous=previous, observed_at=observed_at)

    health = service.health_snapshot()
    assert health["screening_scope_mode"] == "VALIDATION_COHORT"
    assert health["effective_monitor_universe_limit"] == 12
    assert health["priority_monitor_immediate_universe_count"] == 12
    assert health["priority_monitor_immediate_pool_count"] == 34
    assert health["priority_monitor_immediate_deferred_count"] == 22
    assert health["priority_monitor_locator_pool_count"] == 34
    assert health["priority_monitor_locator_admission_deferred_count"] == 22
    assert health["priority_monitor_locator_capacity_sufficient"] is False
    assert health["priority_monitor_locator_deferred_codes"] == list(codes[12:])
    assert health["priority_monitor_last_failure_reason_counts"] == {
        "ONE_MINUTE_LOCATOR_ADMISSION_CAPACITY_INSUFFICIENT": 1,
    }
    assert health["priority_monitor_ready"] is False
    assert health["candidate_monitor_signal_pool_count"] == 34
    assert health["candidate_monitor_signal_admitted_count"] == 12
    assert health["candidate_monitor_signal_deferred_count"] == 22
    assert health["candidate_monitor_signal_rotation_active"] is True
    assert health["candidate_monitor_five_minute"]["universe_count"] == 12
    assert {code for code, _frequencies in market.bundle_frequency_requests} == set(
        codes[:12]
    )


def test_segment_monitor_membership_requires_execution_fresh_setup_anchor() -> None:
    observed_at = datetime(2026, 7, 20, 13, 5, tzinfo=AS_OF.tzinfo)
    signal = {
        "lifecycle_stage": "triggered",
        "setup_5m": {
            "anchor_at": datetime(
                2026,
                7,
                20,
                11,
                30,
                tzinfo=AS_OF.tzinfo,
            ).isoformat(),
            "available_at": datetime(
                2026,
                7,
                20,
                11,
                30,
                tzinfo=AS_OF.tzinfo,
            ).isoformat(),
        },
    }

    assert (
        trading_screening_subject._current_five_minute_setup_requires_segment_monitor(
            signal,
            observed_at,
        )
        is True
    )
    assert (
        trading_screening_subject._current_five_minute_setup_requires_segment_monitor(
            {
                "lifecycle_stage": "triggered",
                "setup_5m": {
                    "anchor_at": observed_at - timedelta(days=5),
                    "terminal_segment_available_at": datetime(
                        2026,
                        7,
                        20,
                        11,
                        30,
                        tzinfo=AS_OF.tzinfo,
                    ).isoformat(),
                },
            },
            observed_at,
        )
        is False
    )
    assert (
        trading_screening_subject._current_five_minute_setup_requires_segment_monitor(
            {
                "lifecycle_stage": "triggered",
                "setup_5m": {
                    "terminal_segment_available_at": datetime(
                        2026,
                        7,
                        20,
                        11,
                        15,
                        tzinfo=AS_OF.tzinfo,
                    ).isoformat()
                },
            },
            observed_at,
        )
        is False
    )
    assert (
        trading_screening_subject._current_five_minute_setup_requires_segment_monitor(
            {
                "lifecycle_stage": "triggered",
                "setup_5m": {"available_at": "malformed"},
            },
            observed_at,
        )
        is False
    )
    # Removed legacy rows without a causal anchor fail closed.
    assert (
        trading_screening_subject._current_five_minute_setup_requires_segment_monitor(
            {"lifecycle_stage": "triggered", "setup_5m": {}},
            observed_at,
        )
        is False
    )


def test_first_one_minute_witness_never_reopens_after_boundary_expiry() -> None:
    observed_at = datetime(2026, 7, 20, 10, 2, tzinfo=AS_OF.tzinfo)
    base = {
        "side": "buy",
        "segment_difference_1m": {"point_id": "segment:one"},
        "entry_execution_boundary": {
            "entry_valid_until": (observed_at + timedelta(minutes=1)).isoformat(),
        },
    }

    assert (
        trading_screening_subject._one_minute_segment_requires_monitor(
            base,
            observed_at,
        )
        is False
    )
    assert (
        trading_screening_subject._one_minute_segment_requires_monitor(
            {
                **base,
                "entry_execution_boundary": {
                    "entry_valid_until": observed_at.isoformat(),
                },
            },
            observed_at,
        )
        is False
    )
    assert (
        trading_screening_subject._one_minute_segment_requires_monitor(
            {**base, "entry_execution_boundary": None},
            observed_at,
        )
        is False
    )
    assert (
        trading_screening_subject._one_minute_segment_requires_monitor(
            {**base, "side": "sell", "entry_execution_boundary": None},
            observed_at,
        )
        is False
    )
    assert (
        trading_screening_subject._one_minute_segment_requires_monitor(
            {"side": "sell", "segment_difference_1m": None},
            observed_at,
        )
        is False
    )
    assert (
        trading_screening_subject._one_minute_segment_requires_monitor(
            {"side": "buy", "segment_difference_1m": None},
            observed_at,
        )
        is True
    )


def test_priority_monitor_tombstone_persists_until_newer_full_snapshot() -> None:
    cutoff = AS_OF.replace(hour=10, minute=0, second=0, microsecond=0)
    observed = cutoff + timedelta(minutes=1)
    main = (
        {
            "signal_id": "old-signal",
            "code": "SH.600001",
            "point_type": "3buy",
            "lifecycle_stage": "formed",
        },
    )

    merged, overlay, authoritative, superseded = (
        trading_screening_subject._merge_authoritative_monitor_documents(
            main,
            (),
            {"SH.600001": (observed, "5m")},
            snapshot_market_data_as_of=cutoff.isoformat(),
        )
    )

    assert merged == ()
    assert overlay == ()
    assert authoritative == frozenset({"SH.600001"})
    assert superseded == frozenset()

    monitor_document = {
        **main[0],
        "signal_id": "new-signal",
        "lifecycle_stage": "triggered",
    }
    merged, overlay, authoritative, superseded = (
        trading_screening_subject._merge_authoritative_monitor_documents(
            main,
            (monitor_document,),
            {"SH.600001": (observed, "5m")},
            snapshot_market_data_as_of=cutoff.isoformat(),
        )
    )
    assert merged == (monitor_document,)
    assert overlay == (monitor_document,)
    assert authoritative == frozenset({"SH.600001"})
    assert superseded == frozenset()

    merged, overlay, authoritative, superseded = (
        trading_screening_subject._merge_authoritative_monitor_documents(
            main,
            (monitor_document,),
            {"SH.600001": (observed, "5m")},
            snapshot_market_data_as_of=(observed + timedelta(minutes=1)).isoformat(),
        )
    )
    assert merged == main
    assert overlay == ()
    assert authoritative == frozenset()
    assert superseded == frozenset({"SH.600001"})


def test_priority_batch_rotates_after_a_partial_deadline_round() -> None:
    # The configured admission cap can be much larger than physical per-minute
    # throughput.  Rotate after the last completed code even when the whole
    # universe fits under that configured cap, otherwise the time-budgeted
    # evaluator retries the same prefix forever and starves the tail.
    batch = _take_rotating_priority_batch(
        ("A", "B", "C", "D"),
        previous_codes=("A", "B"),
        max_symbols=512,
    )

    assert batch == ("C", "D", "A", "B")


def test_priority_batch_keeps_new_urgent_signal_ahead() -> None:
    batch = _take_rotating_priority_batch(
        ("EXECUTABLE_NEW", "EXECUTABLE_OLD", "TRIGGERED_OLD"),
        previous_codes=("EXECUTABLE_OLD",),
        max_symbols=512,
    )

    assert batch == ("EXECUTABLE_NEW", "TRIGGERED_OLD", "EXECUTABLE_OLD")


def test_priority_monitor_failure_does_not_fail_frozen_coverage_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    symbols = ("SZ.000001", "SZ.000002", "SZ.000003", "SZ.000004")
    observed_at = [AS_OF.replace(hour=14, minute=58)]

    catalog = MultiMemberSectorCatalog(symbols)
    market = RecordingMarketData()
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=catalog,
        engine=RecordingEngine(),
        scan_planner=SequencedPlanner((symbols,)),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: observed_at[0],
        notifier=None,
        config=TradingScreeningConfig(
            max_symbols_per_refresh=1,
            priority_monitoring_enabled=True,
        ),
    )

    service.refresh_now()

    def fail_priority_monitor(**_kwargs) -> None:
        raise RuntimeError("fresh priority stock read failed")

    monkeypatch.setattr(service, "_run_priority_monitor", fail_priority_monitor)
    observed_at[0] += timedelta(minutes=1)
    second = service.refresh_now()

    assert second["scan_state"] == "in_progress"
    assert second["full_coverage_state"] == "in_progress"
    assert second["coverage_manifest"]["completed_codes"] == list(symbols[:2])
    assert all(
        row.get("error_type") != "priority_monitor_error" for row in second["errors"]
    )
    health = service.health_snapshot()
    assert health["priority_monitor_ready"] is False
    assert health["priority_monitor_status"] == "degraded"
    assert health["priority_monitor_last_error_count"] == 1
    assert health["priority_monitor_last_failure_reason_counts"] == {
        "PRIORITY_MONITOR_FAILED": 1
    }


def test_closed_session_retains_monitor_errors_without_reporting_current_outage(
    tmp_path: Path,
) -> None:
    closed_at = AS_OF.replace(year=2026, month=8, day=16, hour=10, minute=0)
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=MultiMemberSectorCatalog(("SZ.000001",)),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: closed_at,
        notifier=None,
        config=TradingScreeningConfig(priority_monitoring_enabled=True),
    )
    with service._background_lock:
        service._priority_monitor_last_errors = (
            {
                "reason_code": "PRIORITY_MONITOR_TIME_BUDGET_EXHAUSTED",
                "reason": "previous open-session run",
            },
        )
        service._candidate_monitor_last_errors = (
            {
                "reason_code": "CANDIDATE_MONITOR_TIME_BUDGET_EXHAUSTED",
                "reason": "previous open-session run",
            },
        )

    health = service.health_snapshot()

    assert health["priority_monitor_session_open"] is False
    assert health["priority_monitor_status"] == "not_due"
    assert health["priority_monitor_ready"] is True
    assert health["priority_monitor_last_error_count"] == 1
    assert health["candidate_monitor_status"] == "not_due"
    assert health["candidate_monitor_ready"] is True
    assert health["candidate_monitor_last_error_count"] == 1


def test_priority_monitor_notification_is_early_and_idempotent(
    tmp_path: Path,
) -> None:
    symbols = ("SZ.000001", "SZ.000002", "SZ.000003", "SZ.000004")
    observed_at = [AS_OF.replace(hour=14, minute=58)]

    class WatchlistMarket(ActionableMarketData):
        def active_watchlist(self) -> tuple[str, ...]:
            return (symbols[0],)

        def structure_bundle_with_risk_cutoff(
            self,
            code: str,
            *,
            as_of: datetime,
            sector,
            frequencies=(),
            risk_evidence_cutoff: datetime,
        ) -> SymbolStructureBundle:
            del frequencies
            self.bundle_codes.append(code)
            if code != symbols[0]:
                return SymbolStructureBundle(
                    code=code,
                    as_of=as_of,
                    sector=sector,
                    thirty_direction="neutral",
                    thirty_points=(),
                    five_points=(),
                    one_points=(),
                    opposite_points=(),
                    physical_timeframe_recursive=True,
                )
            setup = _current_terminal_point(
                confirmed_point("2buy", code=code, minutes_after=295)
            )
            trigger = (
                None
                if as_of.minute == 58
                else _current_terminal_point(
                    confirmed_point(
                        "1buy",
                        code=code,
                        frequency="1m",
                        minutes_after=294,
                        available_minutes_after=5,
                    ),
                    terminal_minutes=1,
                )
            )

            def green(subject: str) -> HigherTimeframeGateEvidence:
                identity = sha256_json(
                    {
                        "schema": "fresh-priority-green-gate",
                        "subject": subject,
                        "observed_at": risk_evidence_cutoff.isoformat(),
                    }
                )
                return HigherTimeframeGateEvidence(
                    subject=subject,
                    observed_at=risk_evidence_cutoff,
                    monthly="NONE",
                    weekly="NONE",
                    daily="NONE",
                    gate="GREEN",
                    grade="RESEARCH_ONLY",
                    snapshot_id=identity,
                    source_revision=identity,
                )

            boundaries = (
                ()
                if trigger is None
                else (
                    EntryExecutionBoundary(
                        symbol=code,
                        setup_occurrence_id=structural_point_occurrence_id(setup),
                        point_id=trigger.point_id,
                        source_frequency="1m",
                        confirmation_bar_closed_at=trigger.available_at,
                        raw_open=Decimal("9.95"),
                        raw_high=Decimal("10.05"),
                        raw_low=Decimal("9.90"),
                        raw_close=Decimal("10.00"),
                        raw_volume=Decimal("10000"),
                        entry_valid_until=a_share_optional_entry_valid_until(
                            trigger.available_at
                        ),
                        raw_price_basis_revision="test-raw",
                    ),
                )
            )
            return SymbolStructureBundle(
                code=code,
                as_of=as_of,
                sector=sector,
                thirty_direction="neutral",
                thirty_points=(),
                five_points=(setup,),
                one_points=(() if trigger is None else (trigger,)),
                opposite_points=(),
                higher_timeframe_gates=HigherTimeframeGateBundle(
                    market=green("SH.000300"),
                    sector=green(sector.sector_id),
                    symbol=green(code),
                ),
                enforce_higher_timeframe_entry_gate=True,
                warmup_converged=True,
                enforce_warmup_entry_gate=True,
                physical_timeframe_recursive=True,
                entry_execution_boundaries=boundaries,
            )

    class RecordingSender:
        def __init__(self) -> None:
            self.messages: list[tuple[str, list[str]]] = []

        def send(self, title, lines) -> bool:
            self.messages.append((title, list(lines)))
            return True

    sender = RecordingSender()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "notification_state.json",
        clock=lambda: observed_at[0],
    )
    supportive_sector = replace(eligible_sector(), regime="supportive")
    sector_catalog = RecordingSectorCatalog(
        SectorAssessmentBatch(
            assessments=(supportive_sector,),
            discovered_count=1,
            completed_count=1,
            failure_counts=(),
            errors=(),
        )
    )
    service = TradingScreeningService(
        market_data=WatchlistMarket(),
        sector_catalog=sector_catalog,
        engine=HumanAssistedDecisionCore(),
        scan_planner=SequencedPlanner((symbols,)),
        cache_path=tmp_path / "snapshot.json",
        selection_research=(valid_selection_research(),),
        clock=lambda: observed_at[0],
        notifier=dispatcher,
        config=TradingScreeningConfig(
            max_symbols_per_refresh=1,
            priority_monitoring_enabled=True,
            max_five_minute_candidate_symbols_per_refresh=1,
            max_thirty_minute_candidate_symbols_per_refresh=1,
            priority_monitor_interval_seconds=60,
        ),
    )

    first = service.refresh_now()
    assert first["signals"], first["errors"]
    assert first["signals"][0]["lifecycle_stage"] == "triggered"
    # The first pass is still a frozen full-universe coverage epoch.  It is
    # persisted for the daily shortlist, but may not masquerade as a current
    # minute warning.
    assert len(sender.messages) == 0

    observed_at[0] += timedelta(minutes=1)
    second = service.refresh_now()
    assert second["coverage_manifest"]["complete"] is False
    assert len(sender.messages) == 1, (
        dispatcher.health_snapshot(),
        service._priority_monitor_latest_documents,
    )
    presentation = service.presentation_snapshot()
    archive = service.snapshot()
    assert presentation["priority_live_overlay"]["live"] is True
    assert presentation["priority_live_overlay"]["signal_count"] >= 1
    assert (
        presentation["priority_live_overlay"]["notification_dispatcher_configured"]
        is True
    )
    assert any(
        row.get("observation_lane") == "PRIORITY_CURRENT_1M"
        and row.get("realtime_observation") is True
        for row in presentation["signals"]
    )
    assert all("observation_lane" not in row for row in archive["signals"])
    health = service.health_snapshot()
    assert health["notification_dispatcher_configured"] is True
    assert health["notification_operationally_verified"] is True
    assert health["notification_delivered_event_count"] == 1
    assert health["notification_delivery"]["status"] == "verified"
    assert health["realtime_alert_ready"] is True
    assert health["realtime_alert_status"] == "ready"

    observed_at[0] += timedelta(minutes=1)
    service.refresh_now()
    assert len(sender.messages) == 1


def test_fresh_five_minute_candidate_is_notification_authoritative(
    tmp_path: Path,
) -> None:
    class RecordingNotifier:
        def __init__(self) -> None:
            self.calls: list[tuple[dict[str, object], dict[str, object]]] = []

        def dispatch_changes(self, previous, current) -> None:
            self.calls.append((dict(previous), dict(current)))

    notifier = RecordingNotifier()
    service = TradingScreeningService(
        market_data=ActionableMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=HumanAssistedDecisionCore(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=notifier,
        config=TradingScreeningConfig(priority_monitoring_enabled=True),
    )
    previous = {
        "signals": [
            {
                "signal_id": "formed-buy",
                "code": "SZ.000001",
                "point_type": "2buy",
                "lifecycle_stage": "formed",
            }
        ]
    }

    service._run_priority_monitor(previous=previous, observed_at=AS_OF)

    assert len(notifier.calls) == 1
    _previous_notification, current_notification = notifier.calls[0]
    assert [row["code"] for row in current_notification["signals"]] == ["SZ.000001"]
    assert current_notification["notification_authoritative_codes"] == ["SZ.000001"]
    [overlay] = service.presentation_snapshot()["signals"]
    assert overlay["observation_lane"] == "CANDIDATE_CURRENT_5M"
    assert overlay["realtime_observation"] is False
    candidate_overlay = service.presentation_snapshot()["candidate_live_overlay"]
    assert candidate_overlay["realtime_notification_authorized"] is False
    assert candidate_overlay["fresh_five_minute_notification_authorized"] is True


def test_priority_notification_is_admitted_before_monitor_checkpoint(
    tmp_path: Path,
) -> None:
    order: list[str] = []

    class OrderingNotifier:
        def dispatch_changes(self, _previous, _current) -> None:
            order.append("dispatch")

    service = TradingScreeningService(
        market_data=ActionableMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=HumanAssistedDecisionCore(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=OrderingNotifier(),
        config=TradingScreeningConfig(priority_monitoring_enabled=True),
    )
    service._persist_priority_monitor_state = lambda: order.append("persist")

    service._run_priority_monitor(
        previous={
            "signals": [
                {
                    "signal_id": "formed-buy",
                    "code": "SZ.000001",
                    "point_type": "2buy",
                    "lifecycle_stage": "formed",
                }
            ]
        },
        observed_at=AS_OF,
    )

    assert "dispatch" in order
    assert "persist" in order
    assert order.index("dispatch") < order.index("persist")


def test_supportive_sector_discovery_runs_on_five_minute_cadence_and_notifies(
    tmp_path: Path,
) -> None:
    code = "SZ.000001"

    class FrequencyRecordingMarket(ActionableMarketData):
        def structure_bundle(
            self,
            code: str,
            *,
            as_of: datetime,
            sector,
            frequencies=(),
        ) -> SymbolStructureBundle:
            self.bundle_frequency_requests.append((code, tuple(frequencies)))
            return super().structure_bundle(
                code,
                as_of=as_of,
                sector=sector,
                frequencies=frequencies,
            )

    class RecordingNotifier:
        def __init__(self) -> None:
            self.calls: list[tuple[dict[str, object], dict[str, object]]] = []

        def dispatch_changes(self, previous, current) -> None:
            self.calls.append((dict(previous), dict(current)))

    catalog = MultiMemberSectorCatalog((code,))
    catalog.batch = SectorAssessmentBatch(
        assessments=(replace(eligible_sector(), regime="supportive"),),
        discovered_count=1,
        completed_count=1,
        failure_counts=(),
        errors=(),
    )
    market = FrequencyRecordingMarket()
    notifier = RecordingNotifier()
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=catalog,
        engine=HumanAssistedDecisionCore(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=notifier,
        config=TradingScreeningConfig(priority_monitoring_enabled=True),
    )
    # Make the slower discovery lane current. The symbol must still be admitted
    # by the independent 5m lane even though it was not present in any previous
    # signal document.
    service._candidate_monitor_thirty_last_success_at[code] = AS_OF

    service._run_priority_monitor(previous=service.snapshot(), observed_at=AS_OF)

    assert market.bundle_frequency_requests == [(code, ("5m",))]
    assert len(notifier.calls) == 1
    assert notifier.calls[0][1]["notification_authoritative_codes"] == [code]
    health = service.health_snapshot()
    assert health["candidate_monitor_five_minute"]["universe_count"] == 1
    assert health["candidate_monitor_five_minute"]["scope"] == (
        "OWNED_WATCHED_EXISTING_AND_SUPPORTIVE_SECTOR_DISCOVERY"
    )
    assert "candidate_monitor_notification_freshness_seconds" not in health
    assert "candidate_monitor_notification_headroom_seconds" not in health
    assert "candidate_monitor_initial_notification_headroom_seconds" not in health
    assert health["candidate_notification_streaming_enabled"] is True
    assert health["candidate_notification_publish_batch_size"] == 4


def test_supportive_candidate_notifications_publish_before_a_slow_tail_finishes(
    tmp_path: Path,
) -> None:
    symbols = tuple(f"SZ.{index:06d}" for index in range(1, 6))
    tail_started = threading.Event()
    release_tail = threading.Event()
    notification_published = threading.Event()

    class SlowTailMarket(ActionableMarketData):
        def __init__(self) -> None:
            super().__init__()
            self._call_lock = threading.Lock()
            self._call_count = 0

        def structure_bundle(self, code: str, **kwargs) -> SymbolStructureBundle:
            with self._call_lock:
                self._call_count += 1
                call_number = self._call_count
            if call_number == len(symbols):
                tail_started.set()
                assert release_tail.wait(timeout=5)
            return super().structure_bundle(code, **kwargs)

    class RecordingNotifier:
        def __init__(self) -> None:
            self.calls: list[tuple[dict[str, object], dict[str, object]]] = []
            self._lock = threading.Lock()

        def dispatch_changes(self, previous, current) -> None:
            with self._lock:
                self.calls.append((dict(previous), dict(current)))
            notification_published.set()

    catalog = MultiMemberSectorCatalog(symbols)
    catalog.batch = SectorAssessmentBatch(
        assessments=(replace(eligible_sector(), regime="supportive"),),
        discovered_count=1,
        completed_count=1,
        failure_counts=(),
        errors=(),
    )
    notifier = RecordingNotifier()
    service = TradingScreeningService(
        market_data=SlowTailMarket(),
        sector_catalog=catalog,
        engine=HumanAssistedDecisionCore(formal_selection_required=False),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=notifier,
        config=TradingScreeningConfig(
            priority_monitoring_enabled=True,
            stock_worker_count=3,
        ),
    )
    for code in symbols:
        service._candidate_monitor_thirty_last_success_at[code] = AS_OF
    service._priority_monitor_last_at = AS_OF - timedelta(minutes=5)

    worker = threading.Thread(
        target=service._run_priority_monitor,
        kwargs={"previous": service.snapshot(), "observed_at": AS_OF},
        daemon=True,
    )
    worker.start()
    try:
        assert tail_started.wait(timeout=3)
        assert notification_published.wait(timeout=1)
    finally:
        release_tail.set()
        worker.join(timeout=5)

    assert not worker.is_alive()
    assert len(notifier.calls) == 2
    assert set(notifier.calls[0][1]["notification_authoritative_codes"]).issubset(
        set(symbols)
    )
    published_codes = [
        code
        for _previous, current in notifier.calls
        for code in current["notification_authoritative_codes"]
    ]
    assert sorted(published_codes) == sorted(symbols)
    assert len(published_codes) == len(set(published_codes))


def test_failed_streaming_candidate_handoff_retries_at_round_end(
    tmp_path: Path,
) -> None:
    symbols = tuple(f"SZ.{index:06d}" for index in range(1, 5))

    class FlakyNotifier:
        def __init__(self) -> None:
            self.calls: list[tuple[dict[str, object], dict[str, object]]] = []

        def dispatch_changes(self, previous, current) -> None:
            self.calls.append((dict(previous), dict(current)))
            if len(self.calls) == 1:
                raise RuntimeError("injected streaming handoff failure")

    catalog = MultiMemberSectorCatalog(symbols)
    catalog.batch = SectorAssessmentBatch(
        assessments=(replace(eligible_sector(), regime="supportive"),),
        discovered_count=1,
        completed_count=1,
        failure_counts=(),
        errors=(),
    )
    notifier = FlakyNotifier()
    service = TradingScreeningService(
        market_data=ActionableMarketData(),
        sector_catalog=catalog,
        engine=HumanAssistedDecisionCore(formal_selection_required=False),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=notifier,
        config=TradingScreeningConfig(
            priority_monitoring_enabled=True,
            stock_worker_count=3,
        ),
    )
    for code in symbols:
        service._candidate_monitor_thirty_last_success_at[code] = AS_OF
    service._priority_monitor_last_at = AS_OF - timedelta(minutes=5)

    service._run_priority_monitor(previous=service.snapshot(), observed_at=AS_OF)

    assert len(notifier.calls) == 2
    assert set(notifier.calls[0][1]["notification_authoritative_codes"]) == set(symbols)
    assert set(notifier.calls[1][1]["notification_authoritative_codes"]) == set(symbols)


def test_fresh_five_minute_candidate_notifies_without_one_minute_segment(
    tmp_path: Path,
) -> None:
    class FreshFiveMinuteOnlyMarket(ActionableMarketData):
        def structure_bundle(self, code: str, **kwargs) -> SymbolStructureBundle:
            bundle = super().structure_bundle(code, **kwargs)
            return replace(
                bundle,
                five_points=(
                    _current_terminal_point(
                        confirmed_point("2buy", code=code, minutes_after=295)
                    ),
                ),
                one_points=(),
                physical_timeframe_recursive=True,
            )

    class RecordingSender:
        def __init__(self) -> None:
            self.messages: list[tuple[str, list[str]]] = []

        def send(self, title: str, lines: list[str]) -> bool:
            self.messages.append((title, lines))
            return True

    sender = RecordingSender()
    dispatcher = SignalNotificationDispatcher(
        sender,
        state_path=tmp_path / "notification_state.json",
        clock=lambda: AS_OF,
    )
    service = TradingScreeningService(
        market_data=FreshFiveMinuteOnlyMarket(),
        sector_catalog=RecordingSectorCatalog(),
        engine=HumanAssistedDecisionCore(formal_selection_required=False),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=dispatcher,
        config=TradingScreeningConfig(priority_monitoring_enabled=True),
    )

    service._run_priority_monitor(
        previous={
            "signals": [
                {
                    "signal_id": "formed-buy",
                    "code": "SZ.000001",
                    "point_type": "2buy",
                    "lifecycle_stage": "formed",
                }
            ]
        },
        observed_at=AS_OF,
    )

    assert len(sender.messages) == 1, dispatcher.health_snapshot()
    title, lines = sender.messages[0]
    assert "5分钟二类买点" in title
    assert "段差已定位" not in title
    assert "1分钟区间套：暂未出现（5分钟信号保留，精确执行尚未解锁）" in "\n".join(
        lines
    )


def test_partial_coverage_epoch_resumes_after_service_restart(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "snapshot.json"
    first_market = RecordingMarketData()
    first_planner = RecordingPlanner(("SZ.000001", "SZ.000002", "SZ.000003"))
    first_service = TradingScreeningService(
        market_data=first_market,
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=first_planner,
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(max_symbols_per_refresh=1),
    )
    first = first_service.refresh_now()
    assert first["coverage_manifest"]["complete"] is False
    assert set(first["coverage_manifest"]["pending_frequencies"]) == {
        "SZ.000002",
        "SZ.000003",
    }

    second_market = RecordingMarketData()
    second_planner = RecordingPlanner(("SHOULD.NOT.REPLAN",))
    second_service = TradingScreeningService(
        market_data=second_market,
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=second_planner,
        cache_path=cache_path,
        clock=lambda: AS_OF + timedelta(minutes=10),
        notifier=None,
        config=TradingScreeningConfig(max_symbols_per_refresh=2),
    )
    second = second_service.refresh_now()

    assert second_planner.calls == 0
    assert second_market.bundle_codes == ["SZ.000002", "SZ.000003"]
    assert second["coverage_epoch_id"] == first["coverage_epoch_id"]
    assert second["coverage_manifest"]["complete"] is True
    assert second["coverage_manifest"]["pending_frequencies"] == {}
    assert second["coverage_manifest"]["completed_codes"] == [
        "SZ.000001",
        "SZ.000002",
        "SZ.000003",
    ]


def test_partial_epoch_restores_exact_sector_batch_across_restart(
    tmp_path: Path,
) -> None:
    """One coverage epoch may not mix sector point identities after restart."""

    cache_path = tmp_path / "snapshot.json"
    symbols = ("SZ.000001", "SZ.000002")
    first_batch = _evidence_sector_batch(symbols, context_revision="first")
    first_service = TradingScreeningService(
        market_data=ActionableMarketData(),
        sector_catalog=EvidenceSectorCatalog(first_batch, symbols),
        engine=HumanAssistedDecisionCore(),
        scan_planner=SequencedPlanner((symbols,)),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(max_symbols_per_refresh=1),
    )

    first = first_service.refresh_now()

    assert first["coverage_manifest"]["complete"] is False
    assert first["signals"]
    first_point_id = first["sectors"][0]["context_5m"]["dominant_point_id"]
    changed_batch = _evidence_sector_batch(symbols, context_revision="second")
    changed_catalog = HydratingEvidenceSectorCatalog(changed_batch, symbols)
    restarted_market = RecordingMarketData()
    restarted = TradingScreeningService(
        market_data=restarted_market,
        sector_catalog=changed_catalog,
        engine=HumanAssistedDecisionCore(),
        scan_planner=RecordingPlanner(("SHOULD.NOT.REPLAN",)),
        cache_path=cache_path,
        clock=lambda: AS_OF + timedelta(minutes=10),
        notifier=None,
        config=TradingScreeningConfig(max_symbols_per_refresh=1),
    )

    completed = restarted.refresh_now()

    assert changed_catalog.assessment_calls == []
    assert changed_catalog.restore_calls == [(AS_OF, first_batch.catalog_revision)]
    assert changed_catalog.member_calls == 1
    assert restarted_market.bundle_codes == ["SZ.000002"]
    assert completed["coverage_manifest"]["complete"] is True
    assert completed["sectors"][0]["context_5m"]["dominant_point_id"] == (
        first_point_id
    )
    assert all(
        signal["sector"]["context_5m"]["dominant_point_id"] == first_point_id
        for signal in completed["signals"]
    )


def test_runtime_retry_retains_last_success_and_restores_frozen_sector_state(
    tmp_path: Path,
) -> None:
    """有认证运行故障时可保留最近成功证据，但重启不得重算板块。"""

    class NativeScreeningWorkerUnavailable(RuntimeError):
        pass

    cache_path = tmp_path / "snapshot.json"
    symbols = ("SZ.000001", "SZ.000002")
    batch = _evidence_sector_batch(symbols, context_revision="runtime-retry")
    market = RecordingMarketData()
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=EvidenceSectorCatalog(batch, symbols),
        engine=RecordingEngine(),
        scan_planner=SequencedPlanner((symbols,)),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(max_symbols_per_refresh=1),
    )
    first = service.refresh_now()
    retained_code = symbols[0]
    assert first["coverage_manifest"]["completed_codes"] == [retained_code]

    original = market.structure_bundle

    def fail_retained_code(code, **kwargs):
        if code == retained_code:
            market.bundle_codes.append(code)
            raise NativeScreeningWorkerUnavailable("worker restarted")
        return original(code, **kwargs)

    market.structure_bundle = fail_retained_code
    service._pending_frequencies[retained_code] = set(
        trading_screening_subject.SCREENING_STRUCTURE_FREQUENCIES
    )
    retried = service.refresh_now()
    manifest = retried["coverage_manifest"]

    assert retained_code in manifest["completed_codes"]
    assert retained_code in manifest["failed_codes"]
    assert retained_code in manifest["backoff_frequencies"]
    assert _cache_is_valid(
        retried,
        service._config,
        service._decision_core_id,
        service._selection_research_revision,
    )

    restarted_catalog = HydratingEvidenceSectorCatalog(batch, symbols)
    restarted = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=restarted_catalog,
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(("SHOULD.NOT.REPLAN",)),
        cache_path=cache_path,
        clock=lambda: AS_OF + timedelta(minutes=1),
        notifier=None,
        config=TradingScreeningConfig(max_symbols_per_refresh=1),
    )

    assert restarted._coverage_cycle_sector_restored is True
    assert restarted._coverage_cycle_sector_batch is not None
    assert restarted._coverage_cycle_sector_members == {
        eligible_sector().sector_id: symbols
    }
    assert restarted_catalog.assessment_calls == []


def test_minimum_history_exclusion_retracts_previous_success_and_signal(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "snapshot.json"
    symbols = ("SZ.000001",)
    batch = _evidence_sector_batch(symbols, context_revision="history-exclusion")
    market = ActionableMarketData()
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=EvidenceSectorCatalog(batch, symbols),
        engine=HumanAssistedDecisionCore(),
        scan_planner=SequencedPlanner((symbols,)),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(max_symbols_per_refresh=1),
    )
    first = service.refresh_now()
    failed_code = symbols[0]
    assert any(row["code"] == failed_code for row in first["signals"])

    original = market.structure_bundle

    def reject_history(code, **kwargs):
        if code == failed_code:
            market.bundle_codes.append(code)
            raise ValueError("kline frame does not meet minimum history")
        return original(code, **kwargs)

    market.structure_bundle = reject_history
    service._pending_frequencies[failed_code] = set(
        trading_screening_subject.SCREENING_STRUCTURE_FREQUENCIES
    )
    failed = service.refresh_now()
    manifest = failed["coverage_manifest"]

    assert failed_code not in manifest["completed_codes"]
    assert failed_code not in manifest["failed_codes"]
    assert failed_code in manifest["excluded_codes"]
    assert failed_code in manifest["deferred_frequencies"]
    assert all(row["code"] != failed_code for row in failed["signals"])
    assert _cache_is_valid(
        failed,
        service._config,
        service._decision_core_id,
        service._selection_research_revision,
    )


def test_complete_epoch_keeps_full_coverage_during_post_restart_monitoring(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "snapshot.json"
    first_service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(("SZ.000001", "SZ.000002", "SZ.000003")),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
    )
    first = first_service.refresh_now()
    assert first["coverage_manifest"]["completed_codes"] == [
        "SZ.000001",
        "SZ.000002",
        "SZ.000003",
    ]

    second_market = RecordingMarketData()
    second_planner = RecordingPlanner(("SZ.000001",))
    second_service = TradingScreeningService(
        market_data=second_market,
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=second_planner,
        cache_path=cache_path,
        clock=lambda: AS_OF + timedelta(minutes=10),
        notifier=None,
    )
    second = second_service.refresh_now()

    assert second_planner.calls == 1
    assert second_market.bundle_codes == ["SZ.000001"]
    assert second["coverage_epoch_id"] == first["coverage_epoch_id"]
    assert second["coverage_manifest"]["complete"] is True
    assert second["scan_audit"]["coverage_cycle_complete"] is True
    assert second["coverage_manifest"]["completed_codes"] == [
        "SZ.000001",
        "SZ.000002",
        "SZ.000003",
    ]


def test_same_epoch_monitoring_never_reopens_completed_coverage_queue(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "snapshot.json"
    symbols = ("SZ.000001", "SZ.000002", "SZ.000003")
    first_service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(symbols),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
    )
    first = first_service.refresh_now()
    assert first["coverage_manifest"]["complete"] is True

    monitor_market = RecordingMarketData()
    monitor_service = TradingScreeningService(
        market_data=monitor_market,
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(symbols),
        cache_path=cache_path,
        clock=lambda: AS_OF + timedelta(minutes=10),
        notifier=None,
        config=TradingScreeningConfig(
            max_symbols_per_refresh=1,
            max_monitor_symbols_per_refresh=1,
        ),
    )

    monitored = monitor_service.refresh_now()

    assert monitor_market.bundle_codes == ["SZ.000001"]
    assert monitored["coverage_epoch_id"] == first["coverage_epoch_id"]
    assert monitored["coverage_manifest"]["complete"] is True
    assert monitored["coverage_manifest"]["pending_frequencies"] == {}
    assert monitored["scan_audit"]["coverage_cycle_complete"] is True


def test_same_epoch_monitoring_with_changed_sector_identity_keeps_publication_immutable(
    tmp_path: Path,
) -> None:
    """A current sector probe may not be mixed into the frozen daily page."""

    cache_path = tmp_path / "snapshot.json"
    symbols = ("SZ.000001",)
    first_batch = _evidence_sector_batch(symbols, context_revision="first")
    first_service = TradingScreeningService(
        market_data=ActionableMarketData(),
        sector_catalog=EvidenceSectorCatalog(first_batch, symbols),
        engine=HumanAssistedDecisionCore(),
        scan_planner=RecordingPlanner(symbols),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
    )
    first = first_service.refresh_now()
    first_point_id = first["sectors"][0]["context_5m"]["dominant_point_id"]
    assert first["signals"]
    assert all(
        row["sector"]["context_5m"]["dominant_point_id"] == first_point_id
        for row in first["signals"]
    )

    changed_batch = _evidence_sector_batch(symbols, context_revision="second")
    changed_catalog = HydratingEvidenceSectorCatalog(changed_batch, symbols)
    monitor_market = ActionableMarketData()
    preopen = AS_OF + timedelta(hours=17, minutes=50)
    monitor_service = TradingScreeningService(
        market_data=monitor_market,
        sector_catalog=changed_catalog,
        engine=HumanAssistedDecisionCore(),
        scan_planner=RecordingPlanner(symbols),
        cache_path=cache_path,
        clock=lambda: preopen,
        notifier=None,
    )

    monitored = monitor_service.refresh_now()

    assert changed_catalog.assessment_calls == [preopen]
    assert monitor_market.bundle_codes == ["SZ.000001"]
    assert monitored == first
    assert monitored["snapshot_content_sha256"] == first["snapshot_content_sha256"]
    assert monitored["sectors"][0]["context_5m"]["dominant_point_id"] == (
        first_point_id
    )
    assert json.loads(cache_path.read_text(encoding="utf-8")) == first


def test_same_epoch_monitoring_failure_keeps_valid_last_good_snapshot(
    tmp_path: Path,
) -> None:
    """A failed observation must not poison an already-complete epoch.

    Coverage errors attest the frozen full-universe pass.  A later monitor-only
    observation is operational work against the same market-data cutoff; if it
    fails, the previously verified signal remains the honest last-good result.
    Mixing that transient error into the coverage error ledger makes the
    manifest internally inconsistent and leaves the epoch invalid forever.
    """

    class FailingMonitorMarket(ActionableMarketData):
        def __init__(self) -> None:
            super().__init__()
            self.fail_monitor = False

        def structure_bundle(self, code: str, **kwargs) -> SymbolStructureBundle:
            if self.fail_monitor:
                self.bundle_codes.append(code)
                raise RuntimeError("monitor-only transport failure")
            return super().structure_bundle(code, **kwargs)

    market = FailingMonitorMarket()
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=RecordingSectorCatalog(),
        engine=HumanAssistedDecisionCore(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
    )

    first = service.refresh_now()
    market.fail_monitor = True
    monitored = service.refresh_now()
    health = service.health_snapshot()

    assert first["coverage_manifest"]["complete"] is True
    assert monitored["coverage_manifest"] == first["coverage_manifest"]
    assert monitored["signals"] == first["signals"]
    assert monitored["errors"] == first["errors"] == []
    assert monitored["data_quality"] == first["data_quality"]
    assert monitored["snapshot_content_sha256"] == first["snapshot_content_sha256"]
    assert health["last_monitoring_failure_count"] == 1
    assert health["last_monitoring_failure_codes"] == ["SZ.000001"]
    assert health["last_monitoring_failure_reason_counts"] == {
        "STOCK_ANALYSIS_UNCLASSIFIED": 1
    }

    market.fail_monitor = False
    recovered = service.refresh_now()
    recovered_health = service.health_snapshot()
    assert recovered["coverage_manifest"] == first["coverage_manifest"]
    assert recovered["errors"] == []
    assert recovered_health["last_monitoring_failure_count"] == 0
    assert recovered_health["last_monitoring_failure_codes"] == []
    assert recovered_health["last_monitoring_failure_reason_counts"] == {}


def test_same_market_cutoff_and_universe_produce_idempotent_snapshot(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "snapshot.json"
    clock_value = [AS_OF]
    sectors = RecordingSectorCatalog()
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=sectors,
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=cache_path,
        clock=lambda: clock_value[0],
        notifier=None,
    )

    first = service.refresh_now()
    first_bytes = cache_path.read_bytes()
    # Still inside the same completed epoch and outside the deliberate
    # post-close sector probe window.
    clock_value[0] = AS_OF + timedelta(minutes=1)
    second = service.refresh_now()

    assert first["snapshot_content_sha256"] == second["snapshot_content_sha256"]
    assert first["coverage_epoch_id"] == second["coverage_epoch_id"]
    assert cache_path.read_bytes() == first_bytes
    assert sectors.assessment_calls == [AS_OF]


def test_full_refresh_plan_flag_is_not_part_of_snapshot_identity(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "snapshot.json"
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
    )

    first = service.refresh_now()
    first_bytes = cache_path.read_bytes()
    second = service.refresh_now()

    assert first["scan_audit"]["background_full_refresh_required"] is True
    assert second["snapshot_content_sha256"] == first["snapshot_content_sha256"]
    assert cache_path.read_bytes() == first_bytes


def test_pending_cycle_is_drained_without_replanning_active_symbols(
    tmp_path: Path,
) -> None:
    market = RecordingMarketData()
    market.active_watchlist = lambda: ("SZ.000001",)
    sectors = RecordingSectorCatalog()
    planner = RecordingPlanner(("SZ.000001", "SZ.000002", "SZ.000003"))
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=sectors,
        engine=RecordingEngine(),
        scan_planner=planner,
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(
            max_symbols_per_refresh=1,
            max_monitor_symbols_per_refresh=1,
        ),
    )

    first = service.refresh_now()
    second = service.refresh_now()

    assert planner.calls == 1
    assert sectors.assessment_calls == [AS_OF]
    assert sectors.member_calls == 1
    assert market.bundle_codes == ["SZ.000001", "SZ.000002", "SZ.000003"]
    assert first["scan_audit"]["pending_symbol_count"] == 1
    assert second["scan_audit"]["pending_symbol_count"] == 0
    assert second["scan_audit"]["coverage_cycle_complete"] is True
    assert second["scan_audit"]["coverage_cycle_batch_count"] == 2


def test_full_scope_uses_frozen_sector_rank_as_scan_order_only(
    tmp_path: Path,
) -> None:
    weak = replace(
        eligible_sector(),
        sector_id="qmt-gics3:weak",
        sector_name="弱板块",
        rank_components=(("structural", 5),),
    )
    strong = replace(
        eligible_sector(),
        sector_id="qmt-gics3:strong",
        sector_name="强板块",
        rank_components=(("structural", 80),),
    )

    class RankedCatalog(RecordingSectorCatalog):
        def __init__(self) -> None:
            self.assessment_calls = []
            self.member_calls = 0
            self.batch = SectorAssessmentBatch(
                assessments=(weak, strong),
                discovered_count=2,
                completed_count=2,
                failure_counts=(),
                errors=(),
            )

        def members(self):
            self.member_calls += 1
            # Lexicographic order would scan SH first.  The stronger SZ sector
            # must be visited first without removing SH from final coverage.
            return {
                weak.sector_id: ("SH.600001",),
                strong.sector_id: ("SZ.000999",),
            }

    def empty_plan(**_kwargs) -> ScanPlan:
        return ScanPlan(
            sectors=(),
            symbols=(),
            symbol_frequencies=(),
            full_market_history_scan=False,
            background_full_refresh_required=False,
        )

    market = RecordingMarketData()
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=RankedCatalog(),
        engine=RecordingEngine(),
        scan_planner=empty_plan,
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(max_symbols_per_refresh=1),
    )

    first = service.refresh_now()
    second = service.refresh_now()

    assert market.bundle_codes == ["SZ.000999", "SH.600001"]
    assert first["scan_audit"]["pending_symbol_count"] == 1
    assert first["scan_audit"]["coverage_cycle_progress_ratio"] == "0.5"
    assert first["scan_audit"]["coverage_cycle_finalized_symbol_count"] == 1
    assert first["scan_audit"]["coverage_cycle_runtime_finalized_symbol_count"] == 1
    assert first["scan_audit"]["coverage_cycle_throughput_symbols_per_minute"] > 0
    assert first["scan_audit"]["coverage_cycle_estimated_remaining_seconds"] >= 0
    assert second["scan_audit"]["coverage_cycle_progress_ratio"] == "1"
    assert second["scan_audit"]["coverage_cycle_finalized_symbol_count"] == 2
    assert second["scan_audit"]["coverage_cycle_estimated_remaining_seconds"] == 0


def test_coverage_throughput_excludes_one_time_sector_initialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakePerformanceClock:
        def __init__(self) -> None:
            self.value = 0.0

        def __call__(self) -> float:
            return self.value

        def advance(self, seconds: float) -> None:
            self.value += seconds

    performance_clock = FakePerformanceClock()
    monkeypatch.setattr(
        trading_screening_subject.time,
        "perf_counter",
        performance_clock,
    )

    class SlowInitialSectorCatalog(MultiMemberSectorCatalog):
        def native_sector_assessments(
            self,
            *,
            as_of: datetime,
            admitted_codes=None,
        ):
            performance_clock.advance(600)
            return super().native_sector_assessments(
                as_of=as_of,
                admitted_codes=admitted_codes,
            )

    class TimedMarketData(RecordingMarketData):
        def structure_bundle(self, code: str, **kwargs) -> SymbolStructureBundle:
            performance_clock.advance(2)
            return super().structure_bundle(code, **kwargs)

    service = TradingScreeningService(
        market_data=TimedMarketData(),
        sector_catalog=SlowInitialSectorCatalog(("SZ.000001", "SZ.000002")),
        engine=RecordingEngine(),
        scan_planner=lambda **_kwargs: ScanPlan(
            sectors=(),
            symbols=(),
            symbol_frequencies=(),
            full_market_history_scan=False,
            background_full_refresh_required=False,
        ),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(max_symbols_per_refresh=1),
    )

    first = service.refresh_now()
    audit = first["scan_audit"]

    assert audit["coverage_cycle_elapsed_ms"] == 602_000
    assert audit["coverage_cycle_runtime_stock_scan_elapsed_ms"] == 2_000
    assert audit["coverage_cycle_runtime_finalized_symbol_count"] == 1
    assert audit["coverage_cycle_throughput_symbols_per_minute"] == 30
    assert audit["coverage_cycle_estimated_remaining_seconds"] == 2


def test_preclose_pending_epoch_is_superseded_after_market_close(
    tmp_path: Path,
) -> None:
    preclose = AS_OF.replace(hour=14, minute=35)
    postclose = AS_OF.replace(hour=16)

    def sector_at(observed_at: datetime) -> SectorAssessmentBatch:
        return SectorAssessmentBatch(
            assessments=(
                replace(
                    eligible_sector(),
                    thirty_context=replace(
                        neutral_context("30m"),
                        observed_at=observed_at,
                    ),
                ),
            ),
            discovered_count=1,
            completed_count=1,
            failure_counts=(),
            errors=(),
        )

    clock_value = [preclose]
    sectors = RecordingSectorCatalog(sector_at(preclose))
    planner = RecordingPlanner(("SZ.000001", "SZ.000002"))
    market = RecordingMarketData()
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=sectors,
        engine=RecordingEngine(),
        scan_planner=planner,
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: clock_value[0],
        notifier=None,
        config=TradingScreeningConfig(max_symbols_per_refresh=1),
    )

    first = service.refresh_now()
    first_epoch = first["coverage_epoch_id"]
    assert first["as_of"] == preclose.isoformat()
    assert first["scan_audit"]["pending_symbol_count"] == 1

    clock_value[0] = postclose
    sectors.batch = sector_at(AS_OF)
    second = service.refresh_now()

    assert planner.calls == 2
    assert sectors.assessment_calls == [preclose, postclose]
    assert market.bundle_codes == ["SZ.000001", "SZ.000001"]
    assert second["coverage_epoch_id"] != first_epoch
    assert second["as_of"] == AS_OF.isoformat()
    assert second["scan_audit"]["pending_symbol_count"] == 1
    assert second["scan_audit"]["preclose_epoch_superseded"] is True
    assert second["scan_audit"]["superseded_coverage_epoch_id"] == first_epoch
    assert second["scan_audit"]["superseded_market_data_as_of"] == preclose.isoformat()
    assert second["coverage_manifest"]["superseded_coverage_epoch_id"] == first_epoch

    final = service.refresh_now()
    assert final["scan_audit"]["coverage_cycle_complete"] is True
    assert final["scan_audit"]["preclose_epoch_superseded"] is True
    assert final["scan_audit"]["superseded_coverage_epoch_id"] == first_epoch
    assert (
        final["coverage_manifest"]["superseded_market_data_as_of"]
        == preclose.isoformat()
    )


def test_preclose_pending_epoch_is_preserved_until_qmt_reaches_close(
    tmp_path: Path,
) -> None:
    preclose = AS_OF.replace(hour=14, minute=35)
    clock_value = [preclose]
    sector = replace(
        eligible_sector(),
        thirty_context=replace(
            neutral_context("30m"),
            observed_at=preclose,
        ),
    )
    sectors = RecordingSectorCatalog(
        SectorAssessmentBatch(
            assessments=(sector,),
            discovered_count=1,
            completed_count=1,
            failure_counts=(),
            errors=(),
        )
    )
    planner = RecordingPlanner(("SZ.000001", "SZ.000002"))
    market = RecordingMarketData()
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=sectors,
        engine=RecordingEngine(),
        scan_planner=planner,
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: clock_value[0],
        notifier=None,
        config=TradingScreeningConfig(max_symbols_per_refresh=1),
    )

    first = service.refresh_now()
    clock_value[0] = AS_OF.replace(hour=16)
    blocked = service.refresh_now()

    assert planner.calls == 1
    assert market.bundle_codes == ["SZ.000001"]
    assert blocked["coverage_epoch_id"] == first["coverage_epoch_id"]
    assert blocked["coverage_manifest"] == first["coverage_manifest"]
    assert blocked["scan_state"] == "postclose_market_data_incomplete"
    assert blocked["scan_audit"]["pending_symbol_count"] == 1
    assert blocked["scan_audit"]["postclose_market_data_refresh_required"] is True
    assert blocked["data_quality"]["failure_codes"] == [
        "postclose_market_data_incomplete"
    ]


def test_failed_symbol_is_deferred_instead_of_spinning_current_cycle(
    tmp_path: Path,
) -> None:
    market = RecordingMarketData()
    original = market.structure_bundle

    def structure_bundle(code, **kwargs):
        if code == "SZ.000002":
            market.bundle_codes.append(code)
            raise ValueError("permanent structure failure")
        return original(code, **kwargs)

    market.structure_bundle = structure_bundle
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(("SZ.000001", "SZ.000002")),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(min_scan_completion_ratio=Decimal("0.5")),
    )

    payload = service.refresh_now()

    assert payload["scan_state"] == "complete"
    assert payload["scan_audit"]["pending_symbol_count"] == 0
    assert payload["scan_audit"]["retry_symbol_count"] == 1
    assert payload["scan_audit"]["coverage_cycle_complete"] is True
    assert payload["scan_audit"]["coverage_cycle_failed_symbol_count"] == 1
    assert payload["data_quality"]["failure_codes"] == ["stock_scan_partial"]
    assert payload["scan_audit"]["batch_duration_ms"] >= 0
    assert payload["scan_audit"]["coverage_cycle_elapsed_ms"] >= 0


def test_background_worker_drains_pending_without_page_requests(
    tmp_path: Path,
) -> None:
    background_as_of = AS_OF + timedelta(minutes=5)
    market = RecordingMarketData()
    all_symbols_visited = threading.Event()
    original_structure_bundle = market.structure_bundle

    def recording_structure_bundle(*args, **kwargs):
        bundle = original_structure_bundle(*args, **kwargs)
        if len(market.bundle_codes) >= 3:
            all_symbols_visited.set()
        return bundle

    market.structure_bundle = recording_structure_bundle
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=SequencedPlanner(
            (("SZ.000001", "SZ.000002", "SZ.000003"), (), ())
        ),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: background_as_of,
        notifier=None,
        config=TradingScreeningConfig(
            refresh_interval_seconds=3600,
            max_symbols_per_refresh=1,
            full_coverage_refresh_enabled=True,
            large_scope_authorized=True,
        ),
    )

    worker = service.start_background()
    try:
        assert all_symbols_visited.wait(timeout=1.0)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if service.snapshot()["scan_audit"]["pending_symbol_count"] == 0:
                break
            time.sleep(0.01)

        assert market.bundle_codes == ["SZ.000001", "SZ.000002", "SZ.000003"]
        assert service.snapshot()["scan_audit"]["pending_symbol_count"] == 0
        assert service.snapshot()["scan_audit"]["coverage_cycle_complete"] is True
        assert service.start_background() is worker
    finally:
        assert service.shutdown_background(wait=True, timeout=1.0) is True
    assert worker.is_alive() is False


def test_background_idles_after_one_process_refresh_of_complete_close_snapshot(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """夜间保留收盘完整快照，但新 Web 进程仍必须先成功复核一次。"""

    now = [AS_OF]
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(("SZ.000001",)),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: now[0],
        notifier=None,
        config=TradingScreeningConfig(
            refresh_interval_seconds=60,
            priority_monitoring_enabled=True,
            full_coverage_refresh_enabled=True,
            large_scope_authorized=True,
        ),
    )
    closed = service.refresh_now()
    assert closed["coverage_manifest"]["complete"] is True
    assert closed["market_data_as_of"] == AS_OF.isoformat()
    monkeypatch.setattr(
        "cl_app.services.trading_screening.validate_live_review_snapshot",
        lambda snapshot: (AS_OF, ()),
    )

    now[0] = AS_OF + timedelta(hours=1)
    # The 15:00 publication predates the 15:05 next-session preparation
    # boundary, so it must be refreshed after the close.
    assert service._needs_refresh() is True
    # 新进程尚无一次成功 refresh attestation，不得直接信任持久快照。
    assert service._needs_refresh() is True
    prepared = service.refresh_now()
    service._record_background_result(prepared)

    assert service._needs_refresh() is False
    health = service.health_snapshot()
    assert health["refresh_suppressed"] is True
    assert health["refresh_suppression_reason"] == (
        "COMPLETE_CLOSE_SNAPSHOT_OUTSIDE_ACTIVE_WINDOW"
    )
    assert health["next_background_active_at"] == ("2026-07-21T08:45:00+08:00")
    assert health["background_active_windows"] == [
        {
            "phase": "POST_CLOSE_PRESELECTION",
            "timezone": "Asia/Shanghai",
            "weekdays": [0, 1, 2, 3, 4],
            "start": "15:05:00",
            "end": "23:59:59.999999",
        },
        {
            "phase": "OVERNIGHT_COVERAGE_CONTINUATION",
            "timezone": "Asia/Shanghai",
            "weekdays": [0, 1, 2, 3, 4],
            "start": "00:00:00",
            "end": "08:45:00",
        },
        {
            "phase": "PREOPEN_RECONCILIATION",
            "timezone": "Asia/Shanghai",
            "weekdays": [0, 1, 2, 3, 4],
            "start": "08:45:00",
            "end": "09:10:00",
        },
    ]

    now[0] = datetime(2026, 7, 21, 8, 44, tzinfo=AS_OF.tzinfo)
    assert service._needs_refresh() is False
    now[0] = datetime(2026, 7, 21, 8, 45, tzinfo=AS_OF.tzinfo)
    assert service._needs_refresh() is True


def test_background_never_idles_on_an_incomplete_preclose_cutoff(
    tmp_path: Path,
) -> None:
    preclose = AS_OF - timedelta(minutes=5)
    now = [preclose]
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(("SZ.000001",)),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: now[0],
        notifier=None,
        config=TradingScreeningConfig(refresh_interval_seconds=60),
    )
    snapshot = service.refresh_now()
    assert snapshot["market_data_as_of"] == preclose.isoformat()
    now[0] = AS_OF + timedelta(hours=1)
    with service._background_lock:
        service._background_last_result_at = now[0]
        service._background_last_error = None

    assert service._needs_refresh() is True
    assert service.health_snapshot()["refresh_suppressed"] is False


def test_background_never_idles_on_invalid_complete_review_boundary(
    tmp_path: Path,
) -> None:
    """A drained ledger is not idle-safe until the shared review core accepts it."""

    now = [AS_OF]
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(("SZ.000001",)),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: now[0],
        notifier=None,
        config=TradingScreeningConfig(refresh_interval_seconds=60),
    )
    snapshot = service.refresh_now()
    assert snapshot["coverage_manifest"]["complete"] is True
    now[0] = AS_OF + timedelta(hours=1)
    with service._background_lock:
        service._background_last_result_at = now[0]
        service._background_last_error = None

    health = service.health_snapshot()
    assert health["screening_review_reason_code"] == "REVIEW_BOUNDARY_INVALID"
    assert service._needs_refresh() is True
    assert health["refresh_suppressed"] is False


def test_background_never_idles_while_close_coverage_is_pending(
    tmp_path: Path,
) -> None:
    now = [AS_OF]
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(("SZ.000001", "SZ.000002", "SZ.000003")),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: now[0],
        notifier=None,
        config=TradingScreeningConfig(
            refresh_interval_seconds=60,
            max_symbols_per_refresh=1,
        ),
    )
    partial = service.refresh_now()
    assert partial["scan_audit"]["pending_symbol_count"] == 2
    assert partial["coverage_manifest"]["complete"] is False
    now[0] = AS_OF + timedelta(hours=1)
    with service._background_lock:
        service._background_last_result_at = now[0]
        service._background_last_error = None

    assert service._needs_refresh() is True
    assert service.health_snapshot()["refresh_suppressed"] is False


def test_background_complete_close_snapshot_idles_through_weekend(
    tmp_path: Path,
    monkeypatch,
) -> None:
    friday_close = datetime(2026, 7, 24, 15, 0, tzinfo=AS_OF.tzinfo)
    now = [friday_close]
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(("SZ.000001",)),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: now[0],
        notifier=None,
        config=TradingScreeningConfig(refresh_interval_seconds=60),
    )
    service.refresh_now()
    monkeypatch.setattr(
        "cl_app.services.trading_screening.validate_live_review_snapshot",
        lambda snapshot: (friday_close, ()),
    )
    now[0] = datetime(2026, 7, 25, 10, 0, tzinfo=AS_OF.tzinfo)
    with service._background_lock:
        service._background_last_result_at = now[0]
        service._background_last_error = None

    assert service._needs_refresh() is False
    assert service.health_snapshot()["next_background_active_at"] == (
        "2026-07-27T08:45:00+08:00"
    )


def test_official_calendar_skips_weekday_holiday_for_selection_and_alerts() -> None:
    national_day = datetime(2026, 10, 1, 10, 0, tzinfo=AS_OF.tzinfo)
    assert _priority_monitor_session_open(national_day) is False
    assert _priority_monitor_compute_window_open(national_day) is False
    assert _next_background_active_start(
        datetime(2026, 9, 30, 23, 30, tzinfo=AS_OF.tzinfo)
    ) == datetime(2026, 10, 8, 8, 45, tzinfo=AS_OF.tzinfo)
    assert (
        _priority_monitor_session_open(
            datetime(2026, 10, 8, 10, 0, tzinfo=AS_OF.tzinfo)
        )
        is True
    )


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    (
        (8, 44, False),
        (8, 45, True),
        (9, 30, True),
        (9, 31, True),
        (11, 30, True),
        (11, 31, True),
        (13, 0, True),
        (13, 1, True),
        (15, 0, True),
        (15, 1, False),
    ),
)
def test_priority_monitor_compute_window_includes_non_notification_work_windows(
    hour: int,
    minute: int,
    expected: bool,
) -> None:
    observed_at = datetime(
        2026,
        7,
        20,
        hour,
        minute,
        tzinfo=AS_OF.tzinfo,
    )

    assert _priority_monitor_compute_window_open(observed_at) is expected


def test_preopen_priority_warmup_computes_without_notifications(
    tmp_path: Path,
) -> None:
    class RecordingNotifier:
        def __init__(self) -> None:
            self.calls: list[tuple[dict[str, object], dict[str, object]]] = []

        def dispatch_changes(self, previous, current) -> None:
            self.calls.append((dict(previous), dict(current)))

    preopen = AS_OF.replace(hour=9, minute=15)
    market = ActionableMarketData()
    notifier = RecordingNotifier()
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=MultiMemberSectorCatalog(("SZ.000001",)),
        engine=HumanAssistedDecisionCore(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: preopen,
        notifier=notifier,
        config=TradingScreeningConfig(priority_monitoring_enabled=True),
    )
    previous = {
        "signals": [
            {
                "signal_id": "armed-buy",
                "code": "SZ.000001",
                "point_type": "1buy",
                "lifecycle_stage": "armed",
            }
        ]
    }

    assert service._priority_monitor_due(preopen) is True
    service._run_priority_monitor(previous=previous, observed_at=preopen)

    assert market.bundle_codes == ["SZ.000001"]
    assert notifier.calls == []
    health = service.health_snapshot()
    assert health["priority_monitor_session_open"] is False
    assert health["priority_monitor_compute_window_open"] is True
    assert health["priority_monitor_preopen_warmup_active"] is True


def test_lunch_catchup_runs_candidate_structures_only_without_notifications(
    tmp_path: Path,
) -> None:
    class RecordingNotifier:
        def __init__(self) -> None:
            self.calls: list[tuple[dict[str, object], dict[str, object]]] = []

        def dispatch_changes(self, previous, current) -> None:
            self.calls.append((dict(previous), dict(current)))

    lunch = AS_OF.replace(hour=12, minute=15)
    market = RecordingMarketData()
    notifier = RecordingNotifier()
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=MultiMemberSectorCatalog(("SZ.000001",)),
        engine=HumanAssistedDecisionCore(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: lunch,
        notifier=notifier,
        config=TradingScreeningConfig(priority_monitoring_enabled=True),
    )
    previous = {
        "signals": [
            {
                "signal_id": "formed-buy",
                "code": "SZ.000001",
                "point_type": "2buy",
                "lifecycle_stage": "formed",
            }
        ]
    }

    assert service._priority_monitor_due(lunch) is True
    service._run_priority_monitor(previous=previous, observed_at=lunch)

    assert market.bundle_frequency_requests == [("SZ.000001", ("30m", "5m"))]
    assert notifier.calls == []
    health = service.health_snapshot()
    assert health["priority_monitor_session_open"] is False
    assert health["priority_monitor_compute_window_open"] is True
    assert health["priority_monitor_preopen_warmup_active"] is False
    assert health["candidate_monitor_lunch_catchup_active"] is True
    assert health["candidate_monitor_status"] == "catching_up"


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    (
        (0, 0, True),
        (8, 44, True),
        (8, 45, True),
        (9, 9, True),
        (9, 10, False),
        (9, 31, False),
        (12, 0, False),
        (15, 4, False),
        (15, 5, True),
        (22, 59, True),
        (23, 0, True),
        (23, 59, True),
    ),
)
def test_full_coverage_refresh_uses_overnight_preopen_and_postclose_windows(
    hour: int,
    minute: int,
    expected: bool,
) -> None:
    observed_at = datetime(
        2026,
        7,
        20,
        hour,
        minute,
        tzinfo=AS_OF.tzinfo,
    )
    assert _full_coverage_refresh_window_open(observed_at) is expected


def test_full_coverage_next_active_boundary_skips_official_holiday() -> None:
    assert _next_full_coverage_active_start(
        datetime(2026, 7, 20, 12, 0, tzinfo=AS_OF.tzinfo)
    ) == datetime(2026, 7, 20, 15, 5, tzinfo=AS_OF.tzinfo)
    assert _next_full_coverage_active_start(
        datetime(2026, 10, 1, 10, 0, tzinfo=AS_OF.tzinfo)
    ) == datetime(2026, 10, 8, 0, 0, tzinfo=AS_OF.tzinfo)


def test_full_coverage_refresh_continues_on_official_weekday_holiday() -> None:
    assert (
        _full_coverage_refresh_window_open(
            datetime(2026, 10, 1, 15, 5, tzinfo=AS_OF.tzinfo)
        )
        is True
    )


@pytest.mark.parametrize(
    "observed_at",
    (
        datetime(2026, 7, 24, 23, 59, tzinfo=AS_OF.tzinfo),
        datetime(2026, 7, 25, 12, 0, tzinfo=AS_OF.tzinfo),
        datetime(2026, 7, 26, 12, 0, tzinfo=AS_OF.tzinfo),
        datetime(2026, 7, 27, 9, 9, tzinfo=AS_OF.tzinfo),
    ),
)
def test_full_coverage_window_continues_across_weekend_until_preopen(
    observed_at: datetime,
) -> None:
    assert _full_coverage_refresh_window_open(observed_at) is True


@pytest.mark.parametrize(
    ("config", "expected_mode", "expected_open"),
    (
        (
            TradingScreeningConfig(
                admitted_universe_codes=("SZ.000001", "SH.600000"),
            ),
            "VALIDATION_COHORT",
            True,
        ),
        (TradingScreeningConfig(), "VALIDATION_COHORT", False),
        (
            TradingScreeningConfig(
                large_scope_authorized=True,
                admitted_universe_codes=("SZ.000001", "SH.600000"),
            ),
            "LARGE_SCOPE",
            False,
        ),
    ),
)
def test_only_exact_nonempty_validation_cohort_opens_bounded_coverage_gate(
    tmp_path: Path,
    config: TradingScreeningConfig,
    expected_mode: str,
    expected_open: bool,
) -> None:
    observed_at = AS_OF.replace(hour=10, minute=0)
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / f"{expected_mode}-{expected_open}.json",
        clock=lambda: observed_at,
        notifier=None,
        config=config,
    )

    assert config.screening_scope_mode == expected_mode
    assert (
        service._full_coverage_execution_window_open(
            service.snapshot(),
            observed_at,
        )
        is expected_open
    )


def test_complete_intraday_validation_snapshot_routes_only_realtime_lane_until_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [AS_OF.replace(hour=10, minute=0)]
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(("SZ.000001",)),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: now[0],
        notifier=None,
        config=TradingScreeningConfig(
            refresh_interval_seconds=60,
            priority_monitoring_enabled=True,
            admitted_universe_codes=("SZ.000001",),
        ),
    )

    snapshot = service.refresh_now()

    assert snapshot["available"] is True
    assert snapshot["scan_state"] == "complete"
    assert snapshot["market_data_as_of"] == now[0].isoformat()
    assert service._validation_snapshot_uses_priority_only(snapshot, now[0]) is True
    assert service._full_coverage_execution_window_open(snapshot, now[0]) is False
    health = service.health_snapshot()
    assert health["validation_snapshot_priority_only"] is True
    assert health["coverage_execution_window_open"] is False

    calls: list[tuple[bool, bool]] = []
    stop = threading.Event()
    monkeypatch.setattr(service, "_needs_refresh", lambda: False)
    monkeypatch.setattr(service, "_priority_monitor_due", lambda _at: True)

    def refresh_now(*, copy_result: bool, priority_only: bool):
        calls.append((copy_result, priority_only))
        stop.set()
        return dict(service._snapshot_reference())

    monkeypatch.setattr(service, "refresh_now", refresh_now)
    service._background_loop(stop, threading.Event())

    assert calls == [(False, True)]

    now[0] = now[0].replace(hour=15, minute=5)
    assert service._validation_snapshot_uses_priority_only(snapshot, now[0]) is False
    assert service._full_coverage_execution_window_open(snapshot, now[0]) is True


def test_complete_validation_close_snapshot_runs_each_archive_phase_once(
    tmp_path: Path,
) -> None:
    now = [AS_OF]
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(("SZ.000001",)),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: now[0],
        notifier=None,
        config=TradingScreeningConfig(
            refresh_interval_seconds=60,
            admitted_universe_codes=("SZ.000001",),
        ),
    )
    snapshot = service.refresh_now()

    now[0] = AS_OF.replace(hour=15, minute=5)
    assert service._validation_snapshot_uses_priority_only(snapshot, now[0]) is False
    with service._background_lock:
        service._background_last_result_at = now[0]
        service._background_last_error = None
    assert service._validation_snapshot_uses_priority_only(snapshot, now[0]) is True

    now[0] = (AS_OF + timedelta(days=1)).replace(hour=8, minute=45)
    assert service._validation_snapshot_uses_priority_only(snapshot, now[0]) is False
    with service._background_lock:
        service._background_last_result_at = now[0]
    assert service._validation_snapshot_uses_priority_only(snapshot, now[0]) is True


def test_priority_monitor_delay_is_measured_start_to_start() -> None:
    started_at = datetime(2026, 7, 20, 10, 0, 2, tzinfo=AS_OF.tzinfo)
    assert (
        _priority_monitor_delay_seconds(
            started_at + timedelta(seconds=50),
            started_at,
            interval_seconds=60,
        )
        == 10
    )
    assert (
        _priority_monitor_delay_seconds(
            started_at + timedelta(seconds=60),
            started_at,
            interval_seconds=60,
        )
        == 0
    )
    assert (
        _priority_monitor_delay_seconds(
            started_at - timedelta(seconds=1),
            started_at,
            interval_seconds=60,
        )
        == 0
    )


@pytest.mark.parametrize(
    ("last_at", "observed_at", "expected_delay"),
    (
        (
            datetime(2026, 7, 20, 9, 30, 40, tzinfo=AS_OF.tzinfo),
            datetime(2026, 7, 20, 9, 30, 50, tzinfo=AS_OF.tzinfo),
            12,
        ),
        (
            datetime(2026, 7, 20, 9, 30, 40, tzinfo=AS_OF.tzinfo),
            datetime(2026, 7, 20, 9, 31, 0, tzinfo=AS_OF.tzinfo),
            2,
        ),
        (
            datetime(2026, 7, 20, 13, 0, 40, tzinfo=AS_OF.tzinfo),
            datetime(2026, 7, 20, 13, 0, 50, tzinfo=AS_OF.tzinfo),
            12,
        ),
        (
            datetime(2026, 7, 20, 13, 0, 40, tzinfo=AS_OF.tzinfo),
            datetime(2026, 7, 20, 13, 1, 0, tzinfo=AS_OF.tzinfo),
            2,
        ),
    ),
)
def test_priority_monitor_delay_wakes_at_completed_minute_session_boundary(
    last_at: datetime,
    observed_at: datetime,
    expected_delay: float,
) -> None:
    assert (
        _priority_monitor_delay_seconds(
            observed_at,
            last_at,
            interval_seconds=60,
        )
        == expected_delay
    )


def test_daily_preselection_fails_closed_after_its_target_session_ends(
    tmp_path: Path,
    monkeypatch,
) -> None:
    friday_close = datetime(2026, 7, 24, 15, 0, tzinfo=AS_OF.tzinfo)
    now = [friday_close]
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(("SZ.000001",)),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: now[0],
        notifier=None,
        config=TradingScreeningConfig(refresh_interval_seconds=60),
    )
    service.refresh_now()
    monkeypatch.setattr(
        "cl_app.services.trading_screening.validate_live_review_snapshot",
        lambda snapshot: (friday_close, ()),
    )

    now[0] = datetime(2026, 7, 27, 14, 59, tzinfo=AS_OF.tzinfo)
    before_close = service.health_snapshot()
    assert before_close["daily_preselection_ready"] is True
    assert before_close["daily_preselection_target_session"] == "2026-07-27"
    assert before_close["daily_preselection_expected_session"] == "2026-07-27"
    assert before_close["daily_preselection_session_aligned"] is True
    assert before_close["daily_preselection_calendar_source"] == (
        "SSE_OFFICIAL_ANNUAL_CALENDAR"
    )

    now[0] = datetime(2026, 7, 27, 15, 1, tzinfo=AS_OF.tzinfo)
    after_close = service.health_snapshot()
    assert after_close["daily_preselection_ready"] is False
    assert after_close["daily_preselection_status"] == "target_session_stale"
    assert after_close["daily_preselection_reason_code"] == (
        "PRESELECTION_TARGET_SESSION_MISMATCH"
    )
    assert after_close["daily_preselection_target_session"] == "2026-07-27"
    assert after_close["daily_preselection_expected_session"] == "2026-07-28"
    assert after_close["daily_preselection_session_aligned"] is False


def test_background_loop_does_not_repeat_night_scan_and_wakes_at_preopen(
    tmp_path: Path,
    monkeypatch,
) -> None:
    now = [AS_OF + timedelta(hours=1)]
    catalog = RecordingSectorCatalog()
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=catalog,
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(("SZ.000001",)),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: now[0],
        notifier=None,
        config=TradingScreeningConfig(
            refresh_interval_seconds=0.05,
            full_coverage_refresh_enabled=True,
            large_scope_authorized=True,
        ),
    )
    monkeypatch.setattr(
        "cl_app.services.trading_screening.validate_live_review_snapshot",
        lambda snapshot: (AS_OF, ()),
    )

    service.start_background()
    try:
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and len(catalog.assessment_calls) < 1:
            time.sleep(0.01)
        assert len(catalog.assessment_calls) == 1
        time.sleep(0.18)
        assert len(catalog.assessment_calls) == 1
        assert service.health_snapshot()["refresh_suppressed"] is True

        now[0] = datetime(2026, 7, 21, 8, 45, tzinfo=AS_OF.tzinfo)
        assert service.ensure_refresh() is True
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and len(catalog.assessment_calls) < 2:
            time.sleep(0.01)
        assert len(catalog.assessment_calls) == 2
    finally:
        assert service.shutdown_background(wait=True, timeout=1.0) is True


def test_background_loop_runs_due_priority_lane_when_coverage_is_idle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_at = AS_OF.replace(hour=10, minute=0)
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(("SZ.000001",)),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: observed_at,
        notifier=None,
        config=TradingScreeningConfig(
            refresh_interval_seconds=0.01,
            full_coverage_refresh_enabled=False,
        ),
    )
    calls: list[tuple[bool, bool]] = []
    stop = threading.Event()

    monkeypatch.setattr(service, "_needs_refresh", lambda: False)
    monkeypatch.setattr(service, "_priority_monitor_due", lambda _at: True)

    def refresh_now(*, copy_result: bool, priority_only: bool):
        calls.append((copy_result, priority_only))
        stop.set()
        return dict(service._snapshot_reference())

    monkeypatch.setattr(service, "refresh_now", refresh_now)
    service._background_loop(stop, threading.Event())

    assert calls == [(False, True)]


@pytest.mark.parametrize(
    ("hour", "minute", "full_coverage_enabled"),
    ((10, 0, False), (18, 30, True)),
)
def test_background_due_priority_waits_for_running_priority_lane(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    hour: int,
    minute: int,
    full_coverage_enabled: bool,
) -> None:
    observed_at = AS_OF.replace(hour=hour, minute=minute)
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(("SZ.000001",)),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: observed_at,
        notifier=None,
        config=TradingScreeningConfig(
            refresh_interval_seconds=60,
            full_coverage_refresh_enabled=full_coverage_enabled,
            large_scope_authorized=full_coverage_enabled,
            priority_monitoring_enabled=True,
        ),
    )
    calls: list[tuple[bool, bool]] = []
    waits: list[float] = []
    stop = threading.Event()

    class StopAfterWait:
        def clear(self) -> None:
            return None

        def wait(self, timeout: float) -> bool:
            waits.append(timeout)
            stop.set()
            return True

    monkeypatch.setattr(service, "_needs_refresh", lambda: False)
    monkeypatch.setattr(service, "_priority_monitor_due", lambda _at: True)

    def refresh_now(*, copy_result: bool, priority_only: bool):
        calls.append((copy_result, priority_only))
        stop.set()
        return dict(service._snapshot_reference())

    monkeypatch.setattr(service, "refresh_now", refresh_now)
    assert service._priority_scan_lock.acquire(blocking=False) is True
    try:
        service._background_loop(stop, StopAfterWait())
    finally:
        service._priority_scan_lock.release()

    assert calls == []
    assert waits == [1.0]
    assert service.health_snapshot()["refresh_attempt_count"] == 0


def test_background_due_priority_backs_off_if_lock_is_lost_after_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_at = AS_OF.replace(hour=10, minute=0)
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(("SZ.000001",)),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: observed_at,
        notifier=None,
        config=TradingScreeningConfig(
            refresh_interval_seconds=60,
            full_coverage_refresh_enabled=False,
            priority_monitoring_enabled=True,
        ),
    )
    calls: list[tuple[bool, bool]] = []
    waits: list[float] = []
    stop = threading.Event()

    class StopAfterWait:
        def clear(self) -> None:
            return None

        def wait(self, timeout: float) -> bool:
            waits.append(timeout)
            stop.set()
            return True

    monkeypatch.setattr(service, "_needs_refresh", lambda: False)
    monkeypatch.setattr(service, "_priority_monitor_due", lambda _at: True)

    def refresh_now(*, copy_result: bool, priority_only: bool):
        calls.append((copy_result, priority_only))
        return dict(service._snapshot_reference())

    monkeypatch.setattr(service, "refresh_now", refresh_now)
    service._background_loop(stop, StopAfterWait())

    assert calls == [(False, True)]
    assert waits == [1.0]
    assert service.health_snapshot()["refresh_attempt_count"] == 1


def test_background_lunch_catchup_keeps_start_to_start_cadence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [AS_OF.replace(hour=12, minute=0)]
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(("SZ.000001",)),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: now[0],
        notifier=None,
        config=TradingScreeningConfig(
            refresh_interval_seconds=60,
            full_coverage_refresh_enabled=False,
            priority_monitoring_enabled=True,
        ),
    )
    calls: list[tuple[bool, bool]] = []
    waits: list[float] = []
    stop = threading.Event()

    class StopOnUnexpectedWait:
        def clear(self) -> None:
            return None

        def wait(self, timeout: float) -> bool:
            waits.append(timeout)
            stop.set()
            return True

    monkeypatch.setattr(service, "_needs_refresh", lambda: False)
    monkeypatch.setattr(service, "_priority_monitor_due", lambda _at: True)

    def refresh_now(*, copy_result: bool, priority_only: bool):
        calls.append((copy_result, priority_only))
        with service._background_lock:
            service._priority_monitor_last_at = now[0]
        now[0] += timedelta(seconds=70)
        if len(calls) == 2:
            stop.set()
        return dict(service._snapshot_reference())

    monkeypatch.setattr(service, "refresh_now", refresh_now)
    service._background_loop(stop, StopOnUnexpectedWait())

    assert calls == [(False, True), (False, True)]
    assert waits == []


def test_missing_first_snapshot_auto_recovers_intraday_without_explicit_force(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_at = AS_OF.replace(hour=10, minute=0)
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(("SZ.000001",)),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: observed_at,
        notifier=None,
        config=TradingScreeningConfig(
            refresh_interval_seconds=0.01,
            full_coverage_refresh_enabled=True,
            large_scope_authorized=True,
        ),
    )
    health = service.health_snapshot()
    assert health["full_coverage_force_active"] is True
    assert health["full_coverage_auto_recovery_active"] is True
    assert health["full_coverage_auto_recovery_reason"] == (
        "PRESELECTION_SNAPSHOT_MISSING"
    )
    assert health["full_coverage_refresh_window_open"] is True

    calls: list[tuple[bool, bool]] = []
    stop = threading.Event()
    monkeypatch.setattr(service, "_needs_refresh", lambda: False)
    monkeypatch.setattr(service, "_priority_monitor_due", lambda _at: False)

    def refresh_now(*, copy_result: bool, priority_only: bool):
        calls.append((copy_result, priority_only))
        stop.set()
        return dict(service._snapshot_reference())

    monkeypatch.setattr(service, "refresh_now", refresh_now)
    service._background_loop(stop, threading.Event())

    assert calls == [(False, False)]


def test_explicit_startup_rebuild_bypasses_intraday_full_coverage_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_at = AS_OF.replace(hour=10, minute=0)
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(("SZ.000001",)),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: observed_at,
        notifier=None,
        config=TradingScreeningConfig(
            refresh_interval_seconds=0.01,
            full_coverage_refresh_enabled=True,
            force_full_coverage_until_complete=True,
            large_scope_authorized=True,
        ),
    )
    calls: list[tuple[bool, bool]] = []
    stop = threading.Event()

    monkeypatch.setattr(service, "_needs_refresh", lambda: False)
    monkeypatch.setattr(service, "_priority_monitor_due", lambda _at: False)

    def refresh_now(*, copy_result: bool, priority_only: bool):
        calls.append((copy_result, priority_only))
        stop.set()
        return dict(service._snapshot_reference())

    monkeypatch.setattr(service, "refresh_now", refresh_now)
    service._background_loop(stop, threading.Event())

    assert calls == [(False, False)]
    health = service.health_snapshot()
    assert health["full_coverage_force_until_complete_enabled"] is True
    assert health["full_coverage_force_active"] is True


def test_explicit_startup_rebuild_restores_schedule_after_complete_snapshot(
    tmp_path: Path,
) -> None:
    observed_at = AS_OF.replace(hour=10, minute=0)
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(("SZ.000001",)),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: observed_at,
        notifier=None,
        config=TradingScreeningConfig(
            full_coverage_refresh_enabled=True,
            force_full_coverage_until_complete=True,
            large_scope_authorized=True,
        ),
    )

    snapshot = service.refresh_now()
    health = service.health_snapshot()

    assert snapshot["coverage_manifest"]["complete"] is True
    assert health["full_coverage_force_until_complete_enabled"] is True
    assert health["full_coverage_force_active"] is False
    assert health["full_coverage_scheduled_window_open"] is False
    assert health["full_coverage_refresh_window_open"] is False
    assert health["full_coverage_refresh_paused"] is True


def test_native_progress_runs_priority_monitor_while_full_refresh_lock_is_held(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_at = AS_OF.replace(hour=10, minute=0)
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(("SZ.000001",)),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: observed_at,
        notifier=None,
        config=TradingScreeningConfig(priority_monitoring_enabled=True),
    )
    completed = threading.Event()
    calls: list[tuple[bool, bool]] = []

    def refresh_now(*, copy_result: bool, priority_only: bool):
        calls.append((copy_result, priority_only))
        completed.set()
        return dict(service._snapshot_reference())

    monkeypatch.setattr(service, "refresh_now", refresh_now)
    with service._background_lock:
        service._background_thread = threading.current_thread()
    assert service._scan_lock.acquire(blocking=False) is True
    try:
        service._record_native_progress()
        assert completed.wait(timeout=1.0)
    finally:
        service._scan_lock.release()
        with service._background_lock:
            service._background_thread = None

    assert calls == [(False, True)]


def test_priority_refresh_has_independent_lock_from_full_coverage(
    tmp_path: Path,
) -> None:
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(("SZ.000001",)),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF.replace(hour=10, minute=0),
        notifier=None,
        config=TradingScreeningConfig(priority_monitoring_enabled=True),
    )

    assert service._scan_lock.acquire(blocking=False) is True
    try:
        snapshot = service.refresh_now(priority_only=True)
    finally:
        service._scan_lock.release()

    assert snapshot["available"] is False
    assert service.health_snapshot()["priority_monitor_runtime_verified"] is True


def test_intraday_full_coverage_uses_non_priority_structure_lane(
    tmp_path: Path,
) -> None:
    class LaneRecordingMarketData(RecordingMarketData):
        def __init__(self) -> None:
            super().__init__()
            self.candidate_codes: list[str] = []

        def candidate_structure_bundle_with_risk_cutoff(
            self,
            code: str,
            *,
            as_of: datetime,
            sector,
            frequencies=(),
            risk_evidence_cutoff: datetime,
        ) -> SymbolStructureBundle:
            del risk_evidence_cutoff
            self.candidate_codes.append(code)
            return self.structure_bundle(
                code,
                as_of=as_of,
                sector=sector,
                frequencies=frequencies,
            )

    market = LaneRecordingMarketData()
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(("SZ.000001",)),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF.replace(hour=10, minute=0),
        notifier=None,
    )

    snapshot = service.refresh_now()

    assert snapshot["coverage_manifest"]["complete"] is True
    assert market.candidate_codes == ["SZ.000001"]


def test_background_verifies_current_priority_before_after_hours_coverage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_at = AS_OF.replace(hour=18, minute=30)
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(("SZ.000001",)),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: observed_at,
        notifier=None,
        config=TradingScreeningConfig(
            refresh_interval_seconds=60,
            priority_monitoring_enabled=True,
            full_coverage_refresh_enabled=True,
            large_scope_authorized=True,
        ),
    )
    calls: list[tuple[bool, bool]] = []
    stop = threading.Event()

    # 即使存在可读取的归档快照，新进程仍必须用当前运行实现复核优先标的；持久
    # 信号不能替代本进程的证券类型、QMT 和通知通道验证。
    with service._state_lock:
        available = dict(service._snapshot)
        available["available"] = True
        service._snapshot = available

    monkeypatch.setattr(service, "_needs_refresh", lambda: True)

    def refresh_now(*, copy_result: bool, priority_only: bool):
        calls.append((copy_result, priority_only))
        if priority_only:
            with service._background_lock:
                service._priority_monitor_runtime_verified = True
        else:
            stop.set()
        return dict(service._snapshot_reference())

    monkeypatch.setattr(service, "refresh_now", refresh_now)
    service._background_loop(stop, threading.Event())

    assert calls == [(False, True), (False, False)]


def test_startup_priority_runs_after_close_without_sector_rebuild(
    tmp_path: Path,
) -> None:
    observed_at = AS_OF.replace(hour=18, minute=30)

    class WatchlistMarket(RecordingMarketData):
        def active_watchlist(self) -> tuple[str, ...]:
            return ("SZ.000001",)

        def structure_bundle(
            self,
            code: str,
            *,
            as_of: datetime,
            sector,
            frequencies=(),
        ) -> SymbolStructureBundle:
            bundle = super().structure_bundle(
                code,
                as_of=as_of,
                sector=sector,
                frequencies=frequencies,
            )
            return replace(
                bundle,
                as_of=as_of.replace(hour=15, minute=0, second=0, microsecond=0),
            )

    class CachedSectorCatalog(RecordingSectorCatalog):
        @staticmethod
        def cached_sector_snapshot_for_priority(*, as_of: datetime):
            del as_of
            return None

    market = WatchlistMarket()
    catalog = CachedSectorCatalog()
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=catalog,
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(("SZ.000001",)),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: observed_at,
        notifier=None,
        config=TradingScreeningConfig(
            priority_monitoring_enabled=True,
            priority_monitor_time_budget_seconds=5,
        ),
    )
    snapshot = service.refresh_now(priority_only=True)
    presentation = service.presentation_snapshot()

    assert snapshot["available"] is False
    assert market.bundle_codes == ["SZ.000001"]
    assert catalog.assessment_calls == []
    startup_health = service.health_snapshot()
    assert startup_health["priority_monitor_runtime_verified"] is True
    assert startup_health["priority_monitor_locator_runtime_verified"] is True
    assert startup_health["priority_monitor_locator_runtime_status"] == "verified"
    assert startup_health["priority_monitor_locator_last_scheduled_count"] == 1
    assert startup_health["priority_monitor_locator_last_attempted_count"] == 1
    assert startup_health["priority_monitor_locator_last_completed_count"] == 1
    assert startup_health["priority_monitor_locator_last_observed_at"] == (
        observed_at.isoformat()
    )
    assert presentation["priority_live_overlay"]["startup_bootstrap"] is True
    assert presentation["priority_live_overlay"]["signal_count"] == len(
        presentation["signals"]
    )

    # 全市场发布物仍不可用时，已由当前规则完成的显式标的复核足以证明实时运行
    # 通道可服务；selection_* 继续关闭，不能把快速复核冒充完整选股。
    with service._state_lock:
        unavailable = dict(service._snapshot)
        unavailable["scan_state"] = "incomplete_not_published"
        unavailable["last_batch_state"] = "incomplete_not_published"
        service._snapshot = unavailable
    with service._background_lock:
        service._background_thread = threading.current_thread()
        service._background_heartbeat_at = observed_at
    health = service.health_snapshot()
    assert health["startup_priority_bootstrap_ready"] is True
    assert health["runtime_ready"] is True
    assert health["selection_ready"] is False
    assert health["selection_reason_code"] == "PRESELECTION_SNAPSHOT_MISSING"
    assert health["reasons"] == []

    # 只有实际完成过当前规则复核才可放行；仅有不可用快照不能绕过发布门槛。
    with service._background_lock:
        service._priority_monitor_runtime_verified = False
    unverified = service.health_snapshot()
    assert unverified["startup_priority_bootstrap_ready"] is False
    assert unverified["runtime_ready"] is True
    assert unverified["reasons"] == []
    assert unverified["selection_operational_reason_codes"] == [
        "screening_snapshot_not_publishable"
    ]


@pytest.mark.parametrize(
    ("observed_at", "expected_retry"),
    (
        (AS_OF.replace(hour=10, minute=0), True),
        (AS_OF.replace(hour=18, minute=30), False),
    ),
)
def test_failed_locator_startup_retry_respects_compute_window(
    tmp_path: Path,
    observed_at: datetime,
    expected_retry: bool,
) -> None:
    class FailingWatchlistMarket(RecordingMarketData):
        def active_watchlist(self) -> tuple[str, ...]:
            return ("SZ.000001",)

        def structure_bundle_with_risk_cutoff(self, code: str, **_kwargs):
            self.bundle_codes.append(code)
            raise RuntimeError("priority route unavailable")

    class CachedSectorCatalog(RecordingSectorCatalog):
        @staticmethod
        def cached_sector_snapshot_for_priority(*, as_of: datetime):
            del as_of
            return None

    service = TradingScreeningService(
        market_data=FailingWatchlistMarket(),
        sector_catalog=CachedSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(("SZ.000001",)),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: observed_at,
        notifier=None,
        config=TradingScreeningConfig(priority_monitoring_enabled=True),
    )

    service.refresh_now(priority_only=True)
    health = service.health_snapshot()

    assert health["priority_monitor_runtime_verified"] is True
    assert health["priority_monitor_locator_runtime_verified"] is False
    assert health["priority_monitor_locator_runtime_status"] == "failed"
    assert health["startup_priority_bootstrap_ready"] is False
    assert service._startup_priority_bootstrap_required() is expected_retry
    assert (
        service.presentation_snapshot()["priority_live_overlay"]["startup_bootstrap"]
        is False
    )


@pytest.mark.parametrize(
    ("observed_at", "expected_priority_only"),
    (
        (AS_OF.replace(hour=10, minute=0), True),
        (AS_OF.replace(hour=15, minute=5), False),
    ),
)
def test_ensure_refresh_without_background_obeys_full_coverage_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    observed_at: datetime,
    expected_priority_only: bool,
) -> None:
    """QMT 启动回调不得绕过后台循环采用的盘中资源闸门。"""

    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(("SZ.000001",)),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: observed_at,
        notifier=None,
        config=TradingScreeningConfig(
            refresh_interval_seconds=60,
            full_coverage_refresh_enabled=True,
            large_scope_authorized=True,
        ),
    )
    assert service.refresh_now()["available"] is True
    calls: list[tuple[bool, bool]] = []
    completed = threading.Event()

    monkeypatch.setattr(service, "_needs_refresh", lambda: True)

    def refresh_now(*, copy_result: bool, priority_only: bool):
        calls.append((copy_result, priority_only))
        completed.set()
        return dict(service._snapshot_reference())

    monkeypatch.setattr(service, "refresh_now", refresh_now)

    assert service.ensure_refresh() is True
    assert completed.wait(timeout=1.0)
    assert calls == [(False, expected_priority_only)]


def test_ensure_refresh_without_background_recovers_missing_snapshot_intraday(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_at = AS_OF.replace(hour=10, minute=0)
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(("SZ.000001",)),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: observed_at,
        notifier=None,
        config=TradingScreeningConfig(
            refresh_interval_seconds=60,
            full_coverage_refresh_enabled=True,
            large_scope_authorized=True,
        ),
    )
    calls: list[tuple[bool, bool]] = []
    completed = threading.Event()
    monkeypatch.setattr(service, "_needs_refresh", lambda: True)

    def refresh_now(*, copy_result: bool, priority_only: bool):
        calls.append((copy_result, priority_only))
        completed.set()
        return dict(service._snapshot_reference())

    monkeypatch.setattr(service, "refresh_now", refresh_now)

    assert service.ensure_refresh() is True
    assert completed.wait(timeout=1.0)
    assert calls == [(False, False)]


def test_background_loop_keeps_full_lane_open_after_2300(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_at = AS_OF.replace(hour=23, minute=30)
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(("SZ.000001",)),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: observed_at,
        notifier=None,
        config=TradingScreeningConfig(
            refresh_interval_seconds=60,
            full_coverage_refresh_enabled=True,
            large_scope_authorized=True,
        ),
    )
    calls: list[tuple[bool, bool]] = []
    stop = threading.Event()

    monkeypatch.setattr(service, "_needs_refresh", lambda: True)

    def refresh_now(*, copy_result: bool, priority_only: bool):
        calls.append((copy_result, priority_only))
        stop.set()
        return dict(service._snapshot_reference())

    monkeypatch.setattr(service, "refresh_now", refresh_now)
    service._background_loop(stop, threading.Event())

    assert calls == [(False, False)]


def test_validation_cohort_background_loop_runs_bounded_coverage_lane_intraday(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_at = AS_OF.replace(hour=10, minute=0)
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(("SZ.000001",)),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: observed_at,
        notifier=None,
        config=TradingScreeningConfig(
            refresh_interval_seconds=60,
            admitted_universe_codes=("SZ.000001", "SZ.000002"),
        ),
    )
    calls: list[tuple[bool, bool]] = []
    stop = threading.Event()

    monkeypatch.setattr(service, "_needs_refresh", lambda: True)

    def refresh_now(*, copy_result: bool, priority_only: bool):
        calls.append((copy_result, priority_only))
        stop.set()
        return dict(service._snapshot_reference())

    monkeypatch.setattr(service, "refresh_now", refresh_now)
    service._background_loop(stop, threading.Event())

    assert calls == [(False, False)]


def test_disabled_full_coverage_never_runs_full_lane_inside_active_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_at = AS_OF.replace(hour=15, minute=5)
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(("SZ.000001",)),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: observed_at,
        notifier=None,
        config=TradingScreeningConfig(
            refresh_interval_seconds=60,
            full_coverage_refresh_enabled=False,
        ),
    )
    calls: list[tuple[bool, bool]] = []
    stop = threading.Event()

    monkeypatch.setattr(service, "_needs_refresh", lambda: True)

    def refresh_now(*, copy_result: bool, priority_only: bool):
        calls.append((copy_result, priority_only))
        stop.set()
        return dict(service._snapshot_reference())

    monkeypatch.setattr(service, "refresh_now", refresh_now)
    service._background_loop(stop, threading.Event())

    assert calls == [(False, True)]
    health = service.health_snapshot()
    assert health["full_coverage_refresh_enabled"] is False
    assert health["full_coverage_refresh_window_open"] is False
    assert health["full_coverage_refresh_pause_reason"] == (
        "FULL_COVERAGE_REFRESH_DISABLED"
    )
    assert health["full_coverage_next_active_at"] is None
    assert health["current_logic_snapshot_required"] is False
    assert "screening_snapshot_unavailable" not in health["reasons"]


def test_membership_edit_forces_next_priority_scan_and_wakes_worker(
    tmp_path: Path,
) -> None:
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(("SZ.300826",)),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF.replace(hour=10, minute=0),
        notifier=None,
        config=TradingScreeningConfig(refresh_interval_seconds=60),
    )
    service._priority_monitor_runtime_verified = True
    service._background_thread = threading.current_thread()
    service._background_wake.clear()

    assert service.notify_instrument_scope_changed() is True
    assert service._priority_monitor_runtime_verified is False
    assert service._background_wake.is_set() is True


def test_background_health_attestation_tracks_worker_snapshot_and_staleness(
    tmp_path: Path,
) -> None:
    background_as_of = AS_OF + timedelta(minutes=5)
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(("SZ.000001",)),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: background_as_of,
        notifier=None,
        config=TradingScreeningConfig(
            refresh_interval_seconds=3600,
            full_coverage_refresh_enabled=True,
            large_scope_authorized=True,
        ),
    )

    before_start = service.health_snapshot()
    assert before_start["ready"] is False
    assert before_start["worker_alive"] is False
    assert before_start["coverage_epoch_id"] is None
    assert before_start["market_data_as_of"] is None
    assert before_start["coverage_cycle_batch_count"] == 0
    assert before_start["reasons"] == [
        "screening_worker_not_running",
        "screening_heartbeat_missing",
    ]
    assert before_start["selection_ready"] is False
    assert before_start["selection_reason_code"] == "PRESELECTION_SNAPSHOT_MISSING"

    worker = service.start_background()
    try:
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            running = service.health_snapshot()
            if running["snapshot_available"]:
                break
            time.sleep(0.01)

        assert running["ready"] is True
        assert running["worker_alive"] is True
        assert running["scan_state"] == "complete"
        assert running["last_batch_state"] == "complete"
        assert running["full_coverage_state"] == "complete"
        assert running["snapshot_available"] is True
        assert str(running["snapshot_content_sha256"]).startswith("sha256:")
        assert str(running["coverage_epoch_id"]).startswith("sha256:")
        assert str(running["screening_policy_id"]).startswith("sha256:")
        assert running["market_data_as_of"] == background_as_of.isoformat()
        assert running["coverage_cycle_batch_count"] == 1
        assert running["discovered_symbol_count"] == 1
        assert running["coverage_cycle_attempted_symbol_count"] == 1
        assert running["coverage_cycle_completed_symbol_count"] == 1
        assert running["coverage_cycle_failed_symbol_count"] == 0
        assert running["coverage_cycle_completion_ratio"] == "1"
        assert running["refresh_attempt_count"] >= 1
        assert running["last_error"] is None
        assert running["reasons"] == []

        with service._background_lock:
            service._background_heartbeat_at = background_as_of - timedelta(
                seconds=int(running["heartbeat_max_age_seconds"]) + 1
            )
        stale = service.health_snapshot()
        assert stale["ready"] is False
        assert stale["worker_alive"] is True
        assert stale["reasons"] == ["screening_heartbeat_stale"]
    finally:
        assert service.shutdown_background(wait=True, timeout=1.0) is True

    stopped = service.health_snapshot()
    assert worker.is_alive() is False
    assert stopped["ready"] is False
    assert "screening_worker_not_running" in stopped["reasons"]


def test_background_health_rechecks_an_untrusted_snapshot_publication(
    tmp_path: Path,
) -> None:
    """An unknown publication must pass the full gate before health trusts it."""

    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(("SZ.000001",)),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
    )
    service.refresh_now()
    with service._background_lock:
        service._background_thread = threading.current_thread()
        service._background_started_at = AS_OF
        service._background_heartbeat_at = AS_OF
        service._background_last_result_at = AS_OF
        service._background_last_error = None

    valid = service.health_snapshot()
    assert valid["ready"] is True
    declared = valid["snapshot_content_sha256"]
    assert isinstance(declared, str) and declared.startswith("sha256:")

    with service._state_lock:
        # Production never mutates an installed publication in place.  Clearing
        # its installation attestation models an unknown/recovered tree and
        # proves health still performs the full semantic hash gate in that case.
        service._snapshot["data_quality"]["stale"] = True
        service._validated_snapshot_sha256 = None
    tampered = service.health_snapshot()

    assert tampered["ready"] is True
    assert tampered["snapshot_content_sha256"] is None
    assert (
        "screening_snapshot_identity_missing"
        in tampered["selection_operational_reason_codes"]
    )

    with service._state_lock:
        service._snapshot["data_quality"]["stale"] = False
        service._snapshot["signal_document_contract_id"] = (
            "chanlun-human-assisted-signal-document"
        )
        service._snapshot["coverage_manifest"]["signal_document_contract_id"] = (
            "chanlun-human-assisted-signal-document"
        )
        service._finalize_snapshot_identity(service._snapshot)
    noncurrent_but_self_hashed = service.health_snapshot()

    assert noncurrent_but_self_hashed["ready"] is True
    assert noncurrent_but_self_hashed["snapshot_content_sha256"] is None
    assert (
        "screening_snapshot_identity_missing"
        in noncurrent_but_self_hashed["selection_operational_reason_codes"]
    )

    with service._state_lock:
        service._snapshot["signal_document_contract_id"] = SIGNAL_DOCUMENT_CONTRACT_ID
        service._snapshot["coverage_manifest"]["signal_document_contract_id"] = (
            SIGNAL_DOCUMENT_CONTRACT_ID
        )
        forged_epoch = "sha256:" + "9" * 64
        service._snapshot["coverage_epoch_id"] = forged_epoch
        service._snapshot["coverage_manifest"]["coverage_epoch_id"] = forged_epoch
        service._finalize_snapshot_identity(service._snapshot)
    forged_but_self_hashed = service.health_snapshot()

    assert forged_but_self_hashed["ready"] is True
    assert forged_but_self_hashed["snapshot_content_sha256"] is None
    assert (
        "screening_snapshot_identity_missing"
        in forged_but_self_hashed["selection_operational_reason_codes"]
    )


def test_background_health_does_not_deep_copy_public_snapshot(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Readiness validates the immutable publication without a 40 MiB copy."""

    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(("SZ.000001",)),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
    )
    service.refresh_now()

    def public_snapshot_must_not_be_called():
        raise AssertionError("health must not deep-copy the publication")

    monkeypatch.setattr(service, "snapshot", public_snapshot_must_not_be_called)

    health = service.health_snapshot()

    assert health["snapshot_available"] is True
    assert str(health["snapshot_content_sha256"]).startswith("sha256:")


def test_background_health_uses_the_shared_forward_review_validator(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Operational readiness must not substitute for archive eligibility."""

    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(("SZ.000001",)),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
    )
    service.refresh_now()
    with service._background_lock:
        service._background_thread = threading.current_thread()
        service._background_started_at = AS_OF
        service._background_heartbeat_at = AS_OF
        service._background_last_result_at = AS_OF
        service._background_last_error = None

    calls: list[str] = []

    def reject(snapshot):
        calls.append(str(snapshot["snapshot_content_sha256"]))
        raise ValueError("archive boundary rejected the complete snapshot")

    monkeypatch.setattr(
        "cl_app.services.trading_screening.validate_live_review_snapshot",
        reject,
    )
    rejected = service.health_snapshot()

    assert rejected["ready"] is True
    assert rejected["runtime_ready"] is True
    assert rejected["selection_ready"] is False
    assert rejected["selection_status"] == "review_blocked"
    assert rejected["daily_preselection_ready"] is False
    assert rejected["daily_preselection_status"] == "review_blocked"
    assert len(calls) == 1

    def accept(snapshot):
        calls.append(str(snapshot["snapshot_content_sha256"]))
        return AS_OF, ()

    monkeypatch.setattr(
        "cl_app.services.trading_screening.validate_live_review_snapshot",
        accept,
    )
    # A validator implementation is immutable in production.  The test swaps
    # it at runtime, so explicitly invalidate the publication-scoped cache.
    with service._state_lock:
        service._review_readiness_cache_sha256 = None
        service._review_readiness_cache = None
    accepted = service.health_snapshot()

    assert accepted["ready"] is True
    assert accepted["runtime_ready"] is True
    assert accepted["selection_ready"] is True
    assert accepted["selection_status"] == "ready"
    assert accepted["selection_reason_code"] == "READY"
    assert accepted["screening_review_ready"] is True
    assert accepted["screening_review_reason_code"] == "READY"
    assert accepted["daily_preselection_ready"] is True
    assert accepted["daily_preselection_status"] == "ready"
    assert accepted["daily_preselection_candidate_count"] == 0
    assert accepted["daily_preselection_buy_candidate_count"] == 0
    assert accepted["daily_preselection_sell_candidate_count"] == 0
    assert accepted["daily_preselection_refresh_schedule"] == (
        "MON-FRI 15:05 Asia/Shanghai FOR_NEXT_SESSION"
    )
    assert accepted["daily_preselection_reconcile_schedule"] == (
        "MON-FRI 08:45 Asia/Shanghai"
    )
    assert accepted["daily_preselection_capture_schedule"] == (
        "MON-FRI 09:10 Asia/Shanghai"
    )
    assert len(calls) == 2


def test_native_process_health_is_required_and_progress_callback_is_registered(
    tmp_path: Path,
) -> None:
    class NativeHealthMarket(RecordingMarketData):
        def __init__(self) -> None:
            super().__init__()
            self.progress = None
            self.native_ready = False

        def set_progress_callback(self, callback) -> None:
            self.progress = callback

        def health_snapshot(self):
            return {
                "required": True,
                "ready": self.native_ready,
                "status": "ready" if self.native_ready else "not_ready",
                "worker_pid": 1234,
                "real_account_access": False,
                "real_order_transport": False,
            }

    market = NativeHealthMarket()
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(("SZ.000001",)),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
    )

    assert callable(market.progress)
    unavailable = service.health_snapshot()
    assert unavailable["native_gateway"]["worker_pid"] == 1234
    assert "screening_native_gateway_not_ready" in unavailable["reasons"]

    market.native_ready = True
    available = service.health_snapshot()
    assert available["native_gateway"]["ready"] is True
    assert "screening_native_gateway_not_ready" not in available["reasons"]


def test_background_health_detects_one_stuck_native_call_and_recovers(
    tmp_path: Path,
) -> None:
    scan_at = AS_OF + timedelta(minutes=5)
    now = [scan_at]
    entered = threading.Event()
    release = threading.Event()

    class BlockingMarketData(RecordingMarketData):
        def structure_bundle(self, *args, **kwargs):
            entered.set()
            if not release.wait(timeout=2.0):
                raise RuntimeError("test did not release native call")
            return super().structure_bundle(*args, **kwargs)

    service = TradingScreeningService(
        market_data=BlockingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(("SZ.000001",)),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: now[0],
        notifier=None,
        config=TradingScreeningConfig(
            full_coverage_refresh_enabled=True,
            large_scope_authorized=True,
        ),
    )

    service.start_background()
    try:
        assert entered.wait(timeout=1.0)
        active = service.health_snapshot()
        assert active["refresh_in_progress"] is True
        assert active["refresh_started_at"] == scan_at.isoformat()

        now[0] += timedelta(seconds=int(active["heartbeat_max_age_seconds"]) + 1)
        stale = service.health_snapshot()
        assert stale["ready"] is False
        assert "screening_heartbeat_stale" in stale["reasons"]

        release.set()
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            recovered = service.health_snapshot()
            if recovered["ready"] and not recovered["refresh_in_progress"]:
                break
            time.sleep(0.01)
        assert recovered["ready"] is True
        assert recovered["refresh_in_progress"] is False
        assert recovered["refresh_attempt_count"] >= 1
        assert recovered["reasons"] == []
    finally:
        release.set()
        assert service.shutdown_background(wait=True, timeout=1.0) is True


def test_incremental_refresh_preserves_current_signals_for_unscanned_symbols(
    tmp_path: Path,
) -> None:
    service = TradingScreeningService(
        market_data=ActionableMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=HumanAssistedDecisionCore(),
        scan_planner=SequencedPlanner((("SZ.000001",), ())),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
    )

    first = service.refresh_now()
    second = service.refresh_now()

    assert len(first["signals"]) == 1
    assert second["signals"] == first["signals"]


def test_active_monitoring_does_not_starve_new_sector_discovery(
    tmp_path: Path,
) -> None:
    market = ActionableMarketData()
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=RecordingSectorCatalog(),
        engine=HumanAssistedDecisionCore(),
        scan_planner=SequencedPlanner(
            (
                ("SZ.000001",),
                ("SZ.000001", "SZ.000002", "SZ.000003"),
            )
        ),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(
            max_symbols_per_refresh=1,
            max_monitor_symbols_per_refresh=1,
        ),
    )

    service.refresh_now()
    second = service.refresh_now()

    assert market.bundle_codes == ["SZ.000001", "SZ.000001", "SZ.000002"]
    assert second["scan_audit"]["pending_symbol_count"] == 1


class MixedSectorCatalog:
    def __init__(self) -> None:
        self.blocked = replace(
            hostile_sector(),
            sector_id="TDX.880999",
            sector_name="未入选行业",
        )

    def native_sector_assessments(
        self,
        *,
        as_of: datetime,
        admitted_codes=None,
    ):
        del as_of, admitted_codes
        return SectorAssessmentBatch(
            assessments=(eligible_sector(), self.blocked),
            discovered_count=2,
            completed_count=2,
            failure_counts=(),
            errors=(),
        )

    def members(self):
        return {
            eligible_sector().sector_id: ("SZ.000002",),
            self.blocked.sector_id: ("SZ.000001",),
        }


def test_watchlist_signal_keeps_its_native_unselected_sector_context(
    tmp_path: Path,
) -> None:
    catalog = MixedSectorCatalog()
    market_data = ActionableMarketData()
    market_data.active_watchlist = lambda: ("SZ.000001",)
    service = TradingScreeningService(
        market_data=market_data,
        sector_catalog=catalog,
        engine=HumanAssistedDecisionCore(),
        scan_planner=SequencedPlanner((("SZ.000001",),)),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
    )

    payload = service.refresh_now()

    assert payload["signals"][0]["sector"]["sector_id"] == catalog.blocked.sector_id
    assert payload["signals"][0]["sector"]["hard_block"] is True
    assert payload["signals"][0]["selection_sources"] == ["ACTIVE_WATCHLIST_MONITOR"]
    assert payload["signals"][0]["sector_triggered"] is False
    assert payload["signals"][0]["monitor_only"] is True


def test_removed_nontradable_monitor_cannot_reenter_through_previous_signal(
    tmp_path: Path,
) -> None:
    class MutableMonitorMarket(ActionableMarketData):
        def __init__(self) -> None:
            super().__init__()
            self.keep_index = True

        def active_watchlist(self) -> tuple[str, ...]:
            return ("SH.000001",) if self.keep_index else ()

        def tradable_instrument_codes(
            self,
            codes: tuple[str, ...],
        ) -> tuple[str, ...]:
            if self.keep_index:
                return codes
            return tuple(code for code in codes if code != "SH.000001")

        def structure_bundle(self, code: str, **kwargs) -> SymbolStructureBundle:
            return super().structure_bundle(code, **kwargs)

    class PreviousScopePlanner:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, **kwargs) -> ScanPlan:
            self.calls += 1
            if self.calls == 1:
                assert kwargs["active_watchlist"] == ("SH.000001",)
                symbols = ("SH.000001",)
            else:
                assert "SH.000001" not in kwargs["active_watchlist"]
                symbols = tuple(kwargs["active_watchlist"])
            return ScanPlan(
                sectors=(eligible_sector().sector_id,),
                symbols=symbols,
                symbol_frequencies=tuple(
                    (code, ("1m", "5m", "30m")) for code in symbols
                ),
                full_market_history_scan=False,
                background_full_refresh_required=False,
            )

    market = MutableMonitorMarket()
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=MixedSectorCatalog(),
        engine=HumanAssistedDecisionCore(),
        scan_planner=PreviousScopePlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
    )

    first = service.refresh_now()
    assert "SH.000001" in {row["code"] for row in first["signals"]}

    market.keep_index = False
    second = service.refresh_now()

    assert market.bundle_codes.count("SH.000001") == 1
    assert "SH.000001" not in {row["code"] for row in second["signals"]}
    assert "SH.000001" not in second["coverage_manifest"]["discovered_codes"]


def test_nontradable_watchlist_exclusion_is_visible_but_not_discovered(
    tmp_path: Path,
) -> None:
    class RejectedIndexMarket(RecordingMarketData):
        def active_watchlist(self) -> tuple[str, ...]:
            return ("SH.000001",)

        def tradable_instrument_codes(
            self,
            codes: tuple[str, ...],
        ) -> tuple[str, ...]:
            return tuple(code for code in codes if code != "SH.000001")

    market = RejectedIndexMarket()
    cache_path = tmp_path / "snapshot.json"
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(
            admitted_universe_codes=("SZ.000001",),
        ),
    )

    payload = service.refresh_now()

    assert payload["monitor_instrument_exclusion_contract_id"] == (
        "chanlun-monitor-instrument-exclusion"
    )
    assert payload["monitor_instrument_exclusions"] == [
        {
            "code": "SH.000001",
            "eligibility": "EXCLUDED_FROM_TRADING_SCREENING",
            "reason_code": "QMT_NATIVE_STOCK_OR_ETF_REQUIRED",
            "selection_sources": ["ACTIVE_WATCHLIST_MONITOR"],
            "evidence_source": "QMT_GET_INSTRUMENT_TYPE",
            "qmt_instrument_type": "index_cn",
            "diagnostic_only": True,
            "tick_data_used": False,
            "real_account_accessed": False,
            "real_order_transport_enabled": False,
            "live_status": "LIVE_DISABLED",
        }
    ]
    assert payload["scan_audit"]["monitor_instrument_exclusion_count"] == 1
    assert "SH.000001" not in payload["coverage_manifest"]["discovered_codes"]
    assert payload["admitted_universe_codes"] == ["SZ.000001"]
    assert service.health_snapshot()["snapshot_available"] is True
    assert cache_path.with_name(f"{cache_path.name}.scope").is_file()


def test_unresolved_qmt_monitor_type_is_explicit_and_fail_closed(
    tmp_path: Path,
) -> None:
    class UnresolvedTypeMarket(RecordingMarketData):
        def active_watchlist(self) -> tuple[str, ...]:
            return ("SH.600000",)

        def tradable_instrument_codes(
            self,
            codes: tuple[str, ...],
        ) -> tuple[str, ...]:
            return ()

        def screening_instrument_types(
            self,
            codes: tuple[str, ...],
        ) -> dict[str, str]:
            return {code: "unresolved_cn" for code in codes}

    service = TradingScreeningService(
        market_data=UnresolvedTypeMarket(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
    )

    payload = service.refresh_now()

    assert payload["monitor_instrument_exclusions"] == [
        {
            "code": "SH.600000",
            "eligibility": "UNRESOLVED_FROM_TRADING_SCREENING",
            "reason_code": "QMT_NATIVE_INSTRUMENT_TYPE_UNRESOLVED",
            "selection_sources": ["ACTIVE_WATCHLIST_MONITOR"],
            "evidence_source": "QMT_GET_INSTRUMENT_TYPE",
            "qmt_instrument_type": "unresolved_cn",
            "diagnostic_only": True,
            "tick_data_used": False,
            "real_account_accessed": False,
            "real_order_transport_enabled": False,
            "live_status": "LIVE_DISABLED",
        }
    ]
    assert "SH.600000" not in payload["coverage_manifest"]["discovered_codes"]


def test_cache_gate_rejects_rehashed_monitor_diagnostic_forgery(
    tmp_path: Path,
) -> None:
    class RejectedIndexMarket(RecordingMarketData):
        def active_watchlist(self) -> tuple[str, ...]:
            return ("SH.000001",)

        def tradable_instrument_codes(
            self,
            codes: tuple[str, ...],
        ) -> tuple[str, ...]:
            return tuple(code for code in codes if code != "SH.000001")

    service = TradingScreeningService(
        market_data=RejectedIndexMarket(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
    )
    valid = service.refresh_now()
    assert _cache_is_valid(
        valid,
        service._config,
        service._decision_core_id,
        service._selection_research_revision,
    )

    forged = json.loads(json.dumps(valid))
    forged["monitor_instrument_exclusions"][0]["qmt_instrument_type"] = "stock_cn"
    forged["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(forged)

    assert not _cache_is_valid(
        forged,
        service._config,
        service._decision_core_id,
        service._selection_research_revision,
    )


def test_monitor_only_scope_is_advisory_for_buy_and_preserves_sell_exit() -> None:
    buy = {
        "code": "SZ.000001",
        "observed_at": AS_OF.isoformat(),
        "selection_path": "INDIVIDUAL_THREE_PROGRAM",
        "selection_research": None,
        "side": "buy",
        "entry_allowed": True,
        "exit_allowed": False,
        "risk_multiplier": "1.00",
        "decision_reasons": [],
    }
    _apply_selection_scope(buy, ("ACTIVE_WATCHLIST_MONITOR",))

    assert buy["sector_triggered"] is False
    assert buy["monitor_only"] is True
    assert buy["entry_allowed"] is True
    assert buy["risk_multiplier"] == "1.00"
    assert buy["decision_reasons"] == [
        "SIGNED_SELECTION_RESEARCH_REQUIRED",
        "QMT_SECTOR_TRIGGER_REQUIRED",
    ]

    sell = {
        "code": "SZ.000001",
        "observed_at": AS_OF.isoformat(),
        "selection_path": "INDIVIDUAL_THREE_PROGRAM",
        "selection_research": None,
        "side": "sell",
        "entry_allowed": False,
        "exit_allowed": True,
        "risk_multiplier": "0",
        "decision_reasons": ["same_or_higher_sell"],
    }
    _apply_selection_scope(sell, ("VIRTUAL_HOLDING_MONITOR",))

    assert sell["monitor_only"] is True
    assert sell["exit_allowed"] is True
    assert sell["decision_reasons"] == ["same_or_higher_sell"]


def test_large_review_readiness_is_single_flight_and_nonblocking(
    monkeypatch,
) -> None:
    service = object.__new__(TradingScreeningService)
    service._state_lock = threading.RLock()
    service._review_readiness_validation_lock = threading.Lock()
    service._review_readiness_cache_sha256 = None
    service._review_readiness_cache = None
    service._review_readiness_validation_sha256 = None
    service._review_readiness_validation_thread = None
    snapshot_sha256 = "sha256:" + "a" * 64
    snapshot = {
        "snapshot_content_sha256": snapshot_sha256,
        "signals": [{} for _ in range(257)],
    }
    service._snapshot = snapshot
    entered = threading.Event()
    release = threading.Event()

    def slow_validator(_snapshot, *, identity_valid):
        assert identity_valid is True
        entered.set()
        assert release.wait(timeout=2)
        return True, "READY"

    monkeypatch.setattr(
        trading_screening_subject,
        "_screening_review_readiness",
        slow_validator,
    )

    started = time.perf_counter()
    first = service._review_readiness_for_publication(
        snapshot,
        identity_valid=True,
    )
    elapsed = time.perf_counter() - started
    second = service._review_readiness_for_publication(
        snapshot,
        identity_valid=True,
    )

    assert first == (False, "REVIEW_BOUNDARY_VALIDATION_PENDING")
    assert second == first
    assert elapsed < 0.5
    assert entered.wait(timeout=1)
    first_worker = service._review_readiness_validation_thread
    assert first_worker is not None

    release.set()
    first_worker.join(timeout=2)

    assert service._review_readiness_for_publication(
        snapshot,
        identity_valid=True,
    ) == (True, "READY")


def test_large_file_backed_review_validation_uses_child_process(
    monkeypatch,
    tmp_path: Path,
) -> None:
    service = object.__new__(TradingScreeningService)
    service._state_lock = threading.RLock()
    service._review_readiness_validation_lock = threading.Lock()
    service._review_readiness_cache_sha256 = None
    service._review_readiness_cache = None
    service._review_readiness_validation_sha256 = None
    service._review_readiness_validation_thread = None
    service._cache_path = tmp_path / "screening.json"
    service._cache_path.write_text("{}", encoding="utf-8")
    snapshot_sha256 = "sha256:" + "b" * 64
    snapshot = {
        "snapshot_content_sha256": snapshot_sha256,
        "signals": [{} for _ in range(257)],
    }
    service._snapshot = snapshot
    entered = threading.Event()
    release = threading.Event()
    commands: list[list[str]] = []

    def child_validator(command, **kwargs):
        commands.append(command)
        assert kwargs["timeout"] == 600
        entered.set()
        assert release.wait(timeout=2)
        return trading_screening_subject.subprocess.CompletedProcess(
            command,
            0,
            json.dumps(
                {
                    "snapshot_content_sha256": snapshot_sha256,
                    "ready": True,
                    "reason_code": "READY",
                }
            ),
            "",
        )

    monkeypatch.setattr(
        trading_screening_subject.subprocess,
        "run",
        child_validator,
    )
    monkeypatch.setattr(
        trading_screening_subject,
        "_screening_review_readiness",
        lambda *_args, **_kwargs: pytest.fail(
            "large file-backed validation ran inside the Web interpreter"
        ),
    )

    result = service._review_readiness_for_publication(
        snapshot,
        identity_valid=True,
    )
    assert result == (False, "REVIEW_BOUNDARY_VALIDATION_PENDING")
    assert entered.wait(timeout=1)
    assert len(commands) == 1
    assert commands[0][0] == sys.executable
    assert commands[0][-1] == snapshot_sha256

    worker = service._review_readiness_validation_thread
    assert worker is not None
    release.set()
    worker.join(timeout=2)
    assert service._review_readiness_for_publication(
        snapshot,
        identity_valid=True,
    ) == (True, "READY")


def test_large_review_reuses_only_exact_materialization_receipt(
    monkeypatch,
    tmp_path: Path,
) -> None:
    service = object.__new__(TradingScreeningService)
    service._state_lock = threading.RLock()
    service._review_readiness_validation_lock = threading.Lock()
    service._review_readiness_cache_sha256 = None
    service._review_readiness_cache = None
    service._review_readiness_validation_sha256 = None
    service._review_readiness_validation_thread = None
    service._cache_path = tmp_path / "screening.json"
    service._cache_path.write_text("{}", encoding="utf-8")
    archive_root = tmp_path / "live_archive"
    report_hash = "sha256:" + "c" * 64
    report_path = archive_root / "2026-08-03" / f"{report_hash[7:]}.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text("{}", encoding="utf-8")
    snapshot_sha256 = "sha256:" + "b" * 64
    decision_source_id = "sha256:" + "d" * 64
    receipt = live_review_materialization_receipt(
        source_path=service._cache_path,
        source_stat=service._cache_path.stat(),
        source_snapshot_content_sha256=snapshot_sha256,
        report_path=report_path,
        report_stat=report_path.stat(),
        report_content_sha256=report_hash,
        decision_source_snapshot_id=decision_source_id,
        archive_root=archive_root,
    )
    receipt_path = archive_root / ".current_live_review.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    service._human_review_archive_root = archive_root
    service._human_review_decision_source_snapshot_id = decision_source_id
    snapshot = {
        "snapshot_content_sha256": snapshot_sha256,
        "signals": [{} for _ in range(257)],
    }
    service._snapshot = snapshot
    child_calls: list[list[str]] = []

    def child_validator(command, **_kwargs):
        child_calls.append(command)
        return trading_screening_subject.subprocess.CompletedProcess(
            command,
            0,
            json.dumps(
                {
                    "snapshot_content_sha256": snapshot_sha256,
                    "ready": True,
                    "reason_code": "READY",
                }
            ),
            "",
        )

    monkeypatch.setattr(
        trading_screening_subject.subprocess,
        "run",
        child_validator,
    )
    monkeypatch.setattr(
        trading_screening_subject,
        "_screening_review_readiness",
        lambda *_args, **_kwargs: pytest.fail(
            "exact materialization receipt was not reused"
        ),
    )

    assert service._review_readiness_for_publication(
        snapshot,
        identity_valid=True,
    ) == (True, "READY")
    assert child_calls == []
    assert service._review_readiness_validation_thread is None

    # The source semantic hash alone is insufficient: an implementation
    # change invalidates the fast path and starts the isolated validator.
    service._review_readiness_cache_sha256 = None
    service._review_readiness_cache = None
    service._human_review_decision_source_snapshot_id = "sha256:" + "e" * 64
    assert service._review_readiness_for_publication(
        snapshot,
        identity_valid=True,
    ) == (False, "REVIEW_BOUNDARY_VALIDATION_PENDING")
    worker = service._review_readiness_validation_thread
    assert worker is not None
    worker.join(timeout=2)
    assert len(child_calls) == 1
