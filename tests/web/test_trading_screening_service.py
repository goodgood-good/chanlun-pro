from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
import threading
import time

from chanlun.decision_support.trading_system.engine import (
    SymbolStructureBundle,
    TradingEngine,
)
from chanlun.decision_support.trading_system.incremental_scan import ScanPlan
from tests.trading_system.helpers import (
    AS_OF,
    confirmed_point,
    eligible_sector,
    hostile_sector,
    provisional_point,
)
from cl_app.services.trading_screening import (
    TradingScreeningConfig,
    TradingScreeningService,
)
from cl_app.services.trading_screening_gateway import (
    SectorAnalysisFailure,
    SectorAssessmentBatch,
)


class RecordingMarketData:
    def __init__(self) -> None:
        self.bundle_codes: list[str] = []

    def changed_bars(self, since: datetime | None):
        del since
        return ()

    def active_watchlist(self) -> tuple[str, ...]:
        return ()

    def holdings(self) -> tuple[str, ...]:
        return ()

    def symbol_name(self, code: str) -> str | None:
        return {"SZ.000001": "平安银行"}.get(code)

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
            five_points=(),
            one_points=(),
            opposite_points=(),
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


class RecordingEngine:
    def __init__(self) -> None:
        self.codes: list[str] = []

    def evaluate_symbol(self, bundle: SymbolStructureBundle):
        self.codes.append(bundle.code)
        return ()


class RecordingPlanner:
    def __init__(self, symbols: tuple[str, ...] = ("SZ.000001",)) -> None:
        self.calls = 0
        self.symbols = symbols

    def __call__(self, **kwargs) -> ScanPlan:
        self.calls += 1
        assert kwargs["sector_members"] == {
            eligible_sector().sector_id: ("SZ.000001",)
        }
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
            symbol_frequencies=tuple(
                (code, ("1m", "5m", "30m")) for code in symbols
            ),
            full_market_history_scan=False,
            background_full_refresh_required=False,
        )


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

    assert payload["schema_version"] == "chanlun-trading-screening/v3"
    assert payload["structure_version"] == "v3"
    assert payload["sector_first"] is True
    assert payload["read_only"] is True
    assert payload["no_order_execution"] is True
    assert payload["screening_policy"] == {
        "latest_per_independent_lane": True,
        "max_five_minute_setup_age_seconds": 345600,
        "sector_catalog_source": "qmt_gics3_components",
        "sector_price_source": "qmt_gics3_component_composite",
        "sector_composite_member_limit": 24,
        "sector_scope": "all_eligible",
        "stock_scope": "all_members_of_all_eligible_sectors",
        "sector_frequencies": ["30m", "5m"],
        "stock_trigger_frequency": "1m",
    }
    assert payload["scan_audit"]["full_market_history_scan"] is False
    assert planner.calls == 1
    assert market.bundle_codes == ["SZ.000001"]
    assert engine.codes == ["SZ.000001"]


def test_sector_infrastructure_failures_below_gate_keep_previous_snapshot(
    tmp_path: Path,
) -> None:
    catalog = RecordingSectorCatalog()
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=catalog,
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
    )
    previous = service.refresh_now()
    successful = tuple(
        replace(eligible_sector(), sector_id=f"TDX.88030{index}")
        for index in range(7)
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
    assert [sector["rank"] for sector in payload["sectors"]] == list(
        range(1, 13)
    )


def test_cache_with_another_schema_is_rejected(tmp_path: Path) -> None:
    cache_path = tmp_path / "snapshot.json"
    cache_path.write_text(
        json.dumps(
            {
                "schema_version": "chanlun-early-screening/v13",
                "algorithm_version": "chanlun-original-low-drawdown/v1",
                "read_only": True,
                "no_order_execution": True,
                "sectors": [],
                "signals": [{"signal_id": "legacy"}],
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
    assert snapshot["schema_version"] == "chanlun-trading-screening/v3"
    assert snapshot["structure_version"] == "v3"
    assert snapshot["scan_state"] == "not_started"
    assert snapshot["signals"] == []


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
            "reason": "fixture failure",
        }
    ]


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
            one_points=(
                confirmed_point("1buy", frequency="1m", minutes_after=1),
            ),
            opposite_points=(),
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
        engine=TradingEngine(),
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


def test_signal_identity_survives_service_restart(tmp_path: Path) -> None:
    cache_path = tmp_path / "snapshot.json"
    first_service = TradingScreeningService(
        market_data=ActionableMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=TradingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=cache_path,
        clock=lambda: AS_OF,
        notifier=None,
    )
    first = first_service.refresh_now()
    second_service = TradingScreeningService(
        market_data=ActionableMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=TradingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=cache_path,
        clock=lambda: AS_OF + timedelta(minutes=1),
        notifier=None,
    )

    second = second_service.refresh_now()

    assert len(first["signals"]) == len(second["signals"]) == 1
    assert first["signals"][0]["signal_id"] == second["signals"][0]["signal_id"]
    assert first["signals"][0]["lifecycle_stage"] == "triggered"
    assert second["signals"][0]["lifecycle_stage"] == "executable"
    assert second["signals"][0]["chart_urls"] == {
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
        engine=TradingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
    )

    [signal] = service.refresh_now()["signals"]

    assert signal["setup_5m"]["available_at"] == confirmed_point(
        "2buy"
    ).available_at.isoformat()
    assert signal["setup_5m"]["price_basis_revision"] == "test-raw-v1"
    assert signal["setup_5m"]["tower"] == "formal"
    assert signal["trigger_1m"]["available_at"] == confirmed_point(
        "1buy", frequency="1m", minutes_after=1
    ).available_at.isoformat()


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


def test_service_batches_discovered_symbols_without_losing_pending_scope(
    tmp_path: Path,
) -> None:
    market = RecordingMarketData()
    service = TradingScreeningService(
        market_data=market,
        sector_catalog=RecordingSectorCatalog(),
        engine=RecordingEngine(),
        scan_planner=SequencedPlanner(
            (("SZ.000001", "SZ.000002", "SZ.000003"), ())
        ),
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


def test_pending_cycle_is_drained_without_replanning_active_symbols(
    tmp_path: Path,
) -> None:
    market = RecordingMarketData()
    market.active_watchlist = lambda: ("SZ.000001",)
    sectors = RecordingSectorCatalog()
    planner = RecordingPlanner(
        ("SZ.000001", "SZ.000002", "SZ.000003")
    )
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
        clock=lambda: AS_OF,
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


def test_incremental_refresh_preserves_current_signals_for_unscanned_symbols(
    tmp_path: Path,
) -> None:
    service = TradingScreeningService(
        market_data=ActionableMarketData(),
        sector_catalog=RecordingSectorCatalog(),
        engine=TradingEngine(),
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
        engine=TradingEngine(),
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
    service = TradingScreeningService(
        market_data=ActionableMarketData(),
        sector_catalog=catalog,
        engine=TradingEngine(),
        scan_planner=SequencedPlanner((("SZ.000001",),)),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
    )

    payload = service.refresh_now()

    assert payload["signals"][0]["sector"]["sector_id"] == catalog.blocked.sector_id
    assert payload["signals"][0]["sector"]["hard_block"] is True
