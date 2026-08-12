"""File-backed human-review screening surface for the web dashboard.

The service intentionally has no exchange or order dependency.  It validates
the immutable screen report produced by the research/forward pipeline, binds
every chart to the candidate's causal ``review_available_at`` and appends
authenticated reviewer judgements to the existing hash-chained feedback
ledger.
"""

from __future__ import annotations

import copy
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from chanlun.decision_support.fingerprints import sha256_json
from chanlun.decision_support.trading_system.decision_source_provenance import (
    current_forward_implementation_provenance,
    current_decision_source_snapshot,
    decision_source_snapshot_id,
)
from chanlun.decision_support.trading_system.live_review_materialization import (
    LIVE_REVIEW_CANDIDATE_DETAIL_SCHEMA,
    LIVE_REVIEW_WEB_INDEX_SCHEMA,
    LiveReviewWebBundle,
    resolve_live_review_materialization_receipt,
    resolve_live_review_web_bundle_receipt,
)
from chanlun.decision_support.trading_system.human_review_screening import (
    HUMAN_REVIEW_SCREEN_SCHEMA,
    HumanReviewAlert,
    HumanReviewFeedback,
    append_human_review_feedback,
    load_human_review_feedback_ledger,
    parse_human_review_alert,
    parse_sector_ranking_review_evidence,
    validate_human_review_feedback_causality,
    validate_human_review_screen_document,
)
from chanlun.decision_support.trading_system.file_lock import interprocess_file_lock
from chanlun.decision_support.trading_system.human_paper_accounting import (
    audit_human_paper_portfolio_decisions,
    audit_human_paper_portfolio_fill_decisions,
    load_human_paper_accounting_parameters,
    rebuild_human_paper_accounting,
)
from chanlun.decision_support.trading_system.human_paper_ledger import (
    HumanPaperEntrySelectionEvidence,
    audit_human_paper_portfolio_rejection_evidence,
    audit_human_paper_entry_boundary_attestations,
    audit_human_paper_entry_selection_attestations,
    audit_human_paper_entry_selection_source_bindings,
    audit_human_paper_execution_evidence,
    audit_human_paper_execution_rejection_evidence,
    audit_human_paper_operations_cancellation_evidence,
    human_paper_cancelled_intent_ids,
    human_paper_pending_sell_quantities,
    human_paper_position_quantities,
    human_paper_portfolio_rejected_intent_ids,
    human_paper_terminal_intent_ids,
    latest_human_paper_pending_continuity,
    load_human_paper_ledger,
    reconcile_human_paper_feedback,
)
from chanlun.decision_support.trading_system.human_paper_valuation import (
    audit_human_paper_valuation_evidence,
)
from chanlun.decision_support.trading_system.models import (
    parse_entry_execution_boundary_document,
)
from chanlun.decision_support.trading_system.bar_execution import (
    STRICT_BAR_EXECUTION_TIMESTAMP_RULE,
    STRICT_BAR_PRICE_RULE,
)
from chanlun.decision_support.trading_system.forward_paper import (
    FORWARD_IMPLEMENTATION_CONTINUITY_SCHEMA,
    FORWARD_PAPER_SESSION_DELIVERY_SCHEMA,
    audit_forward_implementation_continuity,
    audit_forward_paper_session_delivery,
    load_forward_paper_ledger,
    load_forward_contract,
)
from chanlun.decision_support.trading_system.live_human_review import (
    live_human_review_document,
    validate_live_screening_market_watermark,
    validate_live_review_snapshot,
)
from chanlun.decision_support.trading_system.trading_session import (
    resolve_trading_session_requirement,
)
from chanlun.decision_support.trading_system.forward_review_markout import (
    FORWARD_REVIEW_MARKOUT_SCHEMA,
    validate_forward_review_markout_document,
)
from chanlun.decision_support.trading_system.forward_warmup_structure_lineage import (
    validate_forward_warmup_structure_lineage_rollup_document,
)
from chanlun.decision_support.trading_system.candidate_warmup_diagnostics import (
    candidate_warmup_diagnostic_path,
    candidate_warmup_parameter_set_id,
    candidate_warmup_presentation,
    validate_candidate_warmup_diagnostic_document,
)
from chanlun.decision_support.trading_system.qmt_sector_ledger import (
    QMT_FORWARD_CAPTURE_READINESS_SCHEMA,
    QMT_SECTOR_RECEIPT_AUDIT_SCHEMA,
    audit_forward_sector_capture_readiness,
    audit_sector_capture_receipts,
    load_sector_ledger,
)

from .forward_scheduler import validate_forward_scheduler_snapshot


SCREEN_SCHEMA = HUMAN_REVIEW_SCREEN_SCHEMA
WEB_SCHEMA = "chanlun-human-review-web"
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_IMMUTABLE_REPORT_NAME = re.compile(r"^[0-9a-f]{64}\.json$")
_SOURCE_KINDS = frozenset({"latest", "live", "forward", "historical"})
_REVIEW_LANE_ORDER = {
    "POSITION_MANAGEMENT": 0,
    "ACTIONABLE_REVIEW": 1,
    "WATCHLIST": 2,
    "RESEARCH_ARCHIVE": 3,
}
_FORWARD_SCHEDULER_MAX_AGE = timedelta(seconds=90)
_FORWARD_SCHEDULER_FUTURE_TOLERANCE = timedelta(seconds=5)
_FORWARD_CAPTURE_READINESS_CACHE_SECONDS = 300.0
_FORWARD_DELIVERY_READINESS_CACHE_SECONDS = 300.0
_MAX_SYNCHRONOUS_LIVE_SNAPSHOT_BYTES = 8 * 1024 * 1024
_PAPER_RECONCILIABLE_OPERATIONAL_REASONS = frozenset(
    {
        "CURRENT_PAPER_SOURCE_UNAVAILABLE",
        "CURRENT_MARKET_DATA_SESSION_UNAVAILABLE_FOR_PAPER",
        "FORWARD_SCHEDULER_NOT_READY_FOR_PAPER",
        "FORWARD_SCHEDULER_OBSERVATION_STALE_FOR_PAPER",
        "FORWARD_OPERATIONS_CLOCK_INVALID_FOR_PAPER",
        "SAME_SESSION_FORWARD_CAPTURE_NOT_READY_FOR_PAPER",
        "QMT_RANKING_CATALOG_EXACT_REVISION_UNAVAILABLE_FOR_PAPER_ENTRY",
    }
)
_PAPER_RISK_REDUCING_RECONCILIATION_REASONS = frozenset(
    {
        "CURRENT_MARKET_DATA_SESSION_UNAVAILABLE_FOR_PAPER",
        "FORWARD_SCHEDULER_NOT_READY_FOR_PAPER",
        "FORWARD_SCHEDULER_OBSERVATION_STALE_FOR_PAPER",
        "FORWARD_OPERATIONS_CLOCK_INVALID_FOR_PAPER",
        "SAME_SESSION_FORWARD_CAPTURE_NOT_READY_FOR_PAPER",
    }
)
_EXACT_SECTOR_RANKING_CATALOG_ATTESTATION = "EXACT_REVISION_NAME_AND_MEMBERSHIP_MATCH"
_COMPACT_CANDIDATE_FIELDS = frozenset(HumanReviewAlert.__dataclass_fields__) - {
    "sector_higher_timeframe_evidence",
    "market_symbol_higher_timeframe_evidence",
} | {
    "candidate_id",
    "signal_lifecycle_id",
    "evidence_detail_available",
    "sector_higher_timeframe_evidence_id",
    "market_symbol_higher_timeframe_evidence_id",
    "sector_ranking_evidence_id",
    "market_symbol_higher_timeframe_source_attestation",
    "sector_ranking_attestation",
    "detail_locator",
}
_COMPACT_RISK_GATES = frozenset({"GREEN", "AMBER", "RED", "UNRESOLVED"})
_COMPACT_MARKET_SYMBOL_SOURCE_ATTESTATIONS = frozenset(
    {"SELF_CONTAINED", "PARTIAL_SOURCE_SUPPORT", "STRUCTURE_ONLY"}
)


@dataclass(frozen=True, slots=True)
class _HumanReviewCandidateSummary:
    """Small child-validated candidate used only for page presentation."""

    candidate_id: str
    signal_lifecycle_id: str
    symbol: str
    alert_type: str
    signal_at: datetime
    review_available_at: datetime
    sector_id: str | None
    confidence: str
    review_priority: int
    reference_price: Decimal | None
    structural_invalidation_price: Decimal | None
    entry_confirmation_bar_closed_at: datetime | None
    entry_price_cap: Decimal | None
    entry_valid_until: datetime | None
    entry_boundary_evidence_id: str | None
    entry_execution_boundary: object | None
    market_risk_gate: str
    sector_risk_gate: str
    symbol_risk_gate: str
    warning_codes: tuple[str, ...]
    source_fact_ids: tuple[str, ...]
    review_checklist: tuple[str, ...]
    sector_ranking_evidence: object | None
    sector_higher_timeframe_evidence_id: str | None
    market_symbol_higher_timeframe_evidence_id: str | None
    sector_ranking_evidence_id: str | None
    market_symbol_higher_timeframe_source_attestation: str
    sector_ranking_attestation: str
    evidence_detail_available: bool
    detail_offset: int
    detail_length: int
    detail_line_sha256: str
    sector_higher_timeframe_evidence: None = None
    market_symbol_higher_timeframe_evidence: None = None


_ReviewCandidate = HumanReviewAlert | _HumanReviewCandidateSummary


@dataclass(frozen=True, slots=True)
class _LoadedWebBundle:
    bundle: LiveReviewWebBundle
    report: dict[str, object]
    candidates: tuple[_HumanReviewCandidateSummary, ...]
    by_candidate_id: Mapping[str, _HumanReviewCandidateSummary]


def _optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("compact review datetime must be timezone-aware")
    return parsed


def _optional_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    parsed = Decimal(str(value))
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError("compact review price must be positive")
    return parsed


def _parse_candidate_summary(raw: object) -> _HumanReviewCandidateSummary:
    if not isinstance(raw, Mapping) or set(raw) != _COMPACT_CANDIDATE_FIELDS:
        raise ValueError("compact human review candidate must be a mapping")
    candidate_id = str(raw.get("candidate_id") or "")
    lifecycle_id = str(raw.get("signal_lifecycle_id") or "")
    locator = raw.get("detail_locator")
    if (
        _SHA256.fullmatch(candidate_id) is None
        or _SHA256.fullmatch(lifecycle_id) is None
        or not isinstance(locator, Mapping)
        or set(locator) != {"offset", "length", "line_sha256"}
        or type(locator.get("offset")) is not int
        or int(locator["offset"]) < 0
        or type(locator.get("length")) is not int
        or int(locator["length"]) <= 0
        or _SHA256.fullmatch(str(locator.get("line_sha256") or "")) is None
        or raw.get("status") != "REVIEW_REQUIRED"
        or raw.get("automated_action_authorized") is not False
        or raw.get("live_status") != "LIVE_DISABLED"
    ):
        raise ValueError("compact human review candidate boundary is invalid")
    signal_at = _optional_datetime(raw.get("signal_at"))
    review_at = _optional_datetime(raw.get("review_available_at"))
    if signal_at is None or review_at is None or review_at < signal_at:
        raise ValueError("compact human review candidate timing is invalid")
    ranking = raw.get("sector_ranking_evidence")
    parsed_ranking = (
        None if ranking is None else parse_sector_ranking_review_evidence(ranking)
    )
    ranking_id = raw.get("sector_ranking_evidence_id")
    if (parsed_ranking is None) != (ranking_id is None) or (
        parsed_ranking is not None and parsed_ranking.evidence_id != ranking_id
    ):
        raise ValueError("compact sector ranking identity changed")
    boundary = raw.get("entry_execution_boundary")
    parsed_boundary = (
        None if boundary is None else parse_entry_execution_boundary_document(boundary)
    )
    if any(
        not isinstance(raw.get(field), list)
        or any(type(value) is not str or not value for value in raw[field])
        for field in ("warning_codes", "source_fact_ids", "review_checklist")
    ):
        raise ValueError("compact review provenance is invalid")
    warning_codes = tuple(raw["warning_codes"])
    source_fact_ids = tuple(raw["source_fact_ids"])
    review_checklist = tuple(raw["review_checklist"])
    if (
        not source_fact_ids
        or len(warning_codes) != len(set(warning_codes))
        or len(source_fact_ids) != len(set(source_fact_ids))
        or len(review_checklist) != len(set(review_checklist))
    ):
        raise ValueError("compact review provenance is invalid")
    evidence_ids = {
        key: raw.get(key)
        for key in (
            "sector_higher_timeframe_evidence_id",
            "market_symbol_higher_timeframe_evidence_id",
            "sector_ranking_evidence_id",
        )
    }
    if any(
        value is not None and _SHA256.fullmatch(str(value)) is None
        for value in evidence_ids.values()
    ):
        raise ValueError("compact review evidence identity is invalid")
    market_symbol_id = evidence_ids["market_symbol_higher_timeframe_evidence_id"]
    sector_id = evidence_ids["sector_higher_timeframe_evidence_id"]
    market_symbol_source_attestation = raw[
        "market_symbol_higher_timeframe_source_attestation"
    ]
    ranking_attestation = raw["sector_ranking_attestation"]
    evidence_detail_available = raw["evidence_detail_available"]
    if (
        any(
            raw[field] not in _COMPACT_RISK_GATES
            for field in (
                "market_risk_gate",
                "sector_risk_gate",
                "symbol_risk_gate",
            )
        )
        or (
            market_symbol_id is None
            and market_symbol_source_attestation != "SUMMARY_ONLY"
        )
        or (
            market_symbol_id is not None
            and market_symbol_source_attestation
            not in _COMPACT_MARKET_SYMBOL_SOURCE_ATTESTATIONS
        )
        or ranking_attestation
        != ("FULL_STRUCTURAL_COMPONENTS" if ranking_id is not None else "NOT_ATTACHED")
        or type(evidence_detail_available) is not bool
        or evidence_detail_available
        != any(value is not None for value in (sector_id, market_symbol_id, ranking_id))
    ):
        raise ValueError("compact review attestation is invalid")
    return _HumanReviewCandidateSummary(
        candidate_id=candidate_id,
        signal_lifecycle_id=lifecycle_id,
        symbol=str(raw.get("symbol") or ""),
        alert_type=str(raw.get("alert_type") or ""),
        signal_at=signal_at,
        review_available_at=review_at,
        sector_id=(None if raw.get("sector_id") is None else str(raw["sector_id"])),
        confidence=str(raw.get("confidence") or ""),
        review_priority=int(raw.get("review_priority")),
        reference_price=_optional_decimal(raw.get("reference_price")),
        structural_invalidation_price=_optional_decimal(
            raw.get("structural_invalidation_price")
        ),
        entry_confirmation_bar_closed_at=_optional_datetime(
            raw.get("entry_confirmation_bar_closed_at")
        ),
        entry_price_cap=_optional_decimal(raw.get("entry_price_cap")),
        entry_valid_until=_optional_datetime(raw.get("entry_valid_until")),
        entry_boundary_evidence_id=(
            None
            if raw.get("entry_boundary_evidence_id") is None
            else str(raw["entry_boundary_evidence_id"])
        ),
        entry_execution_boundary=parsed_boundary,
        market_risk_gate=str(raw.get("market_risk_gate") or ""),
        sector_risk_gate=str(raw["sector_risk_gate"]),
        symbol_risk_gate=str(raw.get("symbol_risk_gate") or ""),
        warning_codes=warning_codes,
        source_fact_ids=source_fact_ids,
        review_checklist=review_checklist,
        sector_ranking_evidence=parsed_ranking,
        sector_higher_timeframe_evidence_id=(
            None
            if evidence_ids["sector_higher_timeframe_evidence_id"] is None
            else str(evidence_ids["sector_higher_timeframe_evidence_id"])
        ),
        market_symbol_higher_timeframe_evidence_id=(
            None
            if evidence_ids["market_symbol_higher_timeframe_evidence_id"] is None
            else str(evidence_ids["market_symbol_higher_timeframe_evidence_id"])
        ),
        sector_ranking_evidence_id=(
            None
            if evidence_ids["sector_ranking_evidence_id"] is None
            else str(evidence_ids["sector_ranking_evidence_id"])
        ),
        market_symbol_higher_timeframe_source_attestation=str(
            raw.get("market_symbol_higher_timeframe_source_attestation") or ""
        ),
        sector_ranking_attestation=str(raw.get("sector_ranking_attestation") or ""),
        evidence_detail_available=evidence_detail_available,
        detail_offset=int(locator["offset"]),
        detail_length=int(locator["length"]),
        detail_line_sha256=str(locator["line_sha256"]),
    )


def _sector_presentation_rank(alert: _ReviewCandidate) -> tuple[int | None, str | None]:
    """Flatten verified rank evidence for display without changing the alert.

    ``review_priority`` participates in the immutable candidate identity and
    must remain the producer's value.  The horizontal QMT rank is therefore a
    separate presentation key: it can reduce a human queue without becoming a
    trade score or silently changing an archived candidate hash.
    """

    evidence = alert.sector_ranking_evidence
    if evidence is None:
        return None, None
    rank = evidence.horizontal_rank
    if type(rank) is not int or rank <= 0:
        rank = None
    strength = (
        None
        if evidence.horizontal_strength is None
        else format(evidence.horizontal_strength, "f")
    )
    return rank, strength


