from __future__ import annotations

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
    _next_background_active_start,
    _next_full_coverage_active_start,
    _priority_buy_candidate_codes,
    _priority_monitor_delay_seconds,
    _priority_monitor_session_open,
    _take_due_candidate_batch,
    _sector_source_evidence_complete,
)
from cl_app.services.trading_notifications import SignalNotificationDispatcher
from cl_app.services.trading_screening_gateway import (
    SectorAnalysisExclusion,
    SectorAnalysisFailure,
    SectorAssessmentBatch,
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
        self.member_calls = 0
        self.batch = batch or SectorAssessmentBatch(
            assessments=(eligible_sector(),),
            discovered_count=1,
            completed_count=1,
            failure_counts=(),
            errors=(),
        )

    def native_sector_assessments(self, *, as_of: datetime):
        calls = getattr(self, "assessment_calls", None)
        if calls is not None:
            calls.append(as_of)
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

    def native_sector_assessments(self, *, as_of: datetime):
        batch = super().native_sector_assessments(as_of=as_of)
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


def test_stock_structure_requests_use_configured_parallel_workers(
    tmp_path: Path,
) -> None:
    symbols = tuple(f"SZ.{index:06d}" for index in range(1, 7))
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
            stock_worker_count=3,
        ),
    )

    payload = service.refresh_now()

    assert market.max_active >= 2
    assert set(market.bundle_codes) == set(symbols)
    assert payload["scan_audit"]["completed_symbol_count"] == len(symbols)
    assert payload["scan_audit"]["stock_worker_count"] == 3


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
    assert not tuple(tmp_path.glob(".snapshot.json.*.tmp"))


def test_service_recovers_corrupt_primary_from_content_addressed_generation(
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

    assert (
        recovered.snapshot()["snapshot_content_sha256"]
        == (expected["snapshot_content_sha256"])
    )
    health = recovered.health_snapshot()
    assert health["cache_recovered_from_generation"] == str(generations[0])
    assert health["cache_generation_count"] == 1
    assert health["cache_generation_error"] is None


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
        "max_five_minute_setup_age_seconds": 345600,
        "sector_catalog_source": "qmt_gics3_components",
        "sector_price_source": "qmt_gics3_component_composite",
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
        "sector_scope": "all_eligible",
        "stock_scope": "all_members_of_all_eligible_sectors",
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
        "stock_trigger_frequency": "1m",
        "minimum_market_data_frequency": "1m",
        "qmt_one_minute_grid_revision": (
            "QMT_A_SHARE_END_LABELLED_241_TO_COMPLETED_240_TRADE_AWARE"
        ),
        "tick_data_used": False,
        "selection_universe_source": "qmt_gics3_current_components",
        "monitor_instrument_eligibility": ("qmt_native_stock_or_etf_fail_closed"),
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


def test_virtual_holding_is_attached_to_physical_decision_bundle(
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
    assert engine.bundles[0].held_tower == "formal"
    assert engine.bundles[0].held_level == 0


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
    assert health["quarantined_cache_reason"] == "CURRENT_CACHE_CONTRACT_INVALID"


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
    assert {row["code"] for row in current["signals"]} == {"SZ.000001"}

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
        config=TradingScreeningConfig(max_symbols_per_refresh=3),
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
        config=TradingScreeningConfig(max_symbols_per_refresh=3),
    )
    assert restarted.snapshot()["scan_state"] == "incomplete_not_published"
    second = restarted.refresh_now()

    assert first["scan_state"] == "complete"
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
    market = ClusteredFailureMarket()
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(symbols),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
        config=TradingScreeningConfig(max_symbols_per_refresh=7),
    )

    first = service.refresh_now()

    assert first["scan_state"] == "incomplete_not_published"
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

    second = service.refresh_now()

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


