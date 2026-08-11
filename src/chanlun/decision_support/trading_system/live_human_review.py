"""Causal adapter from the shared live decision document to human review.

The staged scanner and historical replay already share the same
``HumanAssistedDecisionCore``.  This module is the only place allowed to turn
one of those canonical decision documents into a review alert.  It preserves
the 30m strategic context, 5m setup and 1m locator distinction and never
creates an order-capable object.
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass, replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import re
from typing import Mapping
from zoneinfo import ZoneInfo

from chanlun.core.strict_structure.base_profile import STRICT_STROKE_MODE
from chanlun.decision_support.fingerprints import normalize_datetime, sha256_json
from chanlun.decision_support.trading_system.decision_source_provenance import (
    decision_source_snapshot_id,
)
from chanlun.decision_support.trading_system.human_assisted_decision import (
    validate_signal_decision_document,
    validate_human_assisted_contract_document,
)
from chanlun.decision_support.trading_system.higher_timeframe_gate import (
    HIGHER_TIMEFRAME_SESSION_EVIDENCE_CONTRACT_ID,
    QMT_SECTOR_NATIVE_DAILY_RESEARCH_SOURCE_MODE,
    QMT_SECTOR_SAME_BASE_SOURCE_MODE,
    HigherTimeframePeriodDiagnostic,
    QmtSectorSameBaseCoverageEvidence,
    sector_native_daily_research_bridge_contract,
)
from chanlun.decision_support.trading_system.models import (
    EntryExecutionBoundary,
    parse_entry_execution_boundary_document,
)
from chanlun.decision_support.trading_system.screening_warmup import (
    SCREENING_QMT_30M_FALLBACK_REASON_CODE,
    SCREENING_WARMUP_DIFFERENCE_CODES,
    SCREENING_WARMUP_FREQUENCIES,
    SCREENING_WARMUP_REQUIRED_BARS,
    expected_screening_warmup_suffix_bar_count,
    screening_warmup_reason_code,
)
from chanlun.decision_support.trading_system.sector_strength import (
    SectorStrengthBatch,
    sector_strength_batch_from_evidence_document,
)
from chanlun.decision_support.trading_system.human_review_screening import (
    HUMAN_REVIEW_SCREEN_SCHEMA,
    MONITOR_ONLY_BUY_REASON_CODE,
    MONITOR_ONLY_WARNING_CODE,
    HumanReviewAlert,
    HUMAN_REVIEW_ALERT_TYPES,
    human_review_alert_document,
    human_review_screening_parameters,
    market_symbol_higher_timeframe_review_evidence_from_risk,
    review_priority,
    sector_higher_timeframe_review_evidence_from_risk,
    sector_ranking_review_evidence_from_live_sector,
)
from chanlun.decision_support.trading_system.selection import (
    HIGHER_TIMEFRAME_RISK_STATES,
    higher_timeframe_risk_gate,
)
from chanlun.decision_support.trading_system.etf_proxy_facts import (
    RiskMappingSupplyFacts,
)
from chanlun.decision_support.trading_system.qmt_same_base_stream import (
    QmtMinuteSessionIssue,
)
from chanlun.decision_support.trading_system.qmt_higher_timeframe import (
    QMT_HIGHER_TIMEFRAME_WARMUP_EVIDENCE_CONTRACT_ID,
    QMT_HIGHER_TIMEFRAME_WARMUP_CONVERGENCE_PARAMETER_SET_ID,
    QMT_HIGHER_TIMEFRAME_WARMUP_REQUIRED_DAILY_BARS,
)
from chanlun.decision_support.trading_system.qmt_native_daily_bridge import (
    QMT_NATIVE_DAILY_CALENDAR_COVERAGE_EVIDENCE_CONTRACT_ID,
    QMT_NATIVE_DAILY_RECONCILIATION_CONTRACT_ID,
    QmtNativeDailyCalendarCoverageEvidence,
)
from chanlun.decision_support.trading_system.warmup_convergence import (
    WARMUP_CONVERGENCE_DIAGNOSTIC_CONTRACT_ID,
    WARMUP_CONVERGENCE_ENVELOPE_CONTRACT_ID,
    WARMUP_MAPPING_SUPPLY_DIAGNOSTIC_CONTRACT_ID,
    WarmupConvergenceDiagnosticEnvelope,
    WarmupConvergenceEnvelope,
    WarmupMappingSupplyDiagnosticEnvelope,
)
from chanlun.decision_support.trading_system.warmup_structure_lineage import (
    WARMUP_STRUCTURE_LINEAGE_DIAGNOSTIC_CONTRACT_ID,
    WarmupStructureLineageDiagnosticEnvelope,
)


LIVE_SCREENING_SCHEMA = "chanlun-trading-screening"
SIGNAL_DOCUMENT_CONTRACT_ID = "chanlun-strict-human-assisted-signal-document"
_GATES = frozenset({"GREEN", "AMBER", "RED", "UNRESOLVED"})
_RISK_PERIODS = ("M", "W", "D")
_RISK_DECISION_FIELDS = frozenset(
    {
        "market_gate",
        "sector_gate",
        "symbol_gate",
        "market_states",
        "sector_states",
        "symbol_states",
        "market_reason_codes",
        "sector_reason_codes",
        "symbol_reason_codes",
        "reason_codes",
        "market_period_diagnostics",
        "sector_period_diagnostics",
        "symbol_period_diagnostics",
        "new_entry_requires_all_green",
    }
)
_RISK_CURRENT_EVIDENCE_FIELDS = frozenset(
    {
        "session_evidence_contract_id",
        "market_session_evidence",
        "sector_session_evidence",
        "symbol_session_evidence",
        "warmup_evidence_contract_id",
        "market_warmup_evidence",
        "sector_warmup_evidence",
        "symbol_warmup_evidence",
        "warmup_convergence_contract_id",
        "market_warmup_convergence_evidence",
        "sector_warmup_convergence_evidence",
        "symbol_warmup_convergence_evidence",
        "warmup_convergence_diagnostic_contract_id",
        "market_warmup_convergence_diagnostic_evidence",
        "sector_warmup_convergence_diagnostic_evidence",
        "symbol_warmup_convergence_diagnostic_evidence",
        "warmup_mapping_supply_diagnostic_contract_id",
        "market_warmup_mapping_supply_diagnostic_evidence",
        "sector_warmup_mapping_supply_diagnostic_evidence",
        "symbol_warmup_mapping_supply_diagnostic_evidence",
        "warmup_structure_lineage_diagnostic_contract_id",
        "market_warmup_structure_lineage_diagnostic_evidence",
        "sector_warmup_structure_lineage_diagnostic_evidence",
        "symbol_warmup_structure_lineage_diagnostic_evidence",
        "native_daily_reconciliation_contract_id",
        "market_native_daily_reconciliation_evidence",
        "sector_native_daily_reconciliation_evidence",
        "symbol_native_daily_reconciliation_evidence",
        "native_daily_calendar_coverage_contract_id",
        "market_native_daily_calendar_coverage_evidence",
        "sector_native_daily_calendar_coverage_evidence",
        "symbol_native_daily_calendar_coverage_evidence",
    }
)
_RISK_SECTOR_SOURCE_FIELDS = frozenset(
    {
        "sector_higher_timeframe_source_mode",
        "sector_strict_same_5m_warmup_evidence",
        "sector_strict_same_5m_source_coverage_evidence",
        "sector_research_bridge_parameter_set_id",
        "sector_strict_same_5m_warmup_convergence_evidence",
        "sector_strict_same_5m_warmup_convergence_diagnostic_evidence",
        "sector_strict_same_5m_warmup_mapping_supply_diagnostic_evidence",
        "sector_strict_same_5m_warmup_structure_lineage_diagnostic_evidence",
    }
)
_SESSION_ISSUE_CODES = frozenset(
    {
        "QMT_ONE_MINUTE_EXPECTED_SESSION_MISSING",
        "QMT_ONE_MINUTE_SESSION_GRID_INVALID",
    }
)
_QMT_MWD_WARMUP_BLOCKING_CODES = frozenset(
    {
        "QMT_HIGHER_TIMEFRAME_WARMUP_HISTORY_INSUFFICIENT",
        "QMT_HIGHER_TIMEFRAME_WARMUP_TAIL_DIVERGED",
    }
)
_POINT_TYPES = frozenset({"1buy", "2buy", "3buy", "1sell", "2sell", "3sell"})
_LIFECYCLE_STAGES = frozenset(
    {"approaching", "formed", "armed", "observed", "triggered", "executable"}
)
_SELECTION_SOURCES = frozenset(
    {
        "QMT_SECTOR_TRIGGER",
        "QMT_SECTOR_ELIGIBLE_SCOPE",
        "ACTIVE_WATCHLIST_MONITOR",
        "HOLDING_MONITOR",
        "VIRTUAL_HOLDING_MONITOR",
        "PREVIOUS_SIGNAL_MONITOR",
        "INCREMENTAL_SCAN_SCOPE",
    }
)
MIN_LIVE_REVIEW_STOCK_COVERAGE = Decimal("0.75")
COVERAGE_MANIFEST_SCHEMA = "chanlun-screening-coverage-manifest"
COVERAGE_STATE_CONTRACT_ID = "chanlun-screening-coverage-state"
COVERAGE_MANIFEST_FIELDS = frozenset(
    {
        "schema",
        "coverage_state_contract_id",
        "signal_document_contract_id",
        "coverage_epoch_id",
        "screening_policy_id",
        "source_cutoff",
        "market_data_as_of",
        "universe_revision",
        "sector_catalog_revision",
        "sector_strength_evidence_revision",
        "superseded_coverage_epoch_id",
        "superseded_market_data_as_of",
        "discovered_codes",
        "completed_codes",
        "excluded_codes",
        "failed_codes",
        "exclusions",
        "discarded_out_of_scope_retry_codes",
        "pending_frequencies",
        "backoff_frequencies",
        "deferred_frequencies",
        "complete",
        "batch_count",
    }
)
SECTOR_COVERAGE_CONTRACT_ID = "chanlun-screening-sector-coverage"
MONITOR_INSTRUMENT_EXCLUSION_CONTRACT_ID = "chanlun-monitor-instrument-exclusion"
COVERAGE_EXCLUSION_REASON_CODES = frozenset({"KLINE_MINIMUM_HISTORY_NOT_MET"})
_MONITOR_SELECTION_SOURCES = frozenset(
    {
        "ACTIVE_WATCHLIST_MONITOR",
        "HOLDING_MONITOR",
        "VIRTUAL_HOLDING_MONITOR",
        "PREVIOUS_SIGNAL_MONITOR",
    }
)


def _is_sha256_identity(value: object) -> bool:
    if (
        not isinstance(value, str)
        or len(value) != 71
        or not value.startswith("sha256:")
    ):
        return False
    try:
        int(value[7:], 16)
    except ValueError:
        return False
    return True


def _native_daily_reconciliation_evidence_is_consistent(
    raw: object,
    *,
    expected_symbol: str | None,
    evidence_cutoff: datetime,
) -> bool:
    """Validate the presentation copy of the certified native-D bridge."""

    if not isinstance(raw, Mapping):
        return False
    required_fields = {
        "contract_id",
        "symbol",
        "observed_at",
        "native_daily_bar_count",
        "one_minute_daily_bar_count",
        "overlap_session_count",
        "first_overlap_session",
        "last_overlap_session",
        "native_daily_content_revision",
        "one_minute_base_revision",
        "price_basis_revision",
        "trading_calendar_revision",
        "price_tolerance_quanta",
        "price_difference_count",
        "price_difference_session_count",
        "price_difference_identities",
        "max_observed_price_difference_quanta",
        "reconciled_source_revision",
        "all_overlap_ohlcv_equal",
        "all_overlap_ohlcv_within_declared_tolerance",
        "native_daily_role",
        "intraday_role",
        "live_status",
    }
    if set(raw) != required_fields or raw.get("contract_id") != (
        QMT_NATIVE_DAILY_RECONCILIATION_CONTRACT_ID
    ):
        return False
    symbol = raw.get("symbol")
    if (
        not isinstance(symbol, str)
        or re.fullmatch(r"(?:SH|SZ|BJ)\.[0-9]{6}", symbol) is None
        or (expected_symbol is not None and symbol != expected_symbol)
    ):
        return False
    try:
        observed_at = datetime.fromisoformat(str(raw.get("observed_at")))
        first = date.fromisoformat(str(raw.get("first_overlap_session")))
        last = date.fromisoformat(str(raw.get("last_overlap_session")))
    except (TypeError, ValueError):
        return False
    if (
        observed_at.tzinfo is None
        or observed_at != evidence_cutoff
        or first > last
        or last > evidence_cutoff.date()
    ):
        return False
    counts = tuple(
        raw.get(field)
        for field in (
            "native_daily_bar_count",
            "one_minute_daily_bar_count",
            "overlap_session_count",
        )
    )
    if any(type(value) is not int or value <= 0 for value in counts):
        return False
    native_count, minute_count, overlap_count = counts
    if overlap_count > min(native_count, minute_count) or (overlap_count == 1) != (
        first == last
    ):
        return False
    for field in (
        "native_daily_content_revision",
        "one_minute_base_revision",
        "price_basis_revision",
        "trading_calendar_revision",
        "reconciled_source_revision",
    ):
        if not _is_sha256_identity(raw.get(field)):
            return False
    tolerance = raw.get("price_tolerance_quanta")
    maximum = raw.get("max_observed_price_difference_quanta")
    identities = _unique_string_list(raw.get("price_difference_identities"))
    if (
        type(tolerance) is not int
        or tolerance not in {0, 1}
        or type(maximum) is not int
        or maximum < 0
        or maximum > tolerance
        or identities is None
        or raw.get("price_difference_count") != len(identities)
    ):
        return False
    difference_sessions: set[date] = set()
    for identity in identities:
        matched = re.fullmatch(
            r"([0-9]{4}-[0-9]{2}-[0-9]{2}):(open|high|low|close)",
            identity,
        )
        if matched is None:
            return False
        try:
            session = date.fromisoformat(matched.group(1))
        except ValueError:
            return False
        if not first <= session <= last:
            return False
        difference_sessions.add(session)
    if (
        raw.get("price_difference_session_count") != len(difference_sessions)
        or maximum != (1 if identities else 0)
        or raw.get("all_overlap_ohlcv_equal") is not (not identities)
        or raw.get("all_overlap_ohlcv_within_declared_tolerance") is not True
        or raw.get("native_daily_role") != "LEFT_HISTORY_BEFORE_ONE_MINUTE_BASE_ONLY"
        or raw.get("intraday_role") != "ONE_MINUTE_DERIVED_30M_AND_DAILY_TAIL"
        or raw.get("live_status") != "LIVE_DISABLED"
    ):
        return False
    return True


def _native_daily_calendar_coverage_evidence_is_consistent(
    raw: object,
    *,
    expected_symbol: str | None,
    evidence_cutoff: datetime,
    side_reasons: tuple[str, ...],
    side_gate: object,
) -> bool:
    """Validate a gap proof without treating a missing bar as suspension."""

    if not isinstance(raw, Mapping):
        return False
    try:
        evidence = QmtNativeDailyCalendarCoverageEvidence.from_document(raw)
    except (TypeError, ValueError):
        return False
    if (
        evidence.observed_at != evidence_cutoff
        or (expected_symbol is not None and evidence.symbol != expected_symbol)
        or evidence.native_last_session > evidence_cutoff.date()
        or evidence.calendar_last_session > evidence_cutoff.date()
    ):
        return False
    mismatch = evidence.status != "EXACT"
    calendar_reason = "QMT_NATIVE_DAILY_TRADING_CALENDAR_MISMATCH" in side_reasons
    return mismatch == (calendar_reason and side_gate == "UNRESOLVED")


def screening_coverage_epoch_id(
    *,
    market_data_as_of: datetime,
    universe_revision: str,
    sector_catalog_revision: str,
    sector_strength_evidence_revision: str | None,
    decision_core_id: str,
    screening_policy_id: str,
    structure_contract_id: str,
    parameter_set_id: str,
    signal_document_contract_id: str = SIGNAL_DOCUMENT_CONTRACT_ID,
) -> str:
    """Return the one canonical identity for a screening coverage epoch.

    Web production and the immutable forward validator must use the same
    derivation.  In particular, a newly recomputed horizontal-strength batch
    cannot reuse stock results collected under a different strength/ranking
    input merely because both batches share the same market close and QMT
    membership catalog.

    ``None`` is retained only for deliberately unaudited test/display
    adapters.  A forward-eligible snapshot separately requires a real
    strength evidence revision.
    """

    observed = normalize_datetime(market_data_as_of, "market_data_as_of")
    for field_name, value in (
        ("universe_revision", universe_revision),
        ("sector_catalog_revision", sector_catalog_revision),
        ("decision_core_id", decision_core_id),
        ("screening_policy_id", screening_policy_id),
    ):
        if not _is_sha256_identity(value):
            raise ValueError(f"{field_name} must be a sha256 identity")
    if sector_strength_evidence_revision is not None and not _is_sha256_identity(
        sector_strength_evidence_revision
    ):
        raise ValueError("sector_strength_evidence_revision must be a sha256 identity")
    for field_name, value in (
        ("structure_contract_id", structure_contract_id),
        ("parameter_set_id", parameter_set_id),
        ("signal_document_contract_id", signal_document_contract_id),
    ):
        if not isinstance(value, str) or not value:
            raise ValueError(f"{field_name} must be a non-empty string")
    return sha256_json(
        {
            "schema": "chanlun-screening-coverage-epoch",
            "coverage_state_contract_id": COVERAGE_STATE_CONTRACT_ID,
            "signal_document_contract_id": signal_document_contract_id,
            "market_data_as_of": observed,
            "universe_revision": universe_revision,
            "sector_catalog_revision": sector_catalog_revision,
            "sector_strength_evidence_revision": (sector_strength_evidence_revision),
            "decision_core_id": decision_core_id,
            "screening_policy_id": screening_policy_id,
            "structure_contract_id": structure_contract_id,
            "parameter_set_id": parameter_set_id,
        }
    )


def live_screening_semantic_snapshot_document(
    payload: Mapping[str, object],
) -> dict[str, object]:
    """Canonical market-semantic document shared by Web and daily archive.

    Wall-clock timings and pacing counters do not create a new market
    decision. Every field capable of changing candidates, coverage or
    provenance remains covered. Keeping this implementation in ``src`` avoids
    a weaker, second hash interpretation in the forward tool.
    """

    # Hashing only reads the document.  A deep copy of a full-universe live
    # snapshot can exceed 40 MiB and used to be performed on every readiness
    # probe.  Copy only the two mapping levels that are actually normalized;
    # all other nested values remain read-only inputs to ``sha256_json``.
    stable = dict(payload)
    for field in (
        "generated_at",
        "scanned_at",
        "snapshot_content_sha256",
        # Delivery eligibility is an operational wall-clock gate.  It decides
        # whether an already authenticated market transition may be announced;
        # it does not create a different market decision or coverage epoch.
        "notification_context",
    ):
        stable.pop(field, None)
    raw_audit = stable.get("scan_audit")
    if isinstance(raw_audit, Mapping):
        audit = dict(raw_audit)
        stable["scan_audit"] = audit
        for field in (
            "batch_duration_ms",
            "sector_scan_duration_ms",
            "stock_scan_duration_ms",
            "coverage_cycle_elapsed_ms",
            "coverage_cycle_batch_count",
            "coverage_cycle_started_at",
            "planned_symbol_count",
            "completed_symbol_count",
            "excluded_symbol_count",
            "completion_ratio",
            "batch_resolution_ratio",
            "planned_frequencies",
            "background_full_refresh_required",
            "monitoring_only_refresh",
            "monitoring_symbol_count",
        ):
            audit.pop(field, None)
    raw_manifest = stable.get("coverage_manifest")
    if isinstance(raw_manifest, Mapping):
        manifest = dict(raw_manifest)
        stable["coverage_manifest"] = manifest
        manifest.pop("batch_count", None)
        manifest.pop("source_cutoff", None)
    return stable


def live_screening_snapshot_content_sha256(
    payload: Mapping[str, object],
) -> str:
    return sha256_json(live_screening_semantic_snapshot_document(payload))


def _jsonable(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    return value


def _entry_boundary_from_document(raw: object) -> EntryExecutionBoundary | None:
    if raw is None:
        return None
    try:
        return parse_entry_execution_boundary_document(raw)
    except ValueError as exc:
        if "identity changed" in str(exc):
            raise ValueError(
                "live screening entry execution boundary identity changed"
            ) from exc
        raise ValueError(
            "live screening entry execution boundary is malformed"
        ) from exc


def _source_screening_policy_id(payload: Mapping[str, object]) -> str:
    """Return the mandatory attested screening-policy identity."""

    policy = payload.get("screening_policy")
    declared = payload.get("screening_policy_id")
    if (
        isinstance(policy, Mapping)
        and isinstance(declared, str)
        and declared.startswith("sha256:")
        and declared == sha256_json(policy)
    ):
        return declared
    raise ValueError("live screening policy identity is unavailable or invalid")


def _canonical_code_list(raw: object, field: str) -> list[str]:
    if (
        not isinstance(raw, list)
        or any(not isinstance(value, str) or not value for value in raw)
        or raw != sorted(set(raw))
    ):
        raise ValueError(f"live screening {field} is not canonical")
    return raw


def _frequency_code_set(raw: object, field: str) -> set[str]:
    if not isinstance(raw, Mapping):
        raise ValueError(f"live screening {field} is malformed")
    codes: set[str] = set()
    for code, frequencies in raw.items():
        if (
            not isinstance(code, str)
            or not code
            or not isinstance(frequencies, list)
            or not frequencies
            or any(not isinstance(value, str) or not value for value in frequencies)
            or frequencies != list(dict.fromkeys(frequencies))
        ):
            raise ValueError(f"live screening {field} is malformed")
        codes.add(code)
    return codes


def _coverage_exclusion_documents(raw: object) -> dict[str, str]:
    """Validate current-epoch eligibility exclusions as signed facts.

    A symbol that cannot meet the frozen minimum-history requirement is not a
    successful analysis and is not an operational failure.  It is an explicit
    universe disposition for this market-data epoch and must be retried when a
    new epoch can supply more completed bars.  Keeping the full normalized
    document in the manifest prevents a bare ``excluded_codes`` list from
    becoming an unaudited way to hide arbitrary scan failures.
    """

    if not isinstance(raw, list):
        raise ValueError("live screening exclusions are malformed")
    expected_keys = {
        "code",
        "exclusion_type",
        "eligibility",
        "reason_code",
        "retry_policy",
        "deterministic_for_coverage_epoch",
        "remote_error_type",
        "reason",
    }
    output: dict[str, str] = {}
    ordered_codes: list[str] = []
    for document in raw:
        if not isinstance(document, Mapping) or set(document) != expected_keys:
            raise ValueError("live screening exclusion document is malformed")
        code = document.get("code")
        reason_code = document.get("reason_code")
        if (
            not isinstance(code, str)
            or not code
            or not isinstance(reason_code, str)
            or reason_code not in COVERAGE_EXCLUSION_REASON_CODES
            or document.get("exclusion_type") != "stock_analysis_exclusion"
            or document.get("eligibility") != "INSUFFICIENT_MINIMUM_HISTORY"
            or document.get("retry_policy") != "NEXT_MARKET_DATA_EPOCH"
            or document.get("deterministic_for_coverage_epoch") is not True
            or not isinstance(document.get("remote_error_type"), str)
            or not document.get("remote_error_type")
            or not isinstance(document.get("reason"), str)
            or not document.get("reason")
        ):
            raise ValueError("live screening exclusion document is malformed")
        ordered_codes.append(code)
        output[code] = reason_code
    if ordered_codes != sorted(set(ordered_codes)) or len(output) != len(raw):
        raise ValueError("live screening exclusions are not canonical")
    return output


def _monitor_instrument_exclusions_are_consistent(
    payload: Mapping[str, object],
    *,
    audit: Mapping[str, object],
    manifest: Mapping[str, object],
) -> bool:
    """Authenticate read-only QMT instrument-type exclusions.

    These rows do not participate in signal generation, but they explain why
    an explicit watchlist, virtual holding or previous signal is absent from
    the screening universe.  The outer snapshot hash alone cannot distinguish
    a genuine document from a forged document followed by re-hashing, so both
    the live watermark and immutable review boundary enforce this exact
    semantic contract.
    """

    raw = payload.get("monitor_instrument_exclusions")
    raw_count = audit.get("monitor_instrument_exclusion_count")
    if (
        payload.get("monitor_instrument_exclusion_contract_id")
        != MONITOR_INSTRUMENT_EXCLUSION_CONTRACT_ID
        or not isinstance(raw, list)
        or type(raw_count) is not int
        or raw_count != len(raw)
    ):
        return False
    expected_keys = {
        "code",
        "eligibility",
        "reason_code",
        "selection_sources",
        "evidence_source",
        "qmt_instrument_type",
        "diagnostic_only",
        "tick_data_used",
        "real_account_accessed",
        "real_order_transport_enabled",
        "live_status",
    }
    discovered_sets: list[set[str]] = []
    for field in (
        "discovered_codes",
        "completed_codes",
        "excluded_codes",
        "failed_codes",
    ):
        values = manifest.get(field)
        if not isinstance(values, list) or any(
            not isinstance(value, str) for value in values
        ):
            return False
        discovered_sets.append(set(values))
    covered_codes = set().union(*discovered_sets)
    ordered_codes: list[str] = []
    for document in raw:
        if not isinstance(document, Mapping) or set(document) != expected_keys:
            return False
        code = document.get("code")
        sources = document.get("selection_sources")
        instrument_type = document.get("qmt_instrument_type")
        unresolved = instrument_type == "unresolved_cn"
        if (
            not isinstance(code, str)
            or re.fullmatch(r"(?:SH|SZ|BJ)\.\d{6}", code) is None
            or code in covered_codes
            or not isinstance(sources, list)
            or not sources
            or any(not isinstance(source, str) for source in sources)
            or sources != sorted(set(sources))
            or not set(sources).issubset(_MONITOR_SELECTION_SOURCES)
            or instrument_type
            not in {"index_cn", "fund_cn", "unsupported_cn", "unresolved_cn"}
            or document.get("eligibility")
            != (
                "UNRESOLVED_FROM_TRADING_SCREENING"
                if unresolved
                else "EXCLUDED_FROM_TRADING_SCREENING"
            )
            or document.get("reason_code")
            != (
                "QMT_NATIVE_INSTRUMENT_TYPE_UNRESOLVED"
                if unresolved
                else "QMT_NATIVE_STOCK_OR_ETF_REQUIRED"
            )
            or document.get("evidence_source") != "QMT_GET_INSTRUMENT_TYPE"
            or document.get("diagnostic_only") is not True
            or document.get("tick_data_used") is not False
            or document.get("real_account_accessed") is not False
            or document.get("real_order_transport_enabled") is not False
            or document.get("live_status") != "LIVE_DISABLED"
        ):
            return False
        ordered_codes.append(code)
    return ordered_codes == sorted(set(ordered_codes))


def monitor_instrument_exclusions_are_consistent(
    payload: Mapping[str, object],
) -> bool:
    """Return whether the page/archive monitor diagnostic contract is exact."""

    audit = payload.get("scan_audit")
    manifest = payload.get("coverage_manifest")
    return bool(
        isinstance(audit, Mapping)
        and isinstance(manifest, Mapping)
        and _monitor_instrument_exclusions_are_consistent(
            payload,
            audit=audit,
            manifest=manifest,
        )
    )


def _coverage_manifest_is_consistent(
    payload: Mapping[str, object],
    *,
    audit: Mapping[str, object],
    manifest: Mapping[str, object],
    errors: list[object],
) -> bool:
    """Recompute coverage claims instead of trusting self-described totals."""

    try:
        discovered = set(
            _canonical_code_list(manifest.get("discovered_codes"), "discovered codes")
        )
        completed = set(
            _canonical_code_list(manifest.get("completed_codes"), "completed codes")
        )
        failed = set(_canonical_code_list(manifest.get("failed_codes"), "failed codes"))
        excluded = set(
            _canonical_code_list(manifest.get("excluded_codes"), "excluded codes")
        )
        exclusion_reasons = _coverage_exclusion_documents(manifest.get("exclusions"))
        discarded = set(
            _canonical_code_list(
                manifest.get("discarded_out_of_scope_retry_codes"),
                "discarded retry codes",
            )
        )
        pending = _frequency_code_set(
            manifest.get("pending_frequencies"), "pending frequencies"
        )
        backoff = _frequency_code_set(
            manifest.get("backoff_frequencies"), "backoff frequencies"
        )
        deferred = _frequency_code_set(
            manifest.get("deferred_frequencies"), "deferred frequencies"
        )
        attempted = completed | excluded | failed
        resolved = completed | excluded
        complete = manifest.get("complete") is True
        market_data_as_of = datetime.fromisoformat(
            str(manifest.get("market_data_as_of"))
        )
        expected_epoch_id = screening_coverage_epoch_id(
            market_data_as_of=market_data_as_of,
            universe_revision=str(manifest.get("universe_revision")),
            sector_catalog_revision=str(manifest.get("sector_catalog_revision")),
            sector_strength_evidence_revision=(
                manifest.get("sector_strength_evidence_revision")
                if isinstance(manifest.get("sector_strength_evidence_revision"), str)
                else None
            ),
            decision_core_id=str(payload.get("decision_core_id")),
            screening_policy_id=_source_screening_policy_id(payload),
            structure_contract_id=str(payload.get("structure_contract_id")),
            parameter_set_id=str(payload.get("parameter_set_id")),
        )
        if (
            set(manifest) != COVERAGE_MANIFEST_FIELDS
            or manifest.get("schema") != COVERAGE_MANIFEST_SCHEMA
            or manifest.get("coverage_state_contract_id") != COVERAGE_STATE_CONTRACT_ID
            or manifest.get("signal_document_contract_id")
            != SIGNAL_DOCUMENT_CONTRACT_ID
            or manifest.get("coverage_epoch_id") != payload.get("coverage_epoch_id")
            or manifest.get("coverage_epoch_id") != expected_epoch_id
            or manifest.get("screening_policy_id") != payload.get("screening_policy_id")
            or manifest.get("market_data_as_of") != payload.get("market_data_as_of")
            or manifest.get("sector_strength_evidence_revision")
            != payload.get("sector_strength_evidence_revision")
            or not _is_sha256_identity(manifest.get("sector_catalog_revision"))
            or completed & failed
            or completed & excluded
            or excluded & failed
            or excluded != set(exclusion_reasons)
            or attempted - discovered
            or pending - discovered
            or backoff - discovered
            or deferred - (failed | excluded)
            or excluded - deferred
            or discarded & discovered
            or pending & attempted
            or backoff & attempted
            or pending & backoff
            or backoff & deferred
            or (complete and attempted != discovered)
            or (complete and (pending or backoff))
            or audit.get("coverage_cycle_complete") is not complete
            or int(audit.get("discovered_symbol_count")) != len(discovered)
            or int(audit.get("coverage_cycle_attempted_symbol_count")) != len(attempted)
            or int(audit.get("coverage_cycle_completed_symbol_count")) != len(completed)
            or int(audit.get("coverage_cycle_excluded_symbol_count")) != len(excluded)
            or int(audit.get("coverage_cycle_failed_symbol_count")) != len(failed)
            or int(audit.get("coverage_cycle_resolved_symbol_count")) != len(resolved)
            or int(audit.get("immediate_pending_symbol_count")) != len(pending)
            or int(audit.get("backoff_retry_symbol_count")) != len(backoff)
            or int(audit.get("pending_symbol_count")) != len(pending) + len(backoff)
            or int(audit.get("next_epoch_retry_symbol_count")) != len(deferred)
            or int(audit.get("retry_symbol_count")) != len(backoff) + len(deferred)
        ):
            return False
        expected_ratio = (
            Decimal("0")
            if not attempted
            else Decimal(len(completed)) / Decimal(len(attempted))
        )
        if Decimal(str(audit.get("coverage_cycle_completion_ratio"))) != expected_ratio:
            return False
        expected_resolution_ratio = (
            Decimal("0")
            if not discovered
            else Decimal(len(resolved)) / Decimal(len(discovered))
        )
        if (
            Decimal(str(audit.get("coverage_cycle_resolution_ratio")))
            != expected_resolution_ratio
        ):
            return False

        stock_error_codes: list[str] = []
        failure_counts: dict[str, int] = {}
        for raw in errors:
            if not isinstance(raw, Mapping):
                return False
            if raw.get("error_type") != "stock_analysis_error":
                continue
            code = raw.get("code")
            reason = raw.get("reason_code")
            if not isinstance(code, str) or not code or not isinstance(reason, str):
                return False
            stock_error_codes.append(code)
            failure_counts[reason] = failure_counts.get(reason, 0) + 1
        if (
            len(stock_error_codes) != len(set(stock_error_codes))
            or set(stock_error_codes) != failed
            or audit.get("stock_failure_counts") != dict(sorted(failure_counts.items()))
        ):
            return False
        exclusion_counts: dict[str, int] = {}
        for reason in exclusion_reasons.values():
            exclusion_counts[reason] = exclusion_counts.get(reason, 0) + 1
        if audit.get("stock_exclusion_counts") != dict(
            sorted(exclusion_counts.items())
        ):
            return False
        raw_signals = payload.get("signals")
        if not isinstance(raw_signals, list) or any(
            not isinstance(signal, Mapping) or not isinstance(signal.get("code"), str)
            for signal in raw_signals
        ):
            return False
        signal_codes = {
            str(signal.get("code"))
            for signal in raw_signals
            if isinstance(signal, Mapping)
        }
        if signal_codes - completed:
            return False
    except (InvalidOperation, TypeError, ValueError):
        return False
    return True


def validate_live_screening_market_watermark(
    payload: Mapping[str, object],
) -> datetime:
    """Validate the current scan's market cutoff without promoting its signals.

    An in-progress coverage epoch is not eligible for human-review promotion,
    but its authenticated market cutoff is still the authoritative watermark
    for deciding whether an older immutable review report may create *new*
    paper intents.  This validator therefore proves only the read-only envelope,
    semantic identity and coverage-manifest identity.  It deliberately does not
    require ``coverage_cycle_complete`` and never returns any candidate signal.
    """

    try:
        review_at = datetime.fromisoformat(str(payload["as_of"]))
        market_data_as_of = datetime.fromisoformat(str(payload["market_data_as_of"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("live screening market watermark is invalid") from exc
    audit = payload.get("scan_audit")
    manifest = payload.get("coverage_manifest")
    errors = payload.get("errors")
    coverage_consistent = (
        _coverage_manifest_is_consistent(
            payload,
            audit=audit,
            manifest=manifest,
            errors=errors,
        )
        if isinstance(audit, Mapping)
        and isinstance(manifest, Mapping)
        and isinstance(errors, list)
        else False
    )
    monitor_exclusions_consistent = monitor_instrument_exclusions_are_consistent(
        payload
    )
    if (
        payload.get("schema") != LIVE_SCREENING_SCHEMA
        or payload.get("signal_document_contract_id") != SIGNAL_DOCUMENT_CONTRACT_ID
        or payload.get("sector_coverage_contract_id") != SECTOR_COVERAGE_CONTRACT_ID
        or payload.get("available") is not True
        or payload.get("scan_state") not in {"scanning", "complete"}
        or payload.get("sector_first") is not True
        or payload.get("read_only") is not True
        or payload.get("research_only") is not True
        or payload.get("no_order_execution") is not True
        or review_at.tzinfo is None
        or market_data_as_of.tzinfo is None
        or market_data_as_of > review_at
        or market_data_as_of.astimezone(ZoneInfo("Asia/Shanghai")).date()
        != review_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
        or not coverage_consistent
        or not monitor_exclusions_consistent
        or not isinstance(payload.get("snapshot_content_sha256"), str)
        or payload.get("snapshot_content_sha256")
        != live_screening_snapshot_content_sha256(payload)
    ):
        raise ValueError("live screening market watermark is unverified")
    return market_data_as_of


def _sector_context_is_consistent(
    raw: object,
    *,
    frequency: str,
    evidence_cutoff: datetime,
) -> bool:
    if not isinstance(raw, Mapping):
        return False
    try:
        observed_at = datetime.fromisoformat(str(raw["observed_at"]))
    except (KeyError, TypeError, ValueError):
        return False
    # QMT sector composites deliberately stop at 5m.  Their serialized 1m
    # row is a neutral sentinel because 1m remains a stock-only locator.  It
    # is not the result of ``classify_context`` and therefore has its own
    # exact contract; 30m/5m sector rows use the ordinary context semantics.
    context_consistent = _decision_context_is_consistent(raw)
    if frequency == "1m":
        context_consistent = bool(
            raw.get("direction") == "neutral"
            and raw.get("disposition") == "neutral"
            and raw.get("hard_block") is False
            and raw.get("dominant_point_id") is None
            and raw.get("dominant_point_type") is None
            and raw.get("reason_codes") == ["stock_one_minute_trigger_only"]
        )
    return bool(
        raw.get("frequency") == frequency
        and context_consistent
        and observed_at.tzinfo is not None
        and observed_at <= evidence_cutoff
    )


def _decision_context_is_consistent(raw: object) -> bool:
    """Recompute one serialized context from its dominant point summary."""

    if not isinstance(raw, Mapping):
        return False
    direction = raw.get("direction")
    dominant_id = raw.get("dominant_point_id")
    dominant_type = raw.get("dominant_point_type")
    if direction not in {"up", "down", "neutral"}:
        return False
    if dominant_type is None:
        if dominant_id is not None:
            return False
        disposition = "neutral"
        reason = "no_active_directional_point"
    else:
        if dominant_type not in _POINT_TYPES or not _is_sha256_identity(dominant_id):
            return False
        if str(dominant_type).endswith("buy"):
            disposition = "supportive"
            reason = "confirmed_buy_structure"
        elif direction == "down":
            disposition = "hostile"
            reason = "confirmed_sell_with_down_structure"
        else:
            disposition = "neutral"
            reason = "mixed_or_transition_structure"
    return bool(
        raw.get("disposition") == disposition
        and raw.get("hard_block") is (disposition == "hostile")
        and raw.get("reason_codes") == [reason]
    )


def _point_document_is_causal(
    raw: object,
    *,
    frequency: str,
    evidence_cutoff: datetime,
) -> bool:
    """Validate the portable time ordering of one structure point.

    A semantic snapshot hash only authenticates what the page claims.  These
    timestamps must also prove that the 5m setup or 1m locator was available by
    the declared market-data cutoff; otherwise a caller can move one nested
    fact into the future and simply re-identify the whole snapshot.
    """

    if not isinstance(raw, Mapping):
        return False
    try:
        anchor_at = datetime.fromisoformat(str(raw["anchor_at"]))
        available_at = datetime.fromisoformat(str(raw["available_at"]))
        confirmed_at = (
            None
            if raw.get("confirmed_at") is None
            else datetime.fromisoformat(str(raw["confirmed_at"]))
        )
    except (KeyError, TypeError, ValueError):
        return False
    status = raw.get("status")
    return bool(
        raw.get("source_frequency") == frequency
        and status in {"provisional", "confirmed", "invalidated"}
        and anchor_at.tzinfo is not None
        and available_at.tzinfo is not None
        and anchor_at <= available_at <= evidence_cutoff
        and (status == "confirmed") == (confirmed_at is not None)
        and (
            confirmed_at is None
            or confirmed_at.tzinfo is not None
            and anchor_at <= confirmed_at <= available_at
        )
    )


def _warmup_evidence_is_consistent(raw: object) -> bool:
    """Recompute the active pairwise warmup verdict from its frozen rows."""

    if not isinstance(raw, Mapping):
        return False
    rows = raw.get("by_frequency")
    reasons = raw.get("reason_codes")
    difference_rows = raw.get("difference_codes_by_frequency")
    if (
        raw.get("required_for_new_entry") is not True
        or type(raw.get("converged")) is not bool
        or not isinstance(rows, list)
        or len(rows) != len(SCREENING_WARMUP_FREQUENCIES)
        or not isinstance(reasons, list)
        or any(not isinstance(value, str) or not value for value in reasons)
        or len(reasons) != len(set(reasons))
        or not isinstance(difference_rows, list)
    ):
        return False
    expected_frequencies = SCREENING_WARMUP_FREQUENCIES
    actual_frequencies: list[str] = []
    converged_values: list[bool] = []
    expected_reasons: list[str] = []
    try:
        for row in rows:
            if not isinstance(row, Mapping):
                return False
            frequency = row.get("frequency")
            converged = row.get("converged")
            full_count = row.get("full_bar_count")
            suffix_count = row.get("suffix_bar_count")
            if not isinstance(frequency, str) or type(converged) is not bool:
                return False
            reason = screening_warmup_reason_code(
                frequency=frequency,
                converged=converged,
                full_bar_count=full_count,  # type: ignore[arg-type]
                suffix_bar_count=suffix_count,  # type: ignore[arg-type]
            )
            actual_frequencies.append(frequency)
            converged_values.append(converged)
            expected_reasons.append(f"{frequency.upper()}:{reason}")
    except (TypeError, ValueError):
        return False
    if len(difference_rows) != len(rows):
        return False
    for warmup_row, diagnostic_row in zip(rows, difference_rows):
        if not isinstance(diagnostic_row, Mapping):
            return False
        codes = diagnostic_row.get("difference_codes")
        if (
            diagnostic_row.get("frequency") != warmup_row.get("frequency")
            or not isinstance(codes, list)
            or any(
                not isinstance(value, str)
                or value not in SCREENING_WARMUP_DIFFERENCE_CODES
                for value in codes
            )
            or len(codes) != len(set(codes))
            or (warmup_row.get("converged") is True and codes)
            or (
                warmup_row.get("converged") is False
                and warmup_row.get("full_bar_count", 0)
                >= SCREENING_WARMUP_REQUIRED_BARS[str(warmup_row.get("frequency"))]
                and not codes
            )
        ):
            return False
    accepted_reasons = [expected_reasons]
    if "30m" in actual_frequencies:
        fallback_reasons = list(expected_reasons)
        thirty_index = actual_frequencies.index("30m")
        fallback_reasons.insert(
            thirty_index + 1,
            f"30M:{SCREENING_QMT_30M_FALLBACK_REASON_CODE}",
        )
        accepted_reasons.append(fallback_reasons)
    return bool(
        tuple(actual_frequencies) == expected_frequencies
        and raw.get("converged") is all(converged_values)
        and reasons in accepted_reasons
    )


def _unique_string_list(raw: object) -> tuple[str, ...] | None:
    if (
        not isinstance(raw, list)
        or any(not isinstance(value, str) or not value for value in raw)
        or len(raw) != len(set(raw))
    ):
        return None
    return tuple(raw)


def _risk_period_diagnostics(
    raw: object,
    *,
    evidence_cutoff: datetime,
) -> tuple[HigherTimeframePeriodDiagnostic, ...] | None:
    """Parse and causally validate the portable M/W/D diagnostic documents."""

    if not isinstance(raw, list) or len(raw) not in {0, 3}:
        return None
    output: list[HigherTimeframePeriodDiagnostic] = []
    for expected_period, value in zip(_RISK_PERIODS, raw):
        if not isinstance(value, Mapping):
            return None
        period = value.get("period")
        state = value.get("state")
        completed_count = value.get("completed_bar_count")
        mapping_unique = value.get("mapping_unique")
        mapped_center_id = value.get("mapped_center_id")
        candidates = _unique_string_list(value.get("mapping_candidate_ids"))
        blockers = _unique_string_list(value.get("blocker_codes"))
        warnings = _unique_string_list(value.get("warning_codes"))
        source_revision = value.get("source_revision")
        has_mapping_supply = "mapping_supply" in value
        mapping_supply = None
        if has_mapping_supply:
            try:
                mapping_supply = RiskMappingSupplyFacts.from_document(
                    value.get("mapping_supply")
                )
            except (TypeError, ValueError):
                return None
        if (
            period != expected_period
            or not isinstance(state, str)
            or state not in HIGHER_TIMEFRAME_RISK_STATES
            or type(completed_count) is not int
            or completed_count < 0
            or type(mapping_unique) is not bool
            or (
                mapped_center_id is not None
                and (not isinstance(mapped_center_id, str) or not mapped_center_id)
            )
            or candidates is None
            or blockers is None
            or warnings is None
            or not _is_sha256_identity(source_revision)
        ):
            return None
        evidence_bar_end = None
        if value.get("evidence_bar_end") is not None:
            try:
                evidence_bar_end = normalize_datetime(
                    datetime.fromisoformat(str(value["evidence_bar_end"])),
                    "higher_timeframe.evidence_bar_end",
                )
            except (TypeError, ValueError):
                return None
            if evidence_bar_end > evidence_cutoff:
                return None
        active_interval = None
        raw_interval = value.get("active_top_interval")
        if raw_interval is not None:
            if not isinstance(raw_interval, list) or len(raw_interval) != 2:
                return None
            try:
                active_interval = tuple(
                    normalize_datetime(
                        datetime.fromisoformat(str(item)),
                        "higher_timeframe.active_top_interval",
                    )
                    for item in raw_interval
                )
            except (TypeError, ValueError):
                return None
            if (
                active_interval[0] > active_interval[1]
                or active_interval[1] > evidence_cutoff
                or evidence_bar_end is None
                or active_interval[1] > evidence_bar_end
            ):
                return None
        if state == "NONE":
            if (
                active_interval is not None
                or mapping_unique is not True
                or mapped_center_id is not None
                or candidates
                or blockers
            ):
                return None
        elif (
            active_interval is None or evidence_bar_end is None or completed_count == 0
        ):
            return None
        if (
            mapping_unique is True
            and state != "NONE"
            and not isinstance(mapped_center_id, str)
        ):
            return None
        if mapping_unique is False and (mapped_center_id is not None or not blockers):
            return None
        if (state == "NONE" and has_mapping_supply) or (
            state != "NONE" and mapping_supply is None
        ):
            return None
        try:
            output.append(
                HigherTimeframePeriodDiagnostic(
                    period=period,  # type: ignore[arg-type]
                    state=state,  # type: ignore[arg-type]
                    completed_bar_count=completed_count,
                    evidence_bar_end=evidence_bar_end,
                    active_top_interval=active_interval,  # type: ignore[arg-type]
                    mapping_unique=mapping_unique,
                    mapped_center_id=mapped_center_id,  # type: ignore[arg-type]
                    mapping_candidate_ids=candidates,
                    blocker_codes=blockers,
                    warning_codes=warnings,
                    source_revision=source_revision,  # type: ignore[arg-type]
                    mapping_supply=mapping_supply,
                )
            )
        except (TypeError, ValueError):
            return None
    return tuple(output)


def _session_evidence_is_consistent(
    raw: object,
    *,
    side_reasons: tuple[str, ...],
    side_gate: object,
    evidence_cutoff: datetime,
) -> bool:
    """Validate the optional, presentation-only QMT session explanation.

    A missing session remains unclassified unless a separate point-in-time
    trade-status source proves suspension.  The current contract therefore
    requires the exact source issue, ``historical_trade_status_proven=false``
    and a fail-closed disposition; a re-hashed story cannot turn absence into
    a suspension or erase the date.
    """

    if not isinstance(raw, Mapping) or set(raw) != {
        "contract_id",
        "status",
        "issue_count",
        "issues",
        "entry_disposition",
    }:
        return False
    status = raw.get("status")
    issues = raw.get("issues")
    issue_count = raw.get("issue_count")
    if (
        raw.get("contract_id") != HIGHER_TIMEFRAME_SESSION_EVIDENCE_CONTRACT_ID
        or status not in {"EXACT", "UNAVAILABLE"}
        or type(issue_count) is not int
        or not isinstance(issues, list)
        or issue_count != len(issues)
        or (status == "UNAVAILABLE" and issues)
    ):
        return False
    expected_disposition = (
        "FAIL_CLOSED" if status == "UNAVAILABLE" or issues else "NO_SESSION_BLOCKER"
    )
    if raw.get("entry_disposition") != expected_disposition:
        return False

    canonical: list[dict[str, object]] = []
    identities: list[tuple[date, str]] = []
    for issue in issues:
        if not isinstance(issue, Mapping) or set(issue) != {
            "session",
            "code",
            "observed_rows",
            "classification",
            "detail",
            "historical_trade_status_proven",
            "entry_disposition",
        }:
            return False
        raw_session = issue.get("session")
        raw_code = issue.get("code")
        raw_rows = issue.get("observed_rows")
        raw_detail = issue.get("detail")
        if (
            not isinstance(raw_session, str)
            or raw_code not in _SESSION_ISSUE_CODES
            or type(raw_rows) is not int
            or not isinstance(raw_detail, str)
            or issue.get("historical_trade_status_proven") is not False
            or issue.get("entry_disposition") != "FAIL_CLOSED"
        ):
            return False
        try:
            session = date.fromisoformat(raw_session)
            reconstructed = QmtMinuteSessionIssue(
                session=session,
                code=raw_code,
                observed_rows=raw_rows,
                detail=raw_detail,
            ).document()
        except (TypeError, ValueError):
            return False
        if dict(issue) != reconstructed or session > evidence_cutoff.date():
            return False
        identities.append((session, raw_code))
        canonical.append(reconstructed)
    if identities != sorted(set(identities)):
        return False
    issue_codes = {str(value["code"]) for value in canonical}
    reason_issue_codes = _SESSION_ISSUE_CODES.intersection(side_reasons)
    if not issue_codes.issubset(set(side_reasons)):
        return False
    if status == "EXACT" and issue_codes != reason_issue_codes:
        return False
    if status == "UNAVAILABLE" and reason_issue_codes:
        return False
    if (status == "UNAVAILABLE" or issues) and side_gate != "UNRESOLVED":
        return False
    return True


def _mwd_warmup_evidence_is_consistent(
    raw: object,
    *,
    side_reasons: tuple[str, ...],
    side_gate: object,
) -> bool:
    """Recompute the frozen count/verdict relations in one M/W/D warmup row."""

    blocking_reasons = _QMT_MWD_WARMUP_BLOCKING_CODES.intersection(side_reasons)
    if raw is None:
        return not blocking_reasons
    if not isinstance(raw, Mapping) or set(raw) != {
        "contract_id",
        "required_daily_bar_count",
        "full_daily_bar_count",
        "suffix_daily_bar_count",
        "converged",
        "reason_code",
        "full_signature",
        "suffix_signature",
        "entry_disposition",
    }:
        return False
    required = raw.get("required_daily_bar_count")
    full_count = raw.get("full_daily_bar_count")
    suffix_count = raw.get("suffix_daily_bar_count")
    converged = raw.get("converged")
    reason = raw.get("reason_code")
    full_signature = raw.get("full_signature")
    suffix_signature = raw.get("suffix_signature")
    if (
        raw.get("contract_id") != QMT_HIGHER_TIMEFRAME_WARMUP_EVIDENCE_CONTRACT_ID
        or required != QMT_HIGHER_TIMEFRAME_WARMUP_REQUIRED_DAILY_BARS
        or type(full_count) is not int
        or full_count < 0
        or type(suffix_count) is not int
        or suffix_count < 0
        or type(converged) is not bool
        or not _is_sha256_identity(full_signature)
    ):
        return False
    if reason == "QMT_HIGHER_TIMEFRAME_WARMUP_HISTORY_INSUFFICIENT":
        consistent = (
            full_count < required
            and suffix_count == 0
            and converged is False
            and suffix_signature is None
        )
    elif reason in {
        "QMT_HIGHER_TIMEFRAME_WARMUP_TAIL_DIVERGED",
        "QMT_HIGHER_TIMEFRAME_WARMUP_TAIL_STABLE",
    }:
        consistent = (
            full_count >= required
            and suffix_count == expected_screening_warmup_suffix_bar_count(full_count)
            and _is_sha256_identity(suffix_signature)
            and converged == (reason == "QMT_HIGHER_TIMEFRAME_WARMUP_TAIL_STABLE")
            and (full_signature == suffix_signature) == converged
        )
    else:
        return False
    if not consistent:
        return False
    blocked = converged is False
    expected_disposition = "FAIL_CLOSED" if blocked else "NO_WARMUP_BLOCKER"
    if raw.get("entry_disposition") != expected_disposition:
        return False
    if blocked:
        return reason in side_reasons and side_gate == "UNRESOLVED"
    return not blocking_reasons and reason not in side_reasons


def _mwd_warmup_convergence_evidence_is_consistent(
    raw: object,
    *,
    evidence_cutoff: datetime,
    side_gate: object,
) -> bool:
    if raw is None:
        return side_gate == "UNRESOLVED"
    if not isinstance(raw, Mapping):
        return False
    try:
        evidence = WarmupConvergenceEnvelope.from_document(raw)
    except (TypeError, ValueError):
        return False
    return bool(
        evidence.as_of == evidence_cutoff
        and evidence.frequency == "d"
        and evidence.parameter_set_id
        == QMT_HIGHER_TIMEFRAME_WARMUP_CONVERGENCE_PARAMETER_SET_ID
        and evidence.diagnostic_only is True
        and evidence.active_gate_unchanged is True
        and evidence.live_status == "LIVE_DISABLED"
    )


def _mwd_warmup_diagnostic_chain_is_consistent(
    *,
    envelope_raw: object,
    semantic_raw: object,
    supply_raw: object,
    lineage_raw: object,
    evidence_cutoff: datetime,
    memo: dict[tuple[str, str], bool] | None = None,
) -> bool:
    """Validate the complete diagnostic lineage once per distinct document.

    The former caller invoked the semantic, mapping-supply and lineage
    validators independently.  Each deeper validator reconstructed and
    revalidated every earlier sibling, so one signal parsed the same envelope
    three times.  Market and sector documents are also shared by hundreds of
    signals.  A validation-run-local cache keyed by the *recomputed canonical
    content hash* preserves the exact tamper checks while avoiding that
    quadratic work.  Declared document hashes are deliberately not trusted as
    cache keys.
    """

    cache_key: tuple[str, str] | None = None
    if memo is not None:
        try:
            cache_key = (
                evidence_cutoff.isoformat(),
                sha256_json(
                    {
                        "envelope": envelope_raw,
                        "semantic": semantic_raw,
                        "mapping_supply": supply_raw,
                        "structure_lineage": lineage_raw,
                    }
                ),
            )
        except (OverflowError, TypeError, ValueError):
            cache_key = None
        if cache_key is not None and cache_key in memo:
            return memo[cache_key]

    def current(value: object) -> bool:
        return bool(
            value.as_of == evidence_cutoff
            and value.frequency == "d"
            and value.parameter_set_id
            == QMT_HIGHER_TIMEFRAME_WARMUP_CONVERGENCE_PARAMETER_SET_ID
            and value.diagnostic_only is True
            and value.active_gate_unchanged is True
            and value.live_status == "LIVE_DISABLED"
        )

    result = False
    if envelope_raw is None:
        result = semantic_raw is None and supply_raw is None and lineage_raw is None
    elif all(
        isinstance(value, Mapping)
        for value in (envelope_raw, semantic_raw, supply_raw, lineage_raw)
    ):
        try:
            envelope = WarmupConvergenceEnvelope.from_document(envelope_raw)
            semantic = WarmupConvergenceDiagnosticEnvelope.from_document(semantic_raw)
            semantic.validate_against(envelope)
            bound = replace(envelope, diagnostic=semantic)
            supply = WarmupMappingSupplyDiagnosticEnvelope.from_document(supply_raw)
            supply.validate_against(bound)
            lineage = WarmupStructureLineageDiagnosticEnvelope.from_document(
                lineage_raw
            )
            lineage.validate_against(replace(bound, mapping_supply_diagnostic=supply))
            result = current(semantic) and current(supply) and current(lineage)
        except (TypeError, ValueError):
            result = False

    if memo is not None and cache_key is not None:
        memo[cache_key] = result
    return result


def _sector_source_extension_mode(
    risk: Mapping[str, object],
    *,
    sector_reasons: tuple[str, ...],
) -> tuple[bool, str | None]:
    """Authenticate the conditional page/replay sector-source provenance.

    A row claiming the native-daily research blocker must carry the complete
    provenance. The precheck is deliberately
    independent of the risk-state recomputation so only a fully identified
    research bridge may activate the GREEN-to-AMBER safety cap below.
    """

    fields = (
        "sector_higher_timeframe_source_mode",
        "sector_strict_same_5m_warmup_evidence",
        "sector_strict_same_5m_source_coverage_evidence",
        "sector_research_bridge_parameter_set_id",
    )
    present = tuple(field in risk for field in fields)
    blocker = "QMT_SECTOR_NATIVE_DAILY_AND_5M_UNRECONCILED_RESEARCH_BRIDGE"
    if not any(present):
        return blocker not in sector_reasons, None
    if not all(present):
        return False, None

    mode = risk.get("sector_higher_timeframe_source_mode")
    bridge_id = risk.get("sector_research_bridge_parameter_set_id")
    if mode == QMT_SECTOR_SAME_BASE_SOURCE_MODE:
        return bridge_id is None and blocker not in sector_reasons, mode
    if mode != QMT_SECTOR_NATIVE_DAILY_RESEARCH_SOURCE_MODE:
        return False, None
    expected_bridge_id = sector_native_daily_research_bridge_contract()[
        "parameter_set_id"
    ]
    valid = bool(
        bridge_id == expected_bridge_id
        and blocker in sector_reasons
        and risk.get("sector_gate") in {"AMBER", "RED", "UNRESOLVED"}
    )
    return valid, mode if valid else None


def _sector_source_extension_is_consistent(
    risk: Mapping[str, object],
    *,
    sector_reasons: tuple[str, ...],
    mode: str | None,
    evidence_cutoff: datetime,
) -> bool:
    """Bind source mode to both the strict and selected warmup evidence."""

    if mode is None:
        return True
    strict_warmup = risk.get("sector_strict_same_5m_warmup_evidence")
    raw_coverage = risk.get("sector_strict_same_5m_source_coverage_evidence")
    selected_warmup = risk.get("sector_warmup_evidence")
    if not isinstance(strict_warmup, Mapping) or not isinstance(
        raw_coverage,
        Mapping,
    ):
        return False
    try:
        coverage = QmtSectorSameBaseCoverageEvidence.from_document(raw_coverage)
    except ValueError:
        return False
    if (
        coverage.observed_at != evidence_cutoff
        or coverage.completed_daily_bar_count
        < strict_warmup.get("full_daily_bar_count")
        or coverage.required_daily_bar_count
        != strict_warmup.get("required_daily_bar_count")
        or coverage.warmup_converged != strict_warmup.get("converged")
        or coverage.warmup_reason_code != strict_warmup.get("reason_code")
    ):
        return False
    if mode == QMT_SECTOR_SAME_BASE_SOURCE_MODE:
        # On the strict path the selected evidence is the strict evidence; a
        # second divergent copy would make page and replay provenance ambiguous.
        return strict_warmup == selected_warmup
    if mode != QMT_SECTOR_NATIVE_DAILY_RESEARCH_SOURCE_MODE:
        return False
    insufficient = "QMT_HIGHER_TIMEFRAME_WARMUP_HISTORY_INSUFFICIENT"
    return _mwd_warmup_evidence_is_consistent(
        strict_warmup,
        side_reasons=(insufficient,),
        side_gate="UNRESOLVED",
    )


def _risk_side_evidence_is_consistent(
    risk: Mapping[str, object],
    *,
    prefix: str,
    evidence_cutoff: datetime,
    allow_green_to_amber_cap: bool = False,
) -> bool:
    gate = risk.get(f"{prefix}_gate")
    raw_states = risk.get(f"{prefix}_states")
    reasons = _unique_string_list(risk.get(f"{prefix}_reason_codes"))
    diagnostics = _risk_period_diagnostics(
        risk.get(f"{prefix}_period_diagnostics"),
        evidence_cutoff=evidence_cutoff,
    )
    if (
        gate not in _GATES
        or not isinstance(raw_states, Mapping)
        or set(raw_states) != set(_RISK_PERIODS)
        or reasons is None
        or diagnostics is None
    ):
        return False
    states = tuple(raw_states[period] for period in _RISK_PERIODS)
    unresolved_snapshot = all(value == "UNRESOLVED" for value in states)
    if unresolved_snapshot:
        if gate != "UNRESOLVED" or not reasons:
            return False
    else:
        if any(
            not isinstance(value, str) or value not in HIGHER_TIMEFRAME_RISK_STATES
            for value in states
        ):
            return False
        if (
            len(diagnostics) != 3
            or tuple(value.state for value in diagnostics) != states
        ):
            return False
        expected_gate = higher_timeframe_risk_gate(
            states=states,  # type: ignore[arg-type]
            completed_ma5_available=True,
            mapping_unique=all(value.mapping_unique for value in diagnostics),
        )
        capped_research_green = bool(
            allow_green_to_amber_cap
            and expected_gate == "GREEN"
            and gate == "AMBER"
            and (
                "QMT_SECTOR_NATIVE_DAILY_AND_5M_UNRECONCILED_RESEARCH_BRIDGE" in reasons
            )
        )
        if gate != expected_gate and not capped_research_green:
            return False
    for diagnostic in diagnostics:
        if any(code not in reasons for code in diagnostic.blocker_codes):
            return False
        if (
            not diagnostic.mapping_unique
            and f"{diagnostic.period}_CENTER_MAPPING_UNRESOLVED" not in reasons
        ):
            return False
    return True


def _risk_evidence_is_consistent(
    raw: object,
    *,
    evidence_cutoff: datetime,
    expected_symbol: str | None = None,
    diagnostic_memo: dict[tuple[str, str], bool] | None = None,
) -> bool:
    if not isinstance(raw, Mapping):
        return False
    market_reasons = _unique_string_list(raw.get("market_reason_codes"))
    sector_reasons = _unique_string_list(raw.get("sector_reason_codes"))
    symbol_reasons = _unique_string_list(raw.get("symbol_reason_codes"))
    merged_reasons = _unique_string_list(raw.get("reason_codes"))
    if (
        raw.get("new_entry_requires_all_green") is not True
        or market_reasons is None
        or sector_reasons is None
        or symbol_reasons is None
        or merged_reasons is None
        or merged_reasons
        != tuple(dict.fromkeys((*market_reasons, *sector_reasons, *symbol_reasons)))
    ):
        return False
    sector_extension_valid, sector_source_mode = _sector_source_extension_mode(
        raw,
        sector_reasons=sector_reasons,
    )
    if not sector_extension_valid:
        return False
    expected_fields = _RISK_DECISION_FIELDS | _RISK_CURRENT_EVIDENCE_FIELDS
    if sector_source_mode is not None:
        expected_fields |= _RISK_SECTOR_SOURCE_FIELDS
    if set(raw) != expected_fields:
        return False
    side_evidence_valid = (
        _risk_side_evidence_is_consistent(
            raw,
            prefix="market",
            evidence_cutoff=evidence_cutoff,
        )
        and _risk_side_evidence_is_consistent(
            raw,
            prefix="sector",
            evidence_cutoff=evidence_cutoff,
            allow_green_to_amber_cap=(
                sector_source_mode == QMT_SECTOR_NATIVE_DAILY_RESEARCH_SOURCE_MODE
            ),
        )
        and _risk_side_evidence_is_consistent(
            raw,
            prefix="symbol",
            evidence_cutoff=evidence_cutoff,
        )
    )
    if not side_evidence_valid:
        return False

    extension_fields = (
        "session_evidence_contract_id",
        "market_session_evidence",
        "sector_session_evidence",
        "symbol_session_evidence",
    )
    if (
        set(extension_fields) - set(raw)
        or raw.get("session_evidence_contract_id")
        != HIGHER_TIMEFRAME_SESSION_EVIDENCE_CONTRACT_ID
    ):
        return False
    session_valid = (
        _session_evidence_is_consistent(
            raw.get("market_session_evidence"),
            side_reasons=market_reasons,
            side_gate=raw.get("market_gate"),
            evidence_cutoff=evidence_cutoff,
        )
        and _session_evidence_is_consistent(
            raw.get("sector_session_evidence"),
            side_reasons=sector_reasons,
            side_gate=raw.get("sector_gate"),
            evidence_cutoff=evidence_cutoff,
        )
        and _session_evidence_is_consistent(
            raw.get("symbol_session_evidence"),
            side_reasons=symbol_reasons,
            side_gate=raw.get("symbol_gate"),
            evidence_cutoff=evidence_cutoff,
        )
    )
    if not session_valid:
        return False

    warmup_fields = (
        "warmup_evidence_contract_id",
        "market_warmup_evidence",
        "sector_warmup_evidence",
        "symbol_warmup_evidence",
    )
    if (
        set(warmup_fields) - set(raw)
        or raw.get("warmup_evidence_contract_id")
        != QMT_HIGHER_TIMEFRAME_WARMUP_EVIDENCE_CONTRACT_ID
    ):
        return False
    warmup_valid = (
        _mwd_warmup_evidence_is_consistent(
            raw.get("market_warmup_evidence"),
            side_reasons=market_reasons,
            side_gate=raw.get("market_gate"),
        )
        and _mwd_warmup_evidence_is_consistent(
            raw.get("sector_warmup_evidence"),
            side_reasons=sector_reasons,
            side_gate=raw.get("sector_gate"),
        )
        and _mwd_warmup_evidence_is_consistent(
            raw.get("symbol_warmup_evidence"),
            side_reasons=symbol_reasons,
            side_gate=raw.get("symbol_gate"),
        )
    )
    if not warmup_valid:
        return False
    convergence_fields = (
        "warmup_convergence_contract_id",
        "market_warmup_convergence_evidence",
        "sector_warmup_convergence_evidence",
        "symbol_warmup_convergence_evidence",
    )
    strict_convergence_field = "sector_strict_same_5m_warmup_convergence_evidence"
    if (
        set(convergence_fields) - set(raw)
        or raw.get("warmup_convergence_contract_id")
        != WARMUP_CONVERGENCE_ENVELOPE_CONTRACT_ID
        or not _mwd_warmup_convergence_evidence_is_consistent(
            raw.get("market_warmup_convergence_evidence"),
            evidence_cutoff=evidence_cutoff,
            side_gate=raw.get("market_gate"),
        )
        or not _mwd_warmup_convergence_evidence_is_consistent(
            raw.get("sector_warmup_convergence_evidence"),
            evidence_cutoff=evidence_cutoff,
            side_gate=raw.get("sector_gate"),
        )
        or not _mwd_warmup_convergence_evidence_is_consistent(
            raw.get("symbol_warmup_convergence_evidence"),
            evidence_cutoff=evidence_cutoff,
            side_gate=raw.get("symbol_gate"),
        )
    ):
        return False
    if sector_source_mode is None:
        if strict_convergence_field in raw:
            return False
    else:
        if strict_convergence_field not in raw:
            return False
        strict_convergence = raw.get(strict_convergence_field)
        if not _mwd_warmup_convergence_evidence_is_consistent(
            strict_convergence,
            evidence_cutoff=evidence_cutoff,
            side_gate=raw.get("sector_gate"),
        ):
            return False
        if (
            sector_source_mode == QMT_SECTOR_SAME_BASE_SOURCE_MODE
            and strict_convergence != raw.get("sector_warmup_convergence_evidence")
        ):
            return False
    diagnostic_fields = (
        "warmup_convergence_diagnostic_contract_id",
        "market_warmup_convergence_diagnostic_evidence",
        "sector_warmup_convergence_diagnostic_evidence",
        "symbol_warmup_convergence_diagnostic_evidence",
    )
    strict_diagnostic_field = (
        "sector_strict_same_5m_warmup_convergence_diagnostic_evidence"
    )
    if (
        set(diagnostic_fields) - set(raw)
        or raw.get("warmup_convergence_diagnostic_contract_id")
        != WARMUP_CONVERGENCE_DIAGNOSTIC_CONTRACT_ID
    ):
        return False
    if sector_source_mode is None:
        if strict_diagnostic_field in raw:
            return False
    elif strict_diagnostic_field not in raw:
        return False
    supply_diagnostic_fields = (
        "warmup_mapping_supply_diagnostic_contract_id",
        "market_warmup_mapping_supply_diagnostic_evidence",
        "sector_warmup_mapping_supply_diagnostic_evidence",
        "symbol_warmup_mapping_supply_diagnostic_evidence",
    )
    strict_supply_diagnostic_field = (
        "sector_strict_same_5m_warmup_mapping_supply_diagnostic_evidence"
    )
    if (
        set(supply_diagnostic_fields) - set(raw)
        or raw.get("warmup_mapping_supply_diagnostic_contract_id")
        != WARMUP_MAPPING_SUPPLY_DIAGNOSTIC_CONTRACT_ID
    ):
        return False
    if sector_source_mode is None:
        if strict_supply_diagnostic_field in raw:
            return False
    elif strict_supply_diagnostic_field not in raw:
        return False
    lineage_diagnostic_fields = (
        "warmup_structure_lineage_diagnostic_contract_id",
        "market_warmup_structure_lineage_diagnostic_evidence",
        "sector_warmup_structure_lineage_diagnostic_evidence",
        "symbol_warmup_structure_lineage_diagnostic_evidence",
    )
    strict_lineage_diagnostic_field = (
        "sector_strict_same_5m_warmup_structure_lineage_diagnostic_evidence"
    )
    if (
        set(lineage_diagnostic_fields) - set(raw)
        or raw.get("warmup_structure_lineage_diagnostic_contract_id")
        != WARMUP_STRUCTURE_LINEAGE_DIAGNOSTIC_CONTRACT_ID
    ):
        return False
    if sector_source_mode is None:
        if strict_lineage_diagnostic_field in raw:
            return False
    elif strict_lineage_diagnostic_field not in raw:
        return False

    for prefix in ("market", "sector", "symbol"):
        if not _mwd_warmup_diagnostic_chain_is_consistent(
            envelope_raw=raw.get(f"{prefix}_warmup_convergence_evidence"),
            semantic_raw=raw.get(f"{prefix}_warmup_convergence_diagnostic_evidence"),
            supply_raw=raw.get(f"{prefix}_warmup_mapping_supply_diagnostic_evidence"),
            lineage_raw=raw.get(
                f"{prefix}_warmup_structure_lineage_diagnostic_evidence"
            ),
            evidence_cutoff=evidence_cutoff,
            memo=diagnostic_memo,
        ):
            return False
    if sector_source_mode is not None and not (
        _mwd_warmup_diagnostic_chain_is_consistent(
            envelope_raw=raw.get(strict_convergence_field),
            semantic_raw=raw.get(strict_diagnostic_field),
            supply_raw=raw.get(strict_supply_diagnostic_field),
            lineage_raw=raw.get(strict_lineage_diagnostic_field),
            evidence_cutoff=evidence_cutoff,
            memo=diagnostic_memo,
        )
    ):
        return False
    if not _sector_source_extension_is_consistent(
        raw,
        sector_reasons=sector_reasons,
        mode=sector_source_mode,
        evidence_cutoff=evidence_cutoff,
    ):
        return False

    native_fields = (
        "native_daily_reconciliation_contract_id",
        "market_native_daily_reconciliation_evidence",
        "sector_native_daily_reconciliation_evidence",
        "symbol_native_daily_reconciliation_evidence",
    )
    if (
        set(native_fields) - set(raw)
        or raw.get("native_daily_reconciliation_contract_id")
        != QMT_NATIVE_DAILY_RECONCILIATION_CONTRACT_ID
        or raw.get("sector_native_daily_reconciliation_evidence") is not None
    ):
        return False

    def native_side_is_consistent(prefix: str, expected: str | None) -> bool:
        evidence = raw.get(f"{prefix}_native_daily_reconciliation_evidence")
        if evidence is None:
            return raw.get(f"{prefix}_gate") == "UNRESOLVED"
        return _native_daily_reconciliation_evidence_is_consistent(
            evidence,
            expected_symbol=expected,
            evidence_cutoff=evidence_cutoff,
        )

    native_valid = native_side_is_consistent(
        "market", None
    ) and native_side_is_consistent("symbol", expected_symbol)
    if not native_valid:
        return False

    calendar_fields = (
        "native_daily_calendar_coverage_contract_id",
        "market_native_daily_calendar_coverage_evidence",
        "sector_native_daily_calendar_coverage_evidence",
        "symbol_native_daily_calendar_coverage_evidence",
    )
    if (
        set(calendar_fields) - set(raw)
        or raw.get("native_daily_calendar_coverage_contract_id")
        != QMT_NATIVE_DAILY_CALENDAR_COVERAGE_EVIDENCE_CONTRACT_ID
        or raw.get("sector_native_daily_calendar_coverage_evidence") is not None
    ):
        return False

    def calendar_side_is_consistent(
        prefix: str,
        expected: str | None,
        reasons: tuple[str, ...],
    ) -> bool:
        evidence = raw.get(f"{prefix}_native_daily_calendar_coverage_evidence")
        if evidence is None:
            return "QMT_NATIVE_DAILY_TRADING_CALENDAR_MISMATCH" not in reasons
        if not _native_daily_calendar_coverage_evidence_is_consistent(
            evidence,
            expected_symbol=expected,
            evidence_cutoff=evidence_cutoff,
            side_reasons=reasons,
            side_gate=raw.get(f"{prefix}_gate"),
        ):
            return False
        try:
            coverage = QmtNativeDailyCalendarCoverageEvidence.from_document(evidence)
        except (TypeError, ValueError):
            return False
        reconciliation = raw.get(f"{prefix}_native_daily_reconciliation_evidence")
        return (coverage.status == "EXACT") == (reconciliation is not None)

    return calendar_side_is_consistent(
        "market", None, market_reasons
    ) and calendar_side_is_consistent("symbol", expected_symbol, symbol_reasons)


def _entry_gate_is_consistent(
    signal: Mapping[str, object],
    *,
    risk: Mapping[str, object],
    warmup: Mapping[str, object],
) -> bool:
    """Bind the serialized final decision to its three entry gates.

    ``technical_entry_allowed`` is captured immediately before the M/W/D and
    pairwise-warmup gates in :class:`TradingEngine`.  Consequently the final
    buy permission is exactly their conjunction.  Sell observations use the
    separate exit policy and must never acquire an entry permission here.
    """

    side = signal.get("side")
    technical = signal.get("technical_entry_allowed")
    entry_allowed = signal.get("entry_allowed")
    exit_allowed = signal.get("exit_allowed")
    if (
        side not in {"buy", "sell"}
        or type(technical) is not bool
        or type(entry_allowed) is not bool
        or type(exit_allowed) is not bool
    ):
        return False
    if side == "sell":
        return technical is False and entry_allowed is False
    expected_entry = bool(
        technical
        and warmup.get("converged") is True
        and risk.get("market_gate") == "GREEN"
        and risk.get("sector_gate") == "GREEN"
        and risk.get("symbol_gate") == "GREEN"
        and signal.get("monitor_only") is not True
    )
    return entry_allowed is expected_entry and exit_allowed is False


def _conflict_reason_codes(raw: object) -> tuple[str, ...] | None:
    if not isinstance(raw, Mapping) or type(raw.get("hard_block")) is not bool:
        return None
    blocking = _unique_string_list(raw.get("blocking_point_ids"))
    risk_only = _unique_string_list(raw.get("risk_only_point_ids"))
    if (
        blocking is None
        or risk_only is None
        or any(not _is_sha256_identity(value) for value in (*blocking, *risk_only))
        or blocking != tuple(sorted(blocking))
        or risk_only != tuple(sorted(risk_only))
        or set(blocking).intersection(risk_only)
        or raw.get("hard_block") is not bool(blocking)
    ):
        return None
    if blocking:
        return ("same_or_higher_structure_conflict",)
    if risk_only:
        return ("lower_or_unrelated_structure_risk",)
    return ()


def _decision_decimal(raw: object) -> Decimal | None:
    if not isinstance(raw, str):
        return None
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError):
        return None
    return value if value.is_finite() and value >= 0 else None


def _buy_decision_evidence_is_consistent(
    signal: Mapping[str, object],
    *,
    policy: Mapping[str, object],
    risk: Mapping[str, object],
    warmup: Mapping[str, object],
    conflict_reasons: tuple[str, ...],
) -> bool:
    setup = signal.get("setup_5m")
    trigger = signal.get("trigger_1m")
    sector = signal.get("sector")
    context = signal.get("context_30m")
    daily_context = signal.get("context_d")
    decision_reasons = _unique_string_list(signal.get("decision_reasons"))
    multiplier = _decision_decimal(signal.get("risk_multiplier"))
    if (
        not all(
            isinstance(value, Mapping)
            for value in (setup, sector, context, daily_context)
        )
        or (trigger is not None and not isinstance(trigger, Mapping))
        or decision_reasons is None
        or multiplier is None
    ):
        return False
    structural_point = bool(
        isinstance(setup.get("price_basis_revision"), str)
        and str(setup.get("price_basis_revision"))
    )
    confirmed_buy = bool(
        structural_point
        and setup.get("status") == "confirmed"
        and setup.get("side") == "buy"
        and setup.get("source_frequency") == "5m"
    )
    trigger_confirmed = bool(
        structural_point
        and isinstance(trigger, Mapping)
        and trigger.get("status") == "confirmed"
        and trigger.get("side") == setup.get("side")
        and trigger.get("source_frequency") == "1m"
        and signal.get("lifecycle_stage") in {"triggered", "executable"}
    )
    entry_reasons: list[str] = []
    if policy.get("require_confirmed_five_minute") is True and not confirmed_buy:
        entry_reasons.append("five_minute_not_confirmed")
    if policy.get("require_confirmed_one_minute") is True and not trigger_confirmed:
        entry_reasons.append("one_minute_not_confirmed")
    if (
        policy.get("require_sector_eligibility") is True
        and sector.get("hard_block") is True
    ):
        entry_reasons.append("sector_hostile")
    if (
        policy.get("require_thirty_minute_context") is True
        and context.get("hard_block") is True
    ):
        entry_reasons.append("thirty_minute_hostile")
    conflict = signal.get("conflict")
    if isinstance(conflict, Mapping) and conflict.get("hard_block") is True:
        entry_reasons.append("structure_conflict")
    if structural_point and setup.get("point_type") == "3buy":
        if (
            policy.get("first_center_three_buy_only") is True
            and setup.get("center_ordinal") != 1
        ):
            entry_reasons.append("three_buy_not_first_center")
        try:
            clearance = Decimal(str(setup["anchor_price"])) - Decimal(
                str(setup["center_zg"])
            )
            minimum_tick = Decimal(str(policy["minimum_tick"]))
        except (InvalidOperation, KeyError, TypeError, ValueError):
            clearance = None
            minimum_tick = Decimal("0")
        if (
            setup.get("variant") == "boundary_touch"
            or clearance is None
            or not clearance.is_finite()
            or clearance < minimum_tick
        ):
            entry_reasons.append("three_buy_lacks_tick_clearance")
    multiplier_field = {
        "1buy": "first_buy_risk_multiplier",
        "2buy": "second_buy_risk_multiplier",
        "3buy": "third_buy_risk_multiplier",
    }.get(str(setup.get("point_type")))
    try:
        expected_multiplier = (
            Decimal(str(policy[multiplier_field]))
            if structural_point and multiplier_field is not None
            else Decimal("0")
        )
        expected_stop = (
            Decimal(str(setup["invalidation_price"])) if structural_point else None
        )
        actual_stop = (
            None
            if signal.get("structural_stop") is None
            else Decimal(str(signal["structural_stop"]))
        )
    except (InvalidOperation, KeyError, TypeError, ValueError):
        return False
    if (
        not expected_multiplier.is_finite()
        or expected_multiplier < 0
        or expected_stop is not None
        and (not expected_stop.is_finite() or expected_stop <= 0)
        or actual_stop is not None
        and (not actual_stop.is_finite() or actual_stop <= 0)
    ):
        return False
    if not entry_reasons and daily_context.get("hard_block") is True:
        entry_reasons.append("daily_structure_hostile")
        expected_multiplier *= 0
    technical_entry_allowed = not entry_reasons
    risk_blocked = bool(
        risk.get("market_gate") != "GREEN"
        or risk.get("sector_gate") != "GREEN"
        or risk.get("symbol_gate") != "GREEN"
    )
    warmup_blocked = warmup.get("converged") is not True
    if technical_entry_allowed and (risk_blocked or warmup_blocked):
        if risk_blocked:
            entry_reasons.extend(
                (
                    "HIGHER_TIMEFRAME_GATE_NOT_GREEN",
                    f"MARKET_GATE_{risk.get('market_gate')}",
                    f"SECTOR_GATE_{risk.get('sector_gate')}",
                    f"SYMBOL_GATE_{risk.get('symbol_gate')}",
                    *(str(value) for value in risk.get("reason_codes") or ()),
                )
            )
        if warmup_blocked:
            entry_reasons.extend(
                (
                    "WARMUP_CONVERGENCE_GATE_FAILED",
                    *(str(value) for value in warmup.get("reason_codes") or ()),
                )
            )
        expected_multiplier *= 0
    # ``resolve_conflict`` emits one explicit provenance reason when the setup
    # is still a ProvisionalCandidate.  It has no point IDs to serialize, so
    # the reason must be reconstructed from the setup status rather than from
    # the otherwise empty conflict lists.
    setup_conflict_reasons = (
        ("setup_not_confirmed",) if setup.get("status") == "provisional" else ()
    )
    selection_reasons = (
        (MONITOR_ONLY_BUY_REASON_CODE,) if signal.get("monitor_only") is True else ()
    )
    if selection_reasons:
        # HumanAssistedDecisionCore applies the sector-first scope to the
        # entry decision before ``serialize_evaluated_signal`` appends setup
        # conflict reasons.  Reconstruct the same order here; putting the
        # monitor-only reason after ``setup_not_confirmed`` rejects otherwise
        # canonical provisional watchlist rows.
        entry_reasons.extend(selection_reasons)
        expected_multiplier *= 0
    expected_entry_reasons = tuple(dict.fromkeys(entry_reasons))
    expected_decision_reasons = tuple(
        dict.fromkeys(
            (
                *expected_entry_reasons,
                *conflict_reasons,
                *setup_conflict_reasons,
            )
        )
    )
    return bool(
        signal.get("technical_entry_allowed") is technical_entry_allowed
        and signal.get("entry_allowed")
        is (not expected_entry_reasons and not selection_reasons)
        and signal.get("exit_allowed") is False
        and signal.get("exit_action") == "none"
        and multiplier == expected_multiplier
        and actual_stop == expected_stop
        and decision_reasons == expected_decision_reasons
    )


def _displayed_decision_evidence_is_consistent(
    signal: Mapping[str, object],
    *,
    policy: object,
    risk: Mapping[str, object],
    warmup: Mapping[str, object],
) -> bool:
    context = signal.get("context_30m")
    decision_reasons = _unique_string_list(signal.get("decision_reasons"))
    conflict_reasons = _conflict_reason_codes(signal.get("conflict"))
    if (
        not isinstance(policy, Mapping)
        or not _decision_context_is_consistent(context)
        or decision_reasons is None
        or conflict_reasons is None
    ):
        return False
    if signal.get("side") == "buy":
        return _buy_decision_evidence_is_consistent(
            signal,
            policy=policy,
            risk=risk,
            warmup=warmup,
            conflict_reasons=conflict_reasons,
        )
    multiplier = _decision_decimal(signal.get("risk_multiplier"))
    setup = signal.get("setup_5m")
    trigger = signal.get("trigger_1m")
    if not isinstance(setup, Mapping):
        return False
    confirmed_sell = bool(
        isinstance(setup.get("price_basis_revision"), str)
        and setup.get("price_basis_revision")
        and setup.get("status") == "confirmed"
        and setup.get("side") == "sell"
        and setup.get("source_frequency") == "5m"
    )
    trigger_confirmed = bool(
        isinstance(trigger, Mapping)
        and trigger.get("status") == "confirmed"
        and trigger.get("side") == "sell"
        and trigger.get("source_frequency") == "1m"
        and signal.get("lifecycle_stage") in {"triggered", "executable"}
    )
    exit_action = signal.get("exit_action")
    if not confirmed_sell:
        expected_sell_reasons = ("sell_not_confirmed",)
    elif policy.get("require_confirmed_one_minute") is True and not trigger_confirmed:
        expected_sell_reasons = ("one_minute_sell_not_confirmed",)
    elif exit_action == "none":
        expected_sell_reasons = ("no_active_position",)
    elif exit_action == "exit_full":
        expected_sell_reasons = ("same_or_higher_sell",)
    elif exit_action == "reduce_tactical":
        expected_sell_reasons = ("lower_or_different_structure_sell",)
    else:
        return False
    return bool(
        signal.get("side") == "sell"
        and multiplier == Decimal("0")
        and signal.get("structural_stop") is None
        and signal.get("technical_entry_allowed") is False
        and signal.get("entry_allowed") is False
        and type(signal.get("exit_allowed")) is bool
        and exit_action in {"none", "reduce_tactical", "exit_full"}
        and signal.get("exit_allowed") is (exit_action != "none")
        and not conflict_reasons
        and decision_reasons == expected_sell_reasons
    )


def _validated_sector_strength_evidence(
    payload: Mapping[str, object],
    *,
    review_at: datetime,
) -> SectorStrengthBatch:
    raw = payload.get("sector_strength_evidence")
    revision = payload.get("sector_strength_evidence_revision")
    manifest = payload.get("coverage_manifest")
    try:
        batch = sector_strength_batch_from_evidence_document(raw)
        scanned_at = datetime.fromisoformat(str(payload["scanned_at"]))
        evidence_at = datetime.fromisoformat(
            str(batch.evidence_document()["decision_time"])
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("live screening sector strength evidence is invalid") from exc
    document = batch.evidence_document()
    if (
        not _is_sha256_identity(revision)
        or revision != batch.evidence_revision
        or not isinstance(manifest, Mapping)
        or document.get("membership_revision")
        != manifest.get("sector_catalog_revision")
        or scanned_at.tzinfo is None
        or evidence_at.tzinfo is None
        or evidence_at.date() != review_at.date()
        or evidence_at < review_at
        or evidence_at > scanned_at
    ):
        raise ValueError("live screening sector strength evidence is invalid")
    return batch


def _authenticated_sector_members(
    strength_evidence: SectorStrengthBatch,
) -> dict[str, frozenset[str]]:
    """Recover current QMT membership from the already-validated batch.

    The compact strength objects intentionally omit their full member lists,
    but the canonical batch evidence retains them.  A sector trigger is not
    proven merely because its claimed sector is eligible and ranked: the
    signal symbol must also occur in that sector's authenticated current-QMT
    basket.  The parser has already checked unique/sorted symbols, counts and
    the batch evidence identity before this helper is called.
    """

    document = strength_evidence.evidence_document()
    raw_sectors = document.get("sectors")
    if not isinstance(raw_sectors, list):  # pragma: no cover - parser guards it
        raise ValueError("live screening sector membership is invalid")
    output: dict[str, frozenset[str]] = {}
    for raw in raw_sectors:
        if not isinstance(raw, Mapping):  # pragma: no cover - parser guards it
            raise ValueError("live screening sector membership is invalid")
        sector_id = raw.get("sector_id")
        members = raw.get("member_symbols")
        if not isinstance(sector_id, str) or not isinstance(members, list):
            raise ValueError("live screening sector membership is invalid")
        output[sector_id] = frozenset(str(value) for value in members)
    return output


def _sector_exclusion_documents(
    raw: object,
) -> dict[str, Mapping[str, object]]:
    if not isinstance(raw, list):
        raise ValueError("live screening sector exclusions are unavailable")
    expected_keys = {
        "sector_id",
        "code",
        "exclusion_type",
        "eligibility",
        "reason_code",
        "reason",
        "detail_code",
        "catalog_member_count",
        "universe_member_count",
        "required_member_count",
        "deterministic_for_catalog_revision",
        "retry_policy",
    }
    allowed_details = {
        "sector_catalog_members_missing",
        "sector_constituent_count_below_minimum",
        "sector_universe_member_coverage_insufficient",
    }
    output: dict[str, Mapping[str, object]] = {}
    ordered_ids: list[str] = []
    for document in raw:
        if not isinstance(document, Mapping) or set(document) != expected_keys:
            raise ValueError("live screening sector exclusion is malformed")
        sector_id = document.get("sector_id")
        code = document.get("code")
        detail_code = document.get("detail_code")
        catalog_count = document.get("catalog_member_count")
        universe_count = document.get("universe_member_count")
        required_count = document.get("required_member_count")
        if (
            not isinstance(sector_id, str)
            or not sector_id
            or not isinstance(code, str)
            or not code
            or document.get("exclusion_type") != "sector_analysis_exclusion"
            or document.get("eligibility") != "MINIMUM_SECTOR_MEMBERS_NOT_MET"
            or document.get("reason_code") != "sector_member_coverage_insufficient"
            or not isinstance(document.get("reason"), str)
            or not document.get("reason")
            or detail_code not in allowed_details
            or type(catalog_count) is not int
            or catalog_count < 0
            or type(universe_count) is not int
            or universe_count < 0
            or type(required_count) is not int
            or required_count <= 0
            or universe_count >= required_count
            or document.get("deterministic_for_catalog_revision") is not True
            or document.get("retry_policy") != "NEXT_SECTOR_CATALOG_REVISION"
        ):
            raise ValueError("live screening sector exclusion is malformed")
        expected_detail = (
            "sector_catalog_members_missing"
            if catalog_count == 0
            else (
                "sector_constituent_count_below_minimum"
                if catalog_count < required_count
                else "sector_universe_member_coverage_insufficient"
            )
        )
        expected_reason = (
            f"catalog_members={catalog_count}; "
            f"universe_members={universe_count}; required={required_count}"
        )
        if detail_code != expected_detail or document.get("reason") != expected_reason:
            raise ValueError("live screening sector exclusion is inconsistent")
        ordered_ids.append(sector_id)
        output[sector_id] = document
    if ordered_ids != sorted(set(ordered_ids)) or len(output) != len(raw):
        raise ValueError("live screening sector exclusions are not canonical")
    return output


def _validated_sector_documents(
    payload: Mapping[str, object],
    *,
    review_at: datetime,
    market_data_as_of: datetime,
    strength_evidence: SectorStrengthBatch,
) -> dict[str, Mapping[str, object]]:
    """Recompute the full assessment set and its eligible ranked subset.

    The page deliberately publishes every completed/failed sector assessment:
    eligible sectors receive a rank, while structurally hostile or explicitly
    failed sectors remain unranked so monitor-only symbols retain their actual
    sector context.  Treating the list as an eligible-only table would reject
    every production snapshot containing a higher-timeframe sector block.
    """

    raw_sectors = payload.get("sectors")
    audit = payload.get("scan_audit")
    raw_errors = payload.get("errors")
    raw_exclusions = payload.get("sector_exclusions")
    if (
        not isinstance(raw_sectors, list)
        or not isinstance(audit, Mapping)
        or not isinstance(raw_errors, list)
    ):
        raise ValueError("live screening sector ranking is unavailable")
    sector_exclusions = _sector_exclusion_documents(raw_exclusions)
    sector_failures: dict[str, Mapping[str, object]] = {}
    failure_counts: dict[str, int] = {}
    for error in raw_errors:
        if not isinstance(error, Mapping) or "sector_id" not in error:
            continue
        failed_sector_id = error.get("sector_id")
        error_type = error.get("error_type")
        reason = error.get("reason")
        detail_code = error.get("detail_code")
        if (
            not isinstance(failed_sector_id, str)
            or not failed_sector_id
            or failed_sector_id in sector_failures
            or not isinstance(error_type, str)
            or not error_type
            or error_type == "sector_member_coverage_insufficient"
            or not isinstance(reason, str)
            or not reason
            or detail_code is not None
            and (not isinstance(detail_code, str) or not detail_code)
        ):
            raise ValueError("live screening sector failure evidence is invalid")
        sector_failures[failed_sector_id] = error
        failure_counts[error_type] = failure_counts.get(error_type, 0) + 1
    if set(sector_failures) & set(sector_exclusions):
        raise ValueError("live screening sector dispositions overlap")
    sectors: dict[str, Mapping[str, object]] = {}
    sortable: list[tuple[bool, Decimal, int, str]] = []
    ordered_ids: list[str] = []
    unranked_ids: list[str] = []
    for raw in raw_sectors:
        if not isinstance(raw, Mapping):
            raise ValueError("live screening sector ranking is invalid")
        required_fields = {
            "sector_id",
            "sector_name",
            "eligible",
            "hard_block",
            "regime",
            "rank",
            "rank_score",
            "rank_components",
            "reason_codes",
            "horizontal_strength",
            "horizontal_rank",
            "strength_anchor_session",
            "strength_member_count",
            "strength_source_revision",
            "strength_reason_codes",
            "context_30m",
            "context_5m",
            "context_1m",
        }
        sector_id = raw.get("sector_id")
        sector_name = raw.get("sector_name")
        contexts = {
            "30m": raw.get("context_30m"),
            "5m": raw.get("context_5m"),
            "1m": raw.get("context_1m"),
        }
        if (
            not isinstance(sector_id, str)
            or required_fields - set(raw)
            or not sector_id
            or sector_id in sectors
            or not isinstance(sector_name, str)
            or not sector_name
        ):
            raise ValueError("live screening sector ranking is invalid")
        failure = sector_failures.get(sector_id)
        exclusion = sector_exclusions.get(sector_id)
        if failure is not None or exclusion is not None:
            if any(context is not None for context in contexts.values()):
                raise ValueError("live screening unavailable sector context is invalid")
            hard_block = True
            expected_components: dict[str, int] = {}
            expected_score = 0
            expected_regime = "hostile"
            if failure is not None:
                expected_reasons = [str(failure["error_type"])]
                if failure.get("detail_code") is not None:
                    expected_reasons.append(str(failure["detail_code"]))
            else:
                assert exclusion is not None
                expected_reasons = [
                    str(exclusion["reason_code"]),
                    str(exclusion["detail_code"]),
                ]
            eligible_for_ranking = False
        else:
            if any(
                not _sector_context_is_consistent(
                    context,
                    frequency=frequency,
                    evidence_cutoff=market_data_as_of,
                )
                for frequency, context in contexts.items()
            ):
                raise ValueError("live screening sector ranking is invalid")
            thirty = contexts["30m"]
            five = contexts["5m"]
            one = contexts["1m"]
            assert isinstance(thirty, Mapping)
            assert isinstance(five, Mapping)
            assert isinstance(one, Mapping)
            hard_block = thirty.get("hard_block") is True
            expected_components = {
                "thirty_support": (
                    40 if thirty.get("disposition") == "supportive" else 0
                ),
                "five_support": (30 if five.get("disposition") == "supportive" else 0),
                "one_support": (10 if one.get("disposition") == "supportive" else 0),
                "neutral_access": 0 if hard_block else 5,
            }
            expected_score = sum(expected_components.values())
            expected_regime = (
                "hostile"
                if hard_block
                else "supportive"
                if any(
                    expected_components[name] > 0
                    for name in (
                        "thirty_support",
                        "five_support",
                        "one_support",
                    )
                )
                else "neutral"
            )
            expected_reasons = (
                ["higher_structure_sell_risk"]
                if hard_block
                else ["structural_ranking_only"]
            )
            eligible_for_ranking = not hard_block
        raw_rank = raw.get("rank")
        raw_strength = raw.get("horizontal_strength")
        horizontal_rank = raw.get("horizontal_rank")
        source_revision = raw.get("strength_source_revision")
        strength_reasons = raw.get("strength_reason_codes")
        try:
            strength = None if raw_strength is None else Decimal(str(raw_strength))
            anchor = (
                None
                if raw.get("strength_anchor_session") is None
                else date.fromisoformat(str(raw["strength_anchor_session"]))
            )
            member_count = int(raw.get("strength_member_count"))
        except (InvalidOperation, TypeError, ValueError):
            raise ValueError("live screening sector strength is invalid") from None
        strength_resolved = strength is not None
        expected_strength = strength_evidence.get(sector_id)
        if (
            raw.get("eligible") is not (not hard_block)
            or raw.get("hard_block") is not hard_block
            or (
                type(raw_rank) is not int
                if eligible_for_ranking
                else raw_rank is not None
            )
            or eligible_for_ranking
            and raw_rank <= 0
            or raw.get("rank_components") != expected_components
            or raw.get("rank_score") != expected_score
            or raw.get("regime") != expected_regime
            or raw.get("reason_codes") != expected_reasons
            or strength is not None
            and not strength.is_finite()
            or strength_resolved
            != (type(horizontal_rank) is int and horizontal_rank > 0)
            or strength_resolved
            and (
                anchor is None
                or anchor > review_at.date()
                or member_count <= 0
                or not _is_sha256_identity(source_revision)
            )
            or not strength_resolved
            and (
                horizontal_rank is not None
                or source_revision is None
                and (anchor is not None or member_count != 0)
                or source_revision is not None
                and (
                    not _is_sha256_identity(source_revision)
                    or anchor is not None
                    and anchor > review_at.date()
                )
            )
            or not isinstance(strength_reasons, list)
            or any(
                not isinstance(value, str) or not value for value in strength_reasons
            )
            or len(strength_reasons) != len(set(strength_reasons))
            or expected_strength is None
            or strength != expected_strength.strength
            or horizontal_rank != expected_strength.rank
            or anchor != expected_strength.anchor_session
            or member_count != expected_strength.member_count
            or source_revision != expected_strength.source_revision
            or strength_reasons != list(expected_strength.reason_codes)
        ):
            raise ValueError("live screening sector ranking is invalid")
        sectors[sector_id] = raw
        ordered_ids.append(sector_id)
        if eligible_for_ranking:
            sortable.append(
                (
                    strength is None,
                    -(strength if strength is not None else Decimal("0")),
                    -expected_score,
                    sector_id,
                )
            )
        else:
            unranked_ids.append(sector_id)
    expected_ranked_order = [
        sector_id for _missing, _strength, _score, sector_id in sorted(sortable)
    ]
    expected_order = expected_ranked_order + sorted(unranked_ids)
    if ordered_ids != expected_order:
        raise ValueError("live screening sector ranking order is invalid")
    if any(
        sectors[sector_id].get("rank") != ordinal
        for ordinal, sector_id in enumerate(expected_ranked_order, start=1)
    ):
        raise ValueError("live screening sector ranking is invalid")
    try:
        discovered_count = int(audit.get("sector_discovered_count"))
        completed_count = int(audit.get("sector_completed_count"))
        excluded_count = int(audit.get("sector_excluded_count"))
        failed_count = int(audit.get("sector_failed_count"))
        resolved_count = int(audit.get("sector_resolved_count"))
        selected_count = int(audit.get("selected_sector_count"))
        completion_ratio = Decimal(str(audit.get("sector_completion_ratio")))
        resolution_ratio = Decimal(str(audit.get("sector_resolution_ratio")))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("live screening sector coverage is invalid") from None
    expected_completed = len(sectors) - len(sector_failures) - len(sector_exclusions)
    expected_resolved = expected_completed + len(sector_exclusions)
    expected_ratio = (
        Decimal("0")
        if not sectors
        else Decimal(expected_completed) / Decimal(len(sectors))
    )
    expected_resolution_ratio = (
        Decimal("0")
        if not sectors
        else Decimal(expected_resolved) / Decimal(len(sectors))
    )
    exclusion_counts: dict[str, int] = {}
    for exclusion in sector_exclusions.values():
        reason_code = str(exclusion["reason_code"])
        exclusion_counts[reason_code] = exclusion_counts.get(reason_code, 0) + 1
    if (
        discovered_count != len(sectors)
        or completed_count != expected_completed
        or excluded_count != len(sector_exclusions)
        or failed_count != len(sector_failures)
        or resolved_count != expected_resolved
        or selected_count != len(expected_ranked_order)
        or completion_ratio != expected_ratio
        or resolution_ratio != expected_resolution_ratio
        or audit.get("sector_failure_counts") != dict(sorted(failure_counts.items()))
        or audit.get("sector_exclusion_counts")
        != dict(sorted(exclusion_counts.items()))
        or set(sector_failures).difference(sectors)
        or set(sector_exclusions).difference(sectors)
        or set(strength_evidence) != set(sectors)
    ):
        raise ValueError("live screening sector coverage is invalid")
    return sectors


def validate_live_review_snapshot(
    payload: Mapping[str, object],
    *,
    session: date | None = None,
) -> tuple[datetime, tuple[Mapping[str, object], ...]]:
    """Validate the safety, coverage and 30m/5m/1m provenance boundary."""

    try:
        review_at = datetime.fromisoformat(str(payload["as_of"]))
        market_data_as_of = datetime.fromisoformat(str(payload["market_data_as_of"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("live screening review cutoff is invalid") from exc
    audit = payload.get("scan_audit")
    manifest = payload.get("coverage_manifest")
    raw_signals = payload.get("signals")
    errors = payload.get("errors")
    decision_core = payload.get("decision_core")
    decision_core_id = payload.get("decision_core_id")
    verified_decision_core_id: str | None = None
    if isinstance(decision_core, Mapping):
        try:
            verified_decision_core_id = validate_human_assisted_contract_document(
                decision_core
            )
        except (TypeError, ValueError):
            verified_decision_core_id = None
    coverage_manifest_consistent = (
        _coverage_manifest_is_consistent(
            payload,
            audit=audit,
            manifest=manifest,
            errors=errors,
        )
        if isinstance(audit, Mapping)
        and isinstance(manifest, Mapping)
        and isinstance(errors, list)
        else False
    )
    monitor_exclusions_consistent = monitor_instrument_exclusions_are_consistent(
        payload
    )
    try:
        sector_coverage = (
            Decimal(str(audit.get("sector_completion_ratio")))
            if isinstance(audit, Mapping)
            else Decimal("NaN")
        )
        stock_coverage = (
            Decimal(str(audit.get("coverage_cycle_completion_ratio")))
            if isinstance(audit, Mapping)
            else Decimal("NaN")
        )
    except (InvalidOperation, TypeError, ValueError):
        sector_coverage = Decimal("NaN")
        stock_coverage = Decimal("NaN")
    if (
        payload.get("schema") != LIVE_SCREENING_SCHEMA
        or payload.get("signal_document_contract_id") != SIGNAL_DOCUMENT_CONTRACT_ID
        or payload.get("sector_coverage_contract_id") != SECTOR_COVERAGE_CONTRACT_ID
        or payload.get("available") is not True
        or payload.get("scan_state") != "complete"
        or payload.get("sector_first") is not True
        or payload.get("read_only") is not True
        or payload.get("research_only") is not True
        or payload.get("no_order_execution") is not True
        or review_at.tzinfo is None
        or (
            session is not None
            and review_at.astimezone(ZoneInfo("Asia/Shanghai")).date() != session
        )
        or not isinstance(audit, Mapping)
        or not isinstance(manifest, Mapping)
        or manifest.get("signal_document_contract_id") != SIGNAL_DOCUMENT_CONTRACT_ID
        or not isinstance(raw_signals, list)
        or not isinstance(errors, list)
        or not coverage_manifest_consistent
        or not monitor_exclusions_consistent
        or not isinstance(decision_core, Mapping)
        or not isinstance(decision_core_id, str)
        or not decision_core_id.startswith("sha256:")
        or verified_decision_core_id != decision_core_id
        or decision_core.get("contract_id") != decision_core_id
        or decision_core.get("strategic_frequency") != "30m"
        or decision_core.get("tactical_frequency") != "5m"
        or decision_core.get("locator_frequency") != "1m"
        or decision_core.get("human_confirmation_required") is not True
        or decision_core.get("automated_order_authorized") is not False
        or decision_core.get("live_status") != "LIVE_DISABLED"
        # The coverage epoch authenticates the declared market cutoff, but it
        # cannot by itself prove that the cutoff is causal for this decision
        # page.  A future cutoff can be re-identified with a fresh valid epoch
        # and semantic hash.  Bind both clocks here so /readyz and the archive
        # use one temporal boundary instead of relying on a later duplicate
        # check in the daily tool.
        or market_data_as_of.tzinfo is None
        or market_data_as_of > review_at
        or market_data_as_of.astimezone(ZoneInfo("Asia/Shanghai")).date()
        != review_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
        or audit.get("coverage_cycle_complete") is not True
        or not sector_coverage.is_finite()
        or sector_coverage < Decimal("0.80")
        or not stock_coverage.is_finite()
        or stock_coverage < MIN_LIVE_REVIEW_STOCK_COVERAGE
        or int(audit.get("pending_symbol_count") or 0) != 0
        or manifest.get("complete") is not True
        or not isinstance(payload.get("snapshot_content_sha256"), str)
        or payload.get("snapshot_content_sha256")
        != live_screening_snapshot_content_sha256(payload)
        or audit.get("full_market_history_scan") is not False
    ):
        raise ValueError("live screening snapshot review boundary is incomplete")

    strength_evidence = _validated_sector_strength_evidence(
        payload,
        review_at=review_at,
    )
    sector_members = _authenticated_sector_members(strength_evidence)
    sector_documents = _validated_sector_documents(
        payload,
        review_at=review_at,
        market_data_as_of=market_data_as_of,
        strength_evidence=strength_evidence,
    )
    discovered_codes = manifest.get("discovered_codes")
    if not isinstance(discovered_codes, list):  # coverage parser guards this
        raise ValueError("live screening eligible sector member coverage is invalid")
    required_sector_members = {
        symbol
        for sector_id, document in sector_documents.items()
        if document.get("eligible") is True
        for symbol in sector_members.get(sector_id, frozenset())
    }
    missing_sector_members = required_sector_members.difference(discovered_codes)
    if missing_sector_members:
        # ``universe_revision`` authenticates the service's declared universe,
        # but an opaque hash cannot independently prove that the declaration
        # contains every current member.  The already-validated horizontal
        # strength batch carries the canonical QMT member lists, so bind the
        # completed stock ledger to that evidence before any signal is admitted.
        # Explicit watchlist/holding monitors may add codes; they may never make
        # an eligible sector member optional.
        raise ValueError("live screening eligible sector member coverage is incomplete")

    signals: list[Mapping[str, object]] = []
    diagnostic_memo: dict[tuple[str, str], bool] = {}
    for raw in raw_signals:
        if not isinstance(raw, Mapping):
            raise ValueError("live screening signal is invalid")
        try:
            observed_at = datetime.fromisoformat(str(raw["observed_at"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("live screening signal time is invalid") from exc
        # A page-wide market cutoff is only an outer ceiling.  Every nested
        # decision fact must also have existed by this signal's own lifecycle
        # observation, otherwise an older signal could silently consume a
        # later setup, locator, daily context, or M/W/D diagnostic.
        signal_evidence_cutoff = (
            observed_at
            if observed_at.tzinfo is not None and observed_at <= market_data_as_of
            else market_data_as_of
        )
        setup = raw.get("setup_5m")
        trigger = raw.get("trigger_1m")
        daily_context = raw.get("context_d")
        entry_boundary = _entry_boundary_from_document(
            raw.get("entry_execution_boundary")
        )
        context = raw.get("context_30m")
        risk = raw.get("higher_timeframe_risk")
        warmup = raw.get("warmup")
        market_reasons = (
            risk.get("market_reason_codes") if isinstance(risk, Mapping) else None
        )
        symbol_reasons = (
            risk.get("symbol_reason_codes") if isinstance(risk, Mapping) else None
        )
        sector_reasons = (
            risk.get("sector_reason_codes") if isinstance(risk, Mapping) else None
        )
        merged_reasons = risk.get("reason_codes") if isinstance(risk, Mapping) else None
        market_diagnostics = (
            risk.get("market_period_diagnostics") if isinstance(risk, Mapping) else None
        )
        symbol_diagnostics = (
            risk.get("symbol_period_diagnostics") if isinstance(risk, Mapping) else None
        )
        sector_diagnostics = (
            risk.get("sector_period_diagnostics") if isinstance(risk, Mapping) else None
        )
        reason_contract_valid = (
            isinstance(market_reasons, list)
            and all(isinstance(value, str) for value in market_reasons)
            and isinstance(symbol_reasons, list)
            and all(isinstance(value, str) for value in symbol_reasons)
            and isinstance(sector_reasons, list)
            and all(isinstance(value, str) for value in sector_reasons)
            and isinstance(merged_reasons, list)
            and all(isinstance(value, str) for value in merged_reasons)
            and merged_reasons
            == list(dict.fromkeys((*market_reasons, *sector_reasons, *symbol_reasons)))
        )
        diagnostic_contract_valid = (
            isinstance(market_diagnostics, list)
            and all(isinstance(value, Mapping) for value in market_diagnostics)
            and isinstance(symbol_diagnostics, list)
            and all(isinstance(value, Mapping) for value in symbol_diagnostics)
            and isinstance(sector_diagnostics, list)
            and all(isinstance(value, Mapping) for value in sector_diagnostics)
        )
        if not _warmup_evidence_is_consistent(warmup):
            raise ValueError("live screening signal warmup evidence is invalid")
        if not _risk_evidence_is_consistent(
            risk,
            evidence_cutoff=signal_evidence_cutoff,
            expected_symbol=str(raw.get("code")),
            diagnostic_memo=diagnostic_memo,
        ):
            raise ValueError("live screening signal risk evidence is invalid")
        if (
            not isinstance(risk, Mapping)
            or not isinstance(warmup, Mapping)
            or not _entry_gate_is_consistent(raw, risk=risk, warmup=warmup)
        ):
            raise ValueError("live screening signal entry gate is invalid")
        setup_is_causal = _point_document_is_causal(
            setup,
            frequency="5m",
            evidence_cutoff=signal_evidence_cutoff,
        )
        trigger_is_causal = trigger is None or _point_document_is_causal(
            trigger,
            frequency="1m",
            evidence_cutoff=signal_evidence_cutoff,
        )
        trigger_available_at = None
        if isinstance(trigger, Mapping):
            try:
                trigger_available_at = datetime.fromisoformat(
                    str(trigger["available_at"])
                )
            except (KeyError, TypeError, ValueError):
                trigger_available_at = None
        raw_selection_sources = raw.get("selection_sources")
        selection_sources = (
            tuple(raw_selection_sources)
            if isinstance(raw_selection_sources, list)
            else None
        )
        signal_sector = raw.get("sector")
        sector_document_consistent = False
        if isinstance(signal_sector, Mapping) and selection_sources:
            signal_sector_id = signal_sector.get("sector_id")
            expected_sector = sector_documents.get(str(signal_sector_id))
            if expected_sector is not None:
                expected_signal_sector = dict(expected_sector)
                expected_signal_sector["rank"] = None
                sector_document_consistent = (
                    dict(signal_sector) == expected_signal_sector
                    and (
                        "QMT_SECTOR_TRIGGER" not in selection_sources
                        or (
                            expected_sector.get("eligible") is True
                            and expected_sector.get("regime") == "supportive"
                            and type(expected_sector.get("rank")) is int
                            and isinstance(raw.get("code"), str)
                            and raw.get("code")
                            in sector_members.get(str(signal_sector_id), frozenset())
                        )
                    )
                    and (
                        "QMT_SECTOR_ELIGIBLE_SCOPE" not in selection_sources
                        or (
                            expected_sector.get("eligible") is True
                            and expected_sector.get("regime") != "supportive"
                            and type(expected_sector.get("rank")) is int
                            and isinstance(raw.get("code"), str)
                            and raw.get("code")
                            in sector_members.get(
                                str(signal_sector_id),
                                frozenset(),
                            )
                        )
                    )
                )
            elif "QMT_SECTOR_TRIGGER" not in selection_sources:
                sector_document_consistent = signal_sector_id == "unclassified"
        signal_identity_consistent = False
        if isinstance(setup, Mapping) and isinstance(signal_sector, Mapping):
            setup_point_id = setup.get("point_id")
            sector_id = signal_sector.get("sector_id")
            if isinstance(setup_point_id, str) and isinstance(sector_id, str):
                expected_setup_id = sha256_json(
                    {
                        "schema": "chanlun-trade-setup",
                        "point_id": setup_point_id,
                        "sector_id": sector_id,
                    }
                )
                expected_signal_id = sha256_json(
                    {
                        "schema": "chanlun-signal-lifecycle",
                        "setup_id": expected_setup_id,
                        "side": setup.get("side"),
                    }
                )
                signal_identity_consistent = bool(
                    raw.get("point_id") == setup_point_id
                    and raw.get("point_type") == setup.get("point_type")
                    and raw.get("side") == setup.get("side")
                    and raw.get("tower") == setup.get("tower")
                    and raw.get("recursive_level") == setup.get("recursive_level")
                    and raw.get("setup_id") == expected_setup_id
                    and raw.get("signal_id") == expected_signal_id
                )
        if (
            observed_at.tzinfo is None
            or observed_at > market_data_as_of
            or raw.get("decision_core_id") != decision_core_id
            or raw.get("human_confirmation_required") is not True
            or raw.get("automated_order_authorized") is not False
            or raw.get("live_status") != "LIVE_DISABLED"
            or raw.get("structure_scope") != "physical-timeframe-recursive"
            or raw.get("structure_frequencies") != ["d", "30m", "5m", "1m"]
            or raw.get("stroke_mode") != STRICT_STROKE_MODE
            or raw.get("recursive_structure_used") is not True
            or raw.get("physical_timeframe_recursive") is not True
            or not signal_identity_consistent
            or not isinstance(context, Mapping)
            or not isinstance(setup, Mapping)
            or not _sector_context_is_consistent(
                daily_context,
                frequency="d",
                evidence_cutoff=signal_evidence_cutoff,
            )
            or not isinstance(risk, Mapping)
            or not reason_contract_valid
            or not diagnostic_contract_valid
            or not setup_is_causal
            or (trigger is not None and not isinstance(trigger, Mapping))
            or not trigger_is_causal
            or isinstance(trigger, Mapping)
            and (
                trigger.get("side") != raw.get("side")
                or trigger.get("point_id") == raw.get("point_id")
            )
            or (
                entry_boundary is not None
                and (
                    not isinstance(trigger, Mapping)
                    or entry_boundary.point_id != trigger.get("point_id")
                    or entry_boundary.symbol != raw.get("code")
                    or raw.get("side") != "buy"
                    or trigger_available_at is None
                    or entry_boundary.confirmation_bar_closed_at != trigger_available_at
                    or entry_boundary.confirmation_bar_closed_at
                    > signal_evidence_cutoff
                )
            )
            or selection_sources is None
            or not selection_sources
            or any(
                not isinstance(value, str) or value not in _SELECTION_SOURCES
                for value in selection_sources
            )
            or len(selection_sources) != len(set(selection_sources))
            or raw.get("sector_triggered")
            != ("QMT_SECTOR_TRIGGER" in selection_sources)
            or raw.get("monitor_only") == ("QMT_SECTOR_TRIGGER" in selection_sources)
            or not sector_document_consistent
        ):
            raise ValueError("live screening signal timeframe provenance is invalid")
        signals.append(raw)
    try:
        # Archive readiness must cover the complete read-only conversion, not
        # merely its outer snapshot checks.  Otherwise an illegal lifecycle,
        # risk gate, warmup document or structure price can make /readyz say
        # ready and fail only when the daily pipeline constructs its alerts.
        # The constructor has no I/O or order authority; invoking it here keeps
        # every consumer of this validator on the same semantic boundary.
        for signal in signals:
            signal_sector = signal.get("sector")
            sector_id = (
                signal_sector.get("sector_id")
                if isinstance(signal_sector, Mapping)
                else None
            )
            live_signal_human_review_alert(
                signal,
                review_available_at=review_at,
                source_snapshot_sha256=str(payload["snapshot_content_sha256"]),
                sector_ranking_document=sector_documents.get(str(sector_id)),
                sector_ranking_observed_at=datetime.fromisoformat(
                    str(strength_evidence.evidence_document()["decision_time"])
                ),
                sector_strength_evidence_revision=(
                    str(payload["sector_strength_evidence_revision"])
                    if isinstance(
                        payload.get("sector_strength_evidence_revision"),
                        str,
                    )
                    else None
                ),
                sector_catalog_revision=str(manifest["sector_catalog_revision"]),
            )
    except (ArithmeticError, TypeError, ValueError) as exc:
        raise ValueError("live screening signal review conversion is invalid") from exc
    # Recompute prominent human-review fields only after the lower-level
    # provenance and alert-conversion contracts have been checked.  Besides
    # retaining their more precise diagnostics, this prevents an invalid
    # lifecycle or frequency label from merely surfacing as a derived-policy
    # mismatch.
    for signal in signals:
        risk = signal.get("higher_timeframe_risk")
        warmup = signal.get("warmup")
        if (
            not isinstance(risk, Mapping)
            or not isinstance(warmup, Mapping)
            or not _displayed_decision_evidence_is_consistent(
                signal,
                policy=decision_core.get("policy"),
                risk=risk,
                warmup=warmup,
            )
        ):
            raise ValueError("live screening signal decision evidence is invalid")
    # The portable decision identity is the final per-signal tamper-evidence
    # gate.  It deliberately follows every more specific, independently
    # recomputable domain check so callers keep actionable diagnostics.
    for signal in signals:
        try:
            validate_signal_decision_document(signal)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "live screening signal decision identity is invalid"
            ) from exc
    expected_stage_counts: dict[str, int] = {}
    expected_point_counts: dict[str, int] = {}
    for signal in signals:
        stage = str(signal.get("lifecycle_stage"))
        point_type = str(signal.get("point_type"))
        expected_stage_counts[stage] = expected_stage_counts.get(stage, 0) + 1
        expected_point_counts[point_type] = expected_point_counts.get(point_type, 0) + 1

    def normalized_declared_counts(
        raw: object,
        *,
        allowed: frozenset[str],
    ) -> dict[str, int] | None:
        if not isinstance(raw, Mapping):
            return None
        normalized: dict[str, int] = {}
        for key, value in raw.items():
            if (
                not isinstance(key, str)
                or key not in allowed
                or type(value) is not int
                or value < 0
            ):
                return None
            if value:
                normalized[key] = value
        return normalized

    if (
        normalized_declared_counts(
            payload.get("counts_by_stage"),
            allowed=_LIFECYCLE_STAGES,
        )
        != expected_stage_counts
        or normalized_declared_counts(
            payload.get("counts_by_point_type"),
            allowed=frozenset({"1buy", "2buy", "3buy", "1sell", "2sell", "3sell"}),
        )
        != expected_point_counts
    ):
        raise ValueError("live screening signal aggregates are invalid")
    return review_at, tuple(signals)


def _alert_type(signal: Mapping[str, object]) -> str:
    side = signal.get("side")
    # The live path consumes the canonical recursive graph independently at
    # d/30m/5m/1m.  Preserve the physical 5m side clue and let the review
    # contract decide how it affects the held strategic cycle.
    if side == "sell" and signal.get("physical_timeframe_recursive") is True:
        return "POSSIBLE_SELL_REVIEW"
    if side == "sell" and signal.get("exit_action") == "reduce_tactical":
        return "POSSIBLE_5M_TACTICAL_SELL"
    if side == "buy" and signal.get("decision_role") == "TACTICAL_BUYBACK":
        return "POSSIBLE_5M_TACTICAL_BUYBACK"
    if side == "buy":
        return "POSSIBLE_30M_BUY"
    if side == "sell":
        return "POSSIBLE_30M_EXIT"
    raise ValueError("live screening signal side is invalid")


def live_signal_human_review_alert(
    signal: Mapping[str, object],
    *,
    review_available_at: datetime,
    source_snapshot_sha256: str,
    sector_ranking_document: Mapping[str, object] | None = None,
    sector_ranking_observed_at: datetime | None = None,
    sector_strength_evidence_revision: str | None = None,
    sector_catalog_revision: str | None = None,
) -> HumanReviewAlert:
    """Translate one canonical 30m-context/5m-setup/1m-locator decision."""

    symbol = signal.get("code")
    signal_id = signal.get("signal_id")
    point_id = signal.get("point_id")
    stage = signal.get("lifecycle_stage")
    if (
        not isinstance(symbol, str)
        or not isinstance(signal_id, str)
        or not isinstance(point_id, str)
        or stage not in _LIFECYCLE_STAGES
    ):
        raise ValueError("live screening signal cannot enter human review")
    try:
        signal_at = datetime.fromisoformat(str(signal["observed_at"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("live screening signal review time is invalid") from exc
    if signal_at.tzinfo is None or signal_at > review_available_at:
        raise ValueError("live screening review would expose a future signal")
    risk = signal.get("higher_timeframe_risk")
    warmup = signal.get("warmup")
    sector = signal.get("sector")
    setup = signal.get("setup_5m")
    trigger = signal.get("trigger_1m")
    context = signal.get("context_30m")
    entry_boundary = _entry_boundary_from_document(
        signal.get("entry_execution_boundary")
    )
    if not all(
        isinstance(value, Mapping) for value in (risk, warmup, sector, setup, context)
    ) or (trigger is not None and not isinstance(trigger, Mapping)):
        raise ValueError("live screening review evidence is incomplete")
    market_gate = str(risk.get("market_gate") or "UNRESOLVED")
    sector_gate = str(risk.get("sector_gate") or "UNRESOLVED")
    symbol_gate = str(risk.get("symbol_gate") or "UNRESOLVED")
    if (
        market_gate not in _GATES
        or sector_gate not in _GATES
        or symbol_gate not in _GATES
    ):
        raise ValueError("live screening review risk gate is invalid")
    sector_source_evidence = sector_higher_timeframe_review_evidence_from_risk(
        risk,
        sector_id=(
            str(sector["sector_id"])
            if isinstance(sector.get("sector_id"), str)
            else None
        ),
        observed_at=signal_at,
    )
    if sector_ranking_document is not None and sector_ranking_observed_at is None:
        raise ValueError("live sector ranking observation time is unavailable")
    if sector_ranking_document is None:
        sector_ranking_evidence = None
    else:
        assert sector_ranking_observed_at is not None
        sector_ranking_evidence = sector_ranking_review_evidence_from_live_sector(
            sector_ranking_document,
            observed_at=sector_ranking_observed_at,
            strength_evidence_revision=sector_strength_evidence_revision,
            sector_catalog_revision=sector_catalog_revision,
        )
    market_symbol_source_evidence = (
        market_symbol_higher_timeframe_review_evidence_from_risk(
            risk,
            symbol=symbol,
            observed_at=signal_at,
        )
    )
    reference_source = trigger if trigger is not None else setup
    try:
        reference_price = Decimal(str(reference_source["anchor_price"]))
    except (ArithmeticError, KeyError, TypeError, ValueError) as exc:
        raise ValueError("live screening structure anchor price is invalid") from exc
    if not reference_price.is_finite() or reference_price <= 0:
        raise ValueError("live screening structure anchor price is invalid")
    allowed = bool(signal.get("entry_allowed") or signal.get("exit_allowed"))
    confidence = (
        "HIGH"
        if allowed
        else "MEDIUM"
        if stage in {"observed", "triggered", "executable"}
        else "LOW"
        if stage in {"formed", "armed"}
        else "UNRESOLVED"
    )
    alert_type = _alert_type(signal)
    if entry_boundary is not None and (
        not isinstance(trigger, Mapping)
        or entry_boundary.point_id != trigger.get("point_id")
        or entry_boundary.symbol != symbol
        or signal.get("side") != "buy"
    ):
        raise ValueError("live screening entry boundary does not match its signal")
    selection_sources = tuple(
        str(value) for value in signal.get("selection_sources") or ()
    )
    warnings = tuple(
        dict.fromkeys(
            (
                "STAGED_LIVE_SCREEN_REQUIRES_HUMAN_CONFIRMATION",
                "STRATEGIC_CONTEXT_30M",
                "TACTICAL_SETUP_5M",
                (
                    "PRECISE_LOCATOR_1M_PRESENT"
                    if trigger is not None
                    else "PRECISE_LOCATOR_1M_NOT_YET_PRESENT"
                ),
                f"LIFECYCLE_{str(stage).upper()}",
                *(
                    ("HUMAN_SELECT_30M_EXIT_OR_5M_TACTICAL_ROLE",)
                    if alert_type == "POSSIBLE_SELL_REVIEW"
                    else ()
                ),
                *(
                    (
                        "UNADJUSTED_1M_CONFIRMATION_BAR_BOUNDARY_PRESENT",
                        (
                            "BUY_ENTRY_BOUNDARY_ALREADY_EXPIRED"
                            if entry_boundary.entry_valid_until <= review_available_at
                            else "BUY_ENTRY_BOUNDARY_ACTIVE"
                        ),
                    )
                    if signal.get("side") == "buy" and entry_boundary is not None
                    else (
                        ("BUY_EXECUTION_BOUNDARY_MISSING_REVIEW_ONLY",)
                        if signal.get("side") == "buy"
                        else ()
                    )
                ),
                *(f"SELECTION_SOURCE_{value}" for value in selection_sources),
                *(
                    (MONITOR_ONLY_WARNING_CODE,)
                    if signal.get("monitor_only") is True
                    else ()
                ),
                *(str(value) for value in signal.get("decision_reasons") or ()),
                *(str(value) for value in risk.get("reason_codes") or ()),
                *(str(value) for value in warmup.get("reason_codes") or ()),
                *(() if warmup.get("converged") is True else ("WARMUP_NOT_CONVERGED",)),
            )
        )
    )
    structure_snapshot_id = sha256_json(
        {
            "schema": "chanlun-live-human-review-structure",
            "source_snapshot_sha256": source_snapshot_sha256,
            "strategic_context_30m": dict(context),
            "tactical_setup_5m": dict(setup),
            "precise_locator_1m": None if trigger is None else dict(trigger),
            "signal": dict(signal),
        }
    )
    facts = tuple(
        dict.fromkeys(
            str(value)
            for value in (
                point_id,
                signal.get("setup_id"),
                signal_id,
                signal.get("decision_core_id"),
                structure_snapshot_id,
                source_snapshot_sha256,
                (
                    None
                    if sector_source_evidence is None
                    else sector_source_evidence.evidence_id
                ),
                market_symbol_source_evidence.evidence_id,
                (
                    None
                    if sector_ranking_evidence is None
                    else sector_ranking_evidence.evidence_id
                ),
                None if entry_boundary is None else entry_boundary.evidence_id,
            )
            if isinstance(value, str) and value
        )
    )
    invalidation = signal.get("structural_stop")
    parameters = human_review_screening_parameters()
    return HumanReviewAlert(
        symbol=symbol,
        alert_type=alert_type,  # type: ignore[arg-type]
        signal_at=signal_at,
        review_available_at=review_available_at,
        source_point_id=point_id,
        structure_snapshot_id=structure_snapshot_id,
        sector_id=(
            str(sector["sector_id"])
            if isinstance(sector.get("sector_id"), str)
            else None
        ),
        confidence=confidence,  # type: ignore[arg-type]
        review_priority=review_priority(
            confidence=confidence,  # type: ignore[arg-type]
            exact_green=allowed,
            market_risk_gate=market_gate,
            sector_risk_gate=sector_gate,
            symbol_risk_gate=symbol_gate,
            warning_count=len(warnings),
            parameters=parameters,
        ),
        # This is the finest available causal structure anchor, never a quote
        # or an execution promise: prefer the 1m locator and fall back to the
        # 5m setup while the locator is still forming.
        reference_price=reference_price,
        structural_invalidation_price=(
            None if invalidation is None else Decimal(str(invalidation))
        ),
        market_risk_gate=market_gate,
        sector_risk_gate=sector_gate,
        symbol_risk_gate=symbol_gate,
        warning_codes=warnings,
        source_fact_ids=facts,
        screening_parameter_set_id=parameters.parameter_set_id,
        technical_approximation_parameter_set_id=(
            parameters.technical_approximation_parameter_set_id
        ),
        sector_higher_timeframe_evidence=sector_source_evidence,
        market_symbol_higher_timeframe_evidence=(market_symbol_source_evidence),
        sector_ranking_evidence=sector_ranking_evidence,
        entry_confirmation_bar_closed_at=(
            None
            if entry_boundary is None
            else entry_boundary.confirmation_bar_closed_at
        ),
        entry_price_cap=(None if entry_boundary is None else entry_boundary.raw_high),
        entry_valid_until=(
            None if entry_boundary is None else entry_boundary.entry_valid_until
        ),
        entry_boundary_evidence_id=(
            None if entry_boundary is None else entry_boundary.evidence_id
        ),
        entry_execution_boundary=entry_boundary,
    )


def live_human_review_document(
    *,
    live_snapshot: Mapping[str, object],
    source_snapshot_sha256: str,
    session: date,
    result_label: str = "LIVE_INTRADAY_HUMAN_REVIEW_QUEUE",
    decision_source_snapshot: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Build a review-only report suitable for immutable live archiving."""

    source_implementation_id = (
        None
        if decision_source_snapshot is None
        else decision_source_snapshot_id(decision_source_snapshot)
    )
    review_at, signals = validate_live_review_snapshot(
        live_snapshot,
        session=session,
    )
    ranking_documents = {
        str(value["sector_id"]): value
        for value in live_snapshot.get("sectors") or ()
        if isinstance(value, Mapping) and isinstance(value.get("sector_id"), str)
    }
    strength_document = live_snapshot.get("sector_strength_evidence")
    if not isinstance(strength_document, Mapping):
        raise ValueError("live screening sector strength evidence is unavailable")
    ranking_observed_at = datetime.fromisoformat(
        str(strength_document["decision_time"])
    )
    alerts = tuple(
        sorted(
            (
                live_signal_human_review_alert(
                    signal,
                    review_available_at=review_at,
                    source_snapshot_sha256=source_snapshot_sha256,
                    sector_ranking_document=ranking_documents.get(
                        str(
                            signal.get("sector", {}).get("sector_id")
                            if isinstance(signal.get("sector"), Mapping)
                            else ""
                        )
                    ),
                    sector_ranking_observed_at=ranking_observed_at,
                    sector_strength_evidence_revision=(
                        str(live_snapshot["sector_strength_evidence_revision"])
                        if isinstance(
                            live_snapshot.get("sector_strength_evidence_revision"),
                            str,
                        )
                        else None
                    ),
                    sector_catalog_revision=str(
                        live_snapshot["coverage_manifest"]["sector_catalog_revision"]
                    ),
                )
                for signal in signals
            ),
            key=lambda value: (
                -value.review_priority,
                -int(value.review_available_at.timestamp()),
                value.symbol,
                value.candidate_id,
            ),
        )
    )
    parameters = human_review_screening_parameters()
    stable: dict[str, object] = {
        "schema": HUMAN_REVIEW_SCREEN_SCHEMA,
        "forward_paper_session": session.isoformat(),
        "result_label": result_label,
        "data_grade": "HUMAN_REVIEW_SCREENING",
        "highest_status": "REVIEW_REQUIRED",
        "human_confirmation_required": True,
        "automated_order_authorized": False,
        "portfolio_backtest_performed": False,
        "portfolio_performance_evaluable": False,
        "orders_created": 0,
        "fills_created": 0,
        "positions_created": 0,
        "live_status": "LIVE_DISABLED",
        "sample": {
            "forward_session": session.isoformat(),
            "market_data_as_of": review_at.isoformat(),
            "minimum_bar_period": "1m",
        },
        "scope": {
            "selection_path": "QMT_CURRENT_SECTOR_TECHNICAL_ONLY",
            "three_program_mode": "DISABLED_USER_AUTHORIZED",
            "strategic_frequency": "30m",
            "tactical_frequency": "5m",
            "locator_frequency": "1m",
            "candidate_symbol_count": len({value.symbol for value in alerts}),
        },
        "screening_contract": _jsonable(parameters.document()),
        **(
            {}
            if decision_source_snapshot is None
            else {"decision_source_snapshot": _jsonable(decision_source_snapshot)}
        ),
        "input_hashes": {
            "live_screening_snapshot": source_snapshot_sha256,
            "declared_snapshot_content_sha256": live_snapshot.get(
                "snapshot_content_sha256"
            ),
            "decision_core_id": live_snapshot.get("decision_core_id"),
            "screening_policy_id": _source_screening_policy_id(live_snapshot),
            **(
                {}
                if source_implementation_id is None
                else {"decision_source_snapshot_id": source_implementation_id}
            ),
        },
        "candidate_funnel": {
            "live_screen_candidate_count": len(signals),
            "review_candidate_count": len(alerts),
        },
        "signal_counts": {
            "by_stage": dict(live_snapshot.get("counts_by_stage") or {}),
            "by_point_type": dict(live_snapshot.get("counts_by_point_type") or {}),
            "by_alert_type": {
                alert_type: sum(value.alert_type == alert_type for value in alerts)
                for alert_type in HUMAN_REVIEW_ALERT_TYPES
            },
        },
        "review_queue": [
            {
                **_jsonable(human_review_alert_document(alert)),
                "candidate_id": alert.candidate_id,
                "signal_lifecycle_id": alert.signal_lifecycle_id,
            }
            for alert in alerts
        ],
        "event_study": {"summary": {}, "observations": []},
        "data_caveats": [
            "CURRENT_QMT_MEMBERSHIP_HAS_FORWARD_POINT_IN_TIME_MEANING_ONLY",
            "PROGRAM_OUTPUT_IS_A_REVIEW_CANDIDATE_NOT_A_TRADE_SIGNAL",
            "REFERENCE_PRICE_IS_STRUCTURE_ANCHOR_NOT_EXECUTION_QUOTE",
            "NO_TICK_DATA_AND_NO_PORTFOLIO_PERFORMANCE",
        ],
        "division_of_responsibility": {
            "program": (
                "sector ranking, 30m context, 5m setup, 1m locator, causal chart "
                "lock, risk and warmup evidence"
            ),
            "human": (
                "center, trend type, recursive level, strategic/tactical role, "
                "buy/sell point and any virtual observation decision"
            ),
        },
        "hard_rejections": list(live_snapshot.get("errors") or ()),
        "candidate_audit": [
            {
                "signal_id": signal.get("signal_id"),
                "symbol": signal.get("code"),
                "context_frequency": "30m",
                "setup_frequency": "5m",
                "locator_frequency": (
                    "1m" if signal.get("trigger_1m") is not None else None
                ),
                "decision_reasons": list(signal.get("decision_reasons") or ()),
                "entry_allowed": bool(signal.get("entry_allowed")),
                "exit_allowed": bool(signal.get("exit_allowed")),
                "exit_action": signal.get("exit_action"),
            }
            for signal in signals
        ],
    }
    portable = _jsonable(stable)
    if not isinstance(portable, dict):
        raise TypeError("live human review document must be a mapping")
    return {**portable, "content_sha256": sha256_json(portable)}


__all__ = (
    "COVERAGE_EXCLUSION_REASON_CODES",
    "COVERAGE_MANIFEST_SCHEMA",
    "COVERAGE_MANIFEST_FIELDS",
    "COVERAGE_STATE_CONTRACT_ID",
    "HUMAN_REVIEW_SCREEN_SCHEMA",
    "LIVE_SCREENING_SCHEMA",
    "MIN_LIVE_REVIEW_STOCK_COVERAGE",
    "MONITOR_INSTRUMENT_EXCLUSION_CONTRACT_ID",
    "SECTOR_COVERAGE_CONTRACT_ID",
    "live_screening_semantic_snapshot_document",
    "live_screening_snapshot_content_sha256",
    "monitor_instrument_exclusions_are_consistent",
    "live_human_review_document",
    "live_signal_human_review_alert",
    "screening_coverage_epoch_id",
    "validate_live_screening_market_watermark",
    "validate_live_review_snapshot",
)