def _review_lane(
    alert: _ReviewCandidate,
    *,
    virtual_position_quantity: int,
    paper_reconciliation_pending: bool,
) -> str:
    """Classify one candidate into a display-only human workload lane."""

    is_sell_hint = alert.alert_type in {
        "POSSIBLE_30M_EXIT",
        "POSSIBLE_SELL_REVIEW",
        "POSSIBLE_5M_TACTICAL_SELL",
        "POSSIBLE_5M_TACTICAL_BUYBACK",
    }
    if virtual_position_quantity > 0 and is_sell_hint:
        return "POSITION_MANAGEMENT"
    if alert.alert_type == "POSSIBLE_30M_BUY" and (
        alert.confidence in {"HIGH", "MEDIUM"} or paper_reconciliation_pending
    ):
        return "ACTIONABLE_REVIEW"
    if alert.alert_type == "POSSIBLE_30M_BUY":
        return "WATCHLIST"
    return "RESEARCH_ARCHIVE"


_SECTOR_RANKING_CATALOG_ENTRY_REASONS = {
    "EXACT_REVISION_UNAVAILABLE_AT_OBSERVATION": (
        "QMT_RANKING_CATALOG_EXACT_REVISION_UNAVAILABLE_FOR_PAPER_ENTRY"
    ),
    "EXACT_REVISION_NAME_MISMATCH": (
        "QMT_RANKING_CATALOG_NAME_MISMATCH_FOR_PAPER_ENTRY"
    ),
    "EXACT_REVISION_SYMBOL_NOT_MEMBER": (
        "QMT_RANKING_CATALOG_SYMBOL_NOT_MEMBER_FOR_PAPER_ENTRY"
    ),
    "EXACT_REVISION_SECTOR_ID_UNRESOLVED": (
        "QMT_RANKING_CATALOG_SECTOR_UNRESOLVED_FOR_PAPER_ENTRY"
    ),
}

# 长期运行的网页进程在磁盘文件变化后仍可能执行已导入代码，因此在进程导入时固定一次
# 实现清单，避免把旧内存实现错误归因到较新的磁盘状态。
_WEB_PROCESS_DECISION_SOURCE_SNAPSHOT = current_decision_source_snapshot()
_WEB_PROCESS_DECISION_SOURCE_SNAPSHOT_ID = decision_source_snapshot_id(
    _WEB_PROCESS_DECISION_SOURCE_SNAPSHOT
)


class HumanReviewScreenUnavailable(RuntimeError):
    """Raised when the dashboard cannot prove a safe review snapshot."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _read_json(path: Path, code: str) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HumanReviewScreenUnavailable(code) from exc
    if not isinstance(payload, dict):
        raise HumanReviewScreenUnavailable(code)
    return payload


def _validate_report(payload: dict[str, object]) -> tuple[HumanReviewAlert, ...]:
    try:
        return validate_human_review_screen_document(payload)
    except (TypeError, ValueError) as exc:
        raise HumanReviewScreenUnavailable(str(exc)) from exc


def _market_symbol_source_attestation(alert: _ReviewCandidate) -> str:
    """Classify source support separately from structural M/W/D evidence."""

    if isinstance(alert, _HumanReviewCandidateSummary):
        return alert.market_symbol_higher_timeframe_source_attestation
    evidence = alert.market_symbol_higher_timeframe_evidence
    if evidence is None:
        return "SOURCE_SUPPORT_UNAVAILABLE"
    support_count = sum(
        side.source_support is not None
        for side in (evidence.market, evidence.symbol_evidence)
    )
    if support_count == 2:
        return "SELF_CONTAINED"
    if support_count == 1:
        return "PARTIAL_SOURCE_SUPPORT"
    return "STRUCTURE_ONLY"


def _sector_ranking_attestation(alert: _ReviewCandidate) -> str:
    if isinstance(alert, _HumanReviewCandidateSummary):
        return alert.sector_ranking_attestation
    evidence = alert.sector_ranking_evidence
    if evidence is None:
        return "RANKING_EVIDENCE_UNAVAILABLE"
    return "FULL_STRUCTURAL_COMPONENTS"


def _sector_name_presentation(
    alert: _ReviewCandidate,
    entries: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Resolve a label without backfilling a later QMT catalog as PIT fact."""

    sector_id = alert.sector_id
    ranking = alert.sector_ranking_evidence
    ranking_catalog_revision = (
        ranking.sector_catalog_revision if ranking is not None else None
    )
    if sector_id is None:
        return {
            "sector_name": "板块待分类",
            "sector_name_attestation": "NOT_APPLICABLE",
            "sector_name_point_in_time": False,
            "sector_membership_attestation": "NOT_APPLICABLE",
            "sector_membership_point_in_time": False,
            "sector_name_captured_at": None,
            "sector_name_entry_sha256": None,
            "sector_name_catalog_revision": None,
            "sector_ranking_catalog_attestation": "NOT_APPLICABLE",
        }
    if sector_id == "unclassified":
        return {
            "sector_name": "未归类 QMT GICS3 行业",
            "sector_name_attestation": "UNCLASSIFIED",
            "sector_name_point_in_time": False,
            "sector_membership_attestation": "UNCLASSIFIED",
            "sector_membership_point_in_time": False,
            "sector_name_captured_at": None,
            "sector_name_entry_sha256": None,
            "sector_name_catalog_revision": None,
            "sector_ranking_catalog_attestation": "UNCLASSIFIED",
        }

    signal_at = alert.signal_at.astimezone(ZoneInfo("Asia/Shanghai"))
    catalog_cutoff = (
        ranking.observed_at.astimezone(ZoneInfo("Asia/Shanghai"))
        if ranking_catalog_revision is not None
        else signal_at
    )
    eligible: list[tuple[datetime, Mapping[str, object]]] = []
    for entry in entries:
        captured_at = datetime.fromisoformat(str(entry["captured_at"]))
        if (
            captured_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
            == catalog_cutoff.date()
            and captured_at <= catalog_cutoff
        ):
            eligible.append((captured_at, entry))
    if ranking_catalog_revision is not None:
        exact_revision = tuple(
            value
            for value in eligible
            if value[1].get("catalog_revision") == ranking_catalog_revision
        )
        selected = max(exact_revision, default=None, key=lambda value: value[0])
        if selected is None:
            return {
                "sector_name": ranking.sector_name,
                "sector_name_attestation": (
                    "RANKING_SOURCE_CATALOG_UNAVAILABLE_AT_OBSERVATION"
                ),
                "sector_name_point_in_time": False,
                "sector_membership_attestation": (
                    "RANKING_SOURCE_CATALOG_UNAVAILABLE_AT_OBSERVATION"
                ),
                "sector_membership_point_in_time": False,
                "sector_name_captured_at": None,
                "sector_name_entry_sha256": None,
                "sector_name_catalog_revision": ranking_catalog_revision,
                "sector_ranking_catalog_attestation": (
                    "EXACT_REVISION_UNAVAILABLE_AT_OBSERVATION"
                ),
            }
    else:
        selected = max(eligible, default=None, key=lambda value: value[0])
    if selected is not None:
        captured_at, entry = selected
        sectors = {str(row["sector_id"]): row for row in entry.get("sectors") or ()}
        sector = sectors.get(sector_id)
        ranking_name_matches = bool(
            ranking_catalog_revision is None
            or sector is not None
            and str(sector.get("name")) == ranking.sector_name
        )
        membership_proven = sector is not None and alert.symbol in {
            str(value) for value in sector.get("member_codes") or ()
        }
        exact_attestation = (
            "NOT_APPLICABLE"
            if ranking_catalog_revision is None
            else (
                "EXACT_REVISION_SECTOR_ID_UNRESOLVED"
                if sector is None
                else (
                    "EXACT_REVISION_NAME_MISMATCH"
                    if not ranking_name_matches
                    else (
                        "EXACT_REVISION_SYMBOL_NOT_MEMBER"
                        if not membership_proven
                        else "EXACT_REVISION_NAME_AND_MEMBERSHIP_MATCH"
                    )
                )
            )
        )
        return {
            "sector_name": (
                "板块名称待映射" if sector is None else str(sector["name"])
            ),
            "sector_name_attestation": (
                "RANKING_SOURCE_NAME_MISMATCH"
                if sector is not None and not ranking_name_matches
                else (
                    "POINT_IN_TIME_SAME_SESSION"
                    if sector is not None
                    else "SAME_SESSION_SECTOR_ID_UNRESOLVED"
                )
            ),
            "sector_name_point_in_time": (sector is not None and ranking_name_matches),
            "sector_membership_attestation": (
                "RANKING_SOURCE_NAME_MISMATCH"
                if not ranking_name_matches
                else (
                    "POINT_IN_TIME_SAME_SESSION"
                    if membership_proven
                    else "SAME_SESSION_SYMBOL_NOT_MEMBER"
                )
            ),
            "sector_membership_point_in_time": (
                membership_proven and ranking_name_matches
            ),
            "sector_name_captured_at": captured_at.isoformat(),
            "sector_name_entry_sha256": entry.get("entry_sha256"),
            "sector_name_catalog_revision": entry.get("catalog_revision"),
            "sector_ranking_catalog_attestation": exact_attestation,
        }

    return {
        "sector_name": "板块名称待映射",
        "sector_name_attestation": "UNRESOLVED",
        "sector_name_point_in_time": False,
        "sector_membership_attestation": "UNRESOLVED",
        "sector_membership_point_in_time": False,
        "sector_name_captured_at": None,
        "sector_name_entry_sha256": None,
        "sector_name_catalog_revision": None,
        "sector_ranking_catalog_attestation": "UNRESOLVED",
    }


def _paper_entry_sector_eligibility(
    alert: _ReviewCandidate,
    sector_presentation: Mapping[str, object],
) -> tuple[bool, str | None]:
    """Fail closed new strategic entries when rank membership cannot bind.

    The catalog gate is deliberately candidate-scoped.  It applies only to a
    new 30m strategic buy sourced from complete ranking evidence; sell/exit
    reviews remain available when selection provenance is unavailable.
    """

    ranking = alert.sector_ranking_evidence
    if alert.alert_type != "POSSIBLE_30M_BUY" or ranking is None:
        return True, None
    attestation = str(
        sector_presentation.get("sector_ranking_catalog_attestation") or ""
    )
    if attestation == _EXACT_SECTOR_RANKING_CATALOG_ATTESTATION:
        return True, None
    return (
        False,
        _SECTOR_RANKING_CATALOG_ENTRY_REASONS.get(
            attestation,
            "QMT_RANKING_CATALOG_NOT_EXACT_FOR_PAPER_ENTRY",
        ),
    )


def _paper_entry_selection_evidence(
    feedback: HumanReviewFeedback,
    alert: HumanReviewAlert,
    sector_presentation: Mapping[str, object],
) -> HumanPaperEntrySelectionEvidence | None:
    """Freeze the exact QMT catalog admission used by a ranked buy."""

    ranking = alert.sector_ranking_evidence
    if (
        alert.alert_type != "POSSIBLE_30M_BUY"
        or not feedback.point_judgement.startswith("BUY_")
        or feedback.disposition != "PAPER_OBSERVE"
        or ranking is None
    ):
        return None
    if (
        sector_presentation.get("sector_ranking_catalog_attestation")
        != _EXACT_SECTOR_RANKING_CATALOG_ATTESTATION
    ):
        return None
    entry_sha256 = sector_presentation.get("sector_name_entry_sha256")
    catalog_revision = sector_presentation.get("sector_name_catalog_revision")
    captured_at = sector_presentation.get("sector_name_captured_at")
    if entry_sha256 is None or catalog_revision is None or captured_at is None:
        raise ValueError("exact QMT catalog presentation is incomplete")
    return HumanPaperEntrySelectionEvidence(
        feedback_id=feedback.feedback_id,
        candidate_id=feedback.candidate_id,
        source_screen_content_sha256=feedback.source_screen_content_sha256,
        symbol=alert.symbol,
        sector_id=ranking.sector_id,
        sector_name=ranking.sector_name,
        sector_ranking_evidence_id=ranking.evidence_id,
        sector_ranking_observed_at=ranking.observed_at,
        sector_catalog_revision=str(catalog_revision),
        sector_catalog_entry_sha256=str(entry_sha256),
        sector_catalog_captured_at=datetime.fromisoformat(str(captured_at)),
        attested_at=feedback.reviewed_at,
    )