def test_market_data_rejection_has_stable_reason_and_epoch_retry_policy(
    tmp_path: Path,
) -> None:
    service = TradingScreeningService(
        market_data=UnavailableKlineMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(
            tuple(f"SZ.{index:06d}" for index in range(1, 6))
        ),
        cache_path=tmp_path / "snapshot.json",
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
    def native_sector_assessments(self, *, as_of: datetime):
        del as_of
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

        def native_sector_assessments(self, *, as_of: datetime):
            if self.fail:
                raise RuntimeError("same-epoch sector transport failure")
            return super().native_sector_assessments(as_of=as_of)

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
    assert "screening_background_error" in failed_health["reasons"]

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
        return SymbolStructureBundle(
            code=code,
            as_of=as_of,
            sector=sector,
            thirty_direction="neutral",
            thirty_points=(),
            five_points=(confirmed_point("2buy"),),
            one_points=(confirmed_point("1buy", frequency="1m", minutes_after=1),),
            opposite_points=(),
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
            five_points=(confirmed_point("2buy", minutes_after=295),),
            one_points=(
                confirmed_point(
                    "1buy",
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
            five_points=(provisional_point("2buy"),),
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


def test_snapshot_exposes_exact_session_gap_without_claiming_suspension(
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

    assert signal["entry_allowed"] is False
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

    assert signal["entry_allowed"] is False
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
    assert visible_signal["setup_5m"]["status"] == (full_signal["setup_5m"]["status"])
    assert visible_signal["warmup"]["converged"] == (full_signal["warmup"]["converged"])
    assert "decision_core_id" not in visible_signal
    full_size = len(json.dumps(full, ensure_ascii=False))
    visible_size = len(json.dumps(presentation, ensure_ascii=False))
    assert visible_size < full_size * 0.9

    visible_signal["code"] = "MUTATED"
    again = service.presentation_snapshot()
    assert again["signals"][0]["code"] == "SZ.000001"
    assert service.snapshot()["signals"][0]["code"] == "SZ.000001"


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
        == confirmed_point("2buy").available_at.isoformat()
    )
    assert signal["setup_5m"]["price_basis_revision"] == "test-raw"
    assert signal["setup_5m"]["tower"] == "formal"
    assert (
        signal["trigger_1m"]["available_at"]
        == confirmed_point(
            "1buy", frequency="1m", minutes_after=1
        ).available_at.isoformat()
    )


class PersistenceAssertingNotifier:
    def __init__(self, cache_path: Path) -> None:
        self.cache_path = cache_path
        self.calls = 0

    def dispatch_changes(self, previous, current) -> None:
        del previous
        persisted = json.loads(self.cache_path.read_text(encoding="utf-8"))
        assert persisted == current
        self.calls += 1


def test_notifier_runs_only_after_snapshot_persistence(tmp_path: Path) -> None:
    cache_path = tmp_path / "snapshot.json"
    notifier = PersistenceAssertingNotifier(cache_path)
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=notifier,
    )

    service.refresh_now()

    assert notifier.calls == 1


def test_incomplete_frozen_coverage_never_emits_realtime_notification(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "snapshot.json"
    notifier = PersistenceAssertingNotifier(cache_path)
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
    notifier = PersistenceAssertingNotifier(cache_path)
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
        "bar-cadence-live-candidate-monitor"
    )
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


def test_candidate_health_reports_insufficient_configured_cadence_capacity(
    tmp_path: Path,
) -> None:
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
        notifier=None,
        config=TradingScreeningConfig(
            priority_monitoring_enabled=True,
            max_five_minute_candidate_symbols_per_refresh=3,
            max_thirty_minute_candidate_symbols_per_refresh=3,
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
        ),
    )
    previous = {
        "signals": [
            {
                "signal_id": "armed-buy",
                "code": symbols[0],
                "point_type": "1buy",
                "lifecycle_stage": "armed",
            },
            {
                "signal_id": "formed-buy",
                "code": symbols[1],
                "point_type": "2buy",
                "lifecycle_stage": "formed",
            },
        ]
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
    assert health["candidate_monitor_five_minute"]["current_count"] == 2
    assert health["candidate_monitor_five_minute"]["missing_count"] == 0
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


def test_priority_buy_candidates_exclude_unowned_sell_only_signals() -> None:
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
    candidates = _priority_buy_candidate_codes(
        rows,
        excluded_codes=frozenset({"WATCHED_BUY"}),
    )

    assert candidates == (
        "BUY_EXECUTABLE",
        "BUY_ARMED",
        "BUY_APPROACHING",
        "BUY_APPROACHING_B",
    )
    urgent = _priority_buy_candidate_codes(
        rows,
        excluded_codes=frozenset({"WATCHED_BUY"}),
        allowed_stages=frozenset({"armed", "triggered", "executable", "active"}),
    )
    assert urgent == ("BUY_EXECUTABLE", "BUY_ARMED")


def test_priority_state_prunes_only_unowned_sell_overlay_documents(
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
    documents = {
        "unowned-sell": {
            "signal_id": "unowned-sell",
            "code": "SELL_ONLY",
            "point_type": "3sell",
            "lifecycle_stage": "armed",
        },
        "watched-sell": {
            "signal_id": "watched-sell",
            "code": "WATCHED",
            "point_type": "3sell",
            "lifecycle_stage": "armed",
        },
        "buy": {
            "signal_id": "buy",
            "code": "BUY",
            "point_type": "3buy",
            "lifecycle_stage": "approaching",
        },
    }
    service._priority_monitor_latest_documents = {
        key: dict(value) for key, value in documents.items()
    }
    service._priority_monitor_signal_stages = {
        key: str(value["lifecycle_stage"]) for key, value in documents.items()
    }
    service._priority_monitor_signal_codes = {
        key: str(value["code"]) for key, value in documents.items()
    }

    service._prune_unowned_sell_priority_state(
        mandatory_codes=frozenset({"WATCHED"}),
    )

    assert set(service._priority_monitor_latest_documents) == {"watched-sell", "buy"}
    assert set(service._priority_monitor_signal_stages) == {"watched-sell", "buy"}
    assert set(service._priority_monitor_signal_codes) == {"watched-sell", "buy"}


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

    assert second["scan_state"] == "complete"
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
            setup = confirmed_point("2buy", minutes_after=295)
            trigger = confirmed_point(
                "1buy",
                frequency="1m",
                minutes_after=298,
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

            boundary = EntryExecutionBoundary(
                symbol=code,
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
                higher_timeframe_gates=HigherTimeframeGateBundle(
                    market=green("SH.000300"),
                    sector=green(sector.sector_id),
                    symbol=green(code),
                ),
                enforce_higher_timeframe_entry_gate=True,
                warmup_converged=True,
                enforce_warmup_entry_gate=True,
                physical_timeframe_recursive=True,
                entry_execution_boundaries=(boundary,),
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


def test_candidate_cadence_lane_cannot_emit_realtime_notification(
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
    assert current_notification["signals"] == []
    assert current_notification["notification_authoritative_codes"] == []
    [overlay] = service.presentation_snapshot()["signals"]
    assert overlay["observation_lane"] == "CANDIDATE_CURRENT_5M"
    assert overlay["realtime_observation"] is False


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
        config=TradingScreeningConfig(refresh_interval_seconds=60),
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


def test_full_coverage_refresh_is_closed_on_official_weekday_holiday() -> None:
    assert (
        _full_coverage_refresh_window_open(
            datetime(2026, 10, 1, 15, 5, tzinfo=AS_OF.tzinfo)
        )
        is False
    )


def test_priority_monitor_delay_is_measured_start_to_start() -> None:
    started_at = datetime(2026, 7, 20, 10, 0, tzinfo=AS_OF.tzinfo)
    assert (
        _priority_monitor_delay_seconds(
            started_at + timedelta(seconds=52),
            started_at,
            interval_seconds=60,
        )
        == 8
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
        config=TradingScreeningConfig(refresh_interval_seconds=0.05),
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
        config=TradingScreeningConfig(refresh_interval_seconds=0.01),
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
        config=TradingScreeningConfig(refresh_interval_seconds=60),
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
    assert calls == [(False, expected_priority_only)]


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
        config=TradingScreeningConfig(refresh_interval_seconds=60),
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
        config=TradingScreeningConfig(refresh_interval_seconds=3600),
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
        "screening_snapshot_unavailable",
    ]

    worker = service.start_background()
    try:
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            running = service.health_snapshot()
            if running["ready"]:
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

    assert tampered["ready"] is False
    assert tampered["snapshot_content_sha256"] is None
    assert "screening_snapshot_identity_missing" in tampered["reasons"]

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

    assert noncurrent_but_self_hashed["ready"] is False
    assert noncurrent_but_self_hashed["snapshot_content_sha256"] is None
    assert (
        "screening_snapshot_identity_missing" in (noncurrent_but_self_hashed["reasons"])
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

    assert forged_but_self_hashed["ready"] is False
    assert forged_but_self_hashed["snapshot_content_sha256"] is None
    assert "screening_snapshot_identity_missing" in (forged_but_self_hashed["reasons"])


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
    assert accepted["screening_review_ready"] is True
    assert accepted["screening_review_reason_code"] == "READY"
    assert accepted["daily_preselection_ready"] is True
    assert accepted["daily_preselection_status"] == "ready"
    assert accepted["daily_preselection_candidate_count"] == 0
    assert accepted["daily_preselection_buy_candidate_count"] == 0
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

    def native_sector_assessments(self, *, as_of: datetime):
        del as_of
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
            bundle = super().structure_bundle(code, **kwargs)

            def for_code(point):
                return replace(
                    point,
                    code=code,
                    point_id=f"{point.point_id}:{code}",
                )

            return replace(
                bundle,
                five_points=tuple(for_code(point) for point in bundle.five_points),
                one_points=tuple(for_code(point) for point in bundle.one_points),
            )

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
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
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


def test_monitor_only_scope_blocks_new_buy_but_preserves_sell_exit() -> None:
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
    assert buy["entry_allowed"] is False
    assert buy["risk_multiplier"] == "0"
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
