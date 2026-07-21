"""Incremental, read-only screening service driven only by ``TradingEngine``."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import copy
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
from threading import Lock, RLock, Thread
from typing import Protocol

from chanlun.decision_support.fingerprints import normalize_datetime
from chanlun.decision_support.trading_system.engine import (
    EvaluatedSignal,
    SymbolStructureBundle,
    TradingEngine,
)
from chanlun.decision_support.trading_system.incremental_scan import (
    BarKey,
    ScanCursor,
    ScanPlan,
    build_scan_plan,
)
from chanlun.decision_support.trading_system.models import (
    MAX_FIVE_MINUTE_SETUP_AGE_SECONDS,
    SectorAssessment,
    StructuralPoint,
    TimeframeContext,
)
from chanlun.decision_support.trading_system.portfolio_risk import RiskLimits
from chanlun.decision_support.trading_system.provisional import ProvisionalCandidate
from chanlun.decision_support.trading_system.runtime_config import (
    STRICT_STRATEGY_ID,
)
from chanlun.decision_support.trading_system.sector_policy import rank_sectors
from cl_app.services.trading_screening_gateway import (
    SectorAssessmentBatch,
    _sector_failure_document,
)


SCHEMA_VERSION = "chanlun-trading-screening/v2"
POINT_TYPES = ("1buy", "2buy", "3buy", "1sell", "2sell", "3sell")


def _screening_policy_document() -> dict[str, object]:
    return {
        "latest_per_independent_lane": True,
        "max_five_minute_setup_age_seconds": (
            MAX_FIVE_MINUTE_SETUP_AGE_SECONDS
        ),
        "sector_frequencies": ["30m", "5m"],
        "stock_trigger_frequency": "1m",
    }


class MarketDataGateway(Protocol):
    def changed_bars(self, since: datetime | None) -> tuple[BarKey, ...]: ...

    def active_watchlist(self) -> tuple[str, ...]: ...

    def holdings(self) -> tuple[str, ...]: ...

    def symbol_name(self, code: str) -> str | None: ...

    def structure_bundle(
        self,
        code: str,
        *,
        as_of: datetime,
        sector: SectorAssessment,
        frequencies: tuple[str, ...],
    ) -> SymbolStructureBundle: ...


class SectorCatalogGateway(Protocol):
    def native_sector_assessments(
        self,
        *,
        as_of: datetime,
    ) -> SectorAssessmentBatch: ...

    def members(self) -> Mapping[str, tuple[str, ...]]: ...


class NotificationDispatcher(Protocol):
    def dispatch_changes(
        self,
        previous: Mapping[str, object],
        current: Mapping[str, object],
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class TradingScreeningConfig:
    refresh_interval_seconds: int = 60
    max_selected_sectors: int = 8
    max_visible_symbols: int = 500
    max_symbols_per_refresh: int = 32
    max_monitor_symbols_per_refresh: int = 64
    min_scan_completion_ratio: Decimal = Decimal("0.80")
    max_structure_age_seconds: int = 3600
    algorithm_version: str = STRICT_STRATEGY_ID
    structure_version: str = "v2"
    parameter_version: str = "v1"

    def __post_init__(self) -> None:
        if self.refresh_interval_seconds <= 0:
            raise ValueError("refresh_interval_seconds must be positive")
        if (
            self.max_selected_sectors <= 0
            or self.max_visible_symbols <= 0
            or self.max_symbols_per_refresh <= 0
            or self.max_monitor_symbols_per_refresh <= 0
        ):
            raise ValueError("screening limits must be positive")
        if not Decimal("0") < self.min_scan_completion_ratio <= Decimal("1"):
            raise ValueError("min_scan_completion_ratio must be in (0, 1]")
        if self.max_structure_age_seconds <= 0:
            raise ValueError("max_structure_age_seconds must be positive")
        if not self.algorithm_version:
            raise ValueError("algorithm_version cannot be empty")


def _initial_snapshot(config: TradingScreeningConfig) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "algorithm_version": config.algorithm_version,
        "structure_version": config.structure_version,
        "parameter_version": config.parameter_version,
        "available": False,
        "scan_state": "not_started",
        "generated_at": None,
        "as_of": None,
        "sector_first": True,
        "read_only": True,
        "research_only": True,
        "no_order_execution": True,
        "counts_by_stage": {},
        "counts_by_point_type": {point_type: 0 for point_type in POINT_TYPES},
        "screening_policy": _screening_policy_document(),
        "sectors": [],
        "signals": [],
        "risk_limits": _risk_limits_document(RiskLimits()),
        "scan_audit": {
            "sector_discovered_count": 0,
            "sector_completed_count": 0,
            "sector_failed_count": 0,
            "sector_completion_ratio": "0",
            "sector_failure_counts": {},
            "planned_symbol_count": 0,
            "completed_symbol_count": 0,
            "completion_ratio": "0",
            "full_market_history_scan": False,
            "background_full_refresh_required": True,
        },
        "data_quality": {
            "complete": False,
            "stale": True,
            "failure_codes": ["not_scanned"],
        },
        "backtest_verdict": {
            "live_ready": False,
            "status": "evidence_unavailable",
        },
        "errors": [],
    }


def _risk_limits_document(limits: RiskLimits) -> dict[str, object]:
    return {
        "base_trade_risk": str(limits.base_trade_risk),
        "max_symbol_fraction": str(limits.max_symbol_fraction),
        "max_sector_fraction": str(limits.max_sector_fraction),
        "max_portfolio_heat": str(limits.max_portfolio_heat),
        "first_drawdown": str(limits.first_drawdown),
        "second_drawdown": str(limits.second_drawdown),
        "stop_drawdown": str(limits.stop_drawdown),
    }


def _context_document(
    context: TimeframeContext | None,
) -> dict[str, object] | None:
    if context is None:
        return None
    return {
        "frequency": context.frequency,
        "direction": context.direction,
        "disposition": context.disposition,
        "hard_block": context.hard_block,
        "dominant_point_id": context.dominant_point_id,
        "dominant_point_type": context.dominant_point_type,
        "reason_codes": list(context.reason_codes),
        "observed_at": context.observed_at.isoformat(),
    }


def _sector_document(
    assessment: SectorAssessment,
    *,
    ordinal: int | None,
) -> dict[str, object]:
    return {
        "sector_id": assessment.sector_id,
        "sector_name": assessment.sector_name,
        "eligible": assessment.eligible,
        "hard_block": assessment.hard_block,
        "regime": assessment.regime,
        "rank": ordinal,
        "rank_score": assessment.rank_score,
        "rank_components": dict(assessment.rank_components),
        "reason_codes": list(assessment.reason_codes),
        "context_30m": _context_document(assessment.thirty_context),
        "context_5m": _context_document(assessment.five_context),
        "context_1m": _context_document(assessment.one_context),
    }


def _point_identity(point: StructuralPoint | ProvisionalCandidate) -> str:
    return (
        point.point_id
        if isinstance(point, StructuralPoint)
        else point.candidate_id
    )


def _point_document(
    point: StructuralPoint | ProvisionalCandidate,
) -> dict[str, object]:
    if isinstance(point, ProvisionalCandidate):
        return {
            "point_id": point.candidate_id,
            "point_type": point.point_type,
            "side": point.side,
            "status": point.status,
            "source_frequency": point.source_frequency,
            "tower": point.tower,
            "recursive_level": point.recursive_level,
            "anchor_at": point.observed_at.isoformat(),
            "confirmed_at": None,
            "available_at": point.observed_at.isoformat(),
            "price_basis_revision": None,
            "anchor_price": point.anchor_price,
            "invalidation_price": None,
            "center_id": None,
            "center_zd": None,
            "center_zg": None,
            "center_ordinal": None,
            "variant": None,
            "divergence_kind": None,
            "missing_conditions": list(point.missing_conditions),
            "evidence_codes": list(point.evidence_codes),
        }
    return {
        "point_id": point.point_id,
        "point_type": point.point_type,
        "side": point.side,
        "status": point.status,
        "source_frequency": point.source_frequency,
        "tower": point.tower,
        "recursive_level": point.recursive_level,
        "anchor_at": point.anchor_at.isoformat(),
        "confirmed_at": (
            None if point.confirmed_at is None else point.confirmed_at.isoformat()
        ),
        "available_at": point.available_at.isoformat(),
        "price_basis_revision": point.price_basis_revision,
        "anchor_price": point.structure_anchor_price,
        "invalidation_price": point.structure_invalidation_price,
        "center_id": point.center_id,
        "center_zd": point.center_zd,
        "center_zg": point.center_zg,
        "center_ordinal": point.center_ordinal,
        "variant": point.variant,
        "divergence_kind": point.divergence_kind,
        "missing_conditions": [],
        "evidence_codes": list(point.evidence_codes),
    }


def _chart_urls(code: str) -> dict[str, str]:
    intervals = {"30m": "30", "5m": "5", "1m": "1"}
    return {
        frequency: (
            f"/?market=a&code={code}&layout=single&intervals={interval}"
        )
        for frequency, interval in intervals.items()
    }


def _signal_document(
    item: EvaluatedSignal,
    *,
    previous_stage: str | None,
    name: str | None,
) -> dict[str, object]:
    point = item.setup.point
    trigger = item.trigger
    entry_allowed = item.entry is not None and item.entry.allowed
    exit_allowed = item.exit is not None and item.exit.allowed
    lifecycle_stage = item.lifecycle.stage
    if (entry_allowed or exit_allowed) and previous_stage in {
        "triggered",
        "executable",
        "active",
    }:
        lifecycle_stage = "executable"
    decision_reasons = tuple(
        dict.fromkeys(
            (
                *((item.entry.reason_codes if item.entry is not None else ())),
                *((item.exit.reason_codes if item.exit is not None else ())),
                *item.conflict.reason_codes,
            )
        )
    )
    return {
        "signal_id": item.lifecycle.signal_id,
        "setup_id": item.setup.setup_id,
        "point_id": _point_identity(point),
        "code": point.code,
        "name": name,
        "point_type": point.point_type,
        "side": point.side,
        "tower": point.tower,
        "recursive_level": point.recursive_level,
        "lifecycle_stage": lifecycle_stage,
        "observed_at": item.lifecycle.observed_at.isoformat(),
        "context_30m": {
            "direction": item.setup.context.direction,
            "disposition": item.setup.context.disposition,
            "hard_block": item.setup.context.hard_block,
            "dominant_point_id": item.setup.context.dominant_point_id,
            "dominant_point_type": item.setup.context.dominant_point_type,
            "reason_codes": list(item.setup.context.reason_codes),
        },
        "setup_5m": _point_document(point),
        "trigger_1m": None if trigger is None else _point_document(trigger),
        "sector": _sector_document(item.setup.sector, ordinal=None),
        "structural_stop": (
            None
            if item.entry is None or item.entry.structural_stop is None
            else str(item.entry.structural_stop)
        ),
        "risk_multiplier": (
            "0" if item.entry is None else str(item.entry.risk_multiplier)
        ),
        "entry_allowed": entry_allowed,
        "exit_allowed": exit_allowed,
        "exit_action": "none" if item.exit is None else item.exit.action,
        "decision_reasons": list(decision_reasons),
        "conflict": {
            "hard_block": item.conflict.hard_block,
            "blocking_point_ids": list(item.conflict.blocking_point_ids),
            "risk_only_point_ids": list(item.conflict.risk_only_point_ids),
        },
        "chart_urls": _chart_urls(point.code),
    }


def _cache_is_valid(value: object, config: TradingScreeningConfig) -> bool:
    return bool(
        isinstance(value, Mapping)
        and value.get("schema_version") == SCHEMA_VERSION
        and value.get("algorithm_version") == config.algorithm_version
        and value.get("structure_version") == config.structure_version
        and value.get("parameter_version") == config.parameter_version
        and value.get("read_only") is True
        and value.get("no_order_execution") is True
        and isinstance(value.get("sectors"), list)
        and isinstance(value.get("signals"), list)
        and isinstance(value.get("data_quality"), Mapping)
    )


class TradingScreeningService:
    def __init__(
        self,
        *,
        market_data: MarketDataGateway,
        sector_catalog: SectorCatalogGateway,
        engine: TradingEngine,
        scan_planner: Callable[..., ScanPlan] = build_scan_plan,
        cache_path: Path,
        clock: Callable[[], datetime],
        notifier: NotificationDispatcher | None,
        config: TradingScreeningConfig = TradingScreeningConfig(),
        risk_limits: RiskLimits = RiskLimits(),
        backtest_verdict: Mapping[str, object] | None = None,
    ) -> None:
        self._market_data = market_data
        self._sector_catalog = sector_catalog
        self._engine = engine
        self._scan_planner = scan_planner
        self._cache_path = Path(cache_path)
        self._clock = clock
        self._notifier = notifier
        self._config = config
        self._risk_limits = risk_limits
        self._backtest_verdict = dict(
            backtest_verdict
            or {"live_ready": False, "status": "evidence_unavailable"}
        )
        self._state_lock = RLock()
        self._scan_lock = Lock()
        self._pending_frequencies: dict[str, set[str]] = {}
        self._monitor_offset = 0
        self._snapshot = self._load_valid_cache() or _initial_snapshot(config)
        self._last_as_of = self._cached_as_of(self._snapshot)
        self._cursor = (
            ScanCursor.current(
                structure_version=config.structure_version,
                parameter_version=config.parameter_version,
            )
            if self._last_as_of is not None
            else ScanCursor.empty()
        )

    @staticmethod
    def _cached_as_of(snapshot: Mapping[str, object]) -> datetime | None:
        value = snapshot.get("as_of")
        if not isinstance(value, str):
            return None
        try:
            return normalize_datetime(datetime.fromisoformat(value), "cached as_of")
        except ValueError:
            return None

    def _load_valid_cache(self) -> dict[str, object] | None:
        try:
            value = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        return copy.deepcopy(dict(value)) if _cache_is_valid(value, self._config) else None

    def snapshot(self) -> dict[str, object]:
        with self._state_lock:
            return copy.deepcopy(self._snapshot)

    def _needs_refresh(self) -> bool:
        generated = self._cached_as_of(self._snapshot)
        if generated is None:
            return True
        return normalize_datetime(self._clock(), "clock") - generated >= timedelta(
            seconds=self._config.refresh_interval_seconds
        )

    def ensure_refresh(self) -> bool:
        if not self._needs_refresh() or self._scan_lock.locked():
            return False
        Thread(target=self.refresh_now, daemon=True, name="trading-screening").start()
        return True

    def _persist_atomic(self, payload: Mapping[str, object]) -> None:
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._cache_path.with_suffix(self._cache_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self._cache_path)

    def _take_scan_batch(
        self,
        plan: ScanPlan,
        *,
        priority_codes: tuple[str, ...],
    ) -> tuple[tuple[str, ...], dict[str, tuple[str, ...]]]:
        frequency_order = ("1m", "5m", "30m")
        for code in plan.symbols:
            self._pending_frequencies.setdefault(code, set()).update(
                plan.frequencies_for(code)
            )
        priority = tuple(
            code
            for code in sorted(set(priority_codes))
            if code in self._pending_frequencies
        )
        if priority:
            start = self._monitor_offset % len(priority)
            rotated = priority[start:] + priority[:start]
            monitors = rotated[: self._config.max_monitor_symbols_per_refresh]
            self._monitor_offset = (start + len(monitors)) % len(priority)
        else:
            monitors = ()
            self._monitor_offset = 0
        priority_set = set(priority)
        remaining = tuple(
            code
            for code in sorted(self._pending_frequencies)
            if code not in priority_set
        )
        discovery = remaining[: self._config.max_symbols_per_refresh]
        symbols = monitors + discovery
        frequencies = {
            code: tuple(
                frequency
                for frequency in frequency_order
                if frequency in self._pending_frequencies[code]
            )
            for code in symbols
        }
        for code in symbols:
            self._pending_frequencies.pop(code, None)
        return symbols, frequencies

    def _requeue_symbols(
        self,
        symbols: tuple[str, ...],
        frequencies: Mapping[str, tuple[str, ...]],
    ) -> None:
        for code in symbols:
            self._pending_frequencies.setdefault(code, set()).update(
                frequencies.get(code, ())
            )

    def refresh_now(self) -> dict[str, object]:
        if not self._scan_lock.acquire(blocking=False):
            return self.snapshot()
        try:
            previous = self.snapshot()
            try:
                payload = self._perform_incremental_refresh(previous)
            except Exception as exc:
                payload = copy.deepcopy(dict(previous))
                payload["scan_state"] = "refresh_failed"
                payload["data_quality"] = {
                    "complete": False,
                    "stale": True,
                    "failure_codes": ["refresh_failed"],
                }
                payload["errors"] = [
                    {
                        "error": type(exc).__name__,
                        "reason": str(exc)[:160],
                    }
                ]
                try:
                    self._persist_atomic(payload)
                except OSError:
                    pass
                with self._state_lock:
                    self._snapshot = copy.deepcopy(payload)
                return copy.deepcopy(payload)
            self._persist_atomic(payload)
            with self._state_lock:
                self._snapshot = copy.deepcopy(payload)
            if self._notifier is not None:
                self._notifier.dispatch_changes(previous, payload)
            return copy.deepcopy(payload)
        finally:
            self._scan_lock.release()

    def _perform_incremental_refresh(
        self,
        previous: Mapping[str, object],
    ) -> dict[str, object]:
        as_of = normalize_datetime(self._clock(), "clock")
        sector_batch = self._sector_catalog.native_sector_assessments(as_of=as_of)
        sector_ratio = sector_batch.completion_ratio
        sector_audit: dict[str, object] = {
            "sector_discovered_count": sector_batch.discovered_count,
            "sector_completed_count": sector_batch.completed_count,
            "sector_failed_count": (
                sector_batch.discovered_count - sector_batch.completed_count
            ),
            "sector_completion_ratio": str(sector_ratio),
            "sector_failure_counts": dict(sector_batch.failure_counts),
        }
        sector_errors = [
            _sector_failure_document(item) for item in sector_batch.errors
        ]
        if sector_ratio < self._config.min_scan_completion_ratio:
            failed = copy.deepcopy(dict(previous))
            failed["scan_state"] = "incomplete_not_published"
            previous_audit = failed.get("scan_audit")
            scan_audit = (
                dict(previous_audit) if isinstance(previous_audit, Mapping) else {}
            )
            scan_audit.update(sector_audit)
            failed["scan_audit"] = scan_audit
            failed["data_quality"] = {
                "complete": False,
                "stale": True,
                "failure_codes": ["sector_scan_completion_below_threshold"],
            }
            failed["errors"] = sector_errors
            return failed

        failed_sector_ids = {item.sector_id for item in sector_batch.errors}
        assessments = tuple(
            assessment
            for assessment in sector_batch.assessments
            if assessment.sector_id not in failed_sector_ids
        )
        ranked = rank_sectors(assessments)
        selected = ranked[: self._config.max_selected_sectors]
        selected_by_id = {
            row.assessment.sector_id: row.assessment for row in selected
        }
        all_members = self._sector_catalog.members()
        sector_members = {
            sector_id: tuple(all_members.get(sector_id, ()))
            for sector_id in selected_by_id
        }
        watchlist = self._market_data.active_watchlist()
        holdings = self._market_data.holdings()
        previous_active_codes = tuple(
            sorted(
                {
                    str(row.get("code"))
                    for row in previous.get("signals", ())
                    if isinstance(row, Mapping)
                    and isinstance(row.get("code"), str)
                    and row.get("lifecycle_stage")
                    not in {"closed", "invalidated"}
                }
            )
        )
        priority_codes = tuple(
            sorted(set((*watchlist, *holdings, *previous_active_codes)))
        )
        plan = self._scan_planner(
            changed_bars=self._market_data.changed_bars(self._last_as_of),
            sector_members=sector_members,
            active_watchlist=priority_codes,
            holdings=holdings,
            previous=self._cursor,
            structure_version=self._config.structure_version,
            parameter_version=self._config.parameter_version,
        )
        symbols, batch_frequencies = self._take_scan_batch(
            plan,
            priority_codes=priority_codes,
        )
        previous_signals = {
            str(row.get("signal_id")): row
            for row in previous.get("signals", ())
            if isinstance(row, Mapping) and isinstance(row.get("signal_id"), str)
        }
        signals: list[dict[str, object]] = []
        errors: list[dict[str, str]] = list(sector_errors)
        completed = 0
        completed_codes: set[str] = set()
        sector_by_code: dict[str, SectorAssessment] = {}
        selected_assessments = tuple(row.assessment for row in selected)
        selected_ids = {assessment.sector_id for assessment in selected_assessments}
        assessment_order = selected_assessments + tuple(
            sorted(
                (
                    assessment
                    for assessment in assessments
                    if assessment.sector_id not in selected_ids
                ),
                key=lambda assessment: (
                    not assessment.eligible,
                    -assessment.rank_score,
                    assessment.sector_id,
                ),
            )
        )
        for assessment in assessment_order:
            for member in all_members.get(assessment.sector_id, ()):
                sector_by_code.setdefault(member, assessment)
        for code in symbols:
            sector = sector_by_code.get(
                code,
                SectorAssessment(
                    sector_id="unclassified",
                    sector_name="未匹配原生行业",
                    eligible=False,
                    hard_block=True,
                    regime="hostile",
                    rank_components=(),
                    reason_codes=("sector_membership_missing",),
                ),
            )
            try:
                bundle = self._market_data.structure_bundle(
                    code,
                    as_of=as_of,
                    sector=sector,
                    frequencies=batch_frequencies.get(code, ()),
                )
                age = as_of - bundle.as_of
                if age < timedelta(0) or age > timedelta(
                    seconds=self._config.max_structure_age_seconds
                ):
                    raise ValueError("structure_bundle_stale")
                evaluated = self._engine.evaluate_symbol(bundle)
                name_provider = getattr(self._market_data, "symbol_name", None)
                symbol_name = name_provider(code) if callable(name_provider) else None
                for item in evaluated:
                    previous_stage = None
                    previous_row = previous_signals.get(item.lifecycle.signal_id)
                    if isinstance(previous_row, Mapping):
                        stage = previous_row.get("lifecycle_stage")
                        previous_stage = stage if isinstance(stage, str) else None
                    signals.append(
                        _signal_document(
                            item,
                            previous_stage=previous_stage,
                            name=symbol_name,
                        )
                    )
                completed += 1
                completed_codes.add(code)
            except Exception as exc:
                errors.append(
                    {
                        "code": code,
                        "error_type": "stock_analysis_error",
                        "reason": str(exc)[:160],
                    }
                )
        planned_count = len(symbols)
        completion = (
            Decimal("1")
            if planned_count == 0
            else Decimal(completed) / Decimal(planned_count)
        )
        if completion < self._config.min_scan_completion_ratio:
            self._requeue_symbols(symbols, batch_frequencies)
            failed = copy.deepcopy(dict(previous))
            failed["scan_state"] = "incomplete_not_published"
            failed["errors"] = errors
            previous_audit = failed.get("scan_audit")
            scan_audit = (
                dict(previous_audit) if isinstance(previous_audit, Mapping) else {}
            )
            scan_audit.update(sector_audit)
            scan_audit.update(
                {
                    "planned_symbol_count": planned_count,
                    "completed_symbol_count": completed,
                    "completion_ratio": str(completion),
                }
            )
            failed["scan_audit"] = scan_audit
            failed["data_quality"] = {
                "complete": False,
                "stale": True,
                "failure_codes": ["scan_completion_below_threshold"],
            }
            return failed

        failed_codes = tuple(code for code in symbols if code not in completed_codes)
        self._requeue_symbols(failed_codes, batch_frequencies)
        failure_codes = []
        if sector_batch.errors:
            failure_codes.append("sector_scan_partial")
        if failed_codes:
            failure_codes.append("stock_scan_partial")
        retained_scope = {
            member for members in sector_members.values() for member in members
        }.union(watchlist, holdings)
        retained = [
            copy.deepcopy(dict(row))
            for row in previous.get("signals", ())
            if isinstance(row, Mapping)
            and isinstance(row.get("code"), str)
            and row.get("code") not in completed_codes
            and row.get("code") in retained_scope
        ]
        signals = retained + signals

        signals.sort(
            key=lambda row: (
                POINT_TYPES.index(str(row["point_type"])),
                str(row["code"]),
                str(row["signal_id"]),
            )
        )
        counts_by_stage: dict[str, int] = {}
        counts_by_point = {point_type: 0 for point_type in POINT_TYPES}
        for row in signals:
            stage = str(row["lifecycle_stage"])
            counts_by_stage[stage] = counts_by_stage.get(stage, 0) + 1
            counts_by_point[str(row["point_type"])] += 1
        ranked_ordinals = {
            row.assessment.sector_id: row.ordinal for row in ranked
        }
        payload = {
            "schema_version": SCHEMA_VERSION,
            "algorithm_version": self._config.algorithm_version,
            "structure_version": self._config.structure_version,
            "parameter_version": self._config.parameter_version,
            "available": True,
            "scan_state": "complete",
            "generated_at": as_of.isoformat(),
            "as_of": as_of.isoformat(),
            "sector_first": True,
            "read_only": True,
            "research_only": True,
            "no_order_execution": True,
            "counts_by_stage": dict(sorted(counts_by_stage.items())),
            "counts_by_point_type": counts_by_point,
            "screening_policy": _screening_policy_document(),
            "sectors": [
                _sector_document(
                    assessment,
                    ordinal=ranked_ordinals.get(assessment.sector_id),
                )
                for assessment in sorted(
                    sector_batch.assessments,
                    key=lambda row: (
                        ranked_ordinals.get(row.sector_id, 10**9),
                        row.sector_id,
                    ),
                )
            ],
            "signals": signals,
            "risk_limits": _risk_limits_document(self._risk_limits),
            "scan_audit": {
                **sector_audit,
                "planned_symbol_count": planned_count,
                "discovered_symbol_count": len(plan.symbols),
                "completed_symbol_count": completed,
                "pending_symbol_count": len(self._pending_frequencies),
                "coverage_cycle_complete": not self._pending_frequencies,
                "completion_ratio": str(completion),
                "full_market_history_scan": plan.full_market_history_scan,
                "background_full_refresh_required": (
                    plan.background_full_refresh_required
                ),
                "selected_sector_count": len(selected),
                "planned_frequencies": {
                    code: list(batch_frequencies.get(code, ())) for code in symbols
                },
            },
            "data_quality": {
                "complete": not errors,
                "stale": False,
                "failure_codes": failure_codes,
            },
            "backtest_verdict": copy.deepcopy(self._backtest_verdict),
            "errors": errors,
        }
        self._last_as_of = as_of
        self._cursor = ScanCursor.current(
            structure_version=self._config.structure_version,
            parameter_version=self._config.parameter_version,
        )
        return payload


__all__ = [
    "MarketDataGateway",
    "NotificationDispatcher",
    "POINT_TYPES",
    "SCHEMA_VERSION",
    "SectorCatalogGateway",
    "TradingScreeningConfig",
    "TradingScreeningService",
]