class HumanReviewScreeningService:
    """Validate screens, expose review candidates and record human feedback."""

    def __init__(
        self,
        *,
        repository_root: Path,
        historical_report: Path,
        forward_root: Path,
        feedback_ledger: Path,
        sector_ledger: Path,
        paper_ledger: Path | None = None,
        parameter_snapshot: Path | None = None,
        live_screening_snapshot: Path | None = None,
        live_archive_root: Path | None = None,
        forward_markout_report: Path | None = None,
        forward_warmup_lineage_report: Path | None = None,
        clock: Callable[[], datetime] | None = None,
        sector_capture_due: datetime_time | None = None,
        trading_session_provider: Callable[..., Mapping[str, object]] | None = None,
        readiness_trading_session_provider: Callable[..., Mapping[str, object]]
        | None = None,
        forward_scheduler_provider: Callable[..., Mapping[str, object]] | None = None,
        forward_implementation_provenance_provider: Callable[[], Mapping[str, object]]
        | None = None,
    ) -> None:
        self.repository_root = repository_root.resolve()
        self.historical_report = historical_report.resolve()
        self.forward_root = forward_root.resolve()
        self.feedback_ledger = feedback_ledger.resolve()
        self.sector_ledger = sector_ledger.resolve()
        self._clock = clock or (lambda: datetime.now(ZoneInfo("Asia/Shanghai")))
        self._sector_capture_due = sector_capture_due
        if trading_session_provider is not None and not callable(
            trading_session_provider
        ):
            raise TypeError("trading_session_provider must be callable")
        self._trading_session_provider = trading_session_provider
        if readiness_trading_session_provider is not None and not callable(
            readiness_trading_session_provider
        ):
            raise TypeError("readiness_trading_session_provider must be callable")
        self._readiness_trading_session_provider = readiness_trading_session_provider
        if forward_scheduler_provider is not None and not callable(
            forward_scheduler_provider
        ):
            raise TypeError("forward_scheduler_provider must be callable")
        self._forward_scheduler_provider = forward_scheduler_provider
        if forward_implementation_provenance_provider is not None and not callable(
            forward_implementation_provenance_provider
        ):
            raise TypeError(
                "forward_implementation_provenance_provider must be callable"
            )
        self._forward_implementation_provenance_provider = (
            forward_implementation_provenance_provider
            or (lambda: current_forward_implementation_provenance(self.repository_root))
        )
        self.paper_ledger = (
            paper_ledger
            if paper_ledger is not None
            else feedback_ledger.with_name("paper_ledger.json")
        ).resolve()
        self.parameter_snapshot = (
            parameter_snapshot
            if parameter_snapshot is not None
            else repository_root
            / "config"
            / "decision_support"
            / "human_review_parameters.json"
        ).resolve()
        self.live_screening_snapshot = (
            None
            if live_screening_snapshot is None
            else live_screening_snapshot.resolve()
        )
        self.live_archive_root = (
            None if live_archive_root is None else live_archive_root.resolve()
        )
        self.forward_markout_report = (
            None if forward_markout_report is None else forward_markout_report.resolve()
        )
        self.forward_warmup_lineage_report = (
            forward_root / "forward_warmup_structure_lineage_rollup.json"
            if forward_warmup_lineage_report is None
            else forward_warmup_lineage_report
        ).resolve()
        self._write_lock = threading.RLock()
        # 不可变报告按内容寻址。解析并语义校验全市场报告可能消耗大量 CPU，因此保留
        # 小型精确文件状态缓存，供页面、详情和反馈重复读取。账本与调度覆盖仍在每次
        # 请求时重算，只复用不可变源报告和提醒对象。
        self._report_cache_lock = threading.RLock()
        self._report_cache: dict[
            tuple[str, int, int],
            tuple[dict[str, object], tuple[HumanReviewAlert, ...]],
        ] = {}
        # 子进程还会发布紧凑且绑定哈希的网页索引，避免 Flask 解析和校验超过 80 MiB
        # 的证据归档；只有复核者选中候选或提交反馈时，才从 JSONL 详情库读取完整候选。
        self._web_bundle_cache_lock = threading.RLock()
        self._web_bundle_cache: dict[
            tuple[str, int, int, str, int, int],
            _LoadedWebBundle,
        ] = {}
        # 前向采集严格审计可能经官方日历兜底访问 QMT。直接业务调用仍执行同步审计，
        # 就绪接口则只读取精确缓存或启动一个后台校验，不能占住 HTTP 请求线程。
        self._forward_capture_readiness_lock = threading.Lock()
        self._forward_capture_readiness_cache_key: tuple[object, ...] | None = None
        self._forward_capture_readiness_cache: dict[str, object] | None = None
        self._forward_capture_readiness_cache_at: float | None = None
        self._forward_capture_readiness_inflight_key: tuple[object, ...] | None = None
        self._forward_capture_readiness_thread: threading.Thread | None = None
        # ``forward_delivery_readiness`` 认证完整前向账本、不可变工件和当前实现来源。
        # 直接调用者仍执行同步严格审计，但不能让 ``/readyz`` 变成数分钟请求；下方应用
        # 方法只运行一个后台校验，并仅按精确输入身份缓存判定。
        self._forward_delivery_readiness_lock = threading.Lock()
        self._forward_delivery_readiness_cache_key: tuple[object, ...] | None = None
        self._forward_delivery_readiness_cache: dict[str, object] | None = None
        self._forward_delivery_readiness_cache_at: float | None = None
        self._forward_delivery_readiness_inflight_key: tuple[object, ...] | None = None
        self._forward_delivery_readiness_thread: threading.Thread | None = None

    def _forward_warmup_structure_lineage(self) -> dict[str, object]:
        path = self.forward_warmup_lineage_report
        if not path.is_file():
            return {
                "status": "NOT_AVAILABLE",
                "qualified_session_count": 0,
                "recorded_session_count": 0,
                "structure_event_count": 0,
                "subjects": {},
                "sessions": [],
                "diagnostic_only": True,
                "parameters_changed": False,
                "live_status": "LIVE_DISABLED",
                "reason_codes": ["FORWARD_WARMUP_STRUCTURE_LINEAGE_NOT_YET_AVAILABLE"],
            }
        payload = _read_json(
            path,
            "human_review_forward_warmup_structure_lineage_unreadable",
        )
        try:
            validated = validate_forward_warmup_structure_lineage_rollup_document(
                payload
            )
        except (ArithmeticError, KeyError, TypeError, ValueError) as exc:
            raise HumanReviewScreenUnavailable(
                "human_review_forward_warmup_structure_lineage_invalid"
            ) from exc
        return {
            "status": validated["status"],
            "through_session": validated["through_session"],
            "content_sha256": validated["content_sha256"],
            "source_session_qualification_sha256": validated[
                "source_session_qualification_sha256"
            ],
            "qualified_session_count": validated["qualified_session_count"],
            "recorded_session_count": validated["recorded_session_count"],
            "source_signal_count": validated["source_signal_count"],
            "lineage_extension_signal_count": validated[
                "lineage_extension_signal_count"
            ],
            "unique_lineage_diagnostic_count": validated[
                "unique_lineage_diagnostic_count"
            ],
            "structure_event_count": validated["structure_event_count"],
            "subjects": validated["subjects"],
            "sessions": validated["sessions"],
            "cross_session_convergence_adjudication": validated[
                "cross_session_convergence_adjudication"
            ],
            "validation_scope": "SELF_CONTAINED_DERIVED_INVARIANTS",
            "source_rebuild_required_for_full_verification": True,
            "diagnostic_only": True,
            "parameters_changed": False,
            "live_status": "LIVE_DISABLED",
            "reason_codes": [],
        }

    def _candidate_warmup_diagnostic(
        self,
        source_content_sha256: object,
    ) -> dict[str, object]:
        """Load the sidecar for this exact screen without affecting the gate."""

        unavailable = {
            "status": "NOT_AVAILABLE",
            "selected_candidate_count": 0,
            "classification_counts": [],
            "candidates": {},
            "diagnostic_only": True,
            "active_gate_unchanged": True,
            "ranking_parameters_unchanged": True,
            "candidate_identity_unchanged": True,
            "paper_observation_eligibility_unchanged": True,
            "live_status": "LIVE_DISABLED",
            "reason_codes": ["CANDIDATE_WARMUP_DIAGNOSTIC_NOT_YET_AVAILABLE"],
        }
        if (
            not isinstance(source_content_sha256, str)
            or _SHA256.fullmatch(source_content_sha256) is None
        ):
            return unavailable
        parameter_set_id = candidate_warmup_parameter_set_id()
        path = candidate_warmup_diagnostic_path(
            self.forward_root,
            source_content_sha256=source_content_sha256,
            parameter_set_id=parameter_set_id,
        )
        if not path.is_file():
            return unavailable
        try:
            payload = _read_json(
                path,
                "human_review_candidate_warmup_diagnostic_unreadable",
            )
            document = validate_candidate_warmup_diagnostic_document(
                payload,
                expected_source_content_sha256=source_content_sha256,
                expected_parameter_set_id=parameter_set_id,
            )
            presentation = candidate_warmup_presentation(document)
        except (ArithmeticError, KeyError, OSError, TypeError, ValueError):
            return {
                **unavailable,
                "status": "INVALID",
                "reason_codes": ["CANDIDATE_WARMUP_DIAGNOSTIC_INVALID"],
            }
        return {**presentation, "reason_codes": []}

    def _forward_markout(self) -> dict[str, object]:
        path = self.forward_markout_report
        if path is None or not path.is_file():
            return {
                "status": "NOT_AVAILABLE",
                "diagnostic_only": True,
                "portfolio_performance_evaluable": False,
                "summary": {},
                "sample": {},
                "source_provenance_status": "UNAVAILABLE",
                "reason_codes": ["FORWARD_MARKOUT_NOT_YET_AVAILABLE"],
            }
        payload = _read_json(path, "human_review_forward_markout_unreadable")
        stable = {key: payload[key] for key in payload if key != "content_sha256"}
        summary = payload.get("summary")
        sample = payload.get("sample")
        reasons = payload.get("reason_codes")
        schema = payload.get("schema")
        source_provenance_status = payload.get("source_provenance_status")
        contract_valid = True
        try:
            validate_forward_review_markout_document(payload)
        except (ArithmeticError, KeyError, TypeError, ValueError):
            contract_valid = False
        if (
            schema != FORWARD_REVIEW_MARKOUT_SCHEMA
            or not contract_valid
            or payload.get("content_sha256") != sha256_json(stable)
            or payload.get("diagnostic_only") is not True
            or payload.get("portfolio_performance_evaluable") is not False
            or payload.get("orders_created") != 0
            or payload.get("fills_created") != 0
            or payload.get("positions_created") != 0
            or payload.get("automated_order_authorized") is not False
            or payload.get("broker_transport_available") is not False
            or payload.get("live_status") != "LIVE_DISABLED"
            or not isinstance(summary, Mapping)
            or not isinstance(sample, Mapping)
            or not isinstance(reasons, list)
        ):
            raise HumanReviewScreenUnavailable("human_review_forward_markout_invalid")
        return {
            "status": "AVAILABLE",
            "through_session": payload.get("through_session"),
            "content_sha256": payload.get("content_sha256"),
            "diagnostic_only": True,
            "portfolio_performance_evaluable": False,
            "summary": dict(summary),
            "summary_by_risk_class": dict(payload.get("summary_by_risk_class") or {}),
            "sample": dict(sample),
            "source_provenance_status": source_provenance_status,
            "source_session_qualification": dict(
                payload["source_session_qualification"]
            ),
            "reason_codes": list(reasons),
        }

    @staticmethod
    def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)

    def _materialize_live_report(self) -> Path | None:
        source = self.live_screening_snapshot
        root = self.live_archive_root
        if source is None or root is None or not source.is_file():
            return None
        materialized = self._materialized_live_report_from_receipt(
            source=source,
            root=root,
        )
        if materialized is not None:
            return materialized
        # 小型夹具和紧凑部署保留即时行为。全市场快照超过 100 MiB，在此解析校验会独占
        # 网页解释器；筛选服务改由子进程校验该精确文件并原子发布上方消费的回执。在此
        # 之前，上一份不可变报告仍可用，但会明确标为过期且仅供复核。
        try:
            if source.stat().st_size > _MAX_SYNCHRONOUS_LIVE_SNAPSHOT_BYTES:
                return None
        except OSError:
            return None
        payload = _read_json(source, "human_review_live_snapshot_unreadable")
        try:
            review_at, _signals = validate_live_review_snapshot(payload)
            session = review_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
            # 上方校验会独立重算该语义身份；若重新哈希完整可变 JSON，生成时间、节奏计时
            # 和覆盖批次计数会在市场与决策事实完全不变时制造新候选或新报告。
            source_sha256 = str(payload["snapshot_content_sha256"])
            report = live_human_review_document(
                live_snapshot=payload,
                source_snapshot_sha256=source_sha256,
                session=session,
                decision_source_snapshot=(_WEB_PROCESS_DECISION_SOURCE_SNAPSHOT),
            )
        except (ArithmeticError, TypeError, ValueError) as exc:
            raise HumanReviewScreenUnavailable(
                "human_review_live_snapshot_invalid"
            ) from exc
        content_sha256 = str(report["content_sha256"])
        path = root / session.isoformat() / f"{content_sha256[7:]}.json"
        lock = root / ".live_review_archive.lock"
        with interprocess_file_lock(lock):
            if path.is_file():
                existing = _read_json(path, "human_review_live_archive_unreadable")
                try:
                    _validate_report(existing)
                except HumanReviewScreenUnavailable as exc:
                    raise HumanReviewScreenUnavailable(
                        "human_review_live_archive_collision"
                    ) from exc
                if existing.get("content_sha256") != report.get("content_sha256"):
                    raise HumanReviewScreenUnavailable(
                        "human_review_live_archive_collision"
                    )
            else:
                self._write_json_atomic(path, report)
        return path

    @staticmethod
    def _materialized_live_report_from_receipt(
        *,
        source: Path,
        root: Path,
    ) -> Path | None:
        """Resolve a child-validated report without reopening the huge source."""

        return resolve_live_review_materialization_receipt(
            source_path=source,
            archive_root=root,
            expected_decision_source_snapshot_id=(
                _WEB_PROCESS_DECISION_SOURCE_SNAPSHOT_ID
            ),
        )

    def _web_bundle(
        self,
        *,
        require_current_source: bool,
    ) -> LiveReviewWebBundle | None:
        source = self.live_screening_snapshot
        root = self.live_archive_root
        if source is None or root is None or not source.is_file():
            return None
        return resolve_live_review_web_bundle_receipt(
            source_path=source,
            archive_root=root,
            expected_decision_source_snapshot_id=(
                _WEB_PROCESS_DECISION_SOURCE_SNAPSHOT_ID
                if require_current_source
                else None
            ),
            require_current_source=require_current_source,
        )

    def _current_web_bundle(self) -> LiveReviewWebBundle | None:
        return self._web_bundle(require_current_source=True)

    def _latest_web_bundle(self) -> LiveReviewWebBundle | None:
        return self._web_bundle(require_current_source=False)

    def _load_web_bundle(
        self,
        bundle: LiveReviewWebBundle,
    ) -> _LoadedWebBundle:
        try:
            index_stat = bundle.index_path.stat()
            detail_stat = bundle.detail_path.stat()
        except OSError as exc:
            raise HumanReviewScreenUnavailable(
                "human_review_web_bundle_unreadable"
            ) from exc
        identity = (
            str(bundle.index_path),
            int(index_stat.st_size),
            int(index_stat.st_mtime_ns),
            str(bundle.detail_path),
            int(detail_stat.st_size),
            int(detail_stat.st_mtime_ns),
        )
        with self._web_bundle_cache_lock:
            cached = self._web_bundle_cache.get(identity)
            if cached is not None:
                return cached
        index = _read_json(
            bundle.index_path,
            "human_review_web_bundle_unreadable",
        )
        stable = {key: value for key, value in index.items() if key != "content_sha256"}
        queue = index.get("review_queue")
        try:
            if (
                index.get("schema") != LIVE_REVIEW_WEB_INDEX_SCHEMA
                or index.get("content_sha256") != sha256_json(stable)
                or index.get("content_sha256") != bundle.index_content_sha256
                or index.get("source_report_content_sha256")
                != bundle.report_content_sha256
                or index.get("source_snapshot_content_sha256")
                != bundle.source_snapshot_content_sha256
                or index.get("decision_source_snapshot_id")
                != bundle.decision_source_snapshot_id
                or index.get("highest_status") != "REVIEW_REQUIRED"
                or index.get("human_confirmation_required") is not True
                or index.get("automated_order_authorized") is not False
                or index.get("orders_created") != 0
                or index.get("fills_created") != 0
                or index.get("live_status") != "LIVE_DISABLED"
                or not isinstance(queue, list)
                or index.get("review_queue_count") != len(queue)
            ):
                raise ValueError("compact review index boundary is invalid")
            candidates = tuple(_parse_candidate_summary(value) for value in queue)
            by_candidate_id = {value.candidate_id: value for value in candidates}
            if len(by_candidate_id) != len(candidates):
                raise ValueError("compact review candidate identity duplicated")
            spans = sorted(
                (
                    value.detail_offset,
                    value.detail_offset + value.detail_length,
                )
                for value in candidates
            )
            if candidates and (
                not spans
                or spans[0][0] != 0
                or spans[-1][1] != int(detail_stat.st_size)
                or any(left[1] != right[0] for left, right in zip(spans, spans[1:]))
            ):
                raise ValueError("compact review detail spans are incomplete")
        except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
            raise HumanReviewScreenUnavailable(
                "human_review_web_bundle_invalid"
            ) from exc
        report = {
            "content_sha256": bundle.report_content_sha256,
            "input_hashes": index.get("input_hashes") or {},
            "sample": index.get("sample") or {},
            "scope": index.get("scope") or {},
            "candidate_funnel": index.get("candidate_funnel") or {},
            "signal_counts": index.get("signal_counts") or {},
            "event_study": {"summary": index.get("event_study_summary") or {}},
            "data_caveats": index.get("data_caveats") or [],
            "division_of_responsibility": (
                index.get("division_of_responsibility") or {}
            ),
            "highest_status": "REVIEW_REQUIRED",
            "automated_order_authorized": False,
            "live_status": "LIVE_DISABLED",
        }
        loaded = _LoadedWebBundle(
            bundle=bundle,
            report=report,
            candidates=candidates,
            by_candidate_id=by_candidate_id,
        )
        with self._web_bundle_cache_lock:
            if len(self._web_bundle_cache) >= 2:
                self._web_bundle_cache.pop(next(iter(self._web_bundle_cache)))
            self._web_bundle_cache[identity] = loaded
        return loaded

    def _candidate_from_web_bundle(
        self,
        loaded: _LoadedWebBundle,
        *,
        candidate_id: str,
    ) -> HumanReviewAlert:
        summary = loaded.by_candidate_id.get(candidate_id)
        if summary is None:
            raise HumanReviewScreenUnavailable("human_review_candidate_not_found")
        try:
            with loaded.bundle.detail_path.open("rb") as handle:
                handle.seek(summary.detail_offset)
                encoded = handle.read(summary.detail_length)
            if (
                len(encoded) != summary.detail_length
                or "sha256:" + hashlib.sha256(encoded).hexdigest()
                != summary.detail_line_sha256
            ):
                raise ValueError("candidate detail span identity changed")
            detail = json.loads(encoded.decode("utf-8"))
            if (
                not isinstance(detail, Mapping)
                or detail.get("schema") != LIVE_REVIEW_CANDIDATE_DETAIL_SCHEMA
                or detail.get("source_report_content_sha256")
                != loaded.bundle.report_content_sha256
                or detail.get("candidate_id") != candidate_id
                or detail.get("highest_status") != "REVIEW_REQUIRED"
                or detail.get("automated_order_authorized") is not False
                or detail.get("real_account_accessed") is not False
                or detail.get("real_order_transport_enabled") is not False
                or detail.get("live_status") != "LIVE_DISABLED"
            ):
                raise ValueError("candidate detail boundary is invalid")
            alert = parse_human_review_alert(detail.get("candidate"))
            if alert.candidate_id != candidate_id:
                raise ValueError("candidate detail identity changed")
        except (
            InvalidOperation,
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ) as exc:
            raise HumanReviewScreenUnavailable(
                "human_review_candidate_detail_invalid"
            ) from exc
        return alert

    def _live_reports(self) -> tuple[Path, ...]:
        root = self.live_archive_root
        archived = (
            ()
            if root is None or not root.is_dir()
            else tuple(
                value
                for value in root.glob("*/*.json")
                if _IMMUTABLE_REPORT_NAME.fullmatch(value.name) is not None
            )
        )
        # 后台扫描仍在运行时，覆盖周期可以合理地尚未完成。该快照绝不能晋级，但也不能
        # 使最后一份不可变实时报告或前向/历史来源无法读取；只把当前可变候选视为不可用，
        # 归档报告仍可在下方独立验证。
        try:
            current = self._materialize_live_report()
        except HumanReviewScreenUnavailable:
            current = None
        ordered = sorted(
            set(archived),
            key=lambda value: (value.parent.name, value.stat().st_mtime_ns),
            reverse=True,
        )
        if current is None:
            return tuple(ordered)
        return (current, *(value for value in ordered if value != current))

    def _forward_reports(self) -> tuple[Path, ...]:
        root = self.forward_root / "sessions"
        if not root.is_dir():
            return ()
        aliases = tuple(root.glob("*/forward_human_review_screen.json"))
        immutable = tuple(root.glob("*/objects/forward_human_review_screen/*.json"))
        return tuple(
            sorted(
                set((*aliases, *immutable)),
                key=lambda value: (
                    value.parents[2].name
                    if value.parent.name == "forward_human_review_screen"
                    else value.parent.name,
                    value.stat().st_mtime_ns,
                ),
                reverse=True,
            )
        )

    def _historical_reports(self) -> tuple[Path, ...]:
        """Return the current-release sidecar when it exists."""

        return (self.historical_report,) if self.historical_report.is_file() else ()

    def _candidate_paths(self, source: str) -> tuple[tuple[str, Path], ...]:
        if source not in _SOURCE_KINDS:
            raise HumanReviewScreenUnavailable("human_review_source_invalid")
        if source == "live":
            paths = tuple(("live", value) for value in self._live_reports())
        elif source == "forward":
            paths = tuple(("forward", value) for value in self._forward_reports())
        elif source == "historical":
            paths = tuple(("historical", value) for value in self._historical_reports())
        else:
            live = tuple(("live", value) for value in self._live_reports())
            forward = tuple(("forward", value) for value in self._forward_reports())
            historical = tuple(
                ("historical", value) for value in self._historical_reports()
            )
            paths = live + forward + historical
        if not paths:
            raise HumanReviewScreenUnavailable("human_review_report_unavailable")
        return paths

    def _load_path(
        self, kind: str, path: Path
    ) -> tuple[dict[str, object], tuple[HumanReviewAlert, ...]]:
        del kind  # 以不可变文件标识为键，而不是展示通道。
        try:
            resolved = path.resolve()
            stat = resolved.stat()
        except OSError as exc:
            raise HumanReviewScreenUnavailable(
                "human_review_report_unreadable"
            ) from exc
        identity = (str(resolved), int(stat.st_size), int(stat.st_mtime_ns))
        with self._report_cache_lock:
            cached = self._report_cache.get(identity)
            if cached is not None:
                return cached
            payload = _read_json(resolved, "human_review_report_unreadable")
            alerts = _validate_report(payload)
            loaded = (payload, alerts)
            if len(self._report_cache) >= 4:
                self._report_cache.pop(next(iter(self._report_cache)))
            self._report_cache[identity] = loaded
            return loaded

    def _load_source(
        self, source: str
    ) -> tuple[str, Path, dict[str, object], tuple[HumanReviewAlert, ...]]:
        kind, path = self._candidate_paths(source)[0]
        payload, alerts = self._load_path(kind, path)
        return kind, path, payload, alerts

    def _load_source_for_snapshot(
        self,
        source: str,
        *,
        include_evidence: bool,
    ) -> tuple[
        str,
        Path,
        dict[str, object],
        tuple[_ReviewCandidate, ...],
    ]:
        """Use the compact current-live projection for the browser page."""

        if not include_evidence and source in {"latest", "live"}:
            bundle = self._latest_web_bundle()
            if bundle is not None:
                kind, path = self._candidate_paths(source)[0]
                if kind == "live" and path.resolve() == bundle.report_path:
                    loaded = self._load_web_bundle(bundle)
                    return (
                        kind,
                        path,
                        loaded.report,
                        loaded.candidates,
                    )
        kind, path, report, alerts = self._load_source(source)
        return kind, path, report, alerts

    def _load_candidate_by_hash(
        self,
        *,
        source_sha256: str,
        candidate_id: str,
    ) -> tuple[str, Path, dict[str, object], HumanReviewAlert]:
        if (
            _SHA256.fullmatch(source_sha256) is None
            or _SHA256.fullmatch(candidate_id) is None
        ):
            raise HumanReviewScreenUnavailable(
                "human_review_candidate_identity_invalid"
            )
        bundle = self._latest_web_bundle()
        if bundle is not None and bundle.report_content_sha256 == source_sha256:
            loaded = self._load_web_bundle(bundle)
            return (
                "live",
                bundle.report_path,
                loaded.report,
                self._candidate_from_web_bundle(
                    loaded,
                    candidate_id=candidate_id,
                ),
            )
        kind, path, report, alerts = self._load_by_hash(source_sha256)
        alert = next(
            (value for value in alerts if value.candidate_id == candidate_id),
            None,
        )
        if alert is None:
            raise HumanReviewScreenUnavailable("human_review_candidate_not_found")
        return kind, path, report, alert

    def _load_by_hash(
        self, source_sha256: str
    ) -> tuple[str, Path, dict[str, object], tuple[HumanReviewAlert, ...]]:
        if _SHA256.fullmatch(source_sha256) is None:
            raise HumanReviewScreenUnavailable("human_review_source_hash_invalid")
        paths = self._candidate_paths("latest")
        for kind, path in paths:
            payload = _read_json(path, "human_review_report_unreadable")
            if payload.get("content_sha256") != source_sha256:
                continue
            alerts = _validate_report(payload)
            return kind, path, payload, alerts
        raise HumanReviewScreenUnavailable("human_review_source_not_found")

    @staticmethod
    def _report_market_session(report: Mapping[str, object]) -> date:
        sample = report.get("sample")
        raw_cutoff = (
            sample.get("market_data_as_of") if isinstance(sample, Mapping) else None
        )
        declared_session = report.get("forward_paper_session")
        try:
            if raw_cutoff is not None:
                cutoff = datetime.fromisoformat(str(raw_cutoff))
                if cutoff.tzinfo is None:
                    raise ValueError("market cutoff must be timezone-aware")
                session = cutoff.astimezone(ZoneInfo("Asia/Shanghai")).date()
            else:
                session = date.fromisoformat(str(declared_session))
            if (
                declared_session is not None
                and date.fromisoformat(str(declared_session)) != session
            ):
                raise ValueError("report session differs from market cutoff")
        except (TypeError, ValueError) as exc:
            raise HumanReviewScreenUnavailable(
                "human_review_source_market_session_invalid"
            ) from exc
        return session

    def _current_market_session(self) -> date | None:
        path = self.live_screening_snapshot
        if path is None:
            # 仅前向或测试部署保留原行为；生产环境始终配置这一独立验证来源。
            return None
        bundle = self._current_web_bundle()
        if bundle is not None:
            report = self._load_web_bundle(bundle).report
            sample = report.get("sample")
            raw_cutoff = (
                sample.get("market_data_as_of") if isinstance(sample, Mapping) else None
            )
            try:
                cutoff = datetime.fromisoformat(str(raw_cutoff))
                if cutoff.tzinfo is None or cutoff.utcoffset() is None:
                    raise ValueError("market cutoff must be timezone-aware")
            except (TypeError, ValueError) as exc:
                raise HumanReviewScreenUnavailable(
                    "human_review_current_market_session_unavailable"
                ) from exc
            return cutoff.astimezone(ZoneInfo("Asia/Shanghai")).date()
        if not path.is_file():
            raise HumanReviewScreenUnavailable(
                "human_review_current_market_session_unavailable"
            )
        payload = _read_json(path, "human_review_live_snapshot_unreadable")
        try:
            cutoff = validate_live_screening_market_watermark(payload)
        except (TypeError, ValueError) as exc:
            raise HumanReviewScreenUnavailable(
                "human_review_current_market_session_unavailable"
            ) from exc
        return cutoff.astimezone(ZoneInfo("Asia/Shanghai")).date()

    def _paper_observation_eligibility(
        self,
        *,
        kind: str,
        source_sha256: str,
        source_report: Mapping[str, object],
        force_refresh_scheduler: bool = False,
    ) -> tuple[bool, str | None, str | None, str | None]:
        """Allow paper intents only from the latest live/forward evidence.

        Historical screens remain useful for chart-locked review and feedback,
        but turning an old research candidate into a new current-market intent
        would join an old structural reference to a later 1m execution bar.
        Likewise, an archived live/forward report must not create or cancel an
        intent after a newer immutable report has superseded it.
        """

        if kind not in {"live", "forward"}:
            return False, "HISTORICAL_SOURCE_REVIEW_ONLY", None, None
        try:
            source_session = self._report_market_session(source_report)
        except HumanReviewScreenUnavailable:
            return (
                False,
                "SOURCE_MARKET_SESSION_UNAVAILABLE_FOR_PAPER",
                None,
                None,
            )
        exact_live_bundle = self._current_web_bundle() if kind == "live" else None
        if kind == "live" and exact_live_bundle is None:
            source = self.live_screening_snapshot
            try:
                large_live_source = bool(
                    source is not None
                    and source.stat().st_size > _MAX_SYNCHRONOUS_LIVE_SNAPSHOT_BYTES
                )
            except OSError:
                large_live_source = True
            if large_live_source:
                return (
                    False,
                    "CURRENT_PAPER_SOURCE_UNAVAILABLE",
                    source_session.isoformat(),
                    None,
                )
        try:
            bundle = exact_live_bundle
            if bundle is not None:
                current_kind = "live"
                current_report = self._load_web_bundle(bundle).report
            else:
                current_kind, _path, current_report, _alerts = self._load_source(kind)
        except HumanReviewScreenUnavailable:
            return (
                False,
                "CURRENT_PAPER_SOURCE_UNAVAILABLE",
                source_session.isoformat(),
                None,
            )
        if (
            current_kind != kind
            or current_report.get("content_sha256") != source_sha256
        ):
            return (
                False,
                "SOURCE_SUPERSEDED_FOR_PAPER",
                source_session.isoformat(),
                None,
            )
        try:
            current_market_session = self._current_market_session()
        except HumanReviewScreenUnavailable:
            return (
                False,
                "CURRENT_MARKET_DATA_SESSION_UNAVAILABLE_FOR_PAPER",
                source_session.isoformat(),
                None,
            )
        if (
            current_market_session is not None
            and source_session != current_market_session
        ):
            return (
                False,
                "SOURCE_MARKET_SESSION_NOT_CURRENT_FOR_PAPER",
                source_session.isoformat(),
                current_market_session.isoformat(),
            )
        operations_ready, operations_reason = (
            self._paper_forward_operations_eligibility(
                source_session=source_session,
                force_refresh_scheduler=force_refresh_scheduler,
            )
        )
        if not operations_ready:
            return (
                False,
                operations_reason,
                source_session.isoformat(),
                None
                if current_market_session is None
                else current_market_session.isoformat(),
            )
        return (
            True,
            None,
            source_session.isoformat(),
            None
            if current_market_session is None
            else current_market_session.isoformat(),
        )

    def _paper_forward_operations_eligibility(
        self,
        *,
        source_session: date,
        force_refresh_scheduler: bool = False,
    ) -> tuple[bool, str | None]:
        """Require a serviceable daily delivery path for new virtual intents.

        Human feedback remains independently hash-chained when this gate is
        red.  Only creation/cancellation of virtual intents is withheld, so a
        known-broken scheduler or a not-yet-proven same-session PIT capture
        cannot create a paper sample that the 15:20 evaluator is unable to
        settle or archive.  Screening and hash-chained human feedback remain
        available before the scheduled Capture; only the virtual intent waits
        for immutable point-in-time sector evidence.
        """

        provider = self._forward_scheduler_provider
        if provider is None:
            # 直接研究或测试服务可有意省略前向运行适配器；生产环境会注入严格共享探针。
            return True, None
        try:
            scheduler = validate_forward_scheduler_snapshot(
                provider(force_refresh=force_refresh_scheduler)
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            return False, "FORWARD_SCHEDULER_NOT_READY_FOR_PAPER"
        if scheduler.get("ready") is not True:
            return False, "FORWARD_SCHEDULER_NOT_READY_FOR_PAPER"

        observed_at = self._clock()
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            return False, "FORWARD_OPERATIONS_CLOCK_INVALID_FOR_PAPER"
        local = observed_at.astimezone(ZoneInfo("Asia/Shanghai"))
        scheduler_observed_at = datetime.fromisoformat(
            str(scheduler["observed_at"])
        ).astimezone(ZoneInfo("Asia/Shanghai"))
        if (
            scheduler_observed_at > local + _FORWARD_SCHEDULER_FUTURE_TOLERANCE
            or local - scheduler_observed_at > _FORWARD_SCHEDULER_MAX_AGE
        ):
            return False, "FORWARD_SCHEDULER_OBSERVATION_STALE_FOR_PAPER"
        due = self._sector_capture_due
        if due is None:
            return True, None
        # ``due`` 只配置采集运行计划，不能授权在计划执行前伪造时点证据。否则截止前创建的
        # 虚拟意图可能在后续采集失败时悬空：收盘后评估器会正确拒绝补填缺失回执，悬空
        # 意图则污染下一交易日的因果连续性审计。因此全天都必须要求不可变回执；同一条
        # 幂等人工反馈可在采集后重试并对账成意图，不会丢失复核工作。
        try:
            capture = self.forward_archive_capture_readiness(session=source_session)
        except (OSError, RuntimeError, TypeError, ValueError):
            return False, "SAME_SESSION_FORWARD_CAPTURE_NOT_READY_FOR_PAPER"
        if capture.get("ready") is not True:
            return False, "SAME_SESSION_FORWARD_CAPTURE_NOT_READY_FOR_PAPER"
        return True, None

    def _paper_entry_boundary_source_audit(
        self,
        events: Sequence[Mapping[str, object]],
        *,
        current_source_sha256: str,
        current_alerts: Sequence[_ReviewCandidate],
    ) -> dict[str, object]:
        """Resolve each full ledger boundary back to its immutable alert."""

        cache: dict[str, tuple[_ReviewCandidate, ...] | None] = {
            current_source_sha256: tuple(current_alerts)
        }
        boundary_count = 0
        verified = 0
        unavailable: list[str] = []
        invalid: list[dict[str, str]] = []
        for event in events:
            payload = event.get("payload")
            if (
                event.get("kind") != "INTENT"
                or not isinstance(payload, Mapping)
                or payload.get("side") != "BUY"
                or payload.get("entry_boundary_evidence_id") is None
            ):
                continue
            boundary_count += 1
            intent_id = str(payload.get("intent_id") or "UNKNOWN_INTENT")
            raw_boundary = payload.get("entry_execution_boundary")
            if raw_boundary is None:
                invalid.append(
                    {
                        "intent_id": intent_id,
                        "reason": "ENTRY_EXECUTION_BOUNDARY_REQUIRED",
                    }
                )
                continue
            source_hash = str(payload.get("source_screen_content_sha256") or "")
            if source_hash not in cache:
                try:
                    _kind, _path, _report, source_alerts = self._load_by_hash(
                        source_hash
                    )
                except HumanReviewScreenUnavailable:
                    cache[source_hash] = None
                else:
                    cache[source_hash] = source_alerts
            source_alerts = cache[source_hash]
            if source_alerts is None:
                unavailable.append(intent_id)
                continue
            source_alert = next(
                (
                    value
                    for value in source_alerts
                    if value.candidate_id == payload.get("candidate_id")
                ),
                None,
            )
            if source_alert is None:
                invalid.append(
                    {
                        "intent_id": intent_id,
                        "reason": "SOURCE_REPORT_CANDIDATE_NOT_FOUND",
                    }
                )
                continue
            if source_alert.entry_execution_boundary is None:
                invalid.append(
                    {
                        "intent_id": intent_id,
                        "reason": "SOURCE_ALERT_ENTRY_BOUNDARY_REQUIRED",
                    }
                )
                continue
            try:
                boundary = parse_entry_execution_boundary_document(raw_boundary)
            except ValueError as exc:
                invalid.append(
                    {
                        "intent_id": intent_id,
                        "reason": f"{type(exc).__name__}: {str(exc)[:200]}",
                    }
                )
                continue
            if boundary != source_alert.entry_execution_boundary:
                invalid.append(
                    {
                        "intent_id": intent_id,
                        "reason": "LEDGER_BOUNDARY_DIFFERS_FROM_SOURCE_ALERT",
                    }
                )
                continue
            verified += 1
        if invalid:
            status = "INVALID"
        elif unavailable:
            status = "INCOMPLETE_SOURCE_ARCHIVE"
        elif boundary_count:
            status = "COMPLETE"
        else:
            status = "NO_BOUNDARY_INTENTS"
        return {
            "schema": "chanlun-human-paper-entry-boundary-source-audit",
            "status": status,
            "boundary_intent_count": boundary_count,
            "verified_source_binding_count": verified,
            "source_unavailable_intent_ids": unavailable,
            "invalid_source_bindings": invalid,
            "immutable_source_alert_resolved": status
            in {"COMPLETE", "NO_BOUNDARY_INTENTS"},
            "broker_transport_available": False,
            "live_status": "LIVE_DISABLED",
        }

    def _paper_entry_selection_source_audit(
        self,
        events: Sequence[Mapping[str, object]],
        *,
        current_source_sha256: str,
        current_alerts: Sequence[_ReviewCandidate],
    ) -> dict[str, object]:
        """Bind each required QMT admission proof back to its source rank."""

        required: dict[str, set[str]] = {}
        for event in events:
            payload = event.get("payload")
            if (
                event.get("kind") != "INTENT"
                or not isinstance(payload, Mapping)
                or payload.get("side") != "BUY"
            ):
                continue
            source_hash = str(payload.get("source_screen_content_sha256") or "")
            candidate_id = str(payload.get("candidate_id") or "")
            required.setdefault(source_hash, set()).add(candidate_id)
        resolved: dict[str, tuple[HumanReviewAlert, ...]] = {}
        for source_hash, candidate_ids in required.items():
            current_full = tuple(
                value
                for value in current_alerts
                if isinstance(value, HumanReviewAlert)
                and value.candidate_id in candidate_ids
            )
            if source_hash == current_source_sha256 and len(current_full) == len(
                candidate_ids
            ):
                resolved[source_hash] = current_full
                continue
            loaded: list[HumanReviewAlert] = []
            for candidate_id in sorted(candidate_ids):
                try:
                    _kind, _path, _report, source_alert = self._load_candidate_by_hash(
                        source_sha256=source_hash,
                        candidate_id=candidate_id,
                    )
                except HumanReviewScreenUnavailable:
                    loaded = []
                    break
                loaded.append(source_alert)
            if loaded:
                resolved[source_hash] = tuple(loaded)
        return audit_human_paper_entry_selection_source_bindings(
            events,
            alerts_by_source_content_sha256=resolved,
        )

    def _sector_catalogs(
        self,
    ) -> tuple[tuple[Mapping[str, object], ...], str | None, str | None]:
        if not self.sector_ledger.is_file():
            return (), None, "QMT_SECTOR_LEDGER_UNAVAILABLE"
        try:
            ledger = load_sector_ledger(self.sector_ledger)
        except ValueError as exc:
            raise HumanReviewScreenUnavailable(
                "human_review_sector_ledger_invalid"
            ) from exc
        entries = ledger.get("entries") or ()
        if not entries:
            return (), None, "QMT_SECTOR_LEDGER_EMPTY"
        tail = entries[-1]
        return tuple(entries), str(tail.get("captured_at")), None

    def _required_sector_capture_session(self) -> date | None:
        if self._sector_capture_due is None:
            return None
        observed_at = self._clock()
        if observed_at.tzinfo is None:
            raise ValueError("human review clock must be timezone-aware")
        local = observed_at.astimezone(ZoneInfo("Asia/Shanghai"))
        if local.time() < self._sector_capture_due:
            return None
        requirement, _error = self._trading_session_requirement(
            session=local.date(),
            observed_at=local,
        )
        return local.date() if requirement["required"] is True else None

    def _trading_session_requirement(
        self,
        *,
        session: date,
        observed_at: datetime,
    ) -> tuple[dict[str, object], str | None]:
        return self._trading_session_requirement_from_provider(
            self._trading_session_provider,
            session=session,
            observed_at=observed_at,
        )

    def _readiness_trading_session_requirement(
        self,
        *,
        session: date,
        observed_at: datetime,
    ) -> tuple[dict[str, object], str | None]:
        provider = (
            self._readiness_trading_session_provider
            if self._readiness_trading_session_provider is not None
            else self._trading_session_provider
        )
        return self._trading_session_requirement_from_provider(
            provider,
            session=session,
            observed_at=observed_at,
        )

    @staticmethod
    def _trading_session_requirement_from_provider(
        provider: Callable[..., Mapping[str, object]] | None,
        *,
        session: date,
        observed_at: datetime,
    ) -> tuple[dict[str, object], str | None]:
        evidence: Mapping[str, object] | None = None
        error: str | None = None
        if provider is None:
            error = "TRADING_SESSION_PROVIDER_UNAVAILABLE"
        else:
            try:
                value = provider(session=session, observed_at=observed_at)
                if not isinstance(value, Mapping):
                    raise TypeError(
                        "trading session provider returned an invalid document"
                    )
                evidence = value
            except Exception as exc:
                error = f"{type(exc).__name__}: {str(exc)[:160]}"
        return (
            resolve_trading_session_requirement(
                evidence,
                session=session,
                observed_at=observed_at,
            ),
            error,
        )

    def _sector_receipt_audit(self) -> dict[str, object]:
        if not self.sector_ledger.is_file():
            return {
                "schema": QMT_SECTOR_RECEIPT_AUDIT_SCHEMA,
                "status": "SECTOR_LEDGER_UNAVAILABLE",
                "entry_count": 0,
                "valid_receipt_count": 0,
                "missing_entry_count": 0,
                "invalid_receipt_count": 0,
                "historical_receipts_synthesized": False,
                "real_account_accessed": False,
                "real_order_transport_enabled": False,
                "live_status": "LIVE_DISABLED",
            }
        try:
            return audit_sector_capture_receipts(
                output=self.sector_ledger,
                required_capture_session=self._required_sector_capture_session(),
            )
        except (OSError, ValueError) as exc:
            raise HumanReviewScreenUnavailable(
                "human_review_sector_receipt_audit_invalid"
            ) from exc

    def forward_archive_capture_readiness(
        self,
        *,
        session: date | None,
        _calendar_requirement: tuple[dict[str, object], str | None] | None = None,
    ) -> dict[str, object]:
        """同步认证每日前向归档的采集闸门。"""

        observed_at = self._clock()
        if observed_at.tzinfo is None:
            raise ValueError("human review clock must be timezone-aware")
        local = observed_at.astimezone(ZoneInfo("Asia/Shanghai"))
        if session is None and (
            self._sector_capture_due is None or local.time() < self._sector_capture_due
        ):
            return {
                "schema": QMT_FORWARD_CAPTURE_READINESS_SCHEMA,
                "required": False,
                "ready": False,
                "status": "not_due",
                "reason_code": "FORWARD_SESSION_NOT_DUE",
                "session": None,
                "receipt_proven": False,
                "real_account_accessed": False,
                "real_order_transport_enabled": False,
                "live_status": "LIVE_DISABLED",
            }
        required_session = local.date() if session is None else session
        if isinstance(required_session, datetime) or not isinstance(
            required_session, date
        ):
            raise TypeError("session must be a date")
        requirement, provider_error = (
            self._trading_session_requirement(
                session=required_session,
                observed_at=local,
            )
            if _calendar_requirement is None
            else _calendar_requirement
        )
        if requirement["required"] is not True:
            invalid = (
                requirement["trading_session_reason_code"]
                == "TRADING_SESSION_EVIDENCE_INVALID"
            )
            return {
                "schema": QMT_FORWARD_CAPTURE_READINESS_SCHEMA,
                **requirement,
                "ready": False,
                "status": (
                    "not_due" if requirement["required"] is False else "unresolved"
                ),
                "reason_code": (
                    "NON_TRADING_SESSION_NOT_DUE"
                    if requirement["required"] is False
                    else "TRADING_SESSION_EVIDENCE_INVALID"
                    if invalid
                    else "TRADING_SESSION_EVIDENCE_UNAVAILABLE"
                ),
                "session": required_session.isoformat(),
                "receipt_proven": False,
                "trading_session_provider_error": provider_error,
                "real_account_accessed": False,
                "real_order_transport_enabled": False,
                "live_status": "LIVE_DISABLED",
            }
        if not self.sector_ledger.is_file():
            return {
                "schema": QMT_FORWARD_CAPTURE_READINESS_SCHEMA,
                **requirement,
                "ready": False,
                "status": "not_ready",
                "reason_code": "SECTOR_LEDGER_UNAVAILABLE",
                "session": required_session.isoformat(),
                "receipt_proven": False,
                "trading_session_provider_error": provider_error,
                "real_account_accessed": False,
                "real_order_transport_enabled": False,
                "live_status": "LIVE_DISABLED",
            }
        decision_close = datetime.combine(
            required_session,
            datetime_time(15, 0),
            tzinfo=ZoneInfo("Asia/Shanghai"),
        )
        try:
            result = audit_forward_sector_capture_readiness(
                output=self.sector_ledger,
                session=required_session,
                decision_time=decision_close,
            )
        except (OSError, TypeError, ValueError) as exc:
            return {
                "schema": QMT_FORWARD_CAPTURE_READINESS_SCHEMA,
                **requirement,
                "ready": False,
                "status": "not_ready",
                "reason_code": "SECTOR_CAPTURE_LEDGER_INVALID",
                "session": required_session.isoformat(),
                "receipt_proven": False,
                "error": f"{type(exc).__name__}: {str(exc)[:160]}",
                "trading_session_provider_error": provider_error,
                "real_account_accessed": False,
                "real_order_transport_enabled": False,
                "live_status": "LIVE_DISABLED",
            }
        # 完整目录与回执文档可能很大，且已有专门审计界面；就绪接口只暴露其不可变身份和判定。
        return (
            {
                key: value
                for key, value in result.items()
                if key not in {"catalog", "receipt_audit"}
            }
            | requirement
            | {
                "trading_session_provider_error": provider_error,
            }
        )

    def _forward_capture_readiness_key(
        self,
        *,
        session: date | None,
        observed_at: datetime,
    ) -> tuple[object, ...]:
        local = observed_at.astimezone(ZoneInfo("Asia/Shanghai"))
        required_session = local.date() if session is None else session
        if isinstance(required_session, datetime) or not isinstance(
            required_session, date
        ):
            raise TypeError("session must be a date")
        if required_session != local.date():
            capture_phase = "OTHER_SESSION"
        elif self._sector_capture_due is None:
            capture_phase = "CAPTURE_NOT_SCHEDULED"
        elif local.time() < self._sector_capture_due:
            capture_phase = "BEFORE_CAPTURE"
        else:
            capture_phase = "CAPTURE_DUE"
        return (
            required_session.isoformat(),
            local.date().isoformat(),
            capture_phase,
            self._readiness_file_identity(self.sector_ledger),
        )

    @staticmethod
    def _pending_forward_capture_readiness(
        *,
        session: date,
        observed_at: datetime,
    ) -> dict[str, object]:
        return {
            "schema": QMT_FORWARD_CAPTURE_READINESS_SCHEMA,
            "required": None,
            "requirement_resolved": False,
            "trading_session_status": "UNRESOLVED",
            "trading_session_reason_code": "FORWARD_CAPTURE_VALIDATION_PENDING",
            "trading_session_evidence_proven": False,
            "trading_session_evidence": None,
            "ready": False,
            "status": "validating",
            "reason_code": "FORWARD_CAPTURE_VALIDATION_PENDING",
            "session": session.isoformat(),
            "observed_at": observed_at.isoformat(),
            "receipt_proven": False,
            "background_validation": True,
            "real_account_accessed": False,
            "real_order_transport_enabled": False,
            "live_status": "LIVE_DISABLED",
        }

    def _validate_forward_capture_readiness_in_background(
        self,
        *,
        session: date,
        cache_key: tuple[object, ...],
    ) -> None:
        try:
            observed_at = self._clock()
            if observed_at.tzinfo is None:
                raise ValueError("human review clock must be timezone-aware")
            local = observed_at.astimezone(ZoneInfo("Asia/Shanghai"))
            result = self.forward_archive_capture_readiness(
                session=session,
                _calendar_requirement=self._readiness_trading_session_requirement(
                    session=session,
                    observed_at=local,
                ),
            )
        except Exception as exc:
            observed_at = self._clock()
            if observed_at.tzinfo is None:
                observed_at = observed_at.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
            result = self._pending_forward_capture_readiness(
                session=session,
                observed_at=observed_at.astimezone(ZoneInfo("Asia/Shanghai")),
            )
            result.update(
                status="not_ready",
                reason_code="FORWARD_CAPTURE_VALIDATION_FAILED",
                error=f"{type(exc).__name__}: {str(exc)[:160]}",
            )
        completed_at = time.monotonic()
        with self._forward_capture_readiness_lock:
            if self._forward_capture_readiness_inflight_key == cache_key:
                self._forward_capture_readiness_cache_key = cache_key
                self._forward_capture_readiness_cache = dict(result)
                self._forward_capture_readiness_cache_at = completed_at
                self._forward_capture_readiness_inflight_key = None
                self._forward_capture_readiness_thread = None

    def forward_archive_capture_readiness_nonblocking(
        self,
        *,
        session: date | None,
    ) -> dict[str, object]:
        """返回精确缓存的采集证明，必要时只启动一个后台严格校验。"""

        observed_at = self._clock()
        if observed_at.tzinfo is None:
            raise ValueError("human review clock must be timezone-aware")
        local = observed_at.astimezone(ZoneInfo("Asia/Shanghai"))
        if session is None and (
            self._sector_capture_due is None or local.time() < self._sector_capture_due
        ):
            return self.forward_archive_capture_readiness(session=None)
        required_session = local.date() if session is None else session
        if isinstance(required_session, datetime) or not isinstance(
            required_session, date
        ):
            raise TypeError("session must be a date")
        cache_key = self._forward_capture_readiness_key(
            session=required_session,
            observed_at=local,
        )
        now = time.monotonic()
        with self._forward_capture_readiness_lock:
            cache_fresh = bool(
                self._forward_capture_readiness_cache_key == cache_key
                and self._forward_capture_readiness_cache is not None
                and self._forward_capture_readiness_cache_at is not None
                and now - self._forward_capture_readiness_cache_at
                <= _FORWARD_CAPTURE_READINESS_CACHE_SECONDS
            )
            if cache_fresh:
                return copy.deepcopy(self._forward_capture_readiness_cache)

            worker = self._forward_capture_readiness_thread
            if worker is None or not worker.is_alive():
                self._forward_capture_readiness_inflight_key = cache_key
                worker = threading.Thread(
                    target=self._validate_forward_capture_readiness_in_background,
                    kwargs={
                        "session": required_session,
                        "cache_key": cache_key,
                    },
                    name="forward-capture-readiness-validator",
                    daemon=True,
                )
                self._forward_capture_readiness_thread = worker
                worker.start()
        return self._pending_forward_capture_readiness(
            session=required_session,
            observed_at=local,
        )

    def _feedback_entries(self) -> tuple[dict[str, object], ...]:
        if not self.feedback_ledger.is_file():
            return ()
        try:
            ledger = load_human_review_feedback_ledger(self.feedback_ledger)
            observed_at = self._clock()
            if observed_at.tzinfo is None:
                raise ValueError("human review clock must be timezone-aware")
            entries = tuple(dict(value) for value in ledger.get("entries") or ())
            if any(
                datetime.fromisoformat(str(value["reviewed_at"])) > observed_at
                for value in entries
            ):
                raise ValueError("human review feedback ledger is future-dated")
        except ValueError as exc:
            raise HumanReviewScreenUnavailable(
                "human_review_feedback_ledger_invalid"
            ) from exc
        return entries

    def _paper_events(self) -> tuple[dict[str, object], ...]:
        try:
            ledger = load_human_paper_ledger(self.paper_ledger)
        except ValueError as exc:
            raise HumanReviewScreenUnavailable(
                "human_review_paper_ledger_invalid"
            ) from exc
        return tuple(dict(value) for value in ledger.get("events") or ())

    def virtual_holding_codes(self) -> tuple[str, ...]:
        """Return non-zero virtual holdings for the read-only scan monitor.

        ``load_human_paper_ledger`` authenticates the append-only ledger before
        the position reducer runs.  Propagating an invalid-ledger error is
        intentional: silently returning an empty tuple would drop exit
        monitoring precisely when position continuity can no longer be proven.
        This method has no exchange, account or order dependency.
        """

        positions = human_paper_position_quantities(self._paper_events())
        return tuple(sorted(positions))

    def _forward_events(self) -> tuple[dict[str, object], ...] | None:
        path = self.forward_root / "forward_paper_ledger.json"
        if not path.is_file():
            return None
        try:
            contract = load_forward_contract(self.parameter_snapshot)
            ledger = load_forward_paper_ledger(path, contract=contract)
        except (OSError, TypeError, ValueError):
            # 保持复核页面可用，但不能把缺失或无效的锚定账本伪装成已验证的空历史。
            return None
        return tuple(dict(value) for value in ledger.get("events") or ())

    def _display_path(self, path: Path) -> str:
        try:
            return path.relative_to(self.repository_root).as_posix()
        except ValueError:
            return path.name

    @staticmethod
    def _chart_urls(alert: HumanReviewAlert, source_sha256: str) -> dict[str, str]:
        common = {
            "market": "a",
            "code": alert.symbol,
            "layout": "single",
            "review_candidate_id": alert.candidate_id,
            "review_source_sha256": source_sha256,
            "review_as_of": int(alert.review_available_at.timestamp()),
        }
        return {
            label: "/?" + urlencode({**common, "intervals": interval})
            for label, interval in (("30m", "30"), ("5m", "5"), ("1m", "1"))
        }

    def snapshot(
        self,
        *,
        source: str = "latest",
        include_evidence: bool = True,
    ) -> dict[str, object]:
        kind, path, report, alerts = self._load_source_for_snapshot(
            source,
            include_evidence=include_evidence,
        )
        source_sha256 = str(report["content_sha256"])
        raw_input_hashes = report.get("input_hashes")
        input_hashes = raw_input_hashes if isinstance(raw_input_hashes, Mapping) else {}
        candidate_warmup_diagnostic = self._candidate_warmup_diagnostic(
            input_hashes.get("live_screening_snapshot")
        )
        raw_candidate_warmup_views = candidate_warmup_diagnostic.get("candidates")
        candidate_warmup_views = (
            raw_candidate_warmup_views
            if isinstance(raw_candidate_warmup_views, Mapping)
            else {}
        )
        (
            paper_observation_eligible,
            paper_observation_reason,
            paper_observation_source_session,
            paper_observation_current_market_session,
        ) = self._paper_observation_eligibility(
            kind=kind,
            source_sha256=source_sha256,
            source_report=report,
        )
        sector_catalogs, sector_captured_at, sector_warning = self._sector_catalogs()
        sector_receipt_audit = self._sector_receipt_audit()
        feedback = self._feedback_entries()
        paper_events = self._paper_events()
        paper_entry_boundary_attestation = (
            audit_human_paper_entry_boundary_attestations(paper_events)
        )
        paper_entry_boundary_source_audit = self._paper_entry_boundary_source_audit(
            paper_events,
            current_source_sha256=source_sha256,
            current_alerts=alerts,
        )
        paper_entry_selection_attestation = (
            audit_human_paper_entry_selection_attestations(
                paper_events,
                sector_catalog_entries=sector_catalogs,
            )
        )
        paper_entry_selection_source_audit = self._paper_entry_selection_source_audit(
            paper_events,
            current_source_sha256=source_sha256,
            current_alerts=alerts,
        )
        forward_event_source = self._forward_events()
        forward_events = () if forward_event_source is None else forward_event_source
        pending_continuity = latest_human_paper_pending_continuity(
            paper_events,
            forward_events,
        )
        paper_execution_evidence = audit_human_paper_execution_evidence(
            paper_events,
            forward_root=self.forward_root,
        )
        paper_execution_rejection_evidence = (
            audit_human_paper_execution_rejection_evidence(
                paper_events,
                forward_root=self.forward_root,
            )
        )
        paper_operations_cancellation_evidence = (
            audit_human_paper_operations_cancellation_evidence(
                paper_events,
                forward_root=self.forward_root,
            )
        )
        paper_portfolio_rejection_evidence = (
            audit_human_paper_portfolio_rejection_evidence(
                paper_events,
                forward_root=self.forward_root,
            )
        )
        accounting_parameters = None
        try:
            accounting_parameters = load_human_paper_accounting_parameters(
                self.parameter_snapshot
            )
            paper_portfolio_decision_audit = audit_human_paper_portfolio_decisions(
                paper_events,
                parameters=accounting_parameters,
            )
            paper_portfolio_fill_decision_audit = (
                audit_human_paper_portfolio_fill_decisions(
                    paper_events,
                    parameters=accounting_parameters,
                )
            )
        except (OSError, TypeError, ValueError) as exc:
            paper_portfolio_decision_audit = {
                "schema": "chanlun-human-paper-portfolio-decision-audit",
                "status": "PARAMETER_SNAPSHOT_INVALID",
                "rejection_count": None,
                "verified_rejection_count": 0,
                "invalid_decisions": [
                    {"reason": f"{type(exc).__name__}: {str(exc)[:200]}"}
                ],
                "broker_transport_available": False,
                "live_status": "LIVE_DISABLED",
            }
            paper_portfolio_fill_decision_audit = {
                "schema": ("chanlun-human-paper-portfolio-fill-decision-audit"),
                "status": "PARAMETER_SNAPSHOT_INVALID",
                "approved_fill_count": None,
                "verified_approved_fill_count": 0,
                "invalid_decisions": [
                    {"reason": f"{type(exc).__name__}: {str(exc)[:200]}"}
                ],
                "broker_transport_available": False,
                "live_status": "LIVE_DISABLED",
            }
        try:
            if accounting_parameters is None:
                raise ValueError("human paper accounting parameters are unavailable")
            paper_accounting = rebuild_human_paper_accounting(
                paper_events,
                parameters=load_human_paper_accounting_parameters(
                    self.parameter_snapshot
                ),
                execution_evidence_status=str(
                    paper_execution_evidence.get("status") or "INVALID"
                ),
            )
        except (OSError, TypeError, ValueError) as exc:
            paper_accounting = {
                "schema": "chanlun-human-paper-accounting",
                "status": "PARAMETER_SNAPSHOT_INVALID",
                "accounting_valid": False,
                "performance_evaluable": False,
                "fee_model_attached": False,
                "cash_ledger_attached": False,
                "cash_ledger_complete": False,
                "equity_curve_available": False,
                "reason_codes": [
                    "FROZEN_ACCOUNTING_PARAMETER_SNAPSHOT_INVALID",
                    f"{type(exc).__name__}: {str(exc)[:200]}",
                ],
                "tick_data_used": False,
                "broker_transport_available": False,
                "live_status": "LIVE_DISABLED",
            }
        valuation_source: dict[str, object] = {
            "forward_root": self.forward_root,
            # 这些事件通过冻结前向契约和哈希链校验器加载；成功执行的交易日可直接证明
            # 估值连续性，无需推断周末或交易所假日。
            "forward_events": forward_event_source,
        }
        if accounting_parameters is not None:
            valuation_source.update(
                paper_events=paper_events,
                accounting_parameters=accounting_parameters,
            )
        paper_valuation = audit_human_paper_valuation_evidence(**valuation_source)
        virtual_positions = human_paper_position_quantities(paper_events)
        virtual_reserved_sells = human_paper_pending_sell_quantities(paper_events)
        paper_candidate_by_intent = {
            str(event["payload"]["intent_id"]): str(event["payload"]["candidate_id"])
            for event in paper_events
            if event.get("kind") == "INTENT"
            and isinstance(event.get("payload"), Mapping)
        }
        paper_lifecycle_by_intent = {
            str(event["payload"]["intent_id"]): str(
                event["payload"]["signal_lifecycle_id"]
            )
            for event in paper_events
            if event.get("kind") == "INTENT"
            and isinstance(event.get("payload"), Mapping)
            and isinstance(event["payload"].get("signal_lifecycle_id"), str)
        }
        feedback_by_candidate: dict[str, list[dict[str, object]]] = {}
        feedback_by_lifecycle: dict[str, list[dict[str, object]]] = {}
        for row in feedback:
            if row.get("source_screen_content_sha256") == source_sha256:
                feedback_by_candidate.setdefault(str(row["candidate_id"]), []).append(
                    row
                )
            lifecycle = row.get("signal_lifecycle_id")
            if isinstance(lifecycle, str):
                feedback_by_lifecycle.setdefault(lifecycle, []).append(row)

        queue: list[dict[str, object]] = []
        for alert in alerts:
            compact_alert = isinstance(alert, _HumanReviewCandidateSummary)
            sector_presentation = _sector_name_presentation(
                alert,
                sector_catalogs,
            )
            (
                paper_entry_sector_eligible,
                paper_entry_sector_reason,
            ) = _paper_entry_sector_eligibility(alert, sector_presentation)
            candidate_paper_observation_eligible = (
                paper_observation_eligible and paper_entry_sector_eligible
            )
            candidate_paper_observation_reason = (
                paper_observation_reason
                if not paper_observation_eligible
                else paper_entry_sector_reason
            )
            candidate_paper_reconcilable = (
                paper_observation_eligible
                or paper_observation_reason in _PAPER_RECONCILIABLE_OPERATIONAL_REASONS
            ) and (
                paper_entry_sector_eligible
                or paper_entry_sector_reason in _PAPER_RECONCILIABLE_OPERATIONAL_REASONS
            )
            history_by_id = {
                str(row["feedback_id"]): row
                for row in (
                    *feedback_by_candidate.get(alert.candidate_id, ()),
                    *feedback_by_lifecycle.get(alert.signal_lifecycle_id, ()),
                )
            }
            history = sorted(
                history_by_id.values(),
                key=lambda row: (
                    str(row.get("reviewed_at") or ""),
                    str(row.get("feedback_id") or ""),
                ),
            )
            candidate_paper_events = [
                event
                for event in paper_events
                if isinstance(event.get("payload"), Mapping)
                and (
                    event["payload"].get("candidate_id") == alert.candidate_id
                    or paper_candidate_by_intent.get(
                        str(event["payload"].get("intent_id"))
                    )
                    == alert.candidate_id
                    or event["payload"].get("signal_lifecycle_id")
                    == alert.signal_lifecycle_id
                    or paper_lifecycle_by_intent.get(
                        str(event["payload"].get("intent_id"))
                    )
                    == alert.signal_lifecycle_id
                )
            ]
            latest_feedback = None if not history else history[-1]
            paper_feedback_ids = {
                str(event["payload"].get("feedback_id"))
                for event in candidate_paper_events
                if event.get("kind") == "INTENT"
                and isinstance(event.get("payload"), Mapping)
                and isinstance(event["payload"].get("feedback_id"), str)
            }
            # 运行闸门可能在安全创建虚拟意图前就接受并哈希复核。应暴露这一精确且可幂等
            # 重试的缺口，而不是在调度器或同交易日采集恢复后要求再次人工判断。
            paper_reconciliation_pending = bool(
                isinstance(latest_feedback, Mapping)
                and candidate_paper_reconcilable
                and latest_feedback.get("candidate_id") == alert.candidate_id
                and latest_feedback.get("source_screen_content_sha256") == source_sha256
                and latest_feedback.get("disposition") == "PAPER_OBSERVE"
                and isinstance(latest_feedback.get("request_id"), str)
                and bool(latest_feedback.get("request_id"))
                and str(latest_feedback.get("point_judgement") or "").startswith(
                    ("BUY_", "SELL_")
                )
                and latest_feedback.get("feedback_id") not in paper_feedback_ids
            )
            sector_horizontal_rank, sector_horizontal_strength = (
                _sector_presentation_rank(alert)
            )
            review_lane = _review_lane(
                alert,
                virtual_position_quantity=int(virtual_positions.get(alert.symbol, 0)),
                paper_reconciliation_pending=paper_reconciliation_pending,
            )
            queue.append(
                {
                    "candidate_id": alert.candidate_id,
                    "signal_lifecycle_id": alert.signal_lifecycle_id,
                    "symbol": alert.symbol,
                    "alert_type": alert.alert_type,
                    "signal_at": alert.signal_at.isoformat(),
                    "review_available_at": alert.review_available_at.isoformat(),
                    "review_as_of_unix": int(alert.review_available_at.timestamp()),
                    "sector_id": alert.sector_id,
                    **sector_presentation,
                    "paper_observation_source_eligible": (paper_observation_eligible),
                    "paper_observation_source_reason": (paper_observation_reason),
                    "paper_entry_sector_eligible": paper_entry_sector_eligible,
                    "paper_entry_sector_reason": paper_entry_sector_reason,
                    "paper_observation_eligible": (
                        candidate_paper_observation_eligible
                    ),
                    "paper_observation_reason": (candidate_paper_observation_reason),
                    "confidence": alert.confidence,
                    "review_priority": alert.review_priority,
                    # 仅展示字段，既不修改源提醒，也不授权交易。
                    "review_lane": review_lane,
                    "sector_horizontal_rank": sector_horizontal_rank,
                    "sector_horizontal_strength": (sector_horizontal_strength),
                    "reference_price": (
                        None
                        if alert.reference_price is None
                        else format(alert.reference_price, "f")
                    ),
                    "structural_invalidation_price": (
                        None
                        if alert.structural_invalidation_price is None
                        else format(alert.structural_invalidation_price, "f")
                    ),
                    "entry_confirmation_bar_closed_at": (
                        None
                        if alert.entry_confirmation_bar_closed_at is None
                        else alert.entry_confirmation_bar_closed_at.isoformat()
                    ),
                    "entry_price_cap": (
                        None
                        if alert.entry_price_cap is None
                        else format(alert.entry_price_cap, "f")
                    ),
                    "entry_valid_until": (
                        None
                        if alert.entry_valid_until is None
                        else alert.entry_valid_until.isoformat()
                    ),
                    "entry_boundary_evidence_id": (alert.entry_boundary_evidence_id),
                    "entry_boundary_attestation": (
                        "SELF_CONTAINED_RAW_1M_OHLCV"
                        if alert.entry_execution_boundary is not None
                        else (
                            "MISSING_CURRENT_BOUNDARY_EVIDENCE"
                            if alert.entry_boundary_evidence_id is not None
                            else "NOT_AVAILABLE"
                        )
                    ),
                    "market_risk_gate": alert.market_risk_gate,
                    "sector_risk_gate": alert.sector_risk_gate,
                    "symbol_risk_gate": alert.symbol_risk_gate,
                    "sector_higher_timeframe_evidence": (
                        None
                        if not include_evidence
                        or alert.sector_higher_timeframe_evidence is None
                        else alert.sector_higher_timeframe_evidence.document()
                    ),
                    "sector_higher_timeframe_evidence_id": (
                        alert.sector_higher_timeframe_evidence_id
                        if compact_alert
                        else (
                            None
                            if alert.sector_higher_timeframe_evidence is None
                            else alert.sector_higher_timeframe_evidence.evidence_id
                        )
                    ),
                    "market_symbol_higher_timeframe_evidence": (
                        None
                        if not include_evidence
                        or alert.market_symbol_higher_timeframe_evidence is None
                        else (alert.market_symbol_higher_timeframe_evidence.document())
                    ),
                    "market_symbol_higher_timeframe_evidence_id": (
                        alert.market_symbol_higher_timeframe_evidence_id
                        if compact_alert
                        else (
                            None
                            if alert.market_symbol_higher_timeframe_evidence is None
                            else (
                                alert.market_symbol_higher_timeframe_evidence.evidence_id
                            )
                        )
                    ),
                    "market_symbol_higher_timeframe_source_attestation": (
                        _market_symbol_source_attestation(alert)
                    ),
                    "sector_ranking_evidence": (
                        None
                        if not include_evidence or alert.sector_ranking_evidence is None
                        else alert.sector_ranking_evidence.document()
                    ),
                    "sector_ranking_evidence_id": (
                        alert.sector_ranking_evidence_id
                        if compact_alert
                        else (
                            None
                            if alert.sector_ranking_evidence is None
                            else alert.sector_ranking_evidence.evidence_id
                        )
                    ),
                    "evidence_detail_available": (
                        alert.evidence_detail_available
                        if compact_alert
                        else any(
                            value is not None
                            for value in (
                                alert.sector_higher_timeframe_evidence,
                                alert.market_symbol_higher_timeframe_evidence,
                                alert.sector_ranking_evidence,
                            )
                        )
                    ),
                    "sector_ranking_attestation": (_sector_ranking_attestation(alert)),
                    "warning_codes": list(alert.warning_codes),
                    "source_fact_ids": list(alert.source_fact_ids),
                    "review_checklist": list(alert.review_checklist),
                    # 有界多前缀证据只用于展示并绑定到当前屏幕哈希；它有意不进入提醒/候选
                    # 身份和队列排序。
                    "deep_warmup_diagnostic": candidate_warmup_views.get(alert.symbol),
                    "status": "REVIEW_REQUIRED",
                    "live_status": "LIVE_DISABLED",
                    "automated_action_authorized": False,
                    "chart_urls": self._chart_urls(alert, source_sha256),
                    "feedback_history": history,
                    "latest_feedback": latest_feedback,
                    "paper_events": candidate_paper_events,
                    "paper_reconciliation_pending": (paper_reconciliation_pending),
                    "paper_reconciliation_eligible": (
                        paper_reconciliation_pending
                        and candidate_paper_observation_eligible
                    ),
                }
            )

        queue.sort(
            key=lambda value: (
                _REVIEW_LANE_ORDER[str(value["review_lane"])],
                (
                    int(value["sector_horizontal_rank"])
                    if value["sector_horizontal_rank"] is not None
                    else 10**9
                ),
                -int(value["review_priority"]),
                -int(
                    datetime.fromisoformat(
                        str(value["review_available_at"])
                    ).timestamp()
                ),
                str(value["symbol"]),
                str(value["candidate_id"]),
            )
        )
        review_lane_counts = Counter(str(value["review_lane"]) for value in queue)

        alert_counts = Counter(value.alert_type for value in alerts)
        confidence_counts = Counter(value.confidence for value in alerts)
        event_study = report.get("event_study")
        event_summary = (
            event_study.get("summary")
            if isinstance(event_study, Mapping)
            and isinstance(event_study.get("summary"), Mapping)
            else {}
        )
        source_options = ["historical"] if self._historical_reports() else []
        if self._forward_reports():
            source_options.insert(0, "forward")
        if self._live_reports():
            source_options.insert(0, "live")
        if kind == "historical":
            source_currentness = {
                "status": "CURRENT_RELEASE_SIDECAR",
                "source_session": None,
                "current_market_session": None,
                "reason_code": None,
            }
        elif (
            paper_observation_source_session is not None
            and paper_observation_current_market_session is not None
        ):
            source_is_current = (
                paper_observation_source_session
                == paper_observation_current_market_session
            )
            source_currentness = {
                "status": "CURRENT" if source_is_current else "STALE",
                "source_session": paper_observation_source_session,
                "current_market_session": (paper_observation_current_market_session),
                "reason_code": (
                    None
                    if source_is_current
                    else "SOURCE_MARKET_SESSION_NOT_CURRENT_FOR_PAPER"
                ),
            }
        else:
            source_currentness = {
                "status": "UNPROVEN",
                "source_session": paper_observation_source_session,
                "current_market_session": (paper_observation_current_market_session),
                "reason_code": (
                    paper_observation_reason or "CURRENT_MARKET_SESSION_UNAVAILABLE"
                ),
            }
        warnings = [sector_warning] if sector_warning else []
        receipt_status = str(sector_receipt_audit.get("status") or "UNAVAILABLE")
        if receipt_status != "COMPLETE":
            warnings.append(f"QMT_SECTOR_RECEIPTS_{receipt_status}")
        execution_evidence_status = str(
            paper_execution_evidence.get("status") or "INVALID"
        )
        if execution_evidence_status not in {"COMPLETE", "NO_FILLS"}:
            warnings.append(
                f"HUMAN_PAPER_EXECUTION_EVIDENCE_{execution_evidence_status}"
            )
        entry_attestation_status = str(
            paper_entry_boundary_attestation.get("status") or "INVALID"
        )
        if entry_attestation_status not in {
            "COMPLETE",
            "NO_BOUNDARY_INTENTS",
        }:
            warnings.append(
                f"HUMAN_PAPER_ENTRY_BOUNDARY_ATTESTATION_{entry_attestation_status}"
            )
        entry_source_status = str(
            paper_entry_boundary_source_audit.get("status") or "INVALID"
        )
        if entry_source_status not in {
            "COMPLETE",
            "NO_BOUNDARY_INTENTS",
        }:
            warnings.append(f"HUMAN_PAPER_ENTRY_BOUNDARY_SOURCE_{entry_source_status}")
        selection_attestation_status = str(
            paper_entry_selection_attestation.get("status") or "INVALID"
        )
        if selection_attestation_status not in {
            "COMPLETE",
            "NO_SELECTION_ATTESTATIONS",
        }:
            warnings.append(
                "HUMAN_PAPER_ENTRY_SELECTION_ATTESTATION_"
                f"{selection_attestation_status}"
            )
        selection_source_status = str(
            paper_entry_selection_source_audit.get("status") or "INVALID"
        )
        if selection_source_status not in {
            "COMPLETE",
            "NO_REQUIRED_SELECTION_INTENTS",
        }:
            warnings.append(
                f"HUMAN_PAPER_ENTRY_SELECTION_SOURCE_{selection_source_status}"
            )
        execution_rejection_evidence_status = str(
            paper_execution_rejection_evidence.get("status") or "INVALID"
        )
        if execution_rejection_evidence_status not in {
            "COMPLETE",
            "NO_REJECTIONS",
        }:
            warnings.append(
                "HUMAN_PAPER_EXECUTION_REJECTION_EVIDENCE_"
                f"{execution_rejection_evidence_status}"
            )
        operations_cancellation_evidence_status = str(
            paper_operations_cancellation_evidence.get("status") or "INVALID"
        )
        if operations_cancellation_evidence_status not in {
            "COMPLETE",
            "NO_CANCELLATIONS",
        }:
            warnings.append(
                "HUMAN_PAPER_OPERATIONS_CANCELLATION_EVIDENCE_"
                f"{operations_cancellation_evidence_status}"
            )
        portfolio_rejection_evidence_status = str(
            paper_portfolio_rejection_evidence.get("status") or "INVALID"
        )
        if portfolio_rejection_evidence_status not in {
            "COMPLETE",
            "NO_REJECTIONS",
        }:
            warnings.append(
                "HUMAN_PAPER_PORTFOLIO_REJECTION_EVIDENCE_"
                f"{portfolio_rejection_evidence_status}"
            )
        portfolio_decision_audit_status = str(
            paper_portfolio_decision_audit.get("status") or "INVALID"
        )
        if portfolio_decision_audit_status not in {
            "COMPLETE",
            "NO_REJECTIONS",
        }:
            warnings.append(
                f"HUMAN_PAPER_PORTFOLIO_DECISION_{portfolio_decision_audit_status}"
            )
        portfolio_fill_decision_audit_status = str(
            paper_portfolio_fill_decision_audit.get("status") or "INVALID"
        )
        if portfolio_fill_decision_audit_status not in {
            "COMPLETE",
            "NO_APPROVED_FILLS",
        }:
            warnings.append(
                "HUMAN_PAPER_PORTFOLIO_FILL_DECISION_"
                f"{portfolio_fill_decision_audit_status}"
            )
        accounting_status = str(paper_accounting.get("status") or "INVALID")
        if accounting_status in {
            "PARAMETER_SNAPSHOT_INVALID",
            "EXECUTION_EVIDENCE_UNVERIFIED",
            "CONSTRAINT_VIOLATION",
        }:
            warnings.append(f"HUMAN_PAPER_ACCOUNTING_{accounting_status}")
        continuity_status = str(pending_continuity.get("status") or "UNPROVEN")
        if continuity_status not in {"COMPLETE", "NO_PENDING_INTENTS"}:
            warnings.append(f"HUMAN_PAPER_PENDING_CONTINUITY_{continuity_status}")
        valuation_status = str(paper_valuation.get("status") or "INVALID")
        if valuation_status in {
            "INVALID",
            "SOURCE_UNVERIFIED",
            "CONTINUITY_UNVERIFIED",
            "INCOMPLETE_CURVE",
        } or (
            int(paper_accounting.get("fill_count") or 0) > 0
            and valuation_status != "COMPLETE"
        ):
            warnings.append(f"HUMAN_PAPER_VALUATION_{valuation_status}")
        virtual_intent_count = sum(
            event.get("kind") == "INTENT" for event in paper_events
        )
        virtual_fill_count = sum(event.get("kind") == "FILL" for event in paper_events)
        cancelled_intent_ids = human_paper_cancelled_intent_ids(paper_events)
        operations_cancelled_intent_count = sum(
            event.get("kind") == "OPERATIONS_CANCEL" for event in paper_events
        )
        portfolio_rejected_intent_ids = human_paper_portfolio_rejected_intent_ids(
            paper_events
        )
        terminal_intent_ids = human_paper_terminal_intent_ids(paper_events)
        paper_intents = [
            event["payload"]
            for event in paper_events
            if event.get("kind") == "INTENT"
            and isinstance(event.get("payload"), Mapping)
        ]
        virtual_pending_intent_count = sum(
            intent.get("status") == "PENDING"
            and str(intent.get("intent_id")) not in terminal_intent_ids
            for intent in paper_intents
        )
        virtual_blocked_intent_count = sum(
            intent.get("status") == "BLOCKED_BY_RISK_GATE" for intent in paper_intents
        )
        virtual_observation_only_intent_count = sum(
            intent.get("status") == "OBSERVATION_ONLY" for intent in paper_intents
        )
        try:
            forward_markout = self._forward_markout()
        except HumanReviewScreenUnavailable as exc:
            forward_markout = {
                "status": "INVALID",
                "diagnostic_only": True,
                "portfolio_performance_evaluable": False,
                "summary": {},
                "sample": {},
                "source_provenance_status": "INVALID",
                "reason_codes": [exc.code],
            }
            warnings.append(exc.code)
        try:
            forward_warmup_structure_lineage = self._forward_warmup_structure_lineage()
        except HumanReviewScreenUnavailable as exc:
            forward_warmup_structure_lineage = {
                "status": "INVALID",
                "qualified_session_count": 0,
                "recorded_session_count": 0,
                "structure_event_count": 0,
                "subjects": {},
                "sessions": [],
                "diagnostic_only": True,
                "parameters_changed": False,
                "live_status": "LIVE_DISABLED",
                "reason_codes": [exc.code],
            }
            warnings.append(exc.code)
        return {
            "schema": WEB_SCHEMA,
            "source_kind": kind,
            "source_path": self._display_path(path),
            "source_currentness": source_currentness,
            "source_content_sha256": source_sha256,
            "decision_core_id": input_hashes.get("decision_core_id"),
            "decision_source_snapshot_id": input_hashes.get(
                "decision_source_snapshot_id"
            ),
            "source_options": source_options,
            "paper_observation_eligible": paper_observation_eligible,
            "paper_observation_reason": paper_observation_reason,
            "paper_observation_source_session": (paper_observation_source_session),
            "paper_observation_current_market_session": (
                paper_observation_current_market_session
            ),
            "sample": report.get("sample") or {},
            "scope": report.get("scope") or {},
            "candidate_funnel": report.get("candidate_funnel") or {},
            "signal_counts": report.get("signal_counts") or {},
            "event_study_summary": event_summary,
            "forward_markout": forward_markout,
            "forward_warmup_structure_lineage": (forward_warmup_structure_lineage),
            "candidate_warmup_diagnostic": candidate_warmup_diagnostic,
            "data_caveats": list(report.get("data_caveats") or ()),
            "division_of_responsibility": report.get("division_of_responsibility")
            or {},
            "sector_catalog_captured_at": sector_captured_at,
            "sector_capture_receipts": sector_receipt_audit,
            "alert_counts": dict(alert_counts),
            "confidence_counts": dict(confidence_counts),
            "review_queue": queue,
            "review_queue_count": len(queue),
            "review_lane_counts": dict(review_lane_counts),
            "review_presentation_contract": {
                "default_focus_lanes": [
                    "POSITION_MANAGEMENT",
                    "ACTIONABLE_REVIEW",
                ],
                **(
                    {
                        "initial_candidate_payload": "COMPACT_SUMMARY",
                        "candidate_evidence_loaded_on_demand": True,
                    }
                    if not include_evidence
                    else {}
                ),
                "sector_horizontal_rank_used_for_display_order": True,
                "source_review_priority_unchanged": True,
                "candidate_identity_unchanged": True,
                "trade_authorization_changed": False,
                "live_status": "LIVE_DISABLED",
            },
            "reviewed_candidate_count": sum(
                bool(row["feedback_history"]) for row in queue
            ),
            "feedback_entry_count": sum(len(row["feedback_history"]) for row in queue),
            "virtual_intent_count": virtual_intent_count,
            "virtual_fill_count": virtual_fill_count,
            "virtual_cancelled_intent_count": len(cancelled_intent_ids),
            "virtual_operations_cancelled_intent_count": (
                operations_cancelled_intent_count
            ),
            "virtual_portfolio_rejected_intent_count": len(
                portfolio_rejected_intent_ids
            ),
            "virtual_pending_intent_count": virtual_pending_intent_count,
            "virtual_blocked_intent_count": virtual_blocked_intent_count,
            "virtual_observation_only_intent_count": (
                virtual_observation_only_intent_count
            ),
            "virtual_open_position_count": len(virtual_positions),
            "virtual_open_positions": virtual_positions,
            "virtual_reserved_sell_quantities": virtual_reserved_sells,
            "virtual_reserved_sell_quantity": sum(virtual_reserved_sells.values()),
            "paper_execution_evidence": paper_execution_evidence,
            "paper_entry_boundary_attestation": (paper_entry_boundary_attestation),
            "paper_entry_boundary_source_audit": (paper_entry_boundary_source_audit),
            "paper_entry_selection_attestation": (paper_entry_selection_attestation),
            "paper_entry_selection_source_audit": (paper_entry_selection_source_audit),
            "paper_execution_rejection_evidence": (paper_execution_rejection_evidence),
            "paper_operations_cancellation_evidence": (
                paper_operations_cancellation_evidence
            ),
            "paper_portfolio_rejection_evidence": (paper_portfolio_rejection_evidence),
            "paper_portfolio_decision_audit": paper_portfolio_decision_audit,
            "paper_portfolio_fill_decision_audit": (
                paper_portfolio_fill_decision_audit
            ),
            "paper_pending_continuity": pending_continuity,
            "portfolio_backtest_performed": False,
            "portfolio_performance_evaluable": False,
            "paper_accounting": paper_accounting,
            "paper_valuation": paper_valuation,
            "paper_execution_capabilities": {
                "fill_source": STRICT_BAR_PRICE_RULE,
                "fill_timestamp_rule": STRICT_BAR_EXECUTION_TIMESTAMP_RULE,
                "tick_data_used": False,
                "t_plus_one_sell_enforced": True,
                "pending_sell_quantity_reserved": True,
                "prior_session_continuity_enforced": True,
                "pending_intent_cancellation_supported": True,
                "pending_intent_expiry_supported": True,
                "optional_buy_intent_expiry_supported": True,
                "persistent_strategic_sell_never_expires": True,
                "later_feedback_mutates_existing_intent": False,
                "later_feedback_supersedes_pending_intent": True,
                "fee_model_attached": bool(paper_accounting.get("fee_model_attached")),
                "cash_accounting_attached": bool(
                    paper_accounting.get("cash_ledger_attached")
                ),
                "cash_and_slot_pretrade_enforced": True,
                "portfolio_rejection_exact_1m_evidence_audited": True,
                "portfolio_rejection_ledger_prefix_recomputed": True,
                "slot_fraction_notional_gate_evaluable": True,
                "account_exposure_notional_gate_evaluable": True,
                "synchronous_open_position_one_minute_marks_required": True,
                "unresolved_position_marks_block_new_buys": True,
                "portfolio_approved_fill_ledger_prefix_recomputed": True,
                "one_security_one_strategic_slot_enforced": True,
                "terminal_signal_lifecycle_one_shot_enforced": True,
                "fixed_one_lot_tactical_review_only": True,
                "fixed_one_lot_diagnostic": True,
                "human_trend_type_confirmation_required": True,
                "warmup_divergence_blocks_strategic_virtual_buy": True,
                "warmup_divergence_never_blocks_existing_virtual_exit": True,
                "strategic_buy_confirmation_bar_price_cap_enforced": True,
                "strategic_buy_no_chase_reject_independent_of_volume": True,
                "strategic_buy_entire_bar_strict_cross_enforced": True,
                "strategic_buy_five_percent_bar_volume_cap_enforced": True,
                "persistent_sell_five_percent_bar_volume_cap_enforced": True,
                "adverse_observed_bar_extreme_fill_price_enforced": True,
                "completed_bar_close_fill_timestamp_enforced": True,
                "strategic_buy_one_locator_bar_ttl_enforced": True,
                "strategic_buy_causal_full_1m_window_prechecked": True,
                "full_session_240_bar_grid_required": True,
                "opening_auction_event_merged_into_0931": True,
                "optional_buy_data_fault_cancelled": True,
                "optional_buy_security_gate_cancelled": True,
                "execution_fact_incomplete_optional_buy_cancelled": True,
                "operations_cancellation_exact_evidence_audited": True,
                "persistent_exit_independent_symbol_continues": True,
                "persistent_exit_security_blocked_remains_pending": True,
                "persistent_exit_fact_incomplete_remains_pending": True,
                "fill_and_rejection_full_session_grid_audited": (
                    paper_execution_evidence.get("status") in {"COMPLETE", "NO_FILLS"}
                    and paper_execution_rejection_evidence.get("status")
                    in {"COMPLETE", "NO_REJECTIONS"}
                    and paper_portfolio_rejection_evidence.get("status")
                    in {"COMPLETE", "NO_REJECTIONS"}
                    and paper_operations_cancellation_evidence.get("status")
                    in {"COMPLETE", "NO_CANCELLATIONS"}
                ),
                "pending_continuity_requires_gap_free_240_bar_grid": True,
                "current_pending_continuity_proven": (
                    pending_continuity.get("status")
                    in {"COMPLETE", "NO_PENDING_INTENTS"}
                ),
                "current_review_queue_raw_1m_boundaries_self_contained": all(
                    value.entry_boundary_evidence_id is None
                    or value.entry_execution_boundary is not None
                    for value in alerts
                ),
                "raw_1m_entry_boundary_self_contained": (
                    paper_entry_boundary_attestation.get("status")
                    in {"COMPLETE", "NO_BOUNDARY_INTENTS"}
                ),
                "raw_1m_entry_boundary_source_resolved": (
                    paper_entry_boundary_source_audit.get("status")
                    in {"COMPLETE", "NO_BOUNDARY_INTENTS"}
                ),
                "live_ranked_entry_exact_qmt_catalog_attested": (
                    paper_entry_selection_attestation.get("status")
                    in {"COMPLETE", "NO_SELECTION_ATTESTATIONS"}
                    and paper_entry_selection_source_audit.get("status")
                    in {"COMPLETE", "NO_REQUIRED_SELECTION_INTENTS"}
                ),
                "structure_anchor_never_used_as_execution_cap": True,
                "execution_rejection_exact_1m_evidence_audited": True,
                "cash_and_equity_accounting_attached": bool(
                    paper_accounting.get("cash_ledger_attached")
                    and paper_valuation.get("status") == "COMPLETE"
                    and paper_valuation.get("equity_curve_available") is True
                ),
                "daily_valuation_supported": True,
                "daily_valuation_attached": bool(
                    paper_valuation.get("status") == "COMPLETE"
                    and paper_valuation.get("equity_curve_available") is True
                ),
                "exact_one_minute_bar_evidence_attached": True,
                "immutable_execution_evidence_objects": True,
                "contract_change_required_for_new_execution_semantics": True,
            },
            "paper_contract_id": load_human_paper_ledger(self.paper_ledger)[
                "paper_contract_id"
            ],
            "warnings": warnings,
            "highest_status": "REVIEW_REQUIRED",
            "human_confirmation_required": True,
            "automated_order_authorized": False,
            "orders_created": 0,
            "fills_created": 0,
            "live_status": "LIVE_DISABLED",
        }

    def forward_delivery_readiness(
        self,
        *,
        session: date | None,
        _calendar_requirement: tuple[dict[str, object], str | None] | None = None,
    ) -> dict[str, object]:
        """Prove actual daily Capture/Evaluate delivery from the ledger.

        This is deliberately separate from
        :meth:`forward_archive_capture_readiness`: a usable sector capture is
        only an input gate, while an ``EVALUATED`` event proves that the
        forward day was really archived.
        """

        observed_at = self._clock()
        if observed_at.tzinfo is None:
            raise ValueError("human review clock must be timezone-aware")
        local = observed_at.astimezone(ZoneInfo("Asia/Shanghai"))
        required_session = local.date() if session is None else session
        if isinstance(required_session, datetime) or not isinstance(
            required_session, date
        ):
            raise TypeError("session must be a date")
        requirement, trading_session_provider_error = (
            self._trading_session_requirement(
                session=required_session,
                observed_at=local,
            )
            if _calendar_requirement is None
            else _calendar_requirement
        )
        trading_session_evidence = requirement["trading_session_evidence"]
        ledger_path = self.forward_root / "forward_paper_ledger.json"
        try:
            contract = load_forward_contract(self.parameter_snapshot)
            ledger = (
                load_forward_paper_ledger(ledger_path, contract=contract)
                if ledger_path.is_file()
                else {"events": ()}
            )
            capture_readiness = self.forward_archive_capture_readiness(
                session=required_session,
                _calendar_requirement=(
                    requirement,
                    trading_session_provider_error,
                ),
            )
            result = audit_forward_paper_session_delivery(
                tuple(ledger["events"]),
                session=required_session,
                observed_at=local,
                sector_capture_readiness=capture_readiness,
                trading_session_evidence=trading_session_evidence,
                forward_root=self.forward_root,
            )
            try:
                implementation_continuity = audit_forward_implementation_continuity(
                    tuple(ledger["events"]),
                    session=required_session,
                    current_implementation_provenance=(
                        self._forward_implementation_provenance_provider()
                    ),
                )
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                implementation_continuity = {
                    "schema": FORWARD_IMPLEMENTATION_CONTINUITY_SCHEMA,
                    "session": required_session.isoformat(),
                    "ready": False,
                    "status": "unresolved",
                    "reason_code": ("CURRENT_IMPLEMENTATION_PROVENANCE_UNAVAILABLE"),
                    "capture_event_present": result.get("capture_event_present", False),
                    "evaluation_event_present": result.get(
                        "evaluation_event_present", False
                    ),
                    "market_data_read_authorized": False,
                    "error": f"{type(exc).__name__}: {str(exc)[:160]}",
                    "real_account_accessed": False,
                    "real_order_transport_enabled": False,
                    "paper_status": "REVIEW_REQUIRED",
                    "live_status": "LIVE_DISABLED",
                }
            result["implementation_continuity_preflight"] = implementation_continuity
            result["implementation_continuity_required_before_evaluation"] = True
            if (
                result.get("required") is True
                and result.get("capture_event_present") is True
                and result.get("evaluation_event_present") is not True
                and implementation_continuity.get("ready") is not True
            ):
                result["ready"] = False
                result["status"] = "not_ready"
                result["reason_code"] = implementation_continuity["reason_code"]
            if trading_session_provider_error is not None:
                result["trading_session_provider_error"] = (
                    trading_session_provider_error
                )
            return result
        except (OSError, TypeError, ValueError) as exc:
            return {
                "schema": FORWARD_PAPER_SESSION_DELIVERY_SCHEMA,
                "required": requirement["required"],
                "requirement_resolved": requirement["requirement_resolved"],
                "trading_session_status": requirement["trading_session_status"],
                "trading_session_reason_code": requirement[
                    "trading_session_reason_code"
                ],
                "trading_session_evidence_proven": requirement[
                    "trading_session_evidence_proven"
                ],
                "trading_session_evidence": requirement["trading_session_evidence"],
                "ready": False,
                "status": "not_ready",
                "reason_code": "FORWARD_LEDGER_INVALID",
                "session": required_session.isoformat(),
                "observed_at": local.isoformat(),
                "session_event_count": 0,
                "capture_event_present": False,
                "data_ready_event_present": False,
                "evaluation_event_present": False,
                "capture_ready": False,
                "evaluation_ready": False,
                "capture_evidence_proven": False,
                "data_ready_evidence_proven": False,
                "evaluation_artifacts_proven": False,
                "error": f"{type(exc).__name__}: {str(exc)[:160]}",
                "trading_session_provider_error": (trading_session_provider_error),
                "real_account_accessed": False,
                "real_order_transport_enabled": False,
                "paper_status": "REVIEW_REQUIRED",
                "live_status": "LIVE_DISABLED",
            }

    @staticmethod
    def _readiness_file_identity(path: Path) -> tuple[str, bool, int, int]:
        """Return a cheap identity that invalidates an async readiness cache."""

        try:
            stat = path.stat()
        except FileNotFoundError:
            return str(path), False, 0, 0
        except OSError:
            # 路径暂时不可读时不能复用先前就绪结果；变化哨兵会强制重新执行关闭失败校验。
            return str(path), False, -1, -1
        return str(path), True, int(stat.st_size), int(stat.st_mtime_ns)

    def _forward_delivery_readiness_key(
        self,
        *,
        session: date | None,
        observed_at: datetime,
    ) -> tuple[object, ...]:
        local = observed_at.astimezone(ZoneInfo("Asia/Shanghai"))
        required_session = local.date() if session is None else session
        if isinstance(required_session, datetime) or not isinstance(
            required_session, date
        ):
            raise TypeError("session must be a date")
        if required_session != local.date():
            delivery_phase = "OTHER_SESSION"
        elif local.time() < datetime_time(9, 10):
            delivery_phase = "BEFORE_CAPTURE"
        elif local.time() < datetime_time(15, 20):
            delivery_phase = "CAPTURE_WINDOW"
        elif local.time() < datetime_time(23, 0):
            delivery_phase = "EVALUATION_WINDOW"
        else:
            delivery_phase = "AFTER_EVALUATION_DEADLINE"
        return (
            required_session.isoformat(),
            local.date().isoformat(),
            delivery_phase,
            self._readiness_file_identity(
                self.forward_root / "forward_paper_ledger.json"
            ),
            self._readiness_file_identity(self.parameter_snapshot),
            self._readiness_file_identity(self.sector_ledger),
        )

    @staticmethod
    def _pending_forward_delivery_readiness(
        *,
        session: date,
        observed_at: datetime,
    ) -> dict[str, object]:
        return {
            "schema": FORWARD_PAPER_SESSION_DELIVERY_SCHEMA,
            "required": None,
            "requirement_resolved": False,
            "trading_session_status": "UNRESOLVED",
            "trading_session_reason_code": ("FORWARD_DELIVERY_VALIDATION_PENDING"),
            "trading_session_evidence_proven": False,
            "trading_session_evidence": None,
            "ready": False,
            "status": "validating",
            "reason_code": "FORWARD_DELIVERY_VALIDATION_PENDING",
            "session": session.isoformat(),
            "observed_at": observed_at.isoformat(),
            "session_event_count": 0,
            "capture_event_present": False,
            "data_ready_event_present": False,
            "evaluation_event_present": False,
            "capture_ready": False,
            "evaluation_ready": False,
            "capture_evidence_proven": False,
            "data_ready_evidence_proven": False,
            "evaluation_artifacts_proven": False,
            "background_validation": True,
            "real_account_accessed": False,
            "real_order_transport_enabled": False,
            "automated_order_authorized": False,
            "paper_status": "REVIEW_REQUIRED",
            "live_status": "LIVE_DISABLED",
        }

    def _validate_forward_delivery_readiness_in_background(
        self,
        *,
        session: date,
        cache_key: tuple[object, ...],
    ) -> None:
        try:
            observed_at = self._clock()
            if observed_at.tzinfo is None:
                raise ValueError("human review clock must be timezone-aware")
            local = observed_at.astimezone(ZoneInfo("Asia/Shanghai"))
            result = self.forward_delivery_readiness(
                session=session,
                _calendar_requirement=self._readiness_trading_session_requirement(
                    session=session,
                    observed_at=local,
                ),
            )
        except Exception as exc:  # 就绪状态必须始终可观测，绝不能挂起。
            observed_at = self._clock()
            if observed_at.tzinfo is None:
                observed_at = observed_at.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
            result = self._pending_forward_delivery_readiness(
                session=session,
                observed_at=observed_at.astimezone(ZoneInfo("Asia/Shanghai")),
            )
            result.update(
                status="not_ready",
                reason_code="FORWARD_DELIVERY_VALIDATION_FAILED",
                error=f"{type(exc).__name__}: {str(exc)[:160]}",
            )
        completed_at = time.monotonic()
        with self._forward_delivery_readiness_lock:
            if self._forward_delivery_readiness_inflight_key == cache_key:
                self._forward_delivery_readiness_cache_key = cache_key
                self._forward_delivery_readiness_cache = dict(result)
                self._forward_delivery_readiness_cache_at = completed_at
                self._forward_delivery_readiness_inflight_key = None
                self._forward_delivery_readiness_thread = None

    def forward_delivery_readiness_nonblocking(
        self,
        *,
        session: date | None,
    ) -> dict[str, object]:
        """Return an exact cached audit or start one background validation.

        This is the app-health adapter only.  It never substitutes a stale or
        partial proof for a current one: a changed ledger/parameter/sector
        identity, a deadline transition, or an expired cache returns the
        explicit fail-closed ``VALIDATION_PENDING`` state while exactly one
        worker performs the normal strict audit.
        """

        observed_at = self._clock()
        if observed_at.tzinfo is None:
            raise ValueError("human review clock must be timezone-aware")
        local = observed_at.astimezone(ZoneInfo("Asia/Shanghai"))
        required_session = local.date() if session is None else session
        cache_key = self._forward_delivery_readiness_key(
            session=required_session,
            observed_at=local,
        )
        now = time.monotonic()
        with self._forward_delivery_readiness_lock:
            cache_fresh = bool(
                self._forward_delivery_readiness_cache_key == cache_key
                and self._forward_delivery_readiness_cache is not None
                and self._forward_delivery_readiness_cache_at is not None
                and now - self._forward_delivery_readiness_cache_at
                <= _FORWARD_DELIVERY_READINESS_CACHE_SECONDS
            )
            if cache_fresh:
                return copy.deepcopy(self._forward_delivery_readiness_cache)

            worker = self._forward_delivery_readiness_thread
            if worker is None or not worker.is_alive():
                self._forward_delivery_readiness_inflight_key = cache_key
                worker = threading.Thread(
                    target=(self._validate_forward_delivery_readiness_in_background),
                    kwargs={
                        "session": required_session,
                        "cache_key": cache_key,
                    },
                    name="forward-delivery-readiness-validator",
                    daemon=True,
                )
                self._forward_delivery_readiness_thread = worker
                worker.start()
        return self._pending_forward_delivery_readiness(
            session=required_session,
            observed_at=local,
        )

    def validate_chart_lock(
        self,
        *,
        candidate_id: str,
        source_sha256: str,
        review_as_of: int,
    ) -> dict[str, object]:
        _kind, _path, _report, alert = self._load_candidate_by_hash(
            source_sha256=source_sha256,
            candidate_id=candidate_id,
        )
        expected = int(alert.review_available_at.timestamp())
        if isinstance(review_as_of, bool) or review_as_of != expected:
            raise HumanReviewScreenUnavailable("human_review_chart_lock_mismatch")
        return {
            "candidate_id": alert.candidate_id,
            "source_sha256": source_sha256,
            "symbol": alert.symbol,
            "review_as_of": expected,
            "review_available_at": alert.review_available_at.isoformat(),
        }

    def candidate_detail(
        self,
        *,
        candidate_id: str,
        source_sha256: str,
    ) -> dict[str, object]:
        """Return one deeply verified evidence tree after explicit selection."""

        _kind, _path, _report, alert = self._load_candidate_by_hash(
            source_sha256=source_sha256,
            candidate_id=candidate_id,
        )
        return {
            "schema": "chanlun-human-review-candidate-detail-web",
            "candidate_id": alert.candidate_id,
            "source_content_sha256": source_sha256,
            "sector_higher_timeframe_evidence": (
                None
                if alert.sector_higher_timeframe_evidence is None
                else alert.sector_higher_timeframe_evidence.document()
            ),
            "market_symbol_higher_timeframe_evidence": (
                None
                if alert.market_symbol_higher_timeframe_evidence is None
                else alert.market_symbol_higher_timeframe_evidence.document()
            ),
            "sector_ranking_evidence": (
                None
                if alert.sector_ranking_evidence is None
                else alert.sector_ranking_evidence.document()
            ),
            "sector_higher_timeframe_evidence_id": (
                None
                if alert.sector_higher_timeframe_evidence is None
                else alert.sector_higher_timeframe_evidence.evidence_id
            ),
            "market_symbol_higher_timeframe_evidence_id": (
                None
                if alert.market_symbol_higher_timeframe_evidence is None
                else alert.market_symbol_higher_timeframe_evidence.evidence_id
            ),
            "sector_ranking_evidence_id": (
                None
                if alert.sector_ranking_evidence is None
                else alert.sector_ranking_evidence.evidence_id
            ),
            "market_symbol_higher_timeframe_source_attestation": (
                _market_symbol_source_attestation(alert)
            ),
            "sector_ranking_attestation": _sector_ranking_attestation(alert),
            "evidence_detail_available": True,
            "evidence_detail_loaded": True,
            "highest_status": "REVIEW_REQUIRED",
            "human_confirmation_required": True,
            "automated_order_authorized": False,
            "orders_created": 0,
            "fills_created": 0,
            "live_status": "LIVE_DISABLED",
        }

    def append_feedback(
        self,
        *,
        candidate_id: str,
        source_sha256: str,
        reviewer: str,
        values: Mapping[str, object],
        reviewed_at: datetime,
        request_id: str | None = None,
    ) -> dict[str, object]:
        kind, _path, report, alert = self._load_candidate_by_hash(
            source_sha256=source_sha256,
            candidate_id=candidate_id,
        )
        notes = str(values.get("notes") or "").strip()
        if len(notes) > 4000:
            raise HumanReviewScreenUnavailable("human_review_notes_too_long")
        try:
            observed_at = self._clock()
            if observed_at.tzinfo is None:
                raise ValueError("human review clock must be timezone-aware")
            feedback = HumanReviewFeedback(
                candidate_id=candidate_id,
                source_screen_content_sha256=source_sha256,
                reviewer=reviewer,
                reviewed_at=reviewed_at,
                center_judgement=str(values.get("center_judgement") or ""),
                trend_judgement=str(values.get("trend_judgement") or ""),
                level_judgement=str(values.get("level_judgement") or ""),
                point_judgement=str(values.get("point_judgement") or ""),
                disposition=str(values.get("disposition") or ""),
                decomposition_judgement=str(
                    values.get("decomposition_judgement") or "UNCERTAIN"
                ),
                center_expansion_judgement=str(
                    values.get("center_expansion_judgement") or "UNCERTAIN"
                ),
                nine_segment_upgrade_judgement=str(
                    values.get("nine_segment_upgrade_judgement") or "UNCERTAIN"
                ),
                locator_judgement=str(values.get("locator_judgement") or "UNCERTAIN"),
                notes=notes,
                request_id=request_id,
                signal_lifecycle_id=alert.signal_lifecycle_id,
            )
            validate_human_review_feedback_causality(
                feedback,
                alert,
                source_screen_content_sha256=source_sha256,
            )
            if feedback.reviewed_at > observed_at:
                raise ValueError("human review feedback cannot be future-dated")
        except (TypeError, ValueError) as exc:
            raise HumanReviewScreenUnavailable("human_review_feedback_invalid") from exc
        with self._write_lock:
            (
                paper_observation_eligible,
                paper_observation_reason,
                paper_observation_source_session,
                paper_observation_current_market_session,
            ) = self._paper_observation_eligibility(
                kind=kind,
                source_sha256=source_sha256,
                source_report=report,
                force_refresh_scheduler=True,
            )
            paper_observation_source_eligible = paper_observation_eligible
            paper_observation_source_reason = paper_observation_reason
            sector_catalogs, _captured_at, _warning = self._sector_catalogs()
            sector_presentation = _sector_name_presentation(
                alert,
                sector_catalogs,
            )
            (
                paper_entry_sector_eligible,
                paper_entry_sector_reason,
            ) = _paper_entry_sector_eligibility(alert, sector_presentation)
            paper_observation_eligible = (
                paper_observation_source_eligible and paper_entry_sector_eligible
            )
            paper_observation_reason = (
                paper_observation_source_reason
                if not paper_observation_source_eligible
                else paper_entry_sector_reason
            )
            new_strategic_entry_requested = (
                feedback.disposition == "PAPER_OBSERVE"
                and feedback.point_judgement.startswith("BUY_")
                and alert.alert_type == "POSSIBLE_30M_BUY"
            )
            risk_reducing_cancellation_requested = (
                feedback.disposition != "PAPER_OBSERVE"
            )
            paper_source_reconciliation_eligible = (
                paper_observation_source_eligible
                or (
                    risk_reducing_cancellation_requested
                    and paper_observation_source_reason
                    in _PAPER_RISK_REDUCING_RECONCILIATION_REASONS
                )
            )
            paper_ledger_reconciliation_eligible = (
                paper_source_reconciliation_eligible
                and (paper_entry_sector_eligible or not new_strategic_entry_requested)
            )
            try:
                document = append_human_review_feedback(
                    self.feedback_ledger,
                    feedback,
                )
            except ValueError as exc:
                if "request identity was reused" in str(exc):
                    raise HumanReviewScreenUnavailable(
                        "human_review_request_conflict"
                    ) from exc
                raise HumanReviewScreenUnavailable(
                    "human_review_feedback_ledger_invalid"
                ) from exc
            entry = next(
                value
                for value in document["entries"]
                if value["feedback_id"] == feedback.feedback_id
            )
            stored_values = {
                field: entry.get(field)
                for field in HumanReviewFeedback.__dataclass_fields__
            }
            stored_values["reviewed_at"] = datetime.fromisoformat(
                str(stored_values["reviewed_at"])
            )
            stored_feedback = HumanReviewFeedback(**stored_values)
            paper_document = None
            paper_event = None
            cancellation_events: tuple[dict[str, object], ...] = ()
            paper_changed = False
            if paper_ledger_reconciliation_eligible:
                try:
                    entry_selection_evidence = _paper_entry_selection_evidence(
                        stored_feedback,
                        alert,
                        sector_presentation,
                    )
                    (
                        paper_document,
                        paper_event,
                        cancellation_events,
                        paper_changed,
                    ) = reconcile_human_paper_feedback(
                        self.paper_ledger,
                        feedback=stored_feedback,
                        alert=alert,
                        entry_selection_evidence=(entry_selection_evidence),
                    )
                except ValueError as exc:
                    raise HumanReviewScreenUnavailable(
                        "human_paper_ledger_invalid"
                    ) from exc
        return {
            "feedback": entry,
            "ledger_content_sha256": document["content_sha256"],
            "paper_intent": (None if paper_event is None else paper_event["payload"]),
            "paper_ledger_content_sha256": (
                None if paper_document is None else paper_document["content_sha256"]
            ),
            "superseded_paper_intents": [
                event["payload"] for event in cancellation_events
            ],
            "paper_ledger_changed": paper_changed,
            "paper_observation_eligible": paper_observation_eligible,
            "paper_observation_reason": paper_observation_reason,
            "paper_observation_source_eligible": (paper_observation_source_eligible),
            "paper_observation_source_reason": paper_observation_source_reason,
            "paper_entry_sector_eligible": paper_entry_sector_eligible,
            "paper_entry_sector_reason": paper_entry_sector_reason,
            "paper_ledger_reconciliation_eligible": (
                paper_ledger_reconciliation_eligible
            ),
            "sector_ranking_catalog_attestation": sector_presentation.get(
                "sector_ranking_catalog_attestation"
            ),
            "paper_observation_source_session": (paper_observation_source_session),
            "paper_observation_current_market_session": (
                paper_observation_current_market_session
            ),
            "virtual_only": paper_event is not None or bool(cancellation_events),
            "broker_transport_available": False,
            "automated_order_authorized": False,
            "live_status": "LIVE_DISABLED",
        }


__all__ = (
    "HumanReviewScreenUnavailable",
    "HumanReviewScreeningService",
    "SCREEN_SCHEMA",
    "WEB_SCHEMA",
)
