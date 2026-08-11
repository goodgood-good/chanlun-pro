"""Monthly/weekly/daily risk gates for the human-assisted decision bundle."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from math import ceil
from numbers import Integral
import re
from typing import Literal
from zoneinfo import ZoneInfo

import pandas as pd

from chanlun.decision_support.fingerprints import normalize_datetime, sha256_json
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
from chanlun.decision_support.trading_system.qmt_higher_timeframe import (
    QMT_HIGHER_TIMEFRAME_WARMUP_REQUIRED_DAILY_BARS,
    QMT_HIGHER_TIMEFRAME_WARMUP_CONVERGENCE_PARAMETER_SET_ID,
    QMT_SECTOR_NATIVE_DAILY_RESEARCH_BASE_FREQUENCY,
    QmtHigherTimeframeRiskEnvelope,
    QmtHigherTimeframeWarmupEvidence,
    build_qmt_higher_timeframe_risk,
    qmt_higher_timeframe_inputs,
)
from chanlun.decision_support.trading_system.etf_proxy_facts import (
    RiskDiagnosticBuyPointEvidenceFacts,
    RiskMappingPointEvidenceFacts,
    RiskMappingSupplyFacts,
)
from chanlun.decision_support.trading_system.qmt_native_daily_bridge import (
    QMT_NATIVE_DAILY_CALENDAR_COVERAGE_EVIDENCE_CONTRACT_ID,
    QMT_NATIVE_DAILY_RECONCILED_BASE_FREQUENCY,
    QmtNativeDailyCalendarCoverageEvidence,
    QmtNativeDailyReconciliationError,
    QmtNativeDailyReconciliationEvidence,
    build_qmt_native_daily_bridge,
)
from chanlun.decision_support.trading_system.qmt_sector_same_base import (
    derive_qmt_sector_thirty_minute_frame,
)
from chanlun.decision_support.trading_system.qmt_causal_factor_adjustment import (
    QMT_CAUSAL_FACTOR_ADJUSTMENT_CONTRACT_ID,
    build_causal_sector_price_basis_metadata,
)
from chanlun.decision_support.trading_system.qmt_same_base_stream import (
    QmtMinuteSessionIssue,
    build_qmt_same_base_stream_frames,
)
RiskGate = Literal["GREEN", "AMBER", "RED", "UNRESOLVED"]
RiskPeriod = Literal["M", "W", "D"]
QMT_SECTOR_SAME_BASE_SOURCE_MODE = "PAGE_PARITY_SAME_5M_BASE"
QMT_SECTOR_NATIVE_DAILY_RESEARCH_SOURCE_MODE = (
    "NATIVE_DAILY_MWD_PLUS_5M_30M_UNRECONCILED_RESEARCH"
)
_QMT_SECTOR_COMPOSITE_MEMBER_LIMIT = 24
_QMT_SECTOR_COMPOSITE_MINIMUM_MEMBER_COUNT = 8
_QMT_SECTOR_COMPOSITE_MINIMUM_BAR_COVERAGE = "0.60"
_QMT_SECTOR_COMPOSITE_PROVIDER = "qmt-gics3-composite"
_QMT_SECTOR_COMPOSITE_ADJUSTMENT = (
    "causal-factor-stable-24-member-median"
)
_QMT_SECTOR_COMPOSITE_QUANTUM = Decimal("0.000001")
_QMT_SECTOR_COMPOSITE_MEMBER_MASK_CONTRACT = (
    "BIT_I_IS_SECTOR_COMPOSITE_MEMBERS_I"
)
_QMT_SECTOR_COMPOSITE_METHOD = (
    "DETERMINISTIC_HASH_SAMPLE_CAUSAL_FACTOR_MEDIAN_RETURN_CHAIN"
)
_SHA256_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
HIGHER_TIMEFRAME_SESSION_EVIDENCE_CONTRACT_ID = (
    "chanlun-higher-timeframe-session-evidence"
)
QMT_SECTOR_SAME_BASE_COVERAGE_EVIDENCE_CONTRACT_ID = (
    "chanlun-qmt-sector-same-5m-source-coverage"
)
HIGHER_TIMEFRAME_EFFECTIVENESS_AUDIT_SCHEMA = (
    "chanlun-higher-timeframe-effectiveness-audit"
)
_HIGHER_TIMEFRAME_GATES = frozenset({"GREEN", "AMBER", "RED", "UNRESOLVED"})
_A_SHARE_CHART_SYMBOL = re.compile(r"^(?:SH|SZ|BJ)\.\d{6}$")
_RISK_POINT_CHART_INTERVALS = {"30m": "30", "d": "1D", "w": "1W", "m": "1M"}
_HIGHER_TIMEFRAME_STATES = frozenset(
    {
        "NONE",
        "FORMED",
        "FORMED_UNRESOLVED",
        "PEN_RISK_CONFIRMED",
        "INTERMEDIATE",
        "RESOLVED_CONTINUATION",
        "UNRESOLVED",
    }
)


def _latest_closed_expected_session(
    observed_at: datetime,
    expected_sessions: Sequence[date],
) -> date | None:
    """Return the latest calendar session whose 15:00 close is observable.

    The decision is session-aware rather than wall-clock-date-aware: Friday is
    still the latest completed session on Saturday, Sunday and a following
    exchange holiday.  A current trading session remains unavailable before
    its close, so this helper cannot authorize an intraday refresh.
    """

    observed = normalize_datetime(observed_at, "observed_at").astimezone(
        ZoneInfo("Asia/Shanghai")
    )
    if not expected_sessions:
        return None
    latest = max(expected_sessions)
    latest_close = datetime.combine(
        latest,
        time(15, 0),
        tzinfo=ZoneInfo("Asia/Shanghai"),
    )
    return latest if observed >= latest_close else None


def sector_native_daily_research_bridge_contract() -> dict[str, object]:
    """Return the frozen, non-trading contract for the sector M/W/D fallback."""

    stable: dict[str, object] = {
        "schema": "chanlun-sector-native-daily-research-bridge",
        "activation": "STRICT_SAME_5M_WARMUP_HISTORY_INSUFFICIENT_ONLY",
        "daily_source": "CURRENT_MEMBER_NATIVE_DAILY_MEDIAN_RETURN_CHAIN",
        "thirty_minute_source": "CURRENT_MEMBER_COMPLETED_5M_DERIVED_30M",
        "cross_frequency_reconciliation": "UNRECONCILED_NONLINEAR_MEDIAN",
        "entry_disposition": "RESEARCH_AMBER_OR_RED_ONLY",
        "green_cap": "GREEN_TO_AMBER",
        "confirmed_red_rejects": True,
        "unresolved_rejects": True,
        "minimum_daily_bars": (
            QMT_HIGHER_TIMEFRAME_WARMUP_REQUIRED_DAILY_BARS
        ),
        "tick_data_used": False,
        "data_grade": "RESEARCH_ONLY",
        "live_status": "LIVE_DISABLED",
    }
    return {**stable, "parameter_set_id": sha256_json(stable)}


@dataclass(frozen=True, slots=True)
class HigherTimeframeSessionEvidence:
    """Presentation evidence for the causal QMT 1m session gate.

    This evidence explains an already fail-closed decision.  It never turns a
    missing session into a suspension: that requires an independently
    certified point-in-time trade-status source which the installed QMT client
    does not expose for history.
    """

    status: Literal["EXACT", "UNAVAILABLE"]
    issues: tuple[QmtMinuteSessionIssue, ...] = ()

    def __post_init__(self) -> None:
        if self.status not in {"EXACT", "UNAVAILABLE"}:
            raise ValueError("invalid higher-timeframe session evidence status")
        if self.status == "UNAVAILABLE" and self.issues:
            raise ValueError("unavailable session evidence cannot assert issues")
        identities = tuple((value.session, value.code) for value in self.issues)
        if identities != tuple(sorted(set(identities))):
            raise ValueError(
                "higher-timeframe session issues must be unique and chronological"
            )

    @classmethod
    def exact(
        cls,
        issues: tuple[QmtMinuteSessionIssue, ...] = (),
    ) -> HigherTimeframeSessionEvidence:
        ordered = tuple(sorted(issues, key=lambda value: (value.session, value.code)))
        return cls(status="EXACT", issues=ordered)

    @classmethod
    def unavailable(cls) -> HigherTimeframeSessionEvidence:
        return cls(status="UNAVAILABLE")

    def document(self) -> dict[str, object]:
        document = {
            "contract_id": HIGHER_TIMEFRAME_SESSION_EVIDENCE_CONTRACT_ID,
            "status": self.status,
            "issue_count": len(self.issues),
            "issues": [value.document() for value in self.issues],
            "entry_disposition": (
                "FAIL_CLOSED"
                if self.status == "UNAVAILABLE" or self.issues
                else "NO_SESSION_BLOCKER"
            ),
        }
        return document


@dataclass(frozen=True, slots=True)
class QmtSectorSameBaseCoverageEvidence:
    """Causal left-boundary evidence for the sector's strict 5m base.

    The 480-session value remains the frozen minimum.  This document only
    explains how much of that minimum is physically present in the visible QMT
    prefix; it cannot relax the warmup gate or promote the native-daily research
    bridge to a reconciled source.
    """

    observed_at: datetime
    calendar_first_session: date
    first_visible_bar_at: datetime
    last_visible_bar_at: datetime
    first_completed_session: date | None
    last_completed_session: date | None
    visible_five_minute_bar_count: int
    completed_daily_bar_count: int
    required_daily_bar_count: int
    remaining_daily_bar_count: int
    missing_leading_calendar_session_count: int
    warmup_converged: bool
    warmup_reason_code: str
    boundary_status: Literal[
        "REQUIRED_HISTORY_CONVERGED",
        "REQUIRED_HISTORY_PRESENT_BUT_TAIL_DIVERGED",
        "VISIBLE_PREFIX_STARTS_AFTER_REQUESTED_WARMUP",
        "VISIBLE_PREFIX_INSUFFICIENT_WITHOUT_LEADING_GAP",
    ]
    physical_source_boundary_status: Literal[
        "PHYSICAL_QMT_CACHE_LEFT_BOUNDARY_AFTER_REQUESTED_WARMUP",
        "REQUESTED_REPLAY_LEFT_BOUNDARY_CLIPS_EARLIER_QMT_HISTORY",
        "INSUFFICIENT_PHYSICAL_QMT_MEMBER_FILES",
        "PHYSICAL_QMT_SOURCE_BOUNDARY_UNAVAILABLE",
    ]
    physical_source_requested_start_at: datetime
    physical_source_required_contributor_start_at: datetime | None
    physical_source_representative_member_count: int
    physical_source_available_member_count: int
    physical_source_required_contributor_count: int
    physical_source_inventory_revision: str

    def __post_init__(self) -> None:
        observed = normalize_datetime(self.observed_at, "observed_at")
        first_bar = normalize_datetime(
            self.first_visible_bar_at,
            "first_visible_bar_at",
        )
        last_bar = normalize_datetime(
            self.last_visible_bar_at,
            "last_visible_bar_at",
        )
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "first_visible_bar_at", first_bar)
        object.__setattr__(self, "last_visible_bar_at", last_bar)
        if not first_bar <= last_bar <= observed:
            raise ValueError("sector 5m coverage exceeds its causal prefix")
        if (
            self.visible_five_minute_bar_count <= 0
            or self.completed_daily_bar_count < 0
            or self.required_daily_bar_count
            != QMT_HIGHER_TIMEFRAME_WARMUP_REQUIRED_DAILY_BARS
            or self.remaining_daily_bar_count
            != max(0, self.required_daily_bar_count - self.completed_daily_bar_count)
            or self.missing_leading_calendar_session_count < 0
        ):
            raise ValueError("sector 5m coverage counts are inconsistent")
        if (self.first_completed_session is None) != (
            self.last_completed_session is None
        ):
            raise ValueError("sector completed-session coverage is incomplete")
        if self.first_completed_session is None:
            if self.completed_daily_bar_count != 0:
                raise ValueError("sector completed-session count lost its range")
        elif (
            self.completed_daily_bar_count <= 0
            or self.first_completed_session > self.last_completed_session
            or self.calendar_first_session > self.first_completed_session
        ):
            raise ValueError("sector completed-session range is invalid")
        expected_status: str
        if self.warmup_converged:
            expected_status = "REQUIRED_HISTORY_CONVERGED"
        elif self.completed_daily_bar_count >= self.required_daily_bar_count:
            expected_status = "REQUIRED_HISTORY_PRESENT_BUT_TAIL_DIVERGED"
        elif self.missing_leading_calendar_session_count:
            expected_status = "VISIBLE_PREFIX_STARTS_AFTER_REQUESTED_WARMUP"
        else:
            expected_status = "VISIBLE_PREFIX_INSUFFICIENT_WITHOUT_LEADING_GAP"
        if self.boundary_status != expected_status:
            raise ValueError("sector 5m boundary status contradicts its evidence")
        if self.warmup_converged != self.warmup_reason_code.endswith(
            "TAIL_STABLE"
        ):
            raise ValueError("sector 5m coverage contradicts its warmup verdict")
        if self.physical_source_boundary_status not in {
            "PHYSICAL_QMT_CACHE_LEFT_BOUNDARY_AFTER_REQUESTED_WARMUP",
            "REQUESTED_REPLAY_LEFT_BOUNDARY_CLIPS_EARLIER_QMT_HISTORY",
            "INSUFFICIENT_PHYSICAL_QMT_MEMBER_FILES",
            "PHYSICAL_QMT_SOURCE_BOUNDARY_UNAVAILABLE",
        }:
            raise ValueError("sector physical source boundary status is invalid")
        else:
            requested = self.physical_source_requested_start_at
            representative = self.physical_source_representative_member_count
            available = self.physical_source_available_member_count
            required = self.physical_source_required_contributor_count
            if (
                requested is None
                or
                type(representative) is not int
                or type(available) is not int
                or type(required) is not int
                or representative <= 0
                or available < 0
                or available > representative
                or required <= 0
                or self.physical_source_inventory_revision is None
                or _SHA256_ID.fullmatch(
                    self.physical_source_inventory_revision
                )
                is None
            ):
                raise ValueError("sector physical source counts are invalid")
            requested = normalize_datetime(
                requested,
                "physical_source_requested_start_at",
            )
            object.__setattr__(
                self,
                "physical_source_requested_start_at",
                requested,
            )
            if requested > first_bar:
                raise ValueError(
                    "sector physical source request starts after visible history"
                )
            expected_required = max(
                _QMT_SECTOR_COMPOSITE_MINIMUM_MEMBER_COUNT,
                ceil(
                    Decimal(representative)
                    * Decimal(_QMT_SECTOR_COMPOSITE_MINIMUM_BAR_COVERAGE)
                ),
            )
            if required != expected_required:
                raise ValueError(
                    "sector physical source quorum contradicts representative count"
                )
            if self.physical_source_required_contributor_start_at is not None:
                object.__setattr__(
                    self,
                    "physical_source_required_contributor_start_at",
                    normalize_datetime(
                        self.physical_source_required_contributor_start_at,
                        "physical_source_required_contributor_start_at",
                    ),
                )
            if (
                self.physical_source_boundary_status
                == "INSUFFICIENT_PHYSICAL_QMT_MEMBER_FILES"
            ):
                if (
                    available >= required
                    or self.physical_source_required_contributor_start_at
                    is not None
                ):
                    raise ValueError(
                        "physical member-file shortage contradicts its counts"
                    )
            elif (
                self.physical_source_boundary_status
                == "PHYSICAL_QMT_SOURCE_BOUNDARY_UNAVAILABLE"
            ):
                if (
                    available < required
                    or self.physical_source_required_contributor_start_at
                    is not None
                ):
                    raise ValueError(
                        "unavailable physical boundary lost its available files"
                    )
            else:
                required_start = (
                    self.physical_source_required_contributor_start_at
                )
                if available < required or required_start is None:
                    raise ValueError(
                        "resolved physical source boundary lost its quorum"
                    )
                expected_physical_status = (
                    "PHYSICAL_QMT_CACHE_LEFT_BOUNDARY_AFTER_REQUESTED_WARMUP"
                    if required_start > requested
                    else "REQUESTED_REPLAY_LEFT_BOUNDARY_CLIPS_EARLIER_QMT_HISTORY"
                )
                if self.physical_source_boundary_status != expected_physical_status:
                    raise ValueError(
                        "physical source boundary status contradicts its timestamps"
                    )

    def document(self) -> dict[str, object]:
        document = {
            "contract_id": QMT_SECTOR_SAME_BASE_COVERAGE_EVIDENCE_CONTRACT_ID,
            "observed_at": self.observed_at.isoformat(),
            "calendar_first_session": self.calendar_first_session.isoformat(),
            "first_visible_bar_at": self.first_visible_bar_at.isoformat(),
            "last_visible_bar_at": self.last_visible_bar_at.isoformat(),
            "first_completed_session": (
                None
                if self.first_completed_session is None
                else self.first_completed_session.isoformat()
            ),
            "last_completed_session": (
                None
                if self.last_completed_session is None
                else self.last_completed_session.isoformat()
            ),
            "visible_five_minute_bar_count": self.visible_five_minute_bar_count,
            "completed_daily_bar_count": self.completed_daily_bar_count,
            "required_daily_bar_count": self.required_daily_bar_count,
            "remaining_daily_bar_count": self.remaining_daily_bar_count,
            "missing_leading_calendar_session_count": (
                self.missing_leading_calendar_session_count
            ),
            "history_requirement_met": (
                self.completed_daily_bar_count >= self.required_daily_bar_count
            ),
            "warmup_converged": self.warmup_converged,
            "warmup_reason_code": self.warmup_reason_code,
            "boundary_status": self.boundary_status,
            "physical_source_boundary_status": (
                self.physical_source_boundary_status
            ),
            "physical_source_requested_start_at": (
                None
                if self.physical_source_requested_start_at is None
                else self.physical_source_requested_start_at.isoformat()
            ),
            "physical_source_required_contributor_start_at": (
                None
                if self.physical_source_required_contributor_start_at is None
                else self.physical_source_required_contributor_start_at.isoformat()
            ),
            "physical_source_representative_member_count": (
                self.physical_source_representative_member_count
            ),
            "physical_source_available_member_count": (
                self.physical_source_available_member_count
            ),
            "physical_source_required_contributor_count": (
                self.physical_source_required_contributor_count
            ),
            "physical_source_inventory_revision": (
                self.physical_source_inventory_revision
            ),
            "base_frequency": "5m",
            "prefix_only": True,
            "data_grade": "RESEARCH_ONLY",
            "live_status": "LIVE_DISABLED",
        }
        return document

    @classmethod
    def from_document(
        cls,
        value: Mapping[str, object],
    ) -> QmtSectorSameBaseCoverageEvidence:
        contract_id = value.get("contract_id")
        if (
            contract_id != QMT_SECTOR_SAME_BASE_COVERAGE_EVIDENCE_CONTRACT_ID
            or value.get("base_frequency") != "5m"
            or value.get("prefix_only") is not True
            or value.get("data_grade") != "RESEARCH_ONLY"
            or value.get("live_status") != "LIVE_DISABLED"
        ):
            raise ValueError("sector 5m coverage document contract is invalid")
        integer_fields = (
            "visible_five_minute_bar_count",
            "completed_daily_bar_count",
            "required_daily_bar_count",
            "remaining_daily_bar_count",
            "missing_leading_calendar_session_count",
        )
        if any(type(value.get(name)) is not int for name in integer_fields) or type(
            value.get("warmup_converged")
        ) is not bool:
            raise ValueError("sector 5m coverage document scalar types are invalid")

        def optional_date(name: str) -> date | None:
            raw = value.get(name)
            return None if raw is None else date.fromisoformat(str(raw))

        try:
            observed_at = datetime.fromisoformat(str(value["observed_at"]))
            calendar_first_session = date.fromisoformat(
                str(value["calendar_first_session"])
            )
            first_visible_bar_at = datetime.fromisoformat(
                str(value["first_visible_bar_at"])
            )
            raw_required_start = value.get(
                "physical_source_required_contributor_start_at"
            )
            required_start_at = (
                None
                if raw_required_start is None
                else datetime.fromisoformat(str(raw_required_start))
            )
            result = cls(
                observed_at=observed_at,
                calendar_first_session=calendar_first_session,
                first_visible_bar_at=first_visible_bar_at,
                last_visible_bar_at=datetime.fromisoformat(
                    str(value["last_visible_bar_at"])
                ),
                first_completed_session=optional_date("first_completed_session"),
                last_completed_session=optional_date("last_completed_session"),
                visible_five_minute_bar_count=int(
                    value["visible_five_minute_bar_count"]
                ),
                completed_daily_bar_count=int(value["completed_daily_bar_count"]),
                required_daily_bar_count=int(value["required_daily_bar_count"]),
                remaining_daily_bar_count=int(value["remaining_daily_bar_count"]),
                missing_leading_calendar_session_count=int(
                    value["missing_leading_calendar_session_count"]
                ),
                warmup_converged=value["warmup_converged"],
                warmup_reason_code=str(value["warmup_reason_code"]),
                boundary_status=str(value["boundary_status"]),
                physical_source_boundary_status=str(
                    value["physical_source_boundary_status"]
                ),
                physical_source_requested_start_at=(
                    datetime.fromisoformat(
                        str(value["physical_source_requested_start_at"])
                    )
                ),
                physical_source_required_contributor_start_at=required_start_at,
                physical_source_representative_member_count=(
                    value["physical_source_representative_member_count"]
                ),
                physical_source_available_member_count=(
                    value["physical_source_available_member_count"]
                ),
                physical_source_required_contributor_count=(
                    value["physical_source_required_contributor_count"]
                ),
                physical_source_inventory_revision=(
                    str(value["physical_source_inventory_revision"])
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("sector 5m coverage document is malformed") from exc
        if dict(value) != result.document():
            raise ValueError("sector 5m coverage document is non-canonical")
        return result


class HigherTimeframeDataUnavailable(ValueError):
    """A fail-closed higher-timeframe data gate with machine-readable causes."""

    def __init__(
        self,
        reason_codes: tuple[str, ...],
        *,
        session_issues: tuple[QmtMinuteSessionIssue, ...] = (),
        native_daily_calendar_coverage_evidence: (
            QmtNativeDailyCalendarCoverageEvidence | None
        ) = None,
    ) -> None:
        normalized = tuple(
            dict.fromkeys(str(value).strip() for value in reason_codes if str(value).strip())
        )
        if not normalized:
            normalized = ("QMT_ONE_MINUTE_SAME_BASE_STREAM_UNRESOLVED",)
        self.reason_codes = normalized
        self.session_issues = HigherTimeframeSessionEvidence.exact(
            session_issues
        ).issues
        self.native_daily_calendar_coverage_evidence = (
            native_daily_calendar_coverage_evidence
        )
        issue_codes = {value.code for value in self.session_issues}
        if not issue_codes.issubset(set(self.reason_codes)):
            raise ValueError("session issue codes must be present in reason_codes")
        coverage = self.native_daily_calendar_coverage_evidence
        if coverage is not None and (
            coverage.status == "EXACT"
            or "QMT_NATIVE_DAILY_TRADING_CALENDAR_MISMATCH"
            not in self.reason_codes
        ):
            raise ValueError("native-daily calendar evidence contradicts failure")
        super().__init__(
            "QMT one-minute same-base risk stream is unresolved: "
            + ",".join(normalized)
        )


@dataclass(frozen=True, slots=True)
class HigherTimeframePeriodDiagnostic:
    """Read-only evidence explaining one strict M/W/D risk-state result.

    This object is deliberately diagnostic only.  It exposes the completed-bar
    supply and the frozen top-fractal-to-lower-center mapping that already
    produced the gate; it cannot relax or replace that gate.
    """

    period: RiskPeriod
    state: str
    completed_bar_count: int
    evidence_bar_end: datetime | None
    active_top_interval: tuple[datetime, datetime] | None
    mapping_unique: bool
    mapped_center_id: str | None
    mapping_candidate_ids: tuple[str, ...]
    blocker_codes: tuple[str, ...]
    warning_codes: tuple[str, ...]
    source_revision: str
    mapping_supply: RiskMappingSupplyFacts | None = None

    def __post_init__(self) -> None:
        if self.period not in {"M", "W", "D"}:
            raise ValueError("invalid higher-timeframe diagnostic period")
        if self.state not in _HIGHER_TIMEFRAME_STATES:
            raise ValueError("invalid higher-timeframe diagnostic state")
        if self.completed_bar_count < 0:
            raise ValueError("completed higher-timeframe bar count cannot be negative")
        if not self.source_revision:
            raise ValueError("higher-timeframe diagnostic source revision is required")
        if self.evidence_bar_end is not None:
            object.__setattr__(
                self,
                "evidence_bar_end",
                normalize_datetime(self.evidence_bar_end, "evidence_bar_end"),
            )
        if self.active_top_interval is not None:
            start, end = self.active_top_interval
            normalized = (
                normalize_datetime(start, "active_top_interval.start"),
                normalize_datetime(end, "active_top_interval.end"),
            )
            if normalized[0] > normalized[1]:
                raise ValueError("active top interval is reversed")
            object.__setattr__(self, "active_top_interval", normalized)
        for label, values in (
            ("mapping candidate IDs", self.mapping_candidate_ids),
            ("blocker codes", self.blocker_codes),
            ("warning codes", self.warning_codes),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"higher-timeframe {label} must be unique")
        if self.mapping_supply is not None:
            if self.active_top_interval is None:
                raise ValueError("mapping supply requires an active top fractal")
            if (
                self.mapping_supply.highest_candidate_center_count
                != len(self.mapping_candidate_ids)
            ):
                raise ValueError("mapping supply candidate count changed")
            if self.mapping_unique:
                if self.mapping_supply.classification != "UNIQUE_MAPPING":
                    raise ValueError("unique mapping has inconsistent supply evidence")
            elif self.mapping_supply.classification == "UNIQUE_MAPPING":
                raise ValueError("unresolved mapping claims unique supply")
        if self.state == "NONE":
            if (
                self.active_top_interval is not None
                or not self.mapping_unique
                or self.mapped_center_id is not None
                or self.mapping_candidate_ids
                or self.mapping_supply is not None
            ):
                raise ValueError("empty state cannot retain mapping evidence")
        elif self.mapping_supply is None:
            raise ValueError("active state requires mapping supply evidence")
        if self.state != "NONE" and self.mapping_unique:
            if (
                self.mapped_center_id is None
                or self.mapping_candidate_ids != (self.mapped_center_id,)
            ):
                raise ValueError("unique mapping identity is inconsistent")
        elif self.state != "NONE" and self.mapped_center_id is not None:
            raise ValueError("non-unique mapping cannot retain a mapped center")

    @classmethod
    def from_document(cls, raw: object) -> "HigherTimeframePeriodDiagnostic":
        required = {
            "period",
            "state",
            "completed_bar_count",
            "evidence_bar_end",
            "active_top_interval",
            "mapping_unique",
            "mapped_center_id",
            "mapping_candidate_ids",
            "blocker_codes",
            "warning_codes",
            "source_revision",
        }
        if not isinstance(raw, Mapping) or frozenset(raw) not in {
            frozenset(required),
            frozenset(required | {"mapping_supply"}),
        }:
            raise ValueError("higher-timeframe period diagnostic is malformed")
        if raw.get("period") not in {"M", "W", "D"} or raw.get(
            "state"
        ) not in _HIGHER_TIMEFRAME_STATES:
            raise ValueError("higher-timeframe period/state is malformed")
        if not isinstance(raw.get("source_revision"), str) or not raw[
            "source_revision"
        ]:
            raise ValueError("higher-timeframe source revision is malformed")
        if type(raw.get("completed_bar_count")) is not int:
            raise ValueError("higher-timeframe completed bar count is malformed")
        if type(raw.get("mapping_unique")) is not bool:
            raise ValueError("higher-timeframe mapping uniqueness is malformed")
        mapped_center_id = raw.get("mapped_center_id")
        if mapped_center_id is not None and (
            not isinstance(mapped_center_id, str) or not mapped_center_id
        ):
            raise ValueError("higher-timeframe mapped center identity is malformed")
        collections: dict[str, tuple[str, ...]] = {}
        for field in (
            "mapping_candidate_ids",
            "blocker_codes",
            "warning_codes",
        ):
            value = raw.get(field)
            if not isinstance(value, (tuple, list)) or any(
                not isinstance(item, str) or not item for item in value
            ):
                raise ValueError(
                    f"higher-timeframe {field.replace('_', ' ')} is malformed"
                )
            collections[field] = tuple(value)

        def parse_time(value: object, field: str) -> datetime | None:
            if value is None:
                return None
            if not isinstance(value, str):
                raise ValueError(f"higher-timeframe {field} is malformed")
            try:
                return normalize_datetime(datetime.fromisoformat(value), field)
            except ValueError as exc:
                raise ValueError(f"higher-timeframe {field} is malformed") from exc

        raw_interval = raw.get("active_top_interval")
        if raw_interval is None:
            active_interval = None
        elif isinstance(raw_interval, (tuple, list)) and len(raw_interval) == 2:
            start = parse_time(raw_interval[0], "active_top_interval.start")
            end = parse_time(raw_interval[1], "active_top_interval.end")
            if start is None or end is None:
                raise ValueError("higher-timeframe active top interval is malformed")
            active_interval = (start, end)
        else:
            raise ValueError("higher-timeframe active top interval is malformed")
        mapping_supply = (
            RiskMappingSupplyFacts.from_document(raw["mapping_supply"])
            if "mapping_supply" in raw
            else None
        )
        return cls(
            period=str(raw["period"]),  # type: ignore[arg-type]
            state=str(raw["state"]),
            completed_bar_count=int(raw["completed_bar_count"]),
            evidence_bar_end=parse_time(
                raw.get("evidence_bar_end"), "evidence_bar_end"
            ),
            active_top_interval=active_interval,
            mapping_unique=bool(raw["mapping_unique"]),
            mapped_center_id=(
                None if mapped_center_id is None else str(mapped_center_id)
            ),
            mapping_candidate_ids=collections["mapping_candidate_ids"],
            blocker_codes=collections["blocker_codes"],
            warning_codes=collections["warning_codes"],
            source_revision=str(raw["source_revision"]),
            mapping_supply=mapping_supply,
        )

    def document(self) -> dict[str, object]:
        document = {
            "period": self.period,
            "state": self.state,
            "completed_bar_count": self.completed_bar_count,
            "evidence_bar_end": (
                None
                if self.evidence_bar_end is None
                else self.evidence_bar_end.isoformat()
            ),
            "active_top_interval": (
                None
                if self.active_top_interval is None
                else [value.isoformat() for value in self.active_top_interval]
            ),
            "mapping_unique": self.mapping_unique,
            "mapped_center_id": self.mapped_center_id,
            "mapping_candidate_ids": list(self.mapping_candidate_ids),
            "blocker_codes": list(self.blocker_codes),
            "warning_codes": list(self.warning_codes),
            "source_revision": self.source_revision,
        }
        if self.mapping_supply is not None:
            document["mapping_supply"] = self.mapping_supply.document()
        return document


@dataclass(frozen=True, slots=True)
class HigherTimeframeGateEvidence:
    subject: str
    observed_at: datetime
    monthly: str
    weekly: str
    daily: str
    gate: RiskGate
    grade: str
    snapshot_id: str
    source_revision: str
    reason_codes: tuple[str, ...] = ()
    period_diagnostics: tuple[HigherTimeframePeriodDiagnostic, ...] = ()
    session_evidence: HigherTimeframeSessionEvidence | None = None
    warmup_evidence: QmtHigherTimeframeWarmupEvidence | None = None
    warmup_convergence_evidence: WarmupConvergenceEnvelope | None = None
    native_daily_reconciliation_evidence: (
        QmtNativeDailyReconciliationEvidence | None
    ) = None
    native_daily_calendar_coverage_evidence: (
        QmtNativeDailyCalendarCoverageEvidence | None
    ) = None
    sector_source_mode: str | None = None
    sector_strict_same_base_warmup_evidence: (
        QmtHigherTimeframeWarmupEvidence | None
    ) = None
    sector_strict_same_base_warmup_convergence_evidence: (
        WarmupConvergenceEnvelope | None
    ) = None
    sector_strict_same_base_source_coverage_evidence: (
        QmtSectorSameBaseCoverageEvidence | None
    ) = None
    sector_research_bridge_parameter_set_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "observed_at",
            normalize_datetime(self.observed_at, "observed_at"),
        )
        if not self.subject or not self.snapshot_id or not self.source_revision:
            raise ValueError("higher-timeframe risk provenance is required")
        if self.gate not in {"GREEN", "AMBER", "RED", "UNRESOLVED"}:
            raise ValueError("invalid higher-timeframe risk gate")
        if len(self.reason_codes) != len(set(self.reason_codes)):
            raise ValueError("higher-timeframe reason codes must be unique")
        if self.session_evidence is not None:
            issue_codes = {value.code for value in self.session_evidence.issues}
            if not issue_codes.issubset(set(self.reason_codes)):
                raise ValueError(
                    "higher-timeframe session issue codes must be gate reasons"
                )
        if self.period_diagnostics:
            periods = tuple(value.period for value in self.period_diagnostics)
            if periods != ("M", "W", "D"):
                raise ValueError(
                    "higher-timeframe period diagnostics must be ordered M/W/D"
                )
        coverage = self.native_daily_calendar_coverage_evidence
        if coverage is not None and (
            coverage.observed_at != self.observed_at
            or (
                coverage.status != "EXACT"
                and (
                    self.gate != "UNRESOLVED"
                    or "QMT_NATIVE_DAILY_TRADING_CALENDAR_MISMATCH"
                    not in self.reason_codes
                )
            )
            or (
                coverage.status == "EXACT"
                and self.native_daily_reconciliation_evidence is None
            )
        ):
            raise ValueError("native-daily calendar coverage contradicts gate")
        convergence = self.warmup_convergence_evidence
        if convergence is not None and (
            not isinstance(convergence, WarmupConvergenceEnvelope)
            or convergence.as_of != self.observed_at
            or convergence.frequency != "d"
            or convergence.diagnostic_only is not True
            or convergence.active_gate_unchanged is not True
        ):
            raise ValueError("higher-timeframe warmup convergence is inconsistent")
        if self.sector_source_mode is not None and self.sector_source_mode not in {
            QMT_SECTOR_SAME_BASE_SOURCE_MODE,
            QMT_SECTOR_NATIVE_DAILY_RESEARCH_SOURCE_MODE,
        }:
            raise ValueError("invalid sector higher-timeframe source mode")
        if self.sector_source_mode is None:
            if (
                self.sector_strict_same_base_warmup_evidence is not None
                or self.sector_strict_same_base_warmup_convergence_evidence
                is not None
                or self.sector_strict_same_base_source_coverage_evidence is not None
                or self.sector_research_bridge_parameter_set_id is not None
            ):
                raise ValueError("sector source evidence requires a source mode")
            return
        if self.sector_strict_same_base_warmup_evidence is None:
            raise ValueError("sector source mode requires strict warmup evidence")
        strict_convergence = (
            self.sector_strict_same_base_warmup_convergence_evidence
        )
        if convergence is not None and strict_convergence is None:
            raise ValueError("sector source mode lost strict convergence evidence")
        if strict_convergence is not None and (
            strict_convergence.as_of != self.observed_at
            or strict_convergence.frequency != "d"
            or strict_convergence.diagnostic_only is not True
            or strict_convergence.active_gate_unchanged is not True
        ):
            raise ValueError("sector strict convergence evidence is inconsistent")
        if self.sector_strict_same_base_source_coverage_evidence is None:
            raise ValueError("sector source mode requires strict 5m coverage evidence")
        coverage = self.sector_strict_same_base_source_coverage_evidence
        strict_warmup = self.sector_strict_same_base_warmup_evidence
        if (
            coverage.observed_at != self.observed_at
            or coverage.completed_daily_bar_count
            < strict_warmup.full_daily_bar_count
            or coverage.required_daily_bar_count
            != strict_warmup.required_daily_bar_count
            or coverage.warmup_converged != strict_warmup.converged
            or coverage.warmup_reason_code != strict_warmup.reason_code
        ):
            raise ValueError("sector strict 5m coverage contradicts warmup evidence")
        if self.sector_source_mode == QMT_SECTOR_NATIVE_DAILY_RESEARCH_SOURCE_MODE:
            expected_bridge = sector_native_daily_research_bridge_contract()[
                "parameter_set_id"
            ]
            if (
                self.sector_research_bridge_parameter_set_id != expected_bridge
                or self.gate == "GREEN"
                or self.sector_strict_same_base_warmup_evidence.reason_code
                != "QMT_HIGHER_TIMEFRAME_WARMUP_HISTORY_INSUFFICIENT"
            ):
                raise ValueError("sector native-daily research bridge is unsafe")
        elif (
            self.sector_research_bridge_parameter_set_id is not None
            or self.sector_strict_same_base_warmup_evidence
            != self.warmup_evidence
            or self.sector_strict_same_base_warmup_convergence_evidence
            != self.warmup_convergence_evidence
        ):
            raise ValueError("strict sector source evidence is inconsistent")

    @property
    def allows_new_entry(self) -> bool:
        return self.gate == "GREEN"

    def document(self) -> dict[str, object]:
        document = {
            "subject": self.subject,
            "observed_at": self.observed_at.isoformat(),
            "monthly": self.monthly,
            "weekly": self.weekly,
            "daily": self.daily,
            "gate": self.gate,
            "grade": self.grade,
            "snapshot_id": self.snapshot_id,
            "source_revision": self.source_revision,
            "reason_codes": list(self.reason_codes),
            "period_diagnostics": [
                value.document() for value in self.period_diagnostics
            ],
            "allows_new_entry": self.allows_new_entry,
        }
        if self.session_evidence is not None:
            document["session_evidence"] = self.session_evidence.document()
        if self.warmup_evidence is not None:
            document["warmup_evidence"] = self.warmup_evidence.document()
        if self.warmup_convergence_evidence is not None:
            document["warmup_convergence_evidence"] = (
                self.warmup_convergence_evidence.document()
            )
            if self.warmup_convergence_evidence.diagnostic is not None:
                document["warmup_convergence_diagnostic_evidence"] = (
                    self.warmup_convergence_evidence.diagnostic.document()
                )
            if (
                self.warmup_convergence_evidence.mapping_supply_diagnostic
                is not None
            ):
                document["warmup_mapping_supply_diagnostic_evidence"] = (
                    self.warmup_convergence_evidence.mapping_supply_diagnostic.document()
                )
            if (
                self.warmup_convergence_evidence.structure_lineage_diagnostic
                is not None
            ):
                document["warmup_structure_lineage_diagnostic_evidence"] = (
                    self.warmup_convergence_evidence.structure_lineage_diagnostic.document()
                )
        if self.native_daily_reconciliation_evidence is not None:
            document["native_daily_reconciliation_evidence"] = (
                self.native_daily_reconciliation_evidence.document()
            )
        if self.native_daily_calendar_coverage_evidence is not None:
            document["native_daily_calendar_coverage_evidence"] = (
                self.native_daily_calendar_coverage_evidence.document()
            )
        if self.sector_source_mode is not None:
            document["sector_source_mode"] = self.sector_source_mode
            document["sector_strict_same_base_warmup_evidence"] = (
                None
                if self.sector_strict_same_base_warmup_evidence is None
                else self.sector_strict_same_base_warmup_evidence.document()
            )
            document["sector_strict_same_base_warmup_convergence_evidence"] = (
                None
                if self.sector_strict_same_base_warmup_convergence_evidence is None
                else self.sector_strict_same_base_warmup_convergence_evidence.document()
            )
            document[
                "sector_strict_same_base_warmup_convergence_diagnostic_evidence"
            ] = (
                None
                if self.sector_strict_same_base_warmup_convergence_evidence is None
                or self.sector_strict_same_base_warmup_convergence_evidence.diagnostic
                is None
                else self.sector_strict_same_base_warmup_convergence_evidence.diagnostic.document()
            )
            document[
                "sector_strict_same_base_warmup_mapping_supply_diagnostic_evidence"
            ] = (
                None
                if self.sector_strict_same_base_warmup_convergence_evidence is None
                or self.sector_strict_same_base_warmup_convergence_evidence.mapping_supply_diagnostic
                is None
                else self.sector_strict_same_base_warmup_convergence_evidence.mapping_supply_diagnostic.document()
            )
            document[
                "sector_strict_same_base_warmup_structure_lineage_diagnostic_evidence"
            ] = (
                None
                if self.sector_strict_same_base_warmup_convergence_evidence is None
                or self.sector_strict_same_base_warmup_convergence_evidence.structure_lineage_diagnostic
                is None
                else self.sector_strict_same_base_warmup_convergence_evidence.structure_lineage_diagnostic.document()
            )
            document["sector_strict_same_base_source_coverage_evidence"] = (
                None
                if self.sector_strict_same_base_source_coverage_evidence is None
                else self.sector_strict_same_base_source_coverage_evidence.document()
            )
            document["sector_research_bridge_parameter_set_id"] = (
                self.sector_research_bridge_parameter_set_id
            )
        return document


@dataclass(frozen=True, slots=True)
class SectorHigherTimeframeGateResolution:
    """Shared page/replay choice between strict 5m and research fallback."""

    evidence: HigherTimeframeGateEvidence
    source_mode: str
    strict_warmup_evidence: QmtHigherTimeframeWarmupEvidence | None
    fallback_unavailable_reason_codes: tuple[str, ...] = ()
    strict_warmup_convergence_evidence: WarmupConvergenceEnvelope | None = None

    @property
    def strict_source_coverage_evidence(
        self,
    ) -> QmtSectorSameBaseCoverageEvidence | None:
        return self.evidence.sector_strict_same_base_source_coverage_evidence

    def __post_init__(self) -> None:
        if self.source_mode not in {
            QMT_SECTOR_SAME_BASE_SOURCE_MODE,
            QMT_SECTOR_NATIVE_DAILY_RESEARCH_SOURCE_MODE,
        }:
            raise ValueError("invalid sector higher-timeframe source mode")
        if len(self.fallback_unavailable_reason_codes) != len(
            set(self.fallback_unavailable_reason_codes)
        ):
            raise ValueError("sector fallback reason codes must be unique")
        if (
            self.evidence.sector_source_mode != self.source_mode
            or self.evidence.sector_strict_same_base_warmup_evidence
            != self.strict_warmup_evidence
            or self.evidence.sector_strict_same_base_warmup_convergence_evidence
            != self.strict_warmup_convergence_evidence
            or self.strict_source_coverage_evidence is None
        ):
            raise ValueError("sector resolution lost its shared source evidence")
        if self.source_mode == QMT_SECTOR_NATIVE_DAILY_RESEARCH_SOURCE_MODE:
            blocker = (
                "QMT_SECTOR_NATIVE_DAILY_AND_5M_UNRECONCILED_RESEARCH_BRIDGE"
            )
            if self.evidence.gate == "GREEN" or blocker not in self.evidence.reason_codes:
                raise ValueError("sector research fallback lost its AMBER safety cap")


@dataclass(frozen=True, slots=True)
class HigherTimeframeGateBundle:
    market: HigherTimeframeGateEvidence
    sector: HigherTimeframeGateEvidence
    symbol: HigherTimeframeGateEvidence

    @property
    def allows_new_entry(self) -> bool:
        return (
            self.market.allows_new_entry
            and self.sector.allows_new_entry
            and self.symbol.allows_new_entry
        )


def _from_envelope(
    envelope: QmtHigherTimeframeRiskEnvelope,
) -> HigherTimeframeGateEvidence:
    snapshot = envelope.risk.snapshot
    reasons = tuple(dict.fromkeys(value.code for value in envelope.blockers))
    period_bar_counts = {
        period: len(values) for period, values in envelope.risk.period_bars
    }
    period_diagnostics = tuple(
        HigherTimeframePeriodDiagnostic(
            period=value.fact.period,
            state=value.fact.state,
            completed_bar_count=period_bar_counts.get(value.fact.period, 0),
            evidence_bar_end=value.fact.evidence_bar_end,
            active_top_interval=value.active_top_interval,
            mapping_unique=value.fact.mapping_unique,
            mapped_center_id=value.mapped_center_id,
            mapping_candidate_ids=value.mapping_candidate_ids,
            blocker_codes=tuple(
                dict.fromkeys(blocker.code for blocker in value.blockers)
            ),
            warning_codes=tuple(
                dict.fromkeys(warning.code for warning in value.warnings)
            ),
            source_revision=value.fact.source_revision,
            mapping_supply=value.mapping_supply,
        )
        for value in envelope.structure.states
    )
    if snapshot is None:
        return HigherTimeframeGateEvidence(
            subject=envelope.inputs.symbol,
            observed_at=envelope.inputs.observed_at,
            monthly="UNRESOLVED",
            weekly="UNRESOLVED",
            daily="UNRESOLVED",
            gate="UNRESOLVED",
            grade=envelope.grade,
            snapshot_id=sha256_json(
                {
                    "schema": "chanlun-higher-timeframe-unresolved",
                    "source_revision": envelope.inputs.source_revision,
                    "reason_codes": reasons,
                }
            ),
            source_revision=envelope.inputs.source_revision,
            reason_codes=reasons or ("HIGHER_TIMEFRAME_SNAPSHOT_UNRESOLVED",),
            period_diagnostics=period_diagnostics,
            session_evidence=HigherTimeframeSessionEvidence.exact(),
            warmup_evidence=envelope.warmup,
            warmup_convergence_evidence=envelope.warmup_convergence,
            native_daily_reconciliation_evidence=(
                envelope.inputs.native_daily_reconciliation_evidence
            ),
            native_daily_calendar_coverage_evidence=(
                envelope.inputs.native_daily_calendar_coverage_evidence
            ),
        )
    return HigherTimeframeGateEvidence(
        subject=envelope.inputs.symbol,
        observed_at=envelope.inputs.observed_at,
        monthly=snapshot.monthly,
        weekly=snapshot.weekly,
        daily=snapshot.daily,
        gate=snapshot.gate,
        grade=envelope.grade,
        snapshot_id=snapshot.snapshot_id,
        source_revision=envelope.inputs.source_revision,
        reason_codes=reasons,
        period_diagnostics=period_diagnostics,
        session_evidence=HigherTimeframeSessionEvidence.exact(),
        warmup_evidence=envelope.warmup,
        warmup_convergence_evidence=envelope.warmup_convergence,
        native_daily_reconciliation_evidence=(
            envelope.inputs.native_daily_reconciliation_evidence
        ),
        native_daily_calendar_coverage_evidence=(
            envelope.inputs.native_daily_calendar_coverage_evidence
        ),
    )


def higher_timeframe_gate_evidence_from_envelope(
    envelope: QmtHigherTimeframeRiskEnvelope,
) -> HigherTimeframeGateEvidence:
    """Expose the shared, lossless diagnostic projection of a risk envelope."""

    return _from_envelope(envelope)


def _effectiveness_candidate_rows(
    candidate_audit: Sequence[Mapping[str, object]],
) -> tuple[Mapping[str, object], ...]:
    rows: list[Mapping[str, object]] = []
    required = {
        "market_risk_gate",
        "sector_risk_gate",
        "symbol_risk_gate",
    }
    for row in candidate_audit:
        if not isinstance(row, Mapping):
            raise ValueError("candidate audit rows must be mappings")
        if row.get("decision_at") is None or not required.issubset(row):
            continue
        rows.append(row)
    return tuple(rows)


def _risk_evidence_states(
    row: Mapping[str, object],
    *,
    subject: str,
    gate: str,
) -> tuple[
    dict[str, str],
    dict[str, str],
    tuple[str, ...],
    tuple[str, ...],
    str,
    tuple[
        tuple[
            str,
            tuple[str, str] | None,
            RiskMappingSupplyFacts | None,
        ],
        ...,
    ],
]:
    blockers = row.get(f"{subject}_risk_blocker_codes")
    if not isinstance(blockers, (tuple, list)) or any(
        not isinstance(value, str) or not value for value in blockers
    ):
        raise ValueError(f"{subject} risk blocker codes are malformed")
    candidate_blockers = tuple(
        dict.fromkeys(str(value) for value in blockers)
    )
    evidence = row.get(f"{subject}_risk_warmup_evidence")
    if not isinstance(evidence, Mapping):
        raise ValueError(f"{subject} current risk evidence is required")
    states = {
        period: evidence.get(name)
        for period, name in (("M", "monthly"), ("W", "weekly"), ("D", "daily"))
    }
    if any(
        not isinstance(state, str) or state not in _HIGHER_TIMEFRAME_STATES
        for state in states.values()
    ):
        raise ValueError(f"{subject} risk evidence contains an invalid state")

    diagnostics = evidence.get("period_diagnostics")
    period_blockers: list[str] = []
    diagnostic_states: dict[str, str] = {}
    mapping_supplies: list[
        tuple[str, tuple[str, str] | None, RiskMappingSupplyFacts | None]
    ] = []
    if not isinstance(diagnostics, (tuple, list)) or len(diagnostics) != 3:
        raise ValueError(f"{subject} risk evidence must contain M/W/D diagnostics")
    seen_periods: list[str] = []
    for raw in diagnostics:
        try:
            diagnostic = HigherTimeframePeriodDiagnostic.from_document(raw)
        except ValueError as exc:
            raise ValueError(
                f"{subject} period diagnostic is inconsistent: {exc}"
            ) from exc
        active_interval_key = (
            None
            if diagnostic.active_top_interval is None
            else tuple(
                value.isoformat() for value in diagnostic.active_top_interval
            )
        )
        seen_periods.append(diagnostic.period)
        diagnostic_states[diagnostic.period] = diagnostic.state
        period_blockers.extend(diagnostic.blocker_codes)
        mapping_supplies.append(
            (
                diagnostic.period,
                active_interval_key,
                diagnostic.mapping_supply,
            )
        )
    if tuple(seen_periods) != ("M", "W", "D"):
        raise ValueError(f"{subject} period diagnostics must be ordered M/W/D")
    if gate == "UNRESOLVED":
        if set(states.values()) != {"UNRESOLVED"}:
            raise ValueError(
                f"{subject} unresolved gate retained effective M/W/D states"
            )
    elif diagnostic_states != states:
        raise ValueError(
            f"{subject} resolved gate diverges from its period diagnostics"
        )

    warmup = evidence.get("warmup")
    if not isinstance(warmup, Mapping) or not isinstance(
        warmup.get("reason_code"), str
    ):
        raise ValueError(f"{subject} warmup evidence is malformed")
    warmup_reason = str(warmup["reason_code"])
    return (
        {key: str(value) for key, value in states.items()},
        diagnostic_states,
        candidate_blockers,
        tuple(dict.fromkeys(period_blockers)),
        warmup_reason,
        tuple(mapping_supplies),
    )


def higher_timeframe_effectiveness_audit(
    candidate_audit: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Measure what M/W/D gates actually did at candidate decision times.

    The strict strategy contract permits a new entry only when market, sector and
    symbol are all GREEN.  The explicitly labelled research variant admits
    AMBER for human review.  Reporting only aggregate gate counts hid that
    distinction, so this audit reconstructs both populations directly from
    candidate evidence and binds the result with a content identity.
    """

    rows = _effectiveness_candidate_rows(candidate_audit)
    subject_rows: dict[str, dict[str, object]] = {}
    global_point_types: dict[str, str] = {}
    global_identified_point_occurrences = 0
    global_subject_distinct_point_total = 0
    global_diagnostic_point_types: dict[str, str] = {}
    global_identified_diagnostic_point_occurrences = 0
    global_subject_distinct_diagnostic_point_total = 0
    parsed: dict[int, dict[str, tuple[str, dict[str, str]]]] = {
        index: {} for index in range(len(rows))
    }
    for subject in ("market", "sector", "symbol"):
        gate_counts: Counter[str] = Counter()
        blocker_counts: Counter[str] = Counter()
        period_blocker_counts: Counter[str] = Counter()
        warmup_counts: Counter[str] = Counter()
        warmup_convergence_status_counts: Counter[str] = Counter()
        warmup_convergence_observation_counts: list[int] = []
        pairwise_stable_without_all_prefix_stability_count = 0
        warmup_diagnostic_status_counts: Counter[str] = Counter()
        warmup_mapping_supply_diagnostic_status_counts: Counter[str] = Counter()
        warmup_mapping_supply_transition_counts: Counter[str] = Counter()
        warmup_mapping_supply_comparison_period_counts: Counter[str] = Counter()
        warmup_structure_lineage_diagnostic_status_counts: Counter[str] = Counter()
        warmup_structure_lineage_transition_counts: Counter[str] = Counter()
        warmup_structure_lineage_comparison_period_counts: Counter[str] = Counter()
        warmup_changed_period_counts: Counter[str] = Counter()
        warmup_changed_path_counts: Counter[str] = Counter()
        warmup_non_monotonic_points: list[dict[str, object]] = []
        warmup_mapping_supply_points: list[dict[str, object]] = []
        warmup_structure_lineage_points: list[dict[str, object]] = []
        source_mode_counts: Counter[str] = Counter()
        strict_same_base_warmup_counts: Counter[str] = Counter()
        strict_same_base_convergence_status_counts: Counter[str] = Counter()
        strict_same_base_diagnostic_status_counts: Counter[str] = Counter()
        strict_same_base_mapping_supply_diagnostic_status_counts: Counter[
            str
        ] = Counter()
        strict_same_base_structure_lineage_diagnostic_status_counts: Counter[
            str
        ] = Counter()
        strict_same_base_boundary_counts: Counter[str] = Counter()
        strict_same_base_physical_boundary_counts: Counter[str] = Counter()
        strict_same_base_completed_daily_counts: list[int] = []
        strict_same_base_remaining_daily_counts: list[int] = []
        strict_same_base_leading_gap_counts: list[int] = []
        strict_same_base_physical_representative_counts: list[int] = []
        strict_same_base_physical_available_counts: list[int] = []
        strict_same_base_physical_required_counts: list[int] = []
        strict_same_base_physical_requested_starts: list[datetime] = []
        strict_same_base_physical_required_starts: list[datetime] = []
        native_daily_calendar_status_counts: Counter[str] = Counter()
        native_daily_calendar_missing_counts: list[int] = []
        native_daily_calendar_gap_rows: list[dict[str, object]] = []
        mapping_supply_class_counts: Counter[str] = Counter()
        mapping_point_type_counts: Counter[str] = Counter()
        diagnostic_directional_class_counts: Counter[str] = Counter()
        diagnostic_buy_point_type_counts: Counter[str] = Counter()
        mapping_supply_totals: Counter[str] = Counter()
        event_exposures: dict[
            tuple[str, str, str, str],
            list[tuple[datetime, str, RiskMappingSupplyFacts]],
        ] = {}
        period_counts = {period: Counter() for period in ("M", "W", "D")}
        effective_period_counts = {
            period: Counter() for period in ("M", "W", "D")
        }
        resolved_evidence_count = 0
        state_override_candidate_count = 0
        for index, row in enumerate(rows):
            raw_decision = row.get("decision_at")
            try:
                decision_at = normalize_datetime(
                    (
                        raw_decision
                        if isinstance(raw_decision, datetime)
                        else datetime.fromisoformat(str(raw_decision))
                    ),
                    "candidate_decision_at",
                )
            except (TypeError, ValueError) as exc:
                raise ValueError("candidate decision time is malformed") from exc
            gate = row.get(f"{subject}_risk_gate")
            if not isinstance(gate, str) or gate not in _HIGHER_TIMEFRAME_GATES:
                raise ValueError(f"{subject} candidate gate is invalid")
            (
                effective_states,
                diagnostic_states,
                blockers,
                period_blockers,
                warmup_reason,
                mapping_supplies,
            ) = _risk_evidence_states(row, subject=subject, gate=gate)
            parsed[index][subject] = (gate, effective_states)
            gate_counts[gate] += 1
            blocker_counts.update(blockers)
            period_blocker_counts.update(period_blockers)
            warmup_counts[warmup_reason] += 1
            raw_risk_evidence = row.get(
                f"{subject}_risk_warmup_evidence"
            )
            mapping_supply_comparisons: dict[
                tuple[int, str], dict[str, object]
            ] = {}
            mapping_supply_diagnostic_content_sha256: str | None = None
            raw_convergence = (
                None
                if not isinstance(raw_risk_evidence, Mapping)
                else raw_risk_evidence.get("warmup_convergence")
            )
            if raw_convergence is None:
                raise ValueError(
                    f"{subject} current warmup convergence evidence is required"
                )
            else:
                if not isinstance(raw_convergence, Mapping):
                    raise ValueError(
                        f"{subject} warmup convergence evidence is malformed"
                    )
                convergence = WarmupConvergenceEnvelope.from_document(
                    raw_convergence
                )
                if (
                    convergence.as_of != decision_at
                    or convergence.frequency != "d"
                    or convergence.parameter_set_id
                    != QMT_HIGHER_TIMEFRAME_WARMUP_CONVERGENCE_PARAMETER_SET_ID
                ):
                    raise ValueError(
                        f"{subject} warmup convergence identity changed"
                    )
                warmup_convergence_status_counts[convergence.status] += 1
                warmup_convergence_observation_counts.append(
                    len(convergence.observations)
                )
                pairwise_stable_without_all_prefix_stability_count += int(
                    warmup_reason
                    == "QMT_HIGHER_TIMEFRAME_WARMUP_TAIL_STABLE"
                    and not convergence.stable_all_prefixes
                )
                raw_diagnostic = raw_risk_evidence.get(
                    "warmup_convergence_diagnostic"
                )
                if raw_diagnostic is None:
                    raise ValueError(
                        f"{subject} current warmup semantic diagnostic is required"
                    )
                else:
                    if not isinstance(raw_diagnostic, Mapping):
                        raise ValueError(
                            f"{subject} warmup semantic diagnostic is malformed"
                        )
                    semantic_diagnostic = (
                        WarmupConvergenceDiagnosticEnvelope.from_document(
                            raw_diagnostic
                        )
                    )
                    semantic_diagnostic.validate_against(convergence)
                    warmup_diagnostic_status_counts[
                        semantic_diagnostic.status
                    ] += 1
                    raw_supply_diagnostic = raw_risk_evidence.get(
                        "warmup_mapping_supply_diagnostic"
                    )
                    if raw_supply_diagnostic is None:
                        raise ValueError(
                            f"{subject} current mapping supply diagnostic is required"
                        )
                    else:
                        if not isinstance(raw_supply_diagnostic, Mapping):
                            raise ValueError(
                                f"{subject} warmup mapping supply is malformed"
                            )
                        supply_diagnostic = (
                            WarmupMappingSupplyDiagnosticEnvelope.from_document(
                                raw_supply_diagnostic
                            )
                        )
                        supply_diagnostic.validate_against(
                            replace(convergence, diagnostic=semantic_diagnostic)
                        )
                        mapping_supply_diagnostic_content_sha256 = (
                            supply_diagnostic.content_sha256
                        )
                        warmup_mapping_supply_diagnostic_status_counts[
                            supply_diagnostic.status
                        ] += 1
                        for comparison in supply_diagnostic.comparisons:
                            document = comparison.document()
                            key = (
                                comparison.prefix_bar_count,
                                comparison.period,
                            )
                            mapping_supply_comparisons[key] = document
                            warmup_mapping_supply_comparison_period_counts[
                                comparison.period
                            ] += 1
                            delta = document["delta"]
                            if not isinstance(delta, Mapping):
                                raise ValueError(
                                    "mapping supply comparison delta changed"
                                )
                            warmup_mapping_supply_transition_counts.update(
                                str(value)
                                for value in delta.get("transition_codes", [])
                            )
                        raw_lineage_diagnostic = raw_risk_evidence.get(
                            "warmup_structure_lineage_diagnostic"
                        )
                        if raw_lineage_diagnostic is None:
                            raise ValueError(
                                f"{subject} current structure lineage diagnostic is required"
                            )
                        else:
                            if not isinstance(raw_lineage_diagnostic, Mapping):
                                raise ValueError(
                                    f"{subject} warmup structure lineage is malformed"
                                )
                            lineage_diagnostic = (
                                WarmupStructureLineageDiagnosticEnvelope.from_document(
                                    raw_lineage_diagnostic
                                )
                            )
                            bound_convergence = replace(
                                convergence,
                                diagnostic=semantic_diagnostic,
                                mapping_supply_diagnostic=supply_diagnostic,
                            )
                            lineage_diagnostic.validate_against(bound_convergence)
                            warmup_structure_lineage_diagnostic_status_counts[
                                lineage_diagnostic.status
                            ] += 1
                            for lineage_comparison in lineage_diagnostic.comparisons:
                                lineage_document = lineage_comparison.document()
                                warmup_structure_lineage_comparison_period_counts[
                                    lineage_comparison.period
                                ] += 1
                                lineage_delta = lineage_document.get("delta")
                                if not isinstance(lineage_delta, Mapping):
                                    raise ValueError(
                                        "structure lineage comparison delta changed"
                                    )
                                transition_codes = tuple(
                                    str(value)
                                    for value in lineage_delta.get(
                                        "transition_codes", []
                                    )
                                )
                                warmup_structure_lineage_transition_counts.update(
                                    transition_codes
                                )
                                prefix_snapshot = lineage_document.get(
                                    "prefix_snapshot"
                                )
                                reference_snapshot = lineage_document.get(
                                    "reference_snapshot"
                                )
                                if not isinstance(
                                    prefix_snapshot, Mapping
                                ) or not isinstance(reference_snapshot, Mapping):
                                    continue
                                prefix_centers = {
                                    str(value.get("center_id")): value
                                    for value in prefix_snapshot.get("centers", [])
                                    if isinstance(value, Mapping)
                                }
                                reference_centers = {
                                    str(value.get("center_id")): value
                                    for value in reference_snapshot.get("centers", [])
                                    if isinstance(value, Mapping)
                                }
                                prefix_points = {
                                    str(point.get("point_id")): point
                                    for value in prefix_snapshot.get("points", [])
                                    if isinstance(value, Mapping)
                                    and isinstance(value.get("point"), Mapping)
                                    for point in (value["point"],)
                                }
                                for role in lineage_delta.get(
                                    "point_trigger_role_changes", []
                                ):
                                    if not isinstance(role, Mapping):
                                        raise ValueError(
                                            "structure lineage point role changed"
                                        )
                                    prefix_center_id = str(
                                        role.get("prefix_center_id") or ""
                                    )
                                    peer_center_id = role.get(
                                        "reference_peer_center_id"
                                    )
                                    point_id = str(role.get("point_id") or "")
                                    point_evidence = prefix_points.get(point_id, {})
                                    source_symbol = str(
                                        prefix_snapshot.get("source_symbol") or ""
                                    )
                                    source_frequency = str(
                                        prefix_snapshot.get("source_frequency")
                                        or ""
                                    )
                                    chart_interval = (
                                        _RISK_POINT_CHART_INTERVALS.get(
                                            source_frequency
                                        )
                                    )
                                    chart_supported = bool(
                                        subject != "sector"
                                        and chart_interval is not None
                                        and _A_SHARE_CHART_SYMBOL.fullmatch(
                                            source_symbol
                                        )
                                        is not None
                                    )
                                    # Reuse the mapping-supply audit identity so
                                    # the lineage row opens the exact same causal
                                    # chart lock.  ``role.point_id`` is the
                                    # structural point identity, not the chart-row
                                    # identity accepted by
                                    # ``validate_risk_point_chart_lock``.
                                    chart_lock_point_id = sha256_json(
                                        {
                                            "schema": (
                                                "chanlun-warmup-mapping-"
                                                "supply-chart-point"
                                            ),
                                            "subject": subject,
                                            "review_as_of": decision_at,
                                            "prefix_bar_count": (
                                                lineage_comparison.prefix_bar_count
                                            ),
                                            "period": lineage_comparison.period,
                                            "direction": "LOST_FROM_LONGEST",
                                            "structural_point_id": point_id,
                                            "diagnostic_sha256": (
                                                lineage_diagnostic.mapping_supply_diagnostic_content_sha256
                                            ),
                                        }
                                    )
                                    warmup_structure_lineage_points.append(
                                        {
                                            "subject": subject,
                                            "review_as_of": decision_at.isoformat(),
                                            "review_as_of_unix": int(
                                                decision_at.timestamp()
                                            ),
                                            "period": lineage_comparison.period,
                                            "prefix_bar_count": (
                                                lineage_comparison.prefix_bar_count
                                            ),
                                            "reference_bar_count": (
                                                lineage_comparison.reference_bar_count
                                            ),
                                            "source_symbol": source_symbol,
                                            "source_frequency": source_frequency,
                                            "prefix_source_content_sha256": (
                                                prefix_snapshot.get(
                                                    "source_content_sha256"
                                                )
                                            ),
                                            "reference_source_content_sha256": (
                                                reference_snapshot.get(
                                                    "source_content_sha256"
                                                )
                                            ),
                                            "diagnostic_content_sha256": (
                                                lineage_diagnostic.content_sha256
                                            ),
                                            "mapping_supply_diagnostic_content_sha256": (
                                                lineage_diagnostic.mapping_supply_diagnostic_content_sha256
                                            ),
                                            "transition_codes": list(
                                                transition_codes
                                            ),
                                            "line_sequences": list(
                                                lineage_delta.get(
                                                    "line_sequences", []
                                                )
                                            ),
                                            **dict(role),
                                            "point_id": chart_lock_point_id,
                                            "structural_point_id": point_id,
                                            "point_available_at": (
                                                point_evidence.get(
                                                    "point_available_at"
                                                )
                                            ),
                                            "point_anchor_unix": int(
                                                datetime.fromisoformat(
                                                    str(role["point_anchor_at"])
                                                ).timestamp()
                                            ),
                                            "point_available_unix": (
                                                None
                                                if point_evidence.get(
                                                    "point_available_at"
                                                )
                                                is None
                                                else int(
                                                    datetime.fromisoformat(
                                                        str(
                                                            point_evidence[
                                                                "point_available_at"
                                                            ]
                                                        )
                                                    ).timestamp()
                                                )
                                            ),
                                            "prefix_center": prefix_centers.get(
                                                prefix_center_id
                                            ),
                                            "reference_peer_center": (
                                                None
                                                if peer_center_id is None
                                                else reference_centers.get(
                                                    str(peer_center_id)
                                                )
                                            ),
                                            "chart_interval": chart_interval,
                                            "chart_focus_supported": chart_supported,
                                        }
                                    )
                    if semantic_diagnostic.status == "NON_MONOTONIC":
                        source_symbol = (
                            "SH.000001"
                            if subject == "market"
                            else str(
                                row.get(
                                    "sector_id"
                                    if subject == "sector"
                                    else "symbol"
                                )
                                or ""
                            )
                        )
                        if (
                            subject == "sector"
                            and not source_symbol.startswith("qmt-gics3:")
                        ) or (
                            subject != "sector"
                            and _A_SHARE_CHART_SYMBOL.fullmatch(source_symbol)
                            is None
                        ):
                            raise ValueError(
                                f"{subject} warmup diagnostic source is invalid"
                            )
                        diagnostic_document = semantic_diagnostic.document()
                        reference = diagnostic_document["observations"][-1]
                        if not isinstance(reference, Mapping):
                            raise ValueError("warmup reference diagnostic changed")
                        reference_snapshot = reference.get("snapshot")
                        if not isinstance(reference_snapshot, Mapping):
                            raise ValueError("warmup reference snapshot changed")
                        reference_periods = {
                            str(value.get("period")): value
                            for value in reference_snapshot.get("periods", [])
                            if isinstance(value, Mapping)
                        }
                        reference_ma5 = {
                            str(value.get("period")): value.get("value")
                            for value in reference_snapshot.get("ma5", [])
                            if isinstance(value, Mapping)
                        }
                        for observation in diagnostic_document["observations"]:
                            if not isinstance(observation, Mapping):
                                raise ValueError("warmup diagnostic row changed")
                            changed_paths = tuple(
                                str(value)
                                for value in observation.get(
                                    "changed_paths_from_longest", []
                                )
                            )
                            if not changed_paths:
                                continue
                            snapshot = observation.get("snapshot")
                            if not isinstance(snapshot, Mapping):
                                raise ValueError("warmup semantic snapshot changed")
                            candidate_periods = {
                                str(value.get("period")): value
                                for value in snapshot.get("periods", [])
                                if isinstance(value, Mapping)
                            }
                            candidate_ma5 = {
                                str(value.get("period")): value.get("value")
                                for value in snapshot.get("ma5", [])
                                if isinstance(value, Mapping)
                            }
                            changed_by_period: dict[str, list[str]] = {
                                period: [] for period in ("M", "W", "D")
                            }
                            for path in changed_paths:
                                period, separator, _field = path.partition(".")
                                if not separator or period not in changed_by_period:
                                    raise ValueError(
                                        "warmup semantic changed path is invalid"
                                    )
                                changed_by_period[period].append(path)
                                warmup_changed_path_counts[path] += 1
                            for period, period_paths in changed_by_period.items():
                                if not period_paths:
                                    continue
                                candidate_period = candidate_periods.get(period)
                                reference_period = reference_periods.get(period)
                                if not isinstance(candidate_period, Mapping) or not isinstance(
                                    reference_period, Mapping
                                ):
                                    raise ValueError(
                                        "warmup semantic period snapshot changed"
                                    )
                                anchors: list[datetime] = []
                                for raw_anchor in (
                                    candidate_period.get("evidence_bar_end"),
                                    reference_period.get("evidence_bar_end"),
                                ):
                                    if raw_anchor is not None:
                                        anchors.append(
                                            normalize_datetime(
                                                datetime.fromisoformat(
                                                    str(raw_anchor)
                                                ),
                                                "warmup_point_anchor",
                                            )
                                        )
                                anchor = max(anchors) if anchors else decision_at
                                if anchor > decision_at:
                                    raise ValueError(
                                        "warmup point anchor follows decision"
                                    )
                                point_id = sha256_json(
                                    {
                                        "schema": (
                                            "chanlun-warmup-non-monotonic-"
                                            "chart-point"
                                        ),
                                        "subject": subject,
                                        "source_symbol": source_symbol,
                                        "review_as_of": decision_at,
                                        "diagnostic_sha256": (
                                            semantic_diagnostic.content_sha256
                                        ),
                                        "prefix_bar_count": observation[
                                            "bar_count"
                                        ],
                                        "period": period,
                                    }
                                )
                                supply_comparison = mapping_supply_comparisons.get(
                                    (int(observation["bar_count"]), period)
                                )
                                warmup_changed_period_counts[period] += 1
                                warmup_non_monotonic_points.append(
                                    {
                                        "point_id": point_id,
                                        "subject": subject,
                                        "source_symbol": source_symbol,
                                        "source_frequency": period.lower(),
                                        "chart_interval": {
                                            "M": "1M",
                                            "W": "1W",
                                            "D": "1D",
                                        }[period],
                                        "chart_focus_supported": (
                                            subject != "sector"
                                        ),
                                        "point_type": "WARMUP_DIFF",
                                        "point_anchor_at": anchor.isoformat(),
                                        "point_anchor_unix": int(
                                            anchor.timestamp()
                                        ),
                                        "point_available_at": (
                                            decision_at.isoformat()
                                        ),
                                        "point_available_unix": int(
                                            decision_at.timestamp()
                                        ),
                                        "review_as_of": decision_at.isoformat(),
                                        "review_as_of_unix": int(
                                            decision_at.timestamp()
                                        ),
                                        "prefix_bar_count": observation[
                                            "bar_count"
                                        ],
                                        "prefix_starts_at": observation[
                                            "starts_at"
                                        ],
                                        "reference_bar_count": reference[
                                            "bar_count"
                                        ],
                                        "reference_starts_at": reference[
                                            "starts_at"
                                        ],
                                        "prefix_signature_sha256": observation[
                                            "signature_sha256"
                                        ],
                                        "reference_signature_sha256": reference[
                                            "signature_sha256"
                                        ],
                                        "changed_paths": period_paths,
                                        "prefix_period_facts": dict(
                                            candidate_period
                                        ),
                                        "reference_period_facts": dict(
                                            reference_period
                                        ),
                                        "prefix_ma5": candidate_ma5.get(
                                            period
                                        ),
                                        "reference_ma5": reference_ma5.get(
                                            period
                                        ),
                                        "mapping_supply_comparison": (
                                            None
                                            if supply_comparison is None
                                            else dict(supply_comparison)
                                        ),
                                        "envelope_content_sha256": (
                                            convergence.content_sha256
                                        ),
                                        "diagnostic_content_sha256": (
                                            semantic_diagnostic.content_sha256
                                        ),
                                        "diagnostic_only": True,
                                        "active_gate_unchanged": True,
                                    }
                                )
                                if supply_comparison is not None:
                                    delta = supply_comparison.get("delta")
                                    if not isinstance(delta, Mapping):
                                        raise ValueError(
                                            "warmup mapping supply delta changed"
                                        )
                                    for direction, field_name in (
                                        (
                                            "LOST_FROM_LONGEST",
                                            "lost_points_from_longest",
                                        ),
                                        (
                                            "GAINED_IN_LONGEST",
                                            "gained_points_in_longest",
                                        ),
                                    ):
                                        raw_points = delta.get(field_name, [])
                                        if not isinstance(raw_points, list):
                                            raise ValueError(
                                                "mapping supply point delta changed"
                                            )
                                        for raw_point in raw_points:
                                            if not isinstance(raw_point, Mapping):
                                                raise ValueError(
                                                    "mapping supply point changed"
                                                )
                                            structural_point = (
                                                RiskMappingPointEvidenceFacts.from_document(
                                                    raw_point
                                                )
                                            )
                                            if (
                                                structural_point.point_available_at
                                                > decision_at
                                            ):
                                                raise ValueError(
                                                    "warmup supply point follows decision"
                                                )
                                            chart_interval = (
                                                _RISK_POINT_CHART_INTERVALS.get(
                                                    structural_point.source_frequency
                                                )
                                            )
                                            chart_supported = bool(
                                                subject != "sector"
                                                and chart_interval is not None
                                                and _A_SHARE_CHART_SYMBOL.fullmatch(
                                                    structural_point.source_symbol
                                                )
                                                is not None
                                            )
                                            audit_point_id = sha256_json(
                                                {
                                                    "schema": (
                                                        "chanlun-warmup-mapping-"
                                                        "supply-chart-point"
                                                    ),
                                                    "subject": subject,
                                                    "review_as_of": decision_at,
                                                    "prefix_bar_count": observation[
                                                        "bar_count"
                                                    ],
                                                    "period": period,
                                                    "direction": direction,
                                                    "structural_point_id": (
                                                        structural_point.point_id
                                                    ),
                                                    "diagnostic_sha256": (
                                                        mapping_supply_diagnostic_content_sha256
                                                    ),
                                                }
                                            )
                                            warmup_mapping_supply_points.append(
                                                {
                                                    "point_id": audit_point_id,
                                                    "structural_point_id": (
                                                        structural_point.point_id
                                                    ),
                                                    "subject": subject,
                                                    "source_symbol": (
                                                        structural_point.source_symbol
                                                    ),
                                                    "source_frequency": (
                                                        structural_point.source_frequency
                                                    ),
                                                    "chart_interval": chart_interval,
                                                    "chart_focus_supported": (
                                                        chart_supported
                                                    ),
                                                    "point_type": (
                                                        structural_point.point_type
                                                    ),
                                                    "point_anchor_at": (
                                                        structural_point.point_anchor_at.isoformat()
                                                    ),
                                                    "point_anchor_unix": int(
                                                        structural_point.point_anchor_at.timestamp()
                                                    ),
                                                    "point_available_at": (
                                                        structural_point.point_available_at.isoformat()
                                                    ),
                                                    "point_available_unix": int(
                                                        structural_point.point_available_at.timestamp()
                                                    ),
                                                    "review_as_of": (
                                                        decision_at.isoformat()
                                                    ),
                                                    "review_as_of_unix": int(
                                                        decision_at.timestamp()
                                                    ),
                                                    "center_id": (
                                                        structural_point.center_id
                                                    ),
                                                    "center_level_rank": (
                                                        structural_point.center_level_rank
                                                    ),
                                                    "center_completed": (
                                                        structural_point.center_completed
                                                    ),
                                                    "center_expanded": (
                                                        structural_point.center_expanded
                                                    ),
                                                    "inside_active_top_interval": (
                                                        structural_point.inside_active_top_interval
                                                    ),
                                                    "highest_mapping_candidate": (
                                                        structural_point.highest_mapping_candidate
                                                    ),
                                                    "delta_direction": direction,
                                                    "prefix_period": period,
                                                    "prefix_bar_count": (
                                                        observation["bar_count"]
                                                    ),
                                                    "prefix_starts_at": (
                                                        observation["starts_at"]
                                                    ),
                                                    "reference_bar_count": (
                                                        reference["bar_count"]
                                                    ),
                                                    "reference_starts_at": (
                                                        reference["starts_at"]
                                                    ),
                                                    "mapping_supply_diagnostic_content_sha256": (
                                                        mapping_supply_diagnostic_content_sha256
                                                    ),
                                                    "diagnostic_only": True,
                                                    "active_gate_unchanged": True,
                                                }
                                            )
            raw_native_calendar = row.get(
                f"{subject}_risk_native_daily_calendar_coverage_evidence"
            )
            if subject == "sector":
                if raw_native_calendar is not None:
                    raise ValueError(
                        "sector risk cannot claim stock native-daily calendar evidence"
                    )
                native_daily_calendar_status_counts["NOT_APPLICABLE"] += 1
            elif raw_native_calendar is None:
                raise ValueError(
                    f"{subject} current native-daily calendar evidence is required"
                )
            else:
                if not isinstance(raw_native_calendar, Mapping):
                    raise ValueError(
                        f"{subject} native-daily calendar evidence is malformed"
                    )
                calendar_coverage = (
                    QmtNativeDailyCalendarCoverageEvidence.from_document(
                        raw_native_calendar
                    )
                )
                if (
                    calendar_coverage.observed_at != decision_at
                    or (
                        subject == "symbol"
                        and calendar_coverage.symbol != row.get("symbol")
                    )
                ):
                    raise ValueError(
                        f"{subject} native-daily calendar evidence changed identity"
                    )
                mismatch = calendar_coverage.status != "EXACT"
                if mismatch != (
                    "QMT_NATIVE_DAILY_TRADING_CALENDAR_MISMATCH" in blockers
                    and gate == "UNRESOLVED"
                ):
                    raise ValueError(
                        f"{subject} native-daily calendar evidence contradicts gate"
                    )
                native_daily_calendar_status_counts[
                    calendar_coverage.status
                ] += 1
                missing_count = len(
                    calendar_coverage.unexplained_calendar_only_sessions
                )
                native_daily_calendar_missing_counts.append(missing_count)
                if mismatch:
                    native_daily_calendar_gap_rows.append(
                        {
                            "symbol": calendar_coverage.symbol,
                            "observed_at": decision_at.isoformat(),
                            "status": calendar_coverage.status,
                            "native_only_sessions": [
                                value.isoformat()
                                for value in calendar_coverage.native_only_sessions
                            ],
                            "unexplained_calendar_only_sessions": [
                                value.isoformat()
                                for value in (
                                    calendar_coverage.unexplained_calendar_only_sessions
                                )
                            ],
                            "coverage_revision": (
                                calendar_coverage.coverage_revision
                            ),
                            "entry_disposition": "FAIL_CLOSED",
                        }
                    )
            for period, active_interval, supply in mapping_supplies:
                if supply is None:
                    continue
                if active_interval is None:
                    raise ValueError("mapping supply lost its active top interval")
                mapping_supply_totals["active_top_period_count"] += 1
                mapping_supply_class_counts[supply.classification] += 1
                mapping_point_type_counts.update(dict(supply.point_type_counts))
                diagnostic_directional_class_counts[
                    supply.diagnostic_directional_classification
                ] += 1
                diagnostic_buy_point_type_counts.update(
                    dict(supply.diagnostic_buy_point_type_counts)
                )
                mapping_supply_totals[
                    "diagnostic_buy_point_evidence_count"
                ] += sum(
                    count
                    for _name, count in supply.diagnostic_buy_point_type_counts
                )
                mapping_supply_totals[
                    "diagnostic_buy_point_identified_occurrence_count"
                ] += len(supply.diagnostic_buy_point_evidence)
                mapping_supply_totals["point_evidence_count"] += (
                    supply.point_evidence_count
                )
                mapping_supply_totals["completed_sell12_count"] += (
                    supply.completed_sell12_count
                )
                mapping_supply_totals["in_top_interval_sell12_count"] += (
                    supply.in_top_interval_sell12_count
                )
                mapping_supply_totals[
                    "completed_in_top_interval_sell12_count"
                ] += supply.completed_in_top_interval_sell12_count
                if datetime.fromisoformat(active_interval[1]) > decision_at:
                    raise ValueError("active top interval exceeds candidate decision")
                if subject == "market":
                    event_subject = "MARKET"
                else:
                    raw_subject = row.get(
                        "sector_id" if subject == "sector" else "symbol"
                    )
                    if not isinstance(raw_subject, str) or not raw_subject:
                        raise ValueError(
                            f"{subject} active mapping event lost its identity"
                        )
                    event_subject = raw_subject
                event_key = (
                    event_subject,
                    period,
                    active_interval[0],
                    active_interval[1],
                )
                supply_id = sha256_json(supply.document())
                event_exposures.setdefault(event_key, []).append(
                    (decision_at, supply_id, supply)
                )
            if subject == "sector":
                raw_evidence = row.get("sector_risk_warmup_evidence")
                if not isinstance(raw_evidence, Mapping):
                    raise ValueError("sector source evidence must be a mapping")
                else:
                    source_mode = raw_evidence.get("source_mode")
                    if source_mode not in {
                        QMT_SECTOR_SAME_BASE_SOURCE_MODE,
                        QMT_SECTOR_NATIVE_DAILY_RESEARCH_SOURCE_MODE,
                    }:
                        raise ValueError("sector source mode is invalid")
                    strict_warmup = raw_evidence.get(
                        "strict_same_5m_warmup"
                    )
                    if not isinstance(strict_warmup, Mapping):
                        raise ValueError(
                            "sector strict same-base warmup evidence is missing"
                        )
                    strict_reason = strict_warmup.get("reason_code")
                    strict_converged = strict_warmup.get("converged")
                    if (
                        not isinstance(strict_reason, str)
                        or type(strict_converged) is not bool
                    ):
                        raise ValueError(
                            "sector strict same-base warmup evidence is malformed"
                        )
                    raw_coverage = raw_evidence.get(
                        "strict_same_5m_source_coverage"
                    )
                    if not isinstance(raw_coverage, Mapping):
                        raise ValueError(
                            "sector strict same-base source coverage is missing"
                        )
                    coverage = QmtSectorSameBaseCoverageEvidence.from_document(
                        raw_coverage
                    )
                    strict_full_count = strict_warmup.get(
                        "full_daily_bar_count"
                    )
                    strict_required_count = strict_warmup.get(
                        "required_daily_bar_count"
                    )
                    if (
                        coverage.observed_at != decision_at
                        or type(strict_full_count) is not int
                        or type(strict_required_count) is not int
                        or coverage.completed_daily_bar_count
                        < strict_full_count
                        or coverage.required_daily_bar_count
                        != strict_required_count
                        or coverage.warmup_converged != strict_converged
                        or coverage.warmup_reason_code != strict_reason
                    ):
                        raise ValueError(
                            "sector strict source coverage contradicts warmup"
                        )
                    if source_mode == QMT_SECTOR_NATIVE_DAILY_RESEARCH_SOURCE_MODE:
                        if (
                            strict_converged
                            or strict_reason
                            != "QMT_HIGHER_TIMEFRAME_WARMUP_HISTORY_INSUFFICIENT"
                            or gate == "GREEN"
                        ):
                            raise ValueError(
                                "sector research bridge lost its strict warmup boundary"
                            )
                    elif not strict_converged:
                        raise ValueError(
                            "strict same-base sector source did not converge"
                        )
                    source_mode_counts[str(source_mode)] += 1
                    strict_same_base_warmup_counts[str(strict_reason)] += 1
                    raw_strict_convergence = raw_evidence.get(
                        "strict_same_5m_warmup_convergence"
                    )
                    raw_strict_diagnostic = raw_evidence.get(
                        "strict_same_5m_warmup_convergence_diagnostic"
                    )
                    raw_strict_supply_diagnostic = raw_evidence.get(
                        "strict_same_5m_warmup_mapping_supply_diagnostic"
                    )
                    raw_strict_lineage_diagnostic = raw_evidence.get(
                        "strict_same_5m_warmup_structure_lineage_diagnostic"
                    )
                    if raw_strict_convergence is None:
                        raise ValueError(
                            "sector current strict convergence evidence is required"
                        )
                    else:
                        if not isinstance(raw_strict_convergence, Mapping):
                            raise ValueError(
                                "sector strict warmup convergence is malformed"
                            )
                        strict_convergence = (
                            WarmupConvergenceEnvelope.from_document(
                                raw_strict_convergence
                            )
                        )
                        if (
                            strict_convergence.as_of != decision_at
                            or strict_convergence.parameter_set_id
                            != QMT_HIGHER_TIMEFRAME_WARMUP_CONVERGENCE_PARAMETER_SET_ID
                        ):
                            raise ValueError(
                                "sector strict convergence identity changed"
                            )
                        strict_same_base_convergence_status_counts[
                            strict_convergence.status
                        ] += 1
                        if raw_strict_diagnostic is None:
                            raise ValueError(
                                "sector current strict semantic diagnostic is required"
                            )
                        else:
                            if not isinstance(
                                raw_strict_diagnostic, Mapping
                            ):
                                raise ValueError(
                                    "sector strict warmup semantic diagnostic "
                                    "is malformed"
                                )
                            strict_diagnostic = (
                                WarmupConvergenceDiagnosticEnvelope.from_document(
                                    raw_strict_diagnostic
                                )
                            )
                            strict_diagnostic.validate_against(
                                strict_convergence
                            )
                            strict_same_base_diagnostic_status_counts[
                                strict_diagnostic.status
                            ] += 1
                            if raw_strict_supply_diagnostic is None:
                                raise ValueError(
                                    "sector current strict mapping supply diagnostic is required"
                                )
                            else:
                                if not isinstance(
                                    raw_strict_supply_diagnostic, Mapping
                                ):
                                    raise ValueError(
                                        "sector strict mapping supply diagnostic "
                                        "is malformed"
                                    )
                                strict_supply_diagnostic = (
                                    WarmupMappingSupplyDiagnosticEnvelope.from_document(
                                        raw_strict_supply_diagnostic
                                    )
                                )
                                strict_supply_diagnostic.validate_against(
                                    replace(
                                        strict_convergence,
                                        diagnostic=strict_diagnostic,
                                    )
                                )
                                strict_same_base_mapping_supply_diagnostic_status_counts[
                                    strict_supply_diagnostic.status
                                ] += 1
                                if raw_strict_lineage_diagnostic is None:
                                    raise ValueError(
                                        "sector current strict structure lineage diagnostic is required"
                                    )
                                else:
                                    if not isinstance(
                                        raw_strict_lineage_diagnostic, Mapping
                                    ):
                                        raise ValueError(
                                            "sector strict structure lineage is malformed"
                                        )
                                    strict_lineage_diagnostic = (
                                        WarmupStructureLineageDiagnosticEnvelope.from_document(
                                            raw_strict_lineage_diagnostic
                                        )
                                    )
                                    strict_lineage_diagnostic.validate_against(
                                        replace(
                                            strict_convergence,
                                            diagnostic=strict_diagnostic,
                                            mapping_supply_diagnostic=(
                                                strict_supply_diagnostic
                                            ),
                                        )
                                    )
                                    strict_same_base_structure_lineage_diagnostic_status_counts[
                                        strict_lineage_diagnostic.status
                                    ] += 1
                    strict_same_base_boundary_counts[
                        coverage.boundary_status
                    ] += 1
                    strict_same_base_physical_boundary_counts[
                        coverage.physical_source_boundary_status
                    ] += 1
                    strict_same_base_completed_daily_counts.append(
                        coverage.completed_daily_bar_count
                    )
                    strict_same_base_remaining_daily_counts.append(
                        coverage.remaining_daily_bar_count
                    )
                    strict_same_base_leading_gap_counts.append(
                        coverage.missing_leading_calendar_session_count
                    )
                    if (
                        coverage.physical_source_representative_member_count
                        is not None
                    ):
                        strict_same_base_physical_representative_counts.append(
                            coverage.physical_source_representative_member_count
                        )
                    if (
                        coverage.physical_source_available_member_count
                        is not None
                    ):
                        strict_same_base_physical_available_counts.append(
                            coverage.physical_source_available_member_count
                        )
                    if (
                        coverage.physical_source_required_contributor_count
                        is not None
                    ):
                        strict_same_base_physical_required_counts.append(
                            coverage.physical_source_required_contributor_count
                        )
                    if (
                        coverage.physical_source_requested_start_at
                        is not None
                    ):
                        strict_same_base_physical_requested_starts.append(
                            coverage.physical_source_requested_start_at
                        )
                    if (
                        coverage.physical_source_required_contributor_start_at
                        is not None
                    ):
                        strict_same_base_physical_required_starts.append(
                            coverage.physical_source_required_contributor_start_at
                        )
            for period, state in diagnostic_states.items():
                period_counts[period][state] += 1
            for period, state in effective_states.items():
                effective_period_counts[period][state] += 1
            state_override_candidate_count += int(
                diagnostic_states != effective_states
            )
            if gate != "UNRESOLVED":
                resolved_evidence_count += 1
        event_terminal_class_counts: Counter[str] = Counter()
        event_terminal_point_type_counts: Counter[str] = Counter()
        event_terminal_directional_class_counts: Counter[str] = Counter()
        event_terminal_buy_point_type_counts: Counter[str] = Counter()
        event_terminal_totals: Counter[str] = Counter()
        terminal_point_observations: dict[
            str, list[tuple[datetime, RiskMappingPointEvidenceFacts]]
        ] = {}
        terminal_diagnostic_point_observations: dict[
            str, list[tuple[datetime, RiskDiagnosticBuyPointEvidenceFacts]]
        ] = {}
        distinct_retained_snapshots: set[
            tuple[tuple[str, str, str, str], str]
        ] = set()
        evolving_event_count = 0
        max_event_references = 0
        for event_key, exposures in event_exposures.items():
            max_event_references = max(max_event_references, len(exposures))
            retained_ids = {value[1] for value in exposures}
            distinct_retained_snapshots.update(
                (event_key, value) for value in retained_ids
            )
            evolving_event_count += int(len(retained_ids) > 1)
            latest_at = max(value[0] for value in exposures)
            latest = tuple(value for value in exposures if value[0] == latest_at)
            latest_ids = {value[1] for value in latest}
            if len(latest_ids) != 1:
                raise ValueError(
                    "one active top event has conflicting supply at the same decision time"
                )
            terminal_supply = latest[0][2]
            event_terminal_totals["retained_terminal_supply_event_count"] += 1
            event_terminal_class_counts[terminal_supply.classification] += 1
            event_terminal_point_type_counts.update(
                dict(terminal_supply.point_type_counts)
            )
            event_terminal_directional_class_counts[
                terminal_supply.diagnostic_directional_classification
            ] += 1
            event_terminal_buy_point_type_counts.update(
                dict(terminal_supply.diagnostic_buy_point_type_counts)
            )
            event_terminal_totals[
                "diagnostic_buy_point_evidence_count"
            ] += sum(
                count
                for _name, count in (
                    terminal_supply.diagnostic_buy_point_type_counts
                )
            )
            event_terminal_totals["point_evidence_count"] += (
                terminal_supply.point_evidence_count
            )
            event_interval = (
                datetime.fromisoformat(event_key[2]),
                datetime.fromisoformat(event_key[3]),
            )
            for point in terminal_supply.point_evidence:
                if point.point_available_at > latest_at:
                    raise ValueError(
                        "risk point evidence was unavailable at candidate decision"
                    )
                expected_inside = (
                    event_interval[0]
                    <= point.point_anchor_at
                    <= event_interval[1]
                )
                if point.inside_active_top_interval != expected_inside:
                    raise ValueError(
                        "risk point interval membership contradicts active top event"
                    )
                if subject == "symbol" and point.source_symbol != event_key[0]:
                    raise ValueError(
                        "symbol risk point evidence changed subject identity"
                    )
                terminal_point_observations.setdefault(point.point_id, []).append(
                    (latest_at, point)
                )
            for point in terminal_supply.diagnostic_buy_point_evidence:
                if point.point_available_at > latest_at:
                    raise ValueError(
                        "diagnostic buy point was unavailable at candidate decision"
                    )
                expected_inside = (
                    event_interval[0]
                    <= point.point_anchor_at
                    <= event_interval[1]
                )
                if point.inside_active_top_interval != expected_inside:
                    raise ValueError(
                        "diagnostic buy point interval membership contradicts "
                        "active top event"
                    )
                if subject == "symbol" and point.source_symbol != event_key[0]:
                    raise ValueError(
                        "symbol diagnostic buy point changed subject identity"
                    )
                terminal_diagnostic_point_observations.setdefault(
                    point.point_id, []
                ).append((latest_at, point))
            event_terminal_totals["completed_sell12_count"] += (
                terminal_supply.completed_sell12_count
            )
            event_terminal_totals["in_top_interval_sell12_count"] += (
                terminal_supply.in_top_interval_sell12_count
            )
            event_terminal_totals[
                "completed_in_top_interval_sell12_count"
            ] += terminal_supply.completed_in_top_interval_sell12_count
        identified_point_occurrences = sum(
            len(values) for values in terminal_point_observations.values()
        )
        if identified_point_occurrences != event_terminal_totals.get(
            "point_evidence_count", 0
        ):
            raise ValueError("risk point identities do not cover aggregate supply")
        global_identified_point_occurrences += identified_point_occurrences
        point_rows: list[dict[str, object]] = []
        distinct_point_type_counts: Counter[str] = Counter()
        source_frequency_counts: Counter[str] = Counter()
        source_symbols: set[str] = set()
        chart_focus_supported_count = 0
        for point_id, observations in terminal_point_observations.items():
            first = observations[0][1]
            static_identity = (
                first.source_symbol,
                first.source_frequency,
                first.center_id,
                first.center_level_rank,
                first.point_type,
                first.point_anchor_at,
                first.point_available_at,
            )
            if any(
                (
                    value.source_symbol,
                    value.source_frequency,
                    value.center_id,
                    value.center_level_rank,
                    value.point_type,
                    value.point_anchor_at,
                    value.point_available_at,
                )
                != static_identity
                for _observed_at, value in observations
            ):
                raise ValueError("one risk point identity has conflicting static evidence")
            latest_at = max(value[0] for value in observations)
            latest_values = tuple(
                value for observed_at, value in observations if observed_at == latest_at
            )
            latest_states = {
                (value.center_completed, value.center_expanded)
                for value in latest_values
            }
            if len(latest_states) != 1:
                raise ValueError(
                    "one risk point has conflicting center state at the same decision"
                )
            latest = latest_values[0]
            chart_interval = _RISK_POINT_CHART_INTERVALS.get(
                latest.source_frequency
            )
            chart_supported = bool(
                chart_interval
                and _A_SHARE_CHART_SYMBOL.fullmatch(latest.source_symbol)
            )
            chart_focus_supported_count += int(chart_supported)
            distinct_point_type_counts[latest.point_type] += 1
            source_frequency_counts[latest.source_frequency] += 1
            source_symbols.add(latest.source_symbol)
            previous_type = global_point_types.setdefault(
                point_id, latest.point_type
            )
            if previous_type != latest.point_type:
                raise ValueError("global risk point identity changed point type")
            point_rows.append(
                {
                    "point_id": point_id,
                    "source_symbol": latest.source_symbol,
                    "source_frequency": latest.source_frequency,
                    "chart_interval": chart_interval,
                    "chart_focus_supported": chart_supported,
                    "center_id": latest.center_id,
                    "center_level_rank": latest.center_level_rank,
                    "center_completed_latest": latest.center_completed,
                    "center_expanded_latest": latest.center_expanded,
                    "point_type": latest.point_type,
                    "point_anchor_at": latest.point_anchor_at.isoformat(),
                    "point_anchor_unix": int(latest.point_anchor_at.timestamp()),
                    "point_available_at": latest.point_available_at.isoformat(),
                    "point_available_unix": int(
                        latest.point_available_at.timestamp()
                    ),
                    "review_as_of": latest_at.isoformat(),
                    "review_as_of_unix": int(latest_at.timestamp()),
                    "terminal_event_reference_count": len(observations),
                    "inside_active_top_event_count": sum(
                        value.inside_active_top_interval
                        for _observed_at, value in observations
                    ),
                    "highest_mapping_candidate_event_count": sum(
                        value.highest_mapping_candidate
                        for _observed_at, value in observations
                    ),
                }
            )
        point_rows.sort(
            key=lambda value: (
                str(value["source_symbol"]),
                str(value["source_frequency"]),
                str(value["point_anchor_at"]),
                str(value["point_id"]),
            )
        )
        global_subject_distinct_point_total += len(point_rows)
        globally_deduplicated_point_audit = {
            "counting_basis": (
                "DISTINCT_STABLE_POINT_ID_ACROSS_TERMINAL_ACTIVE_TOP_EVENTS"
            ),
            "identity_contract_id": "chanlun-risk-mapping-point-identity",
            "terminal_event_point_occurrence_count": event_terminal_totals.get(
                "point_evidence_count", 0
            ),
            "identified_terminal_point_occurrence_count": (
                identified_point_occurrences
            ),
            "distinct_point_id_count": len(point_rows),
            "repeated_terminal_point_occurrence_count": (
                identified_point_occurrences - len(point_rows)
            ),
            "distinct_point_type_counts": {
                point_type: distinct_point_type_counts.get(point_type, 0)
                for point_type in ("1sell", "2sell", "3sell", "3buy")
            },
            "distinct_source_symbol_count": len(source_symbols),
            "source_frequency_counts": dict(sorted(source_frequency_counts.items())),
            "chart_focus_supported_point_count": chart_focus_supported_count,
            "chart_focus_unavailable_point_count": (
                len(point_rows) - chart_focus_supported_count
            ),
            "points": point_rows,
        }
        identified_diagnostic_point_occurrences = sum(
            len(values)
            for values in terminal_diagnostic_point_observations.values()
        )
        diagnostic_identity_count_difference = (
            event_terminal_totals.get("diagnostic_buy_point_evidence_count", 0)
            - identified_diagnostic_point_occurrences
        )
        if diagnostic_identity_count_difference != 0:
            raise ValueError(
                "diagnostic buy-point identities do not cover aggregate supply"
            )
        global_identified_diagnostic_point_occurrences += (
            identified_diagnostic_point_occurrences
        )
        diagnostic_point_rows: list[dict[str, object]] = []
        distinct_diagnostic_point_type_counts: Counter[str] = Counter()
        diagnostic_source_frequency_counts: Counter[str] = Counter()
        diagnostic_source_symbols: set[str] = set()
        diagnostic_chart_focus_supported_count = 0
        for point_id, observations in terminal_diagnostic_point_observations.items():
            first = observations[0][1]
            static_identity = (
                first.source_symbol,
                first.source_frequency,
                first.center_id,
                first.center_level_rank,
                first.point_type,
                first.point_anchor_at,
                first.point_available_at,
            )
            if any(
                (
                    value.source_symbol,
                    value.source_frequency,
                    value.center_id,
                    value.center_level_rank,
                    value.point_type,
                    value.point_anchor_at,
                    value.point_available_at,
                )
                != static_identity
                for _observed_at, value in observations
            ):
                raise ValueError(
                    "one diagnostic buy point identity has conflicting static evidence"
                )
            latest_at = max(value[0] for value in observations)
            latest_values = tuple(
                value
                for observed_at, value in observations
                if observed_at == latest_at
            )
            latest_states = {
                (value.center_completed, value.center_expanded)
                for value in latest_values
            }
            if len(latest_states) != 1:
                raise ValueError(
                    "one diagnostic buy point has conflicting center state at the "
                    "same decision"
                )
            latest = latest_values[0]
            chart_interval = _RISK_POINT_CHART_INTERVALS.get(
                latest.source_frequency
            )
            chart_supported = bool(
                chart_interval
                and _A_SHARE_CHART_SYMBOL.fullmatch(latest.source_symbol)
            )
            diagnostic_chart_focus_supported_count += int(chart_supported)
            distinct_diagnostic_point_type_counts[latest.point_type] += 1
            diagnostic_source_frequency_counts[latest.source_frequency] += 1
            diagnostic_source_symbols.add(latest.source_symbol)
            previous_type = global_diagnostic_point_types.setdefault(
                point_id, latest.point_type
            )
            if previous_type != latest.point_type:
                raise ValueError(
                    "global diagnostic buy point identity changed point type"
                )
            diagnostic_point_rows.append(
                {
                    "point_id": point_id,
                    "source_symbol": latest.source_symbol,
                    "source_frequency": latest.source_frequency,
                    "chart_interval": chart_interval,
                    "chart_focus_supported": chart_supported,
                    "center_id": latest.center_id,
                    "center_level_rank": latest.center_level_rank,
                    "center_completed_latest": latest.center_completed,
                    "center_expanded_latest": latest.center_expanded,
                    "point_type": latest.point_type,
                    "point_anchor_at": latest.point_anchor_at.isoformat(),
                    "point_anchor_unix": int(latest.point_anchor_at.timestamp()),
                    "point_available_at": latest.point_available_at.isoformat(),
                    "point_available_unix": int(
                        latest.point_available_at.timestamp()
                    ),
                    "review_as_of": latest_at.isoformat(),
                    "review_as_of_unix": int(latest_at.timestamp()),
                    "terminal_event_reference_count": len(observations),
                    "inside_active_top_event_count": sum(
                        value.inside_active_top_interval
                        for _observed_at, value in observations
                    ),
                    "diagnostic_only": True,
                    "mapping_eligible": False,
                }
            )
        diagnostic_point_rows.sort(
            key=lambda value: (
                str(value["source_symbol"]),
                str(value["source_frequency"]),
                str(value["point_anchor_at"]),
                str(value["point_id"]),
            )
        )
        global_subject_distinct_diagnostic_point_total += len(
            diagnostic_point_rows
        )
        globally_deduplicated_diagnostic_buy_point_audit = {
            "counting_basis": (
                "DISTINCT_STABLE_DIAGNOSTIC_BUY_POINT_ID_ACROSS_TERMINAL_"
                "ACTIVE_TOP_EVENTS"
            ),
            "identity_contract_id": (
                "chanlun-risk-diagnostic-buy-point-identity"
            ),
            "terminal_event_point_occurrence_count": (
                event_terminal_totals.get(
                    "diagnostic_buy_point_evidence_count", 0
                )
            ),
            "identified_terminal_point_occurrence_count": (
                identified_diagnostic_point_occurrences
            ),
            "distinct_point_id_count": len(diagnostic_point_rows),
            "repeated_terminal_point_occurrence_count": (
                identified_diagnostic_point_occurrences
                - len(diagnostic_point_rows)
            ),
            "distinct_point_type_counts": {
                point_type: distinct_diagnostic_point_type_counts.get(
                    point_type, 0
                )
                for point_type in ("1buy", "2buy")
            },
            "distinct_source_symbol_count": len(diagnostic_source_symbols),
            "source_frequency_counts": dict(
                sorted(diagnostic_source_frequency_counts.items())
            ),
            "chart_focus_supported_point_count": (
                diagnostic_chart_focus_supported_count
            ),
            "chart_focus_unavailable_point_count": (
                len(diagnostic_point_rows)
                - diagnostic_chart_focus_supported_count
            ),
            "diagnostic_only": True,
            "mapping_eligible": False,
            "points": diagnostic_point_rows,
        }
        unique_event_audit = {
            "counting_basis": (
                "DISTINCT_SUBJECT_PERIOD_ACTIVE_TOP_INTERVAL_LATEST_OBSERVATION"
            ),
            "candidate_period_exposure_count": sum(
                len(values) for values in event_exposures.values()
            ),
            "distinct_active_top_event_count": len(event_exposures),
            "repeated_candidate_exposure_count": (
                sum(len(values) for values in event_exposures.values())
                - len(event_exposures)
            ),
            "distinct_retained_supply_snapshot_count": len(
                distinct_retained_snapshots
            ),
            "events_with_evolving_supply_count": evolving_event_count,
            "max_candidate_references_to_one_event": max_event_references,
            "terminal_mapping_supply_class_counts": dict(
                sorted(event_terminal_class_counts.items())
            ),
            "terminal_mapping_point_type_counts": {
                point_type: event_terminal_point_type_counts.get(point_type, 0)
                for point_type in ("1sell", "2sell", "3sell", "3buy")
            },
            "terminal_diagnostic_directional_class_counts": dict(
                sorted(event_terminal_directional_class_counts.items())
            ),
            "terminal_diagnostic_buy_point_type_counts": {
                point_type: event_terminal_buy_point_type_counts.get(
                    point_type, 0
                )
                for point_type in ("1buy", "2buy")
            },
            "terminal_mapping_supply_totals": {
                key: event_terminal_totals.get(key, 0)
                for key in (
                    "retained_terminal_supply_event_count",
                    "point_evidence_count",
                    "completed_sell12_count",
                    "in_top_interval_sell12_count",
                    "completed_in_top_interval_sell12_count",
                    "diagnostic_buy_point_evidence_count",
                )
            },
            "point_count_disclosure": (
                "SUM_OF_LATEST_PER_EVENT_EVIDENCE_OCCURRENCES_NOT_"
                "GLOBALLY_DEDUPLICATED_POINT_IDENTITIES"
            ),
        }
        subject_rows[subject] = {
            "candidate_count": len(rows),
            "gate_counts": dict(sorted(gate_counts.items())),
            "distinct_gate_count": len(gate_counts),
            "all_amber": bool(rows) and gate_counts == Counter({"AMBER": len(rows)}),
            "resolved_evidence_count": resolved_evidence_count,
            "period_state_counts": {
                period: dict(sorted(counts.items()))
                for period, counts in period_counts.items()
            },
            "effective_period_state_counts": {
                period: dict(sorted(counts.items()))
                for period, counts in effective_period_counts.items()
            },
            "state_override_candidate_count": state_override_candidate_count,
            "formed_unresolved_period_count": sum(
                counts.get("FORMED_UNRESOLVED", 0)
                for counts in period_counts.values()
            ),
            "blocker_candidate_counts": dict(sorted(blocker_counts.items())),
            "period_blocker_counts": dict(
                sorted(period_blocker_counts.items())
            ),
            "warmup_reason_counts": dict(sorted(warmup_counts.items())),
            "warmup_convergence": {
                "contract_id": WARMUP_CONVERGENCE_ENVELOPE_CONTRACT_ID,
                "parameter_set_id": (
                    QMT_HIGHER_TIMEFRAME_WARMUP_CONVERGENCE_PARAMETER_SET_ID
                ),
                "status_counts": dict(
                    sorted(warmup_convergence_status_counts.items())
                ),
                "evidence_candidate_count": len(
                    warmup_convergence_observation_counts
                ),
                "qualified_prefix_count_range": (
                    None
                    if not warmup_convergence_observation_counts
                    else {
                        "minimum": min(warmup_convergence_observation_counts),
                        "maximum": max(warmup_convergence_observation_counts),
                    }
                ),
                "pairwise_stable_without_all_prefix_stability_count": (
                    pairwise_stable_without_all_prefix_stability_count
                ),
                "strict_same_base_status_counts": dict(
                    sorted(strict_same_base_convergence_status_counts.items())
                ),
                "strict_same_base_semantic_diagnostic_status_counts": dict(
                    sorted(strict_same_base_diagnostic_status_counts.items())
                ),
                "diagnostic_only": True,
                "active_gate_unchanged": True,
                "semantic_diagnostic_contract_id": (
                    WARMUP_CONVERGENCE_DIAGNOSTIC_CONTRACT_ID
                ),
                "semantic_diagnostic_status_counts": dict(
                    sorted(warmup_diagnostic_status_counts.items())
                ),
                "mapping_supply_diagnostic_contract_id": (
                    WARMUP_MAPPING_SUPPLY_DIAGNOSTIC_CONTRACT_ID
                ),
                "mapping_supply_diagnostic_status_counts": dict(
                    sorted(
                        warmup_mapping_supply_diagnostic_status_counts.items()
                    )
                ),
                "mapping_supply_comparison_period_counts": dict(
                    sorted(
                        warmup_mapping_supply_comparison_period_counts.items()
                    )
                ),
                "mapping_supply_transition_code_counts": dict(
                    sorted(warmup_mapping_supply_transition_counts.items())
                ),
                "strict_same_base_mapping_supply_diagnostic_status_counts": dict(
                    sorted(
                        strict_same_base_mapping_supply_diagnostic_status_counts.items()
                    )
                ),
                "structure_lineage_diagnostic_contract_id": (
                    WARMUP_STRUCTURE_LINEAGE_DIAGNOSTIC_CONTRACT_ID
                ),
                "structure_lineage_diagnostic_status_counts": dict(
                    sorted(
                        warmup_structure_lineage_diagnostic_status_counts.items()
                    )
                ),
                "structure_lineage_comparison_period_counts": dict(
                    sorted(
                        warmup_structure_lineage_comparison_period_counts.items()
                    )
                ),
                "structure_lineage_transition_code_counts": dict(
                    sorted(warmup_structure_lineage_transition_counts.items())
                ),
                "strict_same_base_structure_lineage_diagnostic_status_counts": dict(
                    sorted(
                        strict_same_base_structure_lineage_diagnostic_status_counts.items()
                    )
                ),
                "non_monotonic_changed_period_counts": dict(
                    sorted(warmup_changed_period_counts.items())
                ),
                "non_monotonic_changed_path_counts": dict(
                    sorted(warmup_changed_path_counts.items())
                ),
            },
            "warmup_non_monotonic_point_audit": {
                "schema": "chanlun-warmup-non-monotonic-point-audit",
                "counting_basis": (
                    "NON_LONGEST_PREFIX_PERIOD_DIFFERENCE_OCCURRENCES"
                ),
                "point_count": len(warmup_non_monotonic_points),
                "distinct_point_id_count": len(
                    {value["point_id"] for value in warmup_non_monotonic_points}
                ),
                "distinct_source_symbol_count": len(
                    {
                        value["source_symbol"]
                        for value in warmup_non_monotonic_points
                    }
                ),
                "chart_focus_supported_point_count": sum(
                    value["chart_focus_supported"] is True
                    for value in warmup_non_monotonic_points
                ),
                "chart_focus_unavailable_point_count": sum(
                    value["chart_focus_supported"] is not True
                    for value in warmup_non_monotonic_points
                ),
                "changed_period_counts": dict(
                    sorted(warmup_changed_period_counts.items())
                ),
                "changed_path_counts": dict(
                    sorted(warmup_changed_path_counts.items())
                ),
                "points": sorted(
                    warmup_non_monotonic_points,
                    key=lambda value: (
                        str(value["review_as_of"]),
                        str(value["source_symbol"]),
                        int(value["prefix_bar_count"]),
                        str(value["source_frequency"]),
                    ),
                ),
                "diagnostic_only": True,
                "active_gate_unchanged": True,
            },
            "warmup_mapping_supply_point_audit": {
                "schema": "chanlun-warmup-mapping-supply-point-audit",
                "counting_basis": (
                    "PREFIX_VS_LONGEST_LOST_AND_GAINED_STABLE_POINT_IDENTITIES"
                ),
                "point_count": len(warmup_mapping_supply_points),
                "distinct_point_id_count": len(
                    {value["point_id"] for value in warmup_mapping_supply_points}
                ),
                "distinct_structural_point_id_count": len(
                    {
                        value["structural_point_id"]
                        for value in warmup_mapping_supply_points
                    }
                ),
                "delta_direction_counts": dict(
                    sorted(
                        Counter(
                            str(value["delta_direction"])
                            for value in warmup_mapping_supply_points
                        ).items()
                    )
                ),
                "point_type_counts": dict(
                    sorted(
                        Counter(
                            str(value["point_type"])
                            for value in warmup_mapping_supply_points
                        ).items()
                    )
                ),
                "chart_focus_supported_point_count": sum(
                    value["chart_focus_supported"] is True
                    for value in warmup_mapping_supply_points
                ),
                "chart_focus_unavailable_point_count": sum(
                    value["chart_focus_supported"] is not True
                    for value in warmup_mapping_supply_points
                ),
                "points": sorted(
                    warmup_mapping_supply_points,
                    key=lambda value: (
                        str(value["review_as_of"]),
                        str(value["source_symbol"]),
                        int(value["prefix_bar_count"]),
                        str(value["delta_direction"]),
                        str(value["structural_point_id"]),
                    ),
                ),
                "diagnostic_only": True,
                "active_gate_unchanged": True,
            },
            "warmup_structure_lineage_point_audit": {
                "schema": "chanlun-warmup-structure-lineage-point-audit",
                "counting_basis": (
                    "LOST_PREFIX_POINT_TO_BEST_LONGEST_PREFIX_CENTER_ROLE_CHANGES"
                ),
                "point_count": len(warmup_structure_lineage_points),
                "distinct_point_id_count": len(
                    {
                        value["point_id"]
                        for value in warmup_structure_lineage_points
                    }
                ),
                "same_core_interval_count": sum(
                    value["same_core_interval"] is True
                    for value in warmup_structure_lineage_points
                ),
                "one_line_phase_shift_count": sum(
                    value["one_line_phase_shift"] is True
                    for value in warmup_structure_lineage_points
                ),
                "sell_trigger_absorbed_count": sum(
                    value["point_type"] in {"1sell", "2sell"}
                    and value["prefix_trigger_role"] == "AFTER_CENTER"
                    and value["reference_trigger_role"]
                    == "CENTER_CONSTITUENT"
                    for value in warmup_structure_lineage_points
                ),
                "points": sorted(
                    warmup_structure_lineage_points,
                    key=lambda value: (
                        str(value["review_as_of"]),
                        str(value["source_symbol"]),
                        int(value["prefix_bar_count"]),
                        str(value["point_id"]),
                    ),
                ),
                "diagnostic_only": True,
                "active_gate_unchanged": True,
                "live_status": "LIVE_DISABLED",
            },
            "native_daily_calendar_coverage": {
                "contract_id": (
                    QMT_NATIVE_DAILY_CALENDAR_COVERAGE_EVIDENCE_CONTRACT_ID
                ),
                "status_counts": dict(
                    sorted(native_daily_calendar_status_counts.items())
                ),
                "evidence_candidate_count": sum(
                    count
                    for status, count in native_daily_calendar_status_counts.items()
                    if status != "NOT_APPLICABLE"
                ),
                "unexplained_missing_session_occurrence_count": sum(
                    native_daily_calendar_missing_counts
                ),
                "unexplained_missing_session_count_range": (
                    None
                    if not native_daily_calendar_missing_counts
                    else {
                        "minimum": min(native_daily_calendar_missing_counts),
                        "maximum": max(native_daily_calendar_missing_counts),
                    }
                ),
                "gap_rows": native_daily_calendar_gap_rows,
                "missing_session_interpretation": (
                    "UNEXPLAINED_NEVER_INFERRED_AS_SUSPENSION"
                ),
                "diagnostic_only": True,
                "decision_gate_unchanged": True,
            },
            "source_mode_counts": dict(sorted(source_mode_counts.items())),
            "strict_same_base_warmup_reason_counts": dict(
                sorted(strict_same_base_warmup_counts.items())
            ),
            "strict_same_base_source_boundary_counts": dict(
                sorted(strict_same_base_boundary_counts.items())
            ),
            "strict_same_base_physical_source_boundary_counts": dict(
                sorted(strict_same_base_physical_boundary_counts.items())
            ),
            "strict_same_base_completed_daily_bar_range": (
                None
                if not strict_same_base_completed_daily_counts
                else {
                    "minimum": min(strict_same_base_completed_daily_counts),
                    "maximum": max(strict_same_base_completed_daily_counts),
                }
            ),
            "strict_same_base_remaining_daily_bar_range": (
                None
                if not strict_same_base_remaining_daily_counts
                else {
                    "minimum": min(strict_same_base_remaining_daily_counts),
                    "maximum": max(strict_same_base_remaining_daily_counts),
                }
            ),
            "strict_same_base_leading_calendar_gap_range": (
                None
                if not strict_same_base_leading_gap_counts
                else {
                    "minimum": min(strict_same_base_leading_gap_counts),
                    "maximum": max(strict_same_base_leading_gap_counts),
                }
            ),
            "strict_same_base_physical_representative_member_range": (
                None
                if not strict_same_base_physical_representative_counts
                else {
                    "minimum": min(
                        strict_same_base_physical_representative_counts
                    ),
                    "maximum": max(
                        strict_same_base_physical_representative_counts
                    ),
                }
            ),
            "strict_same_base_physical_available_member_range": (
                None
                if not strict_same_base_physical_available_counts
                else {
                    "minimum": min(strict_same_base_physical_available_counts),
                    "maximum": max(strict_same_base_physical_available_counts),
                }
            ),
            "strict_same_base_physical_required_member_range": (
                None
                if not strict_same_base_physical_required_counts
                else {
                    "minimum": min(strict_same_base_physical_required_counts),
                    "maximum": max(strict_same_base_physical_required_counts),
                }
            ),
            "strict_same_base_physical_requested_start_range": (
                None
                if not strict_same_base_physical_requested_starts
                else {
                    "minimum": min(
                        strict_same_base_physical_requested_starts
                    ).isoformat(),
                    "maximum": max(
                        strict_same_base_physical_requested_starts
                    ).isoformat(),
                }
            ),
            "strict_same_base_physical_required_start_range": (
                None
                if not strict_same_base_physical_required_starts
                else {
                    "minimum": min(
                        strict_same_base_physical_required_starts
                    ).isoformat(),
                    "maximum": max(
                        strict_same_base_physical_required_starts
                    ).isoformat(),
                }
            ),
            "mapping_supply_class_counts": dict(
                sorted(mapping_supply_class_counts.items())
            ),
            "mapping_point_type_counts": {
                point_type: mapping_point_type_counts.get(point_type, 0)
                for point_type in ("1sell", "2sell", "3sell", "3buy")
            },
            "diagnostic_directional_class_counts": dict(
                sorted(diagnostic_directional_class_counts.items())
            ),
            "diagnostic_buy_point_type_counts": {
                point_type: diagnostic_buy_point_type_counts.get(point_type, 0)
                for point_type in ("1buy", "2buy")
            },
            "mapping_supply_totals": {
                key: mapping_supply_totals.get(key, 0)
                for key in (
                    "active_top_period_count",
                    "point_evidence_count",
                    "completed_sell12_count",
                    "in_top_interval_sell12_count",
                    "completed_in_top_interval_sell12_count",
                    "diagnostic_buy_point_evidence_count",
                    "diagnostic_buy_point_identified_occurrence_count",
                )
            },
            "mapping_supply_counting_basis": (
                "CANDIDATE_PERIOD_EXPOSURES_WITH_REPEATED_EVENT_REFERENCES"
            ),
            "unique_active_top_event_audit": unique_event_audit,
            "globally_deduplicated_point_audit": (
                globally_deduplicated_point_audit
            ),
            "globally_deduplicated_diagnostic_buy_point_audit": (
                globally_deduplicated_diagnostic_buy_point_audit
            ),
        }

    strict_green = 0
    research_green_or_amber = 0
    hard_rejected = 0
    unresolved_rejected = 0
    red_rejected = 0
    exact_green = 0
    accepted = 0
    for index, row in enumerate(rows):
        gates = tuple(parsed[index][subject][0] for subject in ("market", "sector", "symbol"))
        strict = all(value == "GREEN" for value in gates)
        research = all(value in {"GREEN", "AMBER"} for value in gates)
        has_unresolved = "UNRESOLVED" in gates
        has_red = "RED" in gates
        strict_green += int(strict)
        research_green_or_amber += int(research)
        hard_rejected += int(not research)
        unresolved_rejected += int(has_unresolved)
        red_rejected += int(has_red)
        if not isinstance(row.get("exact_green"), bool) or not isinstance(
            row.get("accepted"), bool
        ):
            raise ValueError("candidate exact_green/accepted evidence is required")
        if not isinstance(row.get("sector_eligible"), bool) or not isinstance(
            row.get("sector_hard_block"), bool
        ):
            raise ValueError(
                "candidate sector trigger/hard-block evidence is required"
            )
        expected_exact_green = (
            strict
            and bool(row["sector_eligible"])
            and not bool(row["sector_hard_block"])
        )
        if bool(row["exact_green"]) != expected_exact_green:
            raise ValueError("candidate exact-green evidence is inconsistent")
        exact_green += int(bool(row["exact_green"]))
        accepted += int(bool(row["accepted"]))

    if strict_green == 0 and research_green_or_amber > 0:
        status = "STRICT_GREEN_EMPTY_RESEARCH_AMBER_ONLY"
    elif strict_green > 0:
        status = "STRICT_GREEN_PRESENT"
    elif rows:
        status = "NO_RISK_ELIGIBLE_CANDIDATE"
    else:
        status = "NO_CANDIDATE_RISK_EVIDENCE"
    stable: dict[str, object] = {
        "schema": HIGHER_TIMEFRAME_EFFECTIVENESS_AUDIT_SCHEMA,
        "status": status,
        "candidate_count": len(rows),
        "subjects": subject_rows,
        "strict_green_risk_eligible_count": strict_green,
        "research_green_or_amber_risk_eligible_count": research_green_or_amber,
        "research_amber_only_risk_eligible_count": (
            research_green_or_amber - strict_green
        ),
        "hard_rejected_candidate_count": hard_rejected,
        "unresolved_rejected_candidate_count": unresolved_rejected,
        "red_rejected_candidate_count": red_rejected,
        "exact_green_candidate_count": exact_green,
        "accepted_candidate_count": accepted,
        "global_point_identity_audit": {
            "counting_basis": (
                "DISTINCT_STABLE_POINT_ID_ACROSS_ALL_SUBJECT_TERMINAL_EVENTS"
            ),
            "identified_terminal_point_occurrence_count": (
                global_identified_point_occurrences
            ),
            "sum_of_subject_distinct_point_counts": (
                global_subject_distinct_point_total
            ),
            "distinct_point_id_count": len(global_point_types),
            "cross_subject_repeated_point_id_count": (
                global_subject_distinct_point_total - len(global_point_types)
            ),
            "distinct_point_type_counts": {
                point_type: sum(
                    value == point_type for value in global_point_types.values()
                )
                for point_type in ("1sell", "2sell", "3sell", "3buy")
            },
        },
        "global_diagnostic_buy_point_identity_audit": {
            "counting_basis": (
                "DISTINCT_STABLE_DIAGNOSTIC_BUY_POINT_ID_ACROSS_ALL_SUBJECT_"
                "TERMINAL_EVENTS"
            ),
            "identity_contract_id": (
                "chanlun-risk-diagnostic-buy-point-identity"
            ),
            "identified_terminal_point_occurrence_count": (
                global_identified_diagnostic_point_occurrences
            ),
            "sum_of_subject_distinct_point_counts": (
                global_subject_distinct_diagnostic_point_total
            ),
            "distinct_point_id_count": len(global_diagnostic_point_types),
            "cross_subject_repeated_point_id_count": (
                global_subject_distinct_diagnostic_point_total
                - len(global_diagnostic_point_types)
            ),
            "distinct_point_type_counts": {
                point_type: sum(
                    value == point_type
                    for value in global_diagnostic_point_types.values()
                )
                for point_type in ("1buy", "2buy")
            },
            "diagnostic_only": True,
            "mapping_eligible": False,
        },
        "strict_contract_allows_amber": False,
        "research_variant_allows_amber": True,
        "strict_contract": "MARKET_SECTOR_SYMBOL_ALL_GREEN_FOR_NEW_ENTRY",
        "research_variant_contract": (
            "GREEN_OR_AMBER_FOR_HUMAN_ASSISTED_RESEARCH_ONLY"
        ),
        "disclosures": [
            "AMBER prohibits new entry under the frozen strict strategy specification",
            "the current replay admits AMBER only inside the explicit human-assisted research variant",
            "period states and blockers are counted at candidate decision times, not inferred from terminal results",
            "when safety logic removes a decision snapshot, effective states become UNRESOLVED while raw period diagnostics remain visible",
            "mapping supply counts explain the frozen first/second-sell mapping and never promote third-class points",
            "first/second-buy evidence is diagnostic-only directional supply with a separate stable identity contract and never enters the sell mapping, risk gate, stable mapping-point identity, or order decision",
            "candidate-period mapping counts measure gate exposure and may repeat one active top event across candidates",
            "unique active-top event counts use each subject/period/fractal interval once and retain its latest observed supply",
            "per-event terminal point totals remain occurrence counts; the separate stable-point audit de-duplicates them across top events and subjects",
            "chart focus is offered only for a stable point with an A-share/index symbol and a causal review cutoff",
            "a native-daily calendar gap remains unexplained and fails closed unless separately captured point-in-time trade-status evidence proves a lawful absence; a missing bar is never inferred to be a suspension",
            "physical QMT member-file boundaries are diagnostic-only and distinguish a vendor-cache left boundary from caller-side replay clipping; they never relax the 480-session warmup gate",
        ],
    }
    return {**stable, "audit_sha256": sha256_json(stable)}


def _sector_five_minute_closes(session: date) -> tuple[datetime, ...]:
    return tuple(
        datetime.combine(
            session,
            time(hour=minute // 60, minute=minute % 60),
            tzinfo=ZoneInfo("Asia/Shanghai"),
        )
        for start, end in (
            (9 * 60 + 35, 11 * 60 + 30),
            (13 * 60 + 5, 15 * 60),
        )
        for minute in range(start, end + 1, 5)
    )


def build_qmt_sector_same_base_coverage_evidence(
    *,
    five_minute_frame: pd.DataFrame,
    observed_at: datetime,
    trading_sessions: Sequence[date],
    warmup_evidence: QmtHigherTimeframeWarmupEvidence,
) -> QmtSectorSameBaseCoverageEvidence:
    """Explain the exact causal history boundary behind a strict sector gate."""

    observed = normalize_datetime(observed_at, "observed_at")
    if not isinstance(five_minute_frame, pd.DataFrame) or five_minute_frame.empty:
        raise ValueError("sector 5m coverage requires a non-empty frame")
    if "date" not in five_minute_frame:
        raise ValueError("sector 5m coverage lost its date column")
    work = five_minute_frame.loc[:, ["date"]].copy()
    work["date"] = pd.to_datetime(work["date"], errors="raise")
    if work["date"].dt.tz is None:
        raise ValueError("sector 5m coverage requires timezone-aware bars")
    work["date"] = work["date"].dt.tz_convert("Asia/Shanghai")
    work = work[work["date"] <= pd.Timestamp(observed)].reset_index(drop=True)
    if (
        work.empty
        or work["date"].duplicated().any()
        or not work["date"].is_monotonic_increasing
    ):
        raise ValueError("sector 5m coverage prefix is invalid")

    visible_calendar = tuple(
        value for value in trading_sessions if value <= observed.date()
    )
    if (
        not visible_calendar
        or visible_calendar != tuple(sorted(set(visible_calendar)))
        or any(type(value) is not date for value in visible_calendar)
    ):
        raise ValueError("sector 5m coverage calendar is invalid")

    completed_sessions: list[date] = []
    for session, rows in work.groupby(work["date"].dt.date, sort=True):
        actual = tuple(
            pd.Timestamp(value).to_pydatetime()
            for value in rows.sort_values("date", kind="stable")["date"]
        )
        if actual == _sector_five_minute_closes(session):
            completed_sessions.append(session)
    # ``warmup_evidence`` may intentionally evaluate only the configured tail
    # (for example 120 of 130 physically visible sessions in a small adapter
    # test).  Coverage describes the actual causal source boundary, so it may
    # be larger than that evaluated tail but can never be smaller.
    if len(completed_sessions) < warmup_evidence.full_daily_bar_count:
        raise ValueError("sector 5m coverage diverges from strict warmup evidence")

    first_completed = completed_sessions[0] if completed_sessions else None
    last_completed = completed_sessions[-1] if completed_sessions else None
    missing_leading = (
        len(visible_calendar)
        if first_completed is None
        else sum(session < first_completed for session in visible_calendar)
    )
    if warmup_evidence.converged:
        boundary_status = "REQUIRED_HISTORY_CONVERGED"
    elif (
        warmup_evidence.full_daily_bar_count
        >= warmup_evidence.required_daily_bar_count
    ):
        boundary_status = "REQUIRED_HISTORY_PRESENT_BUT_TAIL_DIVERGED"
    elif missing_leading:
        boundary_status = "VISIBLE_PREFIX_STARTS_AFTER_REQUESTED_WARMUP"
    else:
        boundary_status = "VISIBLE_PREFIX_INSUFFICIENT_WITHOUT_LEADING_GAP"
    physical_status = ""
    physical_requested_start: datetime | None = None
    physical_required_start: datetime | None = None
    physical_representative_count: int | None = None
    physical_available_count: int | None = None
    physical_required_count: int | None = None
    physical_inventory_revision: str | None = None
    raw_physical = five_minute_frame.attrs.get(
        "qmt_physical_five_minute_source_coverage"
    )
    if raw_physical is None:
        raise ValueError("sector physical 5m source coverage is required")
    if raw_physical is not None:
        if not isinstance(raw_physical, Mapping):
            raise ValueError("sector physical 5m source coverage is malformed")
        physical_stable = dict(raw_physical)
        physical_audit_sha256 = physical_stable.pop("audit_sha256", None)
        def source_datetime(name: str) -> datetime | None:
            raw = raw_physical.get(name)
            if raw is None:
                return None
            return normalize_datetime(
                raw
                if isinstance(raw, datetime)
                else datetime.fromisoformat(str(raw)),
                name,
            )

        try:
            raw_observed = source_datetime("observed_at")
            physical_requested_start = source_datetime("requested_start_at")
            physical_required_start = source_datetime(
                "required_contributor_physical_start_at"
            )
            physical_first_minimum = source_datetime(
                "physical_source_first_at_minimum"
            )
            physical_first_maximum = source_datetime(
                "physical_source_first_at_maximum"
            )
            selected_first_minimum = source_datetime(
                "selected_window_first_at_minimum"
            )
            selected_first_maximum = source_datetime(
                "selected_window_first_at_maximum"
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "sector physical 5m source timestamps changed"
            ) from exc
        physical_status = str(raw_physical.get("boundary_status"))
        physical_representative_count = raw_physical.get(
            "representative_member_count"
        )
        physical_available_count = raw_physical.get(
            "available_member_file_count"
        )
        physical_boundary_count = raw_physical.get(
            "physical_boundary_member_count"
        )
        physical_missing_count = raw_physical.get("missing_member_file_count")
        physical_required_count = raw_physical.get(
            "required_contributor_count"
        )
        physical_inventory_revision = raw_physical.get(
            "source_inventory_revision"
        )
        scalar_counts_valid = all(
            type(value) is int
            for value in (
                physical_representative_count,
                physical_available_count,
                physical_boundary_count,
                physical_missing_count,
                physical_required_count,
            )
        )
        if scalar_counts_valid:
            expected_physical_required = max(
                _QMT_SECTOR_COMPOSITE_MINIMUM_MEMBER_COUNT,
                ceil(
                    Decimal(physical_representative_count)
                    * Decimal(_QMT_SECTOR_COMPOSITE_MINIMUM_BAR_COVERAGE)
                ),
            )
            counts_consistent = (
                physical_representative_count > 0
                and 0 <= physical_available_count <= physical_representative_count
                and 0 <= physical_boundary_count <= physical_available_count
                and physical_missing_count
                == physical_representative_count - physical_available_count
                and physical_required_count == expected_physical_required
            )
        else:
            counts_consistent = False
        physical_pair_consistent = (
            (physical_boundary_count == 0)
            == (physical_first_minimum is None and physical_first_maximum is None)
            and (
                physical_first_minimum is None
                or physical_first_maximum is not None
                and physical_first_minimum <= physical_first_maximum
            )
            and (
                (selected_first_minimum is None)
                == (selected_first_maximum is None)
            )
            and (
                selected_first_minimum is None
                or selected_first_minimum <= selected_first_maximum
            )
        )
        required_start_consistent = scalar_counts_valid and (
            (physical_boundary_count >= physical_required_count)
            == (physical_required_start is not None)
            and (
                physical_required_start is None
                or physical_first_minimum is not None
                and physical_first_maximum is not None
                and physical_first_minimum
                <= physical_required_start
                <= physical_first_maximum
            )
        )
        expected_physical_status = (
            "INSUFFICIENT_PHYSICAL_QMT_MEMBER_FILES"
            if physical_available_count < physical_required_count
            else "PHYSICAL_QMT_SOURCE_BOUNDARY_UNAVAILABLE"
            if physical_required_start is None
            else "PHYSICAL_QMT_CACHE_LEFT_BOUNDARY_AFTER_REQUESTED_WARMUP"
            if physical_required_start > physical_requested_start
            else "REQUESTED_REPLAY_LEFT_BOUNDARY_CLIPS_EARLIER_QMT_HISTORY"
        ) if (
            scalar_counts_valid
            and physical_requested_start is not None
        ) else None
        if (
            raw_physical.get("schema")
            != "chanlun-qmt-current-sector-physical-5m-coverage"
            or raw_physical.get("diagnostic_only") is not True
            or raw_physical.get("decision_core_input") is not False
            or raw_physical.get("warmup_requirement_unchanged") is not True
            or raw_physical.get("data_grade") != "RESEARCH_ONLY"
            or raw_physical.get("live_status") != "LIVE_DISABLED"
            or not isinstance(physical_audit_sha256, str)
            or physical_audit_sha256 != sha256_json(physical_stable)
            or raw_observed != observed
            or physical_requested_start is None
            or physical_requested_start > observed
            or not counts_consistent
            or not physical_pair_consistent
            or not required_start_consistent
            or physical_status != expected_physical_status
            or not isinstance(physical_inventory_revision, str)
            or _SHA256_ID.fullmatch(physical_inventory_revision) is None
            or raw_physical.get("sector_id")
            != five_minute_frame.attrs.get("sector_id")
        ):
            raise ValueError("sector physical 5m source coverage changed")
    return QmtSectorSameBaseCoverageEvidence(
        observed_at=observed,
        calendar_first_session=visible_calendar[0],
        first_visible_bar_at=pd.Timestamp(work.iloc[0]["date"]).to_pydatetime(),
        last_visible_bar_at=pd.Timestamp(work.iloc[-1]["date"]).to_pydatetime(),
        first_completed_session=first_completed,
        last_completed_session=last_completed,
        visible_five_minute_bar_count=len(work),
        completed_daily_bar_count=len(completed_sessions),
        required_daily_bar_count=warmup_evidence.required_daily_bar_count,
        remaining_daily_bar_count=max(
            0,
            warmup_evidence.required_daily_bar_count
            - len(completed_sessions),
        ),
        missing_leading_calendar_session_count=missing_leading,
        warmup_converged=warmup_evidence.converged,
        warmup_reason_code=warmup_evidence.reason_code,
        boundary_status=boundary_status,
        physical_source_boundary_status=physical_status,
        physical_source_requested_start_at=physical_requested_start,
        physical_source_required_contributor_start_at=(
            physical_required_start
        ),
        physical_source_representative_member_count=(
            physical_representative_count
        ),
        physical_source_available_member_count=physical_available_count,
        physical_source_required_contributor_count=physical_required_count,
        physical_source_inventory_revision=physical_inventory_revision,
    )


def _aggregate_sector_intraday(
    five_minute: pd.DataFrame,
) -> pd.DataFrame:
    return derive_qmt_sector_thirty_minute_frame(five_minute)


def _aggregate_sector_daily(
    five_minute: pd.DataFrame,
    complete_sessions: Sequence[date],
) -> pd.DataFrame:
    complete = frozenset(complete_sessions)
    rows = five_minute[
        five_minute["date"].dt.date.isin(complete)
    ].copy()
    if rows.empty:
        return pd.DataFrame(
            columns=("date", "open", "high", "low", "close", "volume")
        )
    return (
        rows.groupby(rows["date"].dt.date, sort=True)
        .agg(
            date=("date", "last"),
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            volume=("volume", "sum"),
        )
        .reset_index(drop=True)
    )


def _expected_sector_composite_members(
    sector_id: str,
    members: tuple[str, ...],
) -> tuple[str, ...]:
    """Independently reproduce the frozen QMT representative sample."""

    if len(members) <= _QMT_SECTOR_COMPOSITE_MEMBER_LIMIT:
        return members
    ranked = sorted(
        members,
        key=lambda code: sha256_json(
            {
                "schema": "chanlun-qmt-gics3-sample",
                "sector_id": sector_id,
                "code": code,
            }
        ),
    )
    return tuple(sorted(ranked[:_QMT_SECTOR_COMPOSITE_MEMBER_LIMIT]))


def _expected_sector_membership_revision(
    sector_id: str,
    members: tuple[str, ...],
    composite_members: tuple[str, ...],
) -> str:
    return sha256_json(
        {
            "schema": "chanlun-qmt-gics3-members",
            "sector_id": sector_id,
            "members": members,
            "composite_members": composite_members,
        }
    )


def _sector_same_base_frames(
    *,
    sector_id: str,
    sector_members: tuple[str, ...],
    five_minute_frame: pd.DataFrame,
    decision_time: datetime,
    expected_sessions: Sequence[date],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Derive sector D/30m from one completed QMT component-composite 5m base."""

    decision = normalize_datetime(decision_time, "decision_time")
    required = (
        "date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "member_mask",
    )
    missing = set(required).difference(five_minute_frame.columns)
    if missing:
        raise HigherTimeframeDataUnavailable(
            ("QMT_SECTOR_FIVE_MINUTE_SAME_BASE_STREAM_UNRESOLVED",)
        )
    work = five_minute_frame.loc[:, list(required)].copy()
    work["date"] = pd.to_datetime(work["date"], errors="raise")
    if work["date"].dt.tz is None:
        raise HigherTimeframeDataUnavailable(
            ("QMT_SECTOR_FIVE_MINUTE_SAME_BASE_STREAM_UNRESOLVED",)
        )
    work["date"] = work["date"].dt.tz_convert("Asia/Shanghai")
    work = work[work["date"] <= pd.Timestamp(decision)].copy()
    if (
        work.empty
        or work["date"].duplicated().any()
        or not work["date"].is_monotonic_increasing
    ):
        raise HigherTimeframeDataUnavailable(
            ("QMT_SECTOR_FIVE_MINUTE_SAME_BASE_STREAM_UNRESOLVED",)
        )
    for field in ("open", "high", "low", "close", "volume"):
        work[field] = pd.to_numeric(work[field], errors="raise")
    raw_member_masks = tuple(work["member_mask"])
    if any(
        isinstance(value, bool) or not isinstance(value, Integral)
        for value in raw_member_masks
    ):
        raise HigherTimeframeDataUnavailable(
            ("QMT_SECTOR_COMPOSITE_MEMBER_PATH_PROVENANCE_MISMATCH",)
        )
    work["member_mask"] = tuple(int(value) for value in raw_member_masks)
    prices = work[["open", "high", "low", "close"]]
    invalid = (
        (prices <= 0).any(axis=1)
        | (work["volume"] < 0)
        | (work["high"] < prices.max(axis=1))
        | (work["low"] > prices.min(axis=1))
    )
    if invalid.any():
        raise HigherTimeframeDataUnavailable(
            ("QMT_SECTOR_FIVE_MINUTE_SAME_BASE_STREAM_UNRESOLVED",)
        )

    accepted: list[pd.DataFrame] = []
    complete_sessions: list[date] = []
    grouped = tuple(work.groupby(work["date"].dt.date, sort=True))
    for index, (session, rows) in enumerate(grouped):
        ordered = rows.sort_values("date", kind="stable").reset_index(
            drop=True
        )
        expected = _sector_five_minute_closes(session)
        actual = tuple(
            pd.Timestamp(value).to_pydatetime()
            for value in ordered["date"]
        )
        if actual == expected:
            accepted.append(ordered)
            complete_sessions.append(session)
            continue
        if session == decision.date() and actual == expected[: len(actual)]:
            accepted.append(ordered)
            continue
        # A count-bounded QMT read may cut only the oldest session.  Interior
        # partial sessions are data gaps and must never be silently discarded.
        if index == 0 and actual == expected[-len(actual) :]:
            continue
        raise HigherTimeframeDataUnavailable(
            ("QMT_SECTOR_FIVE_MINUTE_SESSION_GRID_INVALID",)
        )
    if not accepted:
        raise HigherTimeframeDataUnavailable(
            ("QMT_SECTOR_FIVE_MINUTE_NO_ACCEPTED_COMPLETED_BARS",)
        )
    normalized = pd.concat(accepted, ignore_index=True)
    observed_sessions = frozenset(normalized["date"].dt.date)
    first_observed = min(observed_sessions)
    last_calendar_session = (
        min(decision.date(), max(expected_sessions))
        if expected_sessions
        else decision.date()
    )
    if any(
        first_observed <= session <= last_calendar_session
        and session not in observed_sessions
        for session in expected_sessions
    ):
        raise HigherTimeframeDataUnavailable(
            ("QMT_SECTOR_FIVE_MINUTE_EXPECTED_SESSION_MISSING",)
        )

    input_attrs = dict(five_minute_frame.attrs)
    source_members = input_attrs.get("sector_members")
    composite_members = input_attrs.get("sector_composite_members")
    composite_member_limit = input_attrs.get(
        "sector_composite_member_limit"
    )
    minimum_member_count = input_attrs.get(
        "sector_composite_minimum_member_count"
    )
    minimum_bar_coverage = input_attrs.get(
        "sector_composite_minimum_bar_coverage"
    )
    required_member_count = input_attrs.get(
        "sector_composite_required_member_count"
    )
    member_mask_contract = input_attrs.get(
        "sector_composite_member_mask_contract"
    )
    member_path_revision = input_attrs.get(
        "sector_composite_member_path_revision"
    )
    factor_contract_id = input_attrs.get(
        "sector_factor_adjustment_contract_id"
    )
    factor_revision = input_attrs.get("sector_factor_revision")
    membership_revision = input_attrs.get("sector_membership_revision")
    expected_composite_members = _expected_sector_composite_members(
        sector_id,
        sector_members,
    )
    expected_membership_revision = _expected_sector_membership_revision(
        sector_id,
        sector_members,
        expected_composite_members,
    )
    expected_required_member_count = max(
        _QMT_SECTOR_COMPOSITE_MINIMUM_MEMBER_COUNT,
        ceil(
            len(expected_composite_members)
            * float(_QMT_SECTOR_COMPOSITE_MINIMUM_BAR_COVERAGE)
        ),
    )
    if (
        input_attrs.get("sector_id") != sector_id
        or input_attrs.get("sector_membership_scope") != "CALLER_SUPPLIED"
        or type(source_members) is not tuple
        or source_members != sector_members
        or type(composite_members) is not tuple
        or not composite_members
        or len(composite_members) != len(set(composite_members))
        or any(value not in sector_members for value in composite_members)
        or composite_members != expected_composite_members
        or composite_member_limit != _QMT_SECTOR_COMPOSITE_MEMBER_LIMIT
        or minimum_member_count != _QMT_SECTOR_COMPOSITE_MINIMUM_MEMBER_COUNT
        or minimum_bar_coverage != _QMT_SECTOR_COMPOSITE_MINIMUM_BAR_COVERAGE
        or required_member_count != expected_required_member_count
        or input_attrs.get("sector_composite_method")
        != _QMT_SECTOR_COMPOSITE_METHOD
        or factor_contract_id != QMT_CAUSAL_FACTOR_ADJUSTMENT_CONTRACT_ID
        or not isinstance(factor_revision, str)
        or _SHA256_ID.fullmatch(factor_revision) is None
        or membership_revision != expected_membership_revision
    ):
        raise HigherTimeframeDataUnavailable(
            ("QMT_SECTOR_MEMBERSHIP_PROVENANCE_MISMATCH",)
        )
    contributor_counts = work["volume"]
    if (
        (contributor_counts.round() != contributor_counts).any()
        or (contributor_counts < expected_required_member_count).any()
        or (contributor_counts > len(expected_composite_members)).any()
    ):
        raise HigherTimeframeDataUnavailable(
            ("QMT_SECTOR_COMPOSITE_MEMBER_COVERAGE_MISMATCH",)
        )
    contributor_masks = work["member_mask"]
    if any(
        value <= 0
        or value >= 1 << len(expected_composite_members)
        or value.bit_count() != int(count)
        for value, count in zip(contributor_masks, contributor_counts)
    ):
        raise HigherTimeframeDataUnavailable(
            ("QMT_SECTOR_COMPOSITE_MEMBER_PATH_PROVENANCE_MISMATCH",)
        )
    expected_member_path_revision = sha256_json(
        {
            "schema": "chanlun-qmt-sector-composite-member-path",
            "rows": tuple(
                {
                    "date": pd.Timestamp(row.date).to_pydatetime(),
                    "member_mask": int(row.member_mask),
                }
                for row in work.itertuples(index=False)
            ),
        }
    )
    if (
        member_mask_contract != _QMT_SECTOR_COMPOSITE_MEMBER_MASK_CONTRACT
        or member_path_revision != expected_member_path_revision
    ):
        raise HigherTimeframeDataUnavailable(
            ("QMT_SECTOR_COMPOSITE_MEMBER_PATH_PROVENANCE_MISMATCH",)
        )
    expected_price_basis = build_causal_sector_price_basis_metadata(
        provider=_QMT_SECTOR_COMPOSITE_PROVIDER,
        market="a",
        code=(
            f"{sector_id}:"
            + expected_membership_revision.removeprefix("sha256:")
        ),
        adjustment=_QMT_SECTOR_COMPOSITE_ADJUSTMENT,
        structure_price_quantum=_QMT_SECTOR_COMPOSITE_QUANTUM,
        factor_revision=factor_revision,
    )
    price_basis_revision = input_attrs.get("price_basis_revision")
    if (
        input_attrs.get("structure_price_quantum")
        != format(_QMT_SECTOR_COMPOSITE_QUANTUM.normalize(), "f")
        or input_attrs.get("price_basis_provider")
        != expected_price_basis.provider
        or input_attrs.get("price_basis_adjustment")
        != expected_price_basis.adjustment
        or price_basis_revision != expected_price_basis.price_basis_revision
    ):
        raise HigherTimeframeDataUnavailable(
            ("QMT_SECTOR_FIVE_MINUTE_PRICE_BASIS_UNRESOLVED",)
        )
    base_revision = sha256_json(
        {
            "schema": "chanlun-qmt-sector-five-minute-same-base",
            "sector_id": sector_id,
            "decision_time": decision,
            "price_basis_provider": input_attrs.get("price_basis_provider"),
            "price_basis_adjustment": input_attrs.get(
                "price_basis_adjustment"
            ),
            "price_basis_revision": price_basis_revision,
            "sector_factor_adjustment_contract_id": factor_contract_id,
            "sector_factor_revision": factor_revision,
            "sector_membership_revision": input_attrs.get(
                "sector_membership_revision"
            ),
            "sector_members": source_members,
            "sector_composite_members": composite_members,
            "sector_composite_member_limit": composite_member_limit,
            "sector_composite_minimum_member_count": minimum_member_count,
            "sector_composite_minimum_bar_coverage": minimum_bar_coverage,
            "sector_composite_required_member_count": required_member_count,
            "sector_composite_member_mask_contract": member_mask_contract,
            "sector_composite_member_path_revision": member_path_revision,
            "sector_composite_method": input_attrs.get(
                "sector_composite_method"
            ),
            "five_minute": tuple(
                {
                    "date": pd.Timestamp(row.date).to_pydatetime(),
                    "open": float(row.open),
                    "high": float(row.high),
                    "low": float(row.low),
                    "close": float(row.close),
                    "volume": float(row.volume),
                    "member_mask": int(row.member_mask),
                }
                for row in normalized.itertuples(index=False)
            ),
        }
    )
    thirty = _aggregate_sector_intraday(normalized)
    daily = _aggregate_sector_daily(normalized, complete_sessions)
    lineage = {
        key: input_attrs[key]
        for key in (
            "structure_price_quantum",
            "price_basis_revision",
            "price_basis_provider",
            "price_basis_adjustment",
            "sector_factor_adjustment_contract_id",
            "sector_factor_revision",
        )
        if key in input_attrs
    }
    for frame, frequency in ((thirty, "30m"), (daily, "d")):
        frame.attrs = {
            **lineage,
            "source_base_stream_revision": base_revision,
            "source_base_frequency": "5m",
            "derived_frequency": frequency,
            "sector_membership_revision": input_attrs.get(
                "sector_membership_revision"
            ),
            "sector_members": source_members,
            "sector_composite_members": composite_members,
            "sector_composite_member_limit": composite_member_limit,
            "sector_composite_minimum_member_count": minimum_member_count,
            "sector_composite_minimum_bar_coverage": minimum_bar_coverage,
            "sector_composite_required_member_count": required_member_count,
            "sector_composite_member_mask_contract": member_mask_contract,
            "sector_composite_member_path_revision": member_path_revision,
            "sector_composite_method": input_attrs.get(
                "sector_composite_method"
            ),
            "sector_factor_adjustment_contract_id": factor_contract_id,
            "sector_factor_revision": factor_revision,
        }
    return daily, thirty


def build_sector_higher_timeframe_gate_from_five_minute(
    *,
    sector_id: str,
    sector_members: tuple[str, ...],
    five_minute_frame: pd.DataFrame,
    observed_at: datetime,
    trading_sessions: Sequence[date],
    calendar_coverage_end: date | None = None,
    daily_bars: int = QMT_HIGHER_TIMEFRAME_WARMUP_REQUIRED_DAILY_BARS,
    thirty_minute_bars: int = (
        QMT_HIGHER_TIMEFRAME_WARMUP_REQUIRED_DAILY_BARS * 8
    ),
) -> HigherTimeframeGateEvidence:
    """Run the one shared page/replay sector M/W/D decision core.

    The caller supplies a causally completed QMT sector 5m composite.  This
    function performs the exact same provenance, session-grid, aggregation,
    warmup and structure checks for live screening and historical replay.
    """

    observed = normalize_datetime(observed_at, "observed_at")
    if not sector_id or not sector_id.strip():
        raise ValueError("sector_id must be non-empty")
    if (
        type(sector_members) is not tuple
        or not sector_members
        or len(sector_members) != len(set(sector_members))
        or any(not isinstance(value, str) or not value for value in sector_members)
    ):
        raise ValueError("sector_members must be unique non-empty strings")
    if daily_bars < 60 or thirty_minute_bars < 240:
        raise ValueError("sector higher-timeframe history is too short")
    visible_sessions = tuple(
        value for value in trading_sessions if value <= observed.date()
    )
    if not visible_sessions:
        raise HigherTimeframeDataUnavailable(
            ("QMT_HIGHER_TIMEFRAME_TRADING_CALENDAR_UNAVAILABLE",)
        )
    coverage_end = visible_sessions[-1]
    if calendar_coverage_end is not None:
        if calendar_coverage_end != coverage_end:
            raise HigherTimeframeDataUnavailable(
                ("QMT_HIGHER_TIMEFRAME_CALENDAR_COVERAGE_MISMATCH",)
            )
        coverage_end = calendar_coverage_end
    full_daily, full_thirty = _sector_same_base_frames(
        sector_id=sector_id,
        sector_members=sector_members,
        five_minute_frame=five_minute_frame,
        decision_time=observed,
        expected_sessions=visible_sessions,
    )
    daily = full_daily.iloc[-daily_bars:].copy()
    thirty = full_thirty.iloc[-thirty_minute_bars:].copy()
    daily.attrs = dict(full_daily.attrs)
    thirty.attrs = dict(full_thirty.attrs)
    inputs = qmt_higher_timeframe_inputs(
        symbol=sector_id,
        daily_frame=daily,
        thirty_minute_frame=thirty,
        decision_time=observed,
        required_base_frequency="5m",
    )
    envelope = build_qmt_higher_timeframe_risk(
        inputs=inputs,
        trading_sessions=visible_sessions,
        calendar_coverage_end=coverage_end,
        snapshot_id=sha256_json(
            {
                "schema": "chanlun-live-qmt-sector-mwd-risk",
                "sector_id": sector_id,
                "observed_at": inputs.observed_at,
                "source_revision": inputs.source_revision,
            }
        ),
    )
    result = _from_envelope(envelope)
    if len(full_daily) > len(daily):
        diagnostic_inputs = qmt_higher_timeframe_inputs(
            symbol=sector_id,
            daily_frame=full_daily,
            thirty_minute_frame=full_thirty,
            decision_time=observed,
            required_base_frequency="5m",
        )
        diagnostic = build_qmt_higher_timeframe_risk(
            inputs=diagnostic_inputs,
            trading_sessions=visible_sessions,
            calendar_coverage_end=coverage_end,
            snapshot_id=sha256_json(
                {
                    "schema": "chanlun-live-qmt-sector-mwd-convergence",
                    "sector_id": sector_id,
                    "observed_at": diagnostic_inputs.observed_at,
                    "source_revision": diagnostic_inputs.source_revision,
                    "diagnostic_only": True,
                }
            ),
        )
        result = replace(
            result,
            warmup_convergence_evidence=diagnostic.warmup_convergence,
        )
    return result


def build_sector_higher_timeframe_research_gate_from_native_daily(
    *,
    sector_id: str,
    sector_members: tuple[str, ...],
    native_daily_frame: pd.DataFrame,
    five_minute_frame: pd.DataFrame,
    observed_at: datetime,
    trading_sessions: Sequence[date],
    calendar_coverage_end: date | None = None,
    daily_bars: int = QMT_HIGHER_TIMEFRAME_WARMUP_REQUIRED_DAILY_BARS,
    thirty_minute_bars: int = (
        QMT_HIGHER_TIMEFRAME_WARMUP_REQUIRED_DAILY_BARS * 8
    ),
) -> HigherTimeframeGateEvidence:
    """Research-only M/W/D advisory when the sector 5m warmup is too short.

    The native-daily median and the intraday-median aggregate are nonlinear
    objects and cannot be certified as one base stream.  We therefore retain
    an explicit blocker, cap any otherwise GREEN result to AMBER, and never
    let this fallback enter the exact-GREEN ablation.  Confirmed RED remains a
    real research reject.
    """

    observed = normalize_datetime(observed_at, "observed_at")
    visible_sessions = tuple(
        value for value in trading_sessions if value <= observed.date()
    )
    if not visible_sessions:
        raise HigherTimeframeDataUnavailable(
            ("QMT_HIGHER_TIMEFRAME_TRADING_CALENDAR_UNAVAILABLE",)
        )
    coverage_end = visible_sessions[-1]
    if calendar_coverage_end is not None:
        if calendar_coverage_end != coverage_end:
            raise HigherTimeframeDataUnavailable(
                ("QMT_HIGHER_TIMEFRAME_CALENDAR_COVERAGE_MISMATCH",)
            )
        coverage_end = calendar_coverage_end
    _derived_daily, full_thirty = _sector_same_base_frames(
        sector_id=sector_id,
        sector_members=sector_members,
        five_minute_frame=five_minute_frame,
        decision_time=observed,
        expected_sessions=visible_sessions,
    )
    required = ("date", "open", "high", "low", "close", "volume", "member_mask")
    if native_daily_frame.empty or set(required).difference(
        native_daily_frame.columns
    ):
        raise HigherTimeframeDataUnavailable(
            ("QMT_SECTOR_NATIVE_DAILY_HISTORY_UNAVAILABLE",)
        )
    daily = native_daily_frame.loc[:, list(required)].copy()
    daily["date"] = pd.to_datetime(daily["date"], errors="raise")
    if (
        daily["date"].dt.tz is None
        or daily["date"].duplicated().any()
        or not daily["date"].is_monotonic_increasing
        or any(value.time() != time(15) for value in daily["date"])
    ):
        raise HigherTimeframeDataUnavailable(
            ("QMT_SECTOR_NATIVE_DAILY_SEQUENCE_INVALID",)
        )
    daily = daily[daily["date"] <= pd.Timestamp(observed)].copy()
    attrs = dict(native_daily_frame.attrs)
    expected_composite_members = _expected_sector_composite_members(
        sector_id,
        sector_members,
    )
    expected_membership_revision = _expected_sector_membership_revision(
        sector_id,
        sector_members,
        expected_composite_members,
    )
    path_revision = sha256_json(
        {
            "schema": "chanlun-qmt-sector-composite-member-path",
            "rows": tuple(
                {
                    "date": pd.Timestamp(row.date).to_pydatetime(),
                    "member_mask": int(row.member_mask),
                }
                for row in daily.itertuples(index=False)
            ),
        }
    )
    if (
        attrs.get("sector_id") != sector_id
        or attrs.get("sector_membership_scope") != "CALLER_SUPPLIED"
        or attrs.get("sector_members") != sector_members
        or attrs.get("sector_composite_members") != expected_composite_members
        or attrs.get("sector_membership_revision")
        != expected_membership_revision
        or attrs.get("sector_composite_method")
        != _QMT_SECTOR_COMPOSITE_METHOD
        or attrs.get("source_base_frequency") != "native-d"
        or attrs.get("sector_native_daily_role")
        != "UNRECONCILED_RESEARCH_MWD_ADVISORY_ONLY"
        or attrs.get("sector_composite_member_path_revision") != path_revision
        or not isinstance(attrs.get("sector_factor_revision"), str)
        or _SHA256_ID.fullmatch(str(attrs.get("sector_factor_revision"))) is None
    ):
        raise HigherTimeframeDataUnavailable(
            ("QMT_SECTOR_NATIVE_DAILY_PROVENANCE_MISMATCH",)
        )
    daily.attrs = attrs
    full_native_daily = daily.copy()
    full_native_daily.attrs = attrs
    daily = daily.iloc[-daily_bars:].copy()
    thirty = full_thirty.iloc[-thirty_minute_bars:].copy()
    daily.attrs = attrs
    thirty.attrs = dict(full_thirty.attrs)
    inputs = qmt_higher_timeframe_inputs(
        symbol=sector_id,
        daily_frame=daily,
        thirty_minute_frame=thirty,
        decision_time=observed,
        required_base_frequency=(
            QMT_SECTOR_NATIVE_DAILY_RESEARCH_BASE_FREQUENCY
        ),
    )
    envelope = build_qmt_higher_timeframe_risk(
        inputs=inputs,
        trading_sessions=visible_sessions,
        calendar_coverage_end=coverage_end,
        snapshot_id=sha256_json(
            {
                "schema": "chanlun-qmt-sector-native-daily-research-mwd-risk",
                "sector_id": sector_id,
                "observed_at": inputs.observed_at,
                "source_revision": inputs.source_revision,
            }
        ),
    )
    result = _from_envelope(envelope)
    if len(full_native_daily) > len(daily):
        diagnostic_inputs = qmt_higher_timeframe_inputs(
            symbol=sector_id,
            daily_frame=full_native_daily,
            thirty_minute_frame=full_thirty,
            decision_time=observed,
            required_base_frequency=(
                QMT_SECTOR_NATIVE_DAILY_RESEARCH_BASE_FREQUENCY
            ),
        )
        diagnostic = build_qmt_higher_timeframe_risk(
            inputs=diagnostic_inputs,
            trading_sessions=visible_sessions,
            calendar_coverage_end=coverage_end,
            snapshot_id=sha256_json(
                {
                    "schema": (
                        "chanlun-qmt-sector-native-daily-mwd-convergence"
                    ),
                    "sector_id": sector_id,
                    "observed_at": diagnostic_inputs.observed_at,
                    "source_revision": diagnostic_inputs.source_revision,
                    "diagnostic_only": True,
                }
            ),
        )
        result = replace(
            result,
            warmup_convergence_evidence=diagnostic.warmup_convergence,
        )
    blocker = "QMT_SECTOR_NATIVE_DAILY_AND_5M_UNRECONCILED_RESEARCH_BRIDGE"
    if blocker not in result.reason_codes:
        raise RuntimeError("unreconciled sector research gate lost its blocker")
    if result.gate == "GREEN":
        result = replace(
            result,
            gate="AMBER",
            snapshot_id=sha256_json(
                {
                    "schema": "chanlun-qmt-sector-research-gate-cap",
                    "raw_snapshot_id": result.snapshot_id,
                    "cap": "GREEN_TO_AMBER_UNRECONCILED_NATIVE_DAILY",
                }
            ),
        )
    return result


def resolve_sector_higher_timeframe_gate(
    *,
    sector_id: str,
    sector_members: tuple[str, ...],
    five_minute_frame: pd.DataFrame,
    observed_at: datetime,
    trading_sessions: Sequence[date],
    calendar_coverage_end: date | None = None,
    daily_bars: int = QMT_HIGHER_TIMEFRAME_WARMUP_REQUIRED_DAILY_BARS,
    thirty_minute_bars: int = (
        QMT_HIGHER_TIMEFRAME_WARMUP_REQUIRED_DAILY_BARS * 8
    ),
    native_daily_loader: Callable[[], pd.DataFrame] | None = None,
) -> SectorHigherTimeframeGateResolution:
    """Apply the identical sector M/W/D source choice in page and replay.

    The 5m-derived D/30m path always runs first.  Native daily is read lazily
    only when that exact path lacks the frozen 480-session warmup.  Failure to
    obtain the optional advisory preserves the original fail-closed result;
    successful fallback remains explicitly unreconciled and can never become
    GREEN.
    """

    if native_daily_loader is not None and not callable(native_daily_loader):
        raise TypeError("native_daily_loader must be callable")
    strict = build_sector_higher_timeframe_gate_from_five_minute(
        sector_id=sector_id,
        sector_members=sector_members,
        five_minute_frame=five_minute_frame,
        observed_at=observed_at,
        trading_sessions=trading_sessions,
        calendar_coverage_end=calendar_coverage_end,
        daily_bars=daily_bars,
        thirty_minute_bars=thirty_minute_bars,
    )
    if strict.warmup_evidence is None:
        raise ValueError("strict sector gate lost its warmup evidence")
    strict_coverage = build_qmt_sector_same_base_coverage_evidence(
        five_minute_frame=five_minute_frame,
        observed_at=observed_at,
        trading_sessions=trading_sessions,
        warmup_evidence=strict.warmup_evidence,
    )
    strict = replace(
        strict,
        sector_source_mode=QMT_SECTOR_SAME_BASE_SOURCE_MODE,
        sector_strict_same_base_warmup_evidence=strict.warmup_evidence,
        sector_strict_same_base_warmup_convergence_evidence=(
            strict.warmup_convergence_evidence
        ),
        sector_strict_same_base_source_coverage_evidence=strict_coverage,
    )
    insufficient = "QMT_HIGHER_TIMEFRAME_WARMUP_HISTORY_INSUFFICIENT"
    if insufficient not in strict.reason_codes or native_daily_loader is None:
        return SectorHigherTimeframeGateResolution(
            evidence=strict,
            source_mode=QMT_SECTOR_SAME_BASE_SOURCE_MODE,
            strict_warmup_evidence=strict.warmup_evidence,
            strict_warmup_convergence_evidence=(
                strict.warmup_convergence_evidence
            ),
        )
    try:
        native_daily = native_daily_loader()
        if not isinstance(native_daily, pd.DataFrame):
            raise TypeError("sector native daily loader must return a DataFrame")
        fallback = build_sector_higher_timeframe_research_gate_from_native_daily(
            sector_id=sector_id,
            sector_members=sector_members,
            native_daily_frame=native_daily,
            five_minute_frame=five_minute_frame,
            observed_at=observed_at,
            trading_sessions=trading_sessions,
            calendar_coverage_end=calendar_coverage_end,
            # The 480-bar value is a minimum, not a tail cap.  Retaining every
            # causally returned row gives the pairwise convergence check its
            # older comparison margin without changing a strategy threshold.
            daily_bars=max(daily_bars, len(native_daily)),
            thirty_minute_bars=thirty_minute_bars,
        )
    except HigherTimeframeDataUnavailable as exc:
        unavailable_reasons = exc.reason_codes
    except RuntimeError:
        # QMT transport/history/factor failures are expected data-source
        # failures for this optional advisory.  They must not crash the page or
        # make replay choose a different decision path; preserve the strict
        # fail-closed result and expose a stable machine-readable cause.
        unavailable_reasons = (
            "QMT_SECTOR_NATIVE_DAILY_RESEARCH_SOURCE_UNAVAILABLE",
        )
    else:
        unavailable_reasons = ()
    if unavailable_reasons:
        strict = replace(
            strict,
            reason_codes=tuple(
                dict.fromkeys((*strict.reason_codes, *unavailable_reasons))
            ),
            snapshot_id=sha256_json(
                {
                    "schema": "chanlun-sector-strict-gate-with-fallback-failure",
                    "strict_snapshot_id": strict.snapshot_id,
                    "fallback_unavailable_reason_codes": unavailable_reasons,
                }
            ),
        )
        return SectorHigherTimeframeGateResolution(
            evidence=strict,
            source_mode=QMT_SECTOR_SAME_BASE_SOURCE_MODE,
            strict_warmup_evidence=strict.warmup_evidence,
            fallback_unavailable_reason_codes=unavailable_reasons,
            strict_warmup_convergence_evidence=(
                strict.warmup_convergence_evidence
            ),
        )
    fallback = replace(
        fallback,
        sector_source_mode=QMT_SECTOR_NATIVE_DAILY_RESEARCH_SOURCE_MODE,
        sector_strict_same_base_warmup_evidence=strict.warmup_evidence,
        sector_strict_same_base_warmup_convergence_evidence=(
            strict.warmup_convergence_evidence
        ),
        sector_strict_same_base_source_coverage_evidence=strict_coverage,
        sector_research_bridge_parameter_set_id=(
            str(sector_native_daily_research_bridge_contract()["parameter_set_id"])
        ),
    )
    return SectorHigherTimeframeGateResolution(
        evidence=fallback,
        source_mode=QMT_SECTOR_NATIVE_DAILY_RESEARCH_SOURCE_MODE,
        strict_warmup_evidence=strict.warmup_evidence,
        strict_warmup_convergence_evidence=(
            strict.warmup_convergence_evidence
        ),
    )


class QmtHigherTimeframeGateSource:
    """Use reconciled native D + 1m-derived 30m; sector stays on one 5m base."""

    def __init__(
        self,
        *,
        exchange_provider: Callable[[], object],
        benchmark_symbol: str = "SH.000300",
        daily_bars: int = QMT_HIGHER_TIMEFRAME_WARMUP_REQUIRED_DAILY_BARS,
        thirty_minute_bars: int = (
            QMT_HIGHER_TIMEFRAME_WARMUP_REQUIRED_DAILY_BARS * 8
        ),
        sector_frame_provider: Callable[..., object] | None = None,
        sector_daily_bars: int = (
            QMT_HIGHER_TIMEFRAME_WARMUP_REQUIRED_DAILY_BARS
        ),
        sector_thirty_minute_bars: int = (
            QMT_HIGHER_TIMEFRAME_WARMUP_REQUIRED_DAILY_BARS * 8
        ),
        trading_calendar_provider: Callable[..., Sequence[date]] | None = None,
        refresh_stale_benchmark: bool = True,
    ) -> None:
        if not callable(exchange_provider):
            raise TypeError("exchange_provider must be callable")
        if daily_bars < 60 or thirty_minute_bars < 240:
            raise ValueError("higher-timeframe history is too short")
        if sector_frame_provider is not None and not callable(
            sector_frame_provider
        ):
            raise TypeError("sector_frame_provider must be callable")
        if trading_calendar_provider is not None and not callable(
            trading_calendar_provider
        ):
            raise TypeError("trading_calendar_provider must be callable")
        if sector_daily_bars < 60 or sector_thirty_minute_bars < 240:
            raise ValueError("sector higher-timeframe history is too short")
        if type(refresh_stale_benchmark) is not bool:
            raise TypeError("refresh_stale_benchmark must be an exact bool")
        self._exchange_provider = exchange_provider
        self._benchmark_symbol = benchmark_symbol
        self._daily_bars = daily_bars
        self._thirty_minute_bars = thirty_minute_bars
        self._sector_frame_provider = sector_frame_provider
        self._trading_calendar_provider = trading_calendar_provider
        self._sector_daily_bars = sector_daily_bars
        self._sector_thirty_minute_bars = sector_thirty_minute_bars
        # 480 is the frozen minimum, while convergence compares the full
        # prefix with its oldest-third-trimmed suffix.  Request a mechanical
        # 50% evidence margin on the on-demand native-daily advisory without
        # changing any trading threshold.
        sector_native_daily_minimum = max(
            sector_daily_bars,
            QMT_HIGHER_TIMEFRAME_WARMUP_REQUIRED_DAILY_BARS,
        )
        self._sector_native_daily_bars = sector_native_daily_minimum + ceil(
            sector_native_daily_minimum / 2
        )
        sector_required_sessions = max(
            sector_daily_bars,
            ceil(sector_thirty_minute_bars / 8),
        )
        self._sector_five_minute_bars = (
            sector_required_sessions + 1
        ) * 48
        required_sessions = max(
            daily_bars,
            ceil(thirty_minute_bars / 8),
        )
        # QMT can include a 09:30 opening event in addition to 240 completed
        # minutes.  One extra session lets us discard a leading fragment caused
        # solely by the requested row-count boundary.
        self._one_minute_bars = (required_sessions + 1) * 241
        self._lookback_days = max(120, required_sessions * 2)
        self._native_daily_bars = self._daily_bars + 10
        self._native_daily_lookback_days = max(730, self._daily_bars * 3)
        self._refresh_stale_benchmark = refresh_stale_benchmark
        self._benchmark_refresh_attempts: set[date] = set()
        self._cache: dict[tuple[str, str], HigherTimeframeGateEvidence] = {}
        self._sector_cache: dict[
            tuple[str, str], HigherTimeframeGateEvidence
        ] = {}
        # A symbol can have an incomplete same-base 1m history while the broad
        # market stream remains fully valid.  Cache the resulting fail-closed
        # pair for this exact decision minute so monitoring refreshes neither
        # recompute the same invalid stream nor erase the valid market gate.
        self._bundle_cache: dict[
            tuple[str, str, str | None, str | None],
            HigherTimeframeGateBundle,
        ] = {}
        self._calendar_cache: dict[str, tuple] = {}
        self._minute_cache: dict[tuple[str, str], pd.DataFrame] = {}
        self._native_daily_cache: dict[tuple[str, str], pd.DataFrame] = {}
        # A native daily row can become visible a few seconds before the local
        # 1m store has downloaded the same completed session.  One explicit
        # post-close refresh per symbol/session repairs that transport lag; the
        # reconciliation contract remains fail-closed if the refreshed prefix
        # is still incomplete.
        self._native_daily_ahead_refresh_attempts: set[tuple[str, date]] = set()
        self._native_daily_calendar_coverage_cache: dict[
            tuple[str, str], QmtNativeDailyCalendarCoverageEvidence
        ] = {}

    @staticmethod
    def _drop_requested_leading_fragment(
        frame: pd.DataFrame,
        *,
        decision_time: datetime,
        requested_rows: int,
    ) -> pd.DataFrame:
        """Drop only a row-count-cut oldest session, never an interior gap."""

        if frame.empty or requested_rows <= 0 or "date" not in frame:
            return frame
        dates = pd.to_datetime(frame["date"], errors="raise")
        if dates.dt.tz is None:
            return frame
        local = dates.dt.tz_convert("Asia/Shanghai")
        first_session = local.iloc[0].date()
        if first_session >= decision_time.date():
            return frame
        first = frame.loc[local.dt.date == first_session]
        first_times = tuple(pd.to_datetime(first["date"]).dt.tz_convert("Asia/Shanghai").dt.time)
        complete = (
            len(first_times) == 241
            and first_times[0] == time(9, 30)
            and first_times[-1] == time(15, 0)
        ) or (
            len(first_times) == 240
            and first_times[0] == time(9, 31)
            and first_times[-1] == time(15, 0)
        )
        if complete:
            return frame
        trimmed = frame.loc[local.dt.date != first_session].copy()
        trimmed.attrs = dict(frame.attrs)
        trimmed.attrs["qmt_leading_fragment_dropped"] = first_session.isoformat()
        trimmed.attrs["qmt_leading_fragment_reason"] = (
            "REQUEST_COUNT_BOUNDARY"
            if len(frame) >= requested_rows
            else "LOCAL_HISTORY_COVERAGE_BOUNDARY"
        )
        return trimmed

    def _one_minute_frame(
        self,
        symbol: str,
        as_of: datetime,
        *,
        allow_download: bool = False,
    ) -> pd.DataFrame:
        observed = normalize_datetime(as_of, "as_of")
        bucket = observed.isoformat(timespec="minutes")
        key = (symbol, bucket)
        if allow_download:
            self._minute_cache.pop(key, None)
        cached = self._minute_cache.get(key)
        if cached is not None:
            return cached
        exchange = self._exchange_provider()
        loader = getattr(exchange, "klines", None)
        if not callable(loader):
            raise TypeError("QMT exchange must expose klines")
        start = observed - timedelta(days=self._lookback_days)
        frame = loader(
            symbol,
            "1m",
            # ExchangeQMT accepts a local QMT timestamp, not an ISO-8601
            # offset suffix (``+08:00`` is rejected by xtdata).
            start_date=start.strftime("%Y-%m-%d %H:%M:%S"),
            end_date=observed.strftime("%Y-%m-%d %H:%M:%S"),
            args={
                "dividend_type": "front",
                "skip_download": not allow_download,
                "research_exact_end": True,
                "req_counts": self._one_minute_bars,
            },
        )
        if not isinstance(frame, pd.DataFrame):
            raise TypeError("QMT one-minute higher-timeframe base is unavailable")
        frame = self._drop_requested_leading_fragment(
            frame,
            decision_time=observed,
            requested_rows=self._one_minute_bars,
        )
        self._minute_cache[key] = frame
        return frame

    def _native_daily_frame(
        self,
        symbol: str,
        as_of: datetime,
    ) -> pd.DataFrame:
        observed = normalize_datetime(as_of, "as_of")
        bucket = observed.isoformat(timespec="minutes")
        key = (symbol, bucket)
        cached = self._native_daily_cache.get(key)
        if cached is not None:
            return cached
        exchange = self._exchange_provider()
        loader = getattr(exchange, "klines", None)
        if not callable(loader):
            raise TypeError("QMT exchange must expose klines")
        start = observed - timedelta(days=self._native_daily_lookback_days)
        frame = loader(
            symbol,
            "d",
            start_date=start.strftime("%Y-%m-%d %H:%M:%S"),
            end_date=observed.strftime("%Y-%m-%d %H:%M:%S"),
            args={
                "dividend_type": "front",
                "skip_download": True,
                "research_exact_end": True,
                "req_counts": self._native_daily_bars,
            },
        )
        if not isinstance(frame, pd.DataFrame):
            raise TypeError("QMT native daily higher-timeframe history is unavailable")
        self._native_daily_cache[key] = frame
        return frame

    def _trading_sessions(self, as_of: datetime) -> tuple[date, ...]:
        observed = normalize_datetime(as_of, "as_of")
        bucket = observed.isoformat(timespec="minutes")
        cached = self._calendar_cache.get(bucket)
        if cached is not None:
            return cached
        provider = self._trading_calendar_provider
        if provider is None:
            raise HigherTimeframeDataUnavailable(
                ("QMT_HIGHER_TIMEFRAME_TRADING_CALENDAR_UNAVAILABLE",)
            )
        completed_cutoff = observed.date()
        if observed.timetz().replace(tzinfo=None) < time(15, 0):
            completed_cutoff -= timedelta(days=1)
        start = (observed - timedelta(days=self._native_daily_lookback_days)).date()
        values = provider(
            start=start,
            end=completed_cutoff,
            observed_at=observed,
        )
        sessions = tuple(values)
        if (
            not sessions
            or sessions != tuple(sorted(set(sessions)))
            or any(type(value) is not date for value in sessions)
            or sessions[0] < start
            or sessions[-1] > completed_cutoff
        ):
            raise HigherTimeframeDataUnavailable(
                ("QMT_HIGHER_TIMEFRAME_TRADING_CALENDAR_INVALID",)
            )
        self._calendar_cache = {bucket: sessions}
        return sessions

    def _frames(
        self,
        symbol: str,
        as_of: datetime,
        *,
        expected_sessions: tuple[date, ...],
    ) -> tuple[
        pd.DataFrame,
        pd.DataFrame,
        QmtNativeDailyReconciliationEvidence,
    ]:
        """Reconcile native D against 1m; keep 30m on the exact 1m prefix."""

        def same_base_stream(*, allow_download: bool):
            stream = build_qmt_same_base_stream_frames(
                symbol=symbol,
                one_minute_frame=self._one_minute_frame(
                    symbol,
                    as_of,
                    allow_download=allow_download,
                ),
                decision_time=as_of,
                expected_sessions=expected_sessions,
            )
            if (
                stream.price_basis_revision is None
                or stream.one_minute.empty
                or stream.session_issues
            ):
                reasons = tuple(value.code for value in stream.blockers)
                raise HigherTimeframeDataUnavailable(
                    reasons,
                    session_issues=stream.session_issues,
                )
            return stream

        stream = same_base_stream(allow_download=False)
        native_daily = self._native_daily_frame(symbol, as_of)
        try:
            bridge = build_qmt_native_daily_bridge(
                symbol=symbol,
                native_daily_frame=native_daily,
                same_base=stream,
                decision_time=as_of,
                trading_sessions=expected_sessions,
                max_price_difference_quanta=1,
            )
        except QmtNativeDailyReconciliationError as exc:
            observed = normalize_datetime(as_of, "as_of").astimezone(
                ZoneInfo("Asia/Shanghai")
            )
            latest_expected_session = _latest_closed_expected_session(
                observed,
                expected_sessions,
            )
            # Bind the one-shot refresh to the latest expected trading
            # session, not to the wall-clock date.  On weekends and exchange
            # holidays the native daily bar can legitimately contain Friday
            # (or the prior trading day) while the cached 1m base is still one
            # session behind.  Requiring ``session == observed.date()`` made
            # that mismatch permanently fail closed until the next trading
            # day.  Comparing with the completed session close keeps the
            # refresh causal and still forbids an intraday current-session
            # download.
            refresh_key = (
                None
                if latest_expected_session is None
                else (symbol, latest_expected_session)
            )
            refreshable_ahead = bool(
                exc.code == "QMT_NATIVE_DAILY_AHEAD_OF_ONE_MINUTE_BASE"
                and refresh_key is not None
                and refresh_key not in self._native_daily_ahead_refresh_attempts
            )
            if not refreshable_ahead:
                raise HigherTimeframeDataUnavailable(
                    (exc.code,),
                    native_daily_calendar_coverage_evidence=(
                        exc.calendar_coverage_evidence
                    ),
                ) from exc
            assert refresh_key is not None
            self._native_daily_ahead_refresh_attempts.add(refresh_key)
            stream = same_base_stream(allow_download=True)
            try:
                bridge = build_qmt_native_daily_bridge(
                    symbol=symbol,
                    native_daily_frame=native_daily,
                    same_base=stream,
                    decision_time=as_of,
                    trading_sessions=expected_sessions,
                    max_price_difference_quanta=1,
                )
            except QmtNativeDailyReconciliationError as refreshed_exc:
                raise HigherTimeframeDataUnavailable(
                    (refreshed_exc.code,),
                    native_daily_calendar_coverage_evidence=(
                        refreshed_exc.calendar_coverage_evidence
                    ),
                ) from refreshed_exc
        daily = bridge.daily.iloc[-self._daily_bars :].copy()
        thirty = bridge.thirty_minute.iloc[-self._thirty_minute_bars :].copy()
        daily.attrs = dict(bridge.daily.attrs)
        thirty.attrs = dict(bridge.thirty_minute.attrs)
        bucket = normalize_datetime(as_of, "as_of").isoformat(timespec="minutes")
        self._native_daily_calendar_coverage_cache[(symbol, bucket)] = (
            bridge.calendar_coverage_evidence
        )
        return daily, thirty, bridge.evidence

    def _benchmark_prefix_is_current(self, as_of: datetime) -> bool:
        observed = normalize_datetime(as_of, "as_of")
        local = observed.astimezone(ZoneInfo("Asia/Shanghai"))
        # The forward process does not evaluate weekends.  A weekday holiday
        # remains conservatively unresolved because this adapter has no
        # independently certified future trading calendar.
        if local.weekday() >= 5 or local.time() < time(9, 30):
            return True
        frame = self._one_minute_frame(self._benchmark_symbol, observed)
        current = self._frame_has_current_session(frame, observed=local)
        if (
            not current
            and self._refresh_stale_benchmark
            and local.date() not in self._benchmark_refresh_attempts
        ):
            self._benchmark_refresh_attempts.add(local.date())
            frame = self._one_minute_frame(
                self._benchmark_symbol,
                observed,
                allow_download=True,
            )
            current = self._frame_has_current_session(frame, observed=local)
        return current

    @staticmethod
    def _frame_has_current_session(
        frame: pd.DataFrame,
        *,
        observed: datetime,
    ) -> bool:
        if frame.empty or "date" not in frame:
            return False
        dates = pd.to_datetime(frame["date"], errors="raise")
        if dates.dt.tz is None:
            return False
        latest = dates.dt.tz_convert("Asia/Shanghai").iloc[-1]
        return latest.date() == observed.date() and latest <= pd.Timestamp(observed)

    def _one(
        self,
        symbol: str,
        *,
        as_of: datetime,
        trading_sessions: tuple,
        calendar_coverage_end,
    ) -> HigherTimeframeGateEvidence:
        bucket = normalize_datetime(as_of, "as_of").isoformat(timespec="minutes")
        cache_key = (symbol, bucket)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        daily, thirty, reconciliation = self._frames(
            symbol,
            as_of,
            expected_sessions=trading_sessions,
        )
        calendar_coverage = self._native_daily_calendar_coverage_cache.get(
            (symbol, bucket)
        )
        inputs = qmt_higher_timeframe_inputs(
            symbol=symbol,
            daily_frame=daily,
            thirty_minute_frame=thirty,
            decision_time=as_of,
            required_base_frequency=QMT_NATIVE_DAILY_RECONCILED_BASE_FREQUENCY,
            native_daily_reconciliation_evidence=reconciliation,
            native_daily_calendar_coverage_evidence=calendar_coverage,
        )
        envelope = build_qmt_higher_timeframe_risk(
            inputs=inputs,
            trading_sessions=trading_sessions,
            calendar_coverage_end=calendar_coverage_end,
            snapshot_id=sha256_json(
                {
                    "schema": "chanlun-live-qmt-mwd-risk",
                    "symbol": symbol,
                    "observed_at": inputs.observed_at,
                    "source_revision": inputs.source_revision,
                }
            ),
        )
        result = _from_envelope(envelope)
        self._cache[cache_key] = result
        return result

    def _sector_one(
        self,
        *,
        sector_id: str,
        sector_name: str,
        sector_members: tuple[str, ...],
        as_of: datetime,
        trading_sessions: tuple[date, ...],
        calendar_coverage_end: date,
    ) -> HigherTimeframeGateEvidence:
        sector_identity = sha256_json(
            {
                "schema": "chanlun-higher-timeframe-sector-input",
                "sector_id": sector_id,
                "sector_name": sector_name,
                "sector_members": sector_members,
            }
        )
        bucket = normalize_datetime(as_of, "as_of").isoformat(
            timespec="minutes"
        )
        cached = self._sector_cache.get((sector_identity, bucket))
        if cached is not None:
            return cached
        provider = self._sector_frame_provider
        if provider is None:
            raise HigherTimeframeDataUnavailable(
                ("QMT_SECTOR_HIGHER_TIMEFRAME_RISK_UNAVAILABLE",)
            )
        raw = provider(
            sector_id=sector_id,
            sector_name=sector_name,
            members=sector_members,
            frequency="5m",
            as_of=as_of,
            request_bars=self._sector_five_minute_bars,
        )
        if not isinstance(raw, pd.DataFrame) or raw.empty:
            raise HigherTimeframeDataUnavailable(
                ("QMT_SECTOR_FIVE_MINUTE_SAME_BASE_STREAM_UNRESOLVED",)
            )
        resolution = resolve_sector_higher_timeframe_gate(
            sector_id=sector_id,
            sector_members=sector_members,
            five_minute_frame=raw,
            observed_at=as_of,
            trading_sessions=trading_sessions,
            calendar_coverage_end=calendar_coverage_end,
            daily_bars=self._sector_daily_bars,
            thirty_minute_bars=self._sector_thirty_minute_bars,
            native_daily_loader=lambda: provider(
                sector_id=sector_id,
                sector_name=sector_name,
                members=sector_members,
                frequency="1d",
                as_of=as_of,
                request_bars=self._sector_native_daily_bars,
            ),
        )
        result = resolution.evidence
        self._sector_cache[(sector_identity, bucket)] = result
        return result

    def gates(
        self,
        *,
        symbol: str,
        as_of: datetime,
        sector_id: str | None = None,
        sector_name: str | None = None,
        sector_members: tuple[str, ...] | None = None,
    ) -> HigherTimeframeGateBundle:
        observed = normalize_datetime(as_of, "as_of")
        if sector_id is not None and not sector_id.strip():
            raise ValueError("sector_id must be non-empty when provided")
        if sector_name is not None and not sector_name.strip():
            raise ValueError("sector_name must be non-empty when provided")
        if sector_members is not None and (
            type(sector_members) is not tuple
            or len(sector_members) != len(set(sector_members))
            or any(
                not isinstance(value, str) or not value
                for value in sector_members
            )
        ):
            raise ValueError("sector_members must be unique non-empty strings")
        if not self._benchmark_prefix_is_current(observed):
            return unresolved_higher_timeframe_gates(
                symbol=symbol,
                observed_at=observed,
                reason_code="QMT_BENCHMARK_ONE_MINUTE_PREFIX_STALE",
                sector_subject=sector_id,
            )
        bucket = observed.isoformat(timespec="minutes")
        sector_identity = (
            None
            if sector_id is None
            else sha256_json(
                {
                    "schema": "chanlun-higher-timeframe-sector-input",
                    "sector_id": sector_id,
                    "sector_name": sector_name,
                    "sector_members": sector_members,
                }
            )
        )
        bundle_key = (symbol, bucket, sector_id, sector_identity)
        cached_bundle = self._bundle_cache.get(bundle_key)
        if cached_bundle is not None:
            return cached_bundle
        try:
            sessions = self._trading_sessions(observed)
        except HigherTimeframeDataUnavailable as exc:
            return unresolved_higher_timeframe_gates(
                symbol=symbol,
                observed_at=observed,
                reason_codes=exc.reason_codes,
                sector_subject=sector_id,
                session_evidence=HigherTimeframeSessionEvidence.exact(
                    exc.session_issues
                ),
            )
        except Exception:
            return unresolved_higher_timeframe_gates(
                symbol=symbol,
                observed_at=observed,
                reason_code=(
                    "QMT_HIGHER_TIMEFRAME_TRADING_CALENDAR_PROVIDER_UNAVAILABLE"
                ),
                sector_subject=sector_id,
            )
        if not sessions:
            raise ValueError("benchmark daily calendar is unavailable")
        try:
            market = self._one(
                self._benchmark_symbol,
                as_of=observed,
                trading_sessions=sessions,
                calendar_coverage_end=sessions[-1],
            )
        except HigherTimeframeDataUnavailable as exc:
            return unresolved_higher_timeframe_gates(
                symbol=symbol,
                observed_at=observed,
                reason_codes=exc.reason_codes,
                sector_subject=sector_id,
                session_evidence=HigherTimeframeSessionEvidence.exact(
                    exc.session_issues
                ),
                market_native_daily_calendar_coverage_evidence=(
                    exc.native_daily_calendar_coverage_evidence
                ),
            )
        except Exception:
            return unresolved_higher_timeframe_gates(
                symbol=symbol,
                observed_at=observed,
                reason_code="QMT_MARKET_HIGHER_TIMEFRAME_PROVIDER_UNAVAILABLE",
                sector_subject=sector_id,
            )
        try:
            symbol_gate = self._one(
                symbol,
                as_of=observed,
                trading_sessions=sessions,
                calendar_coverage_end=sessions[-1],
            )
        except HigherTimeframeDataUnavailable as exc:
            # The market and symbol evidence are independent.  Losing the
            # symbol's session grid must keep new entry fail-closed, but it
            # must not rewrite a valid market risk assessment to UNRESOLVED.
            symbol_gate = unresolved_higher_timeframe_gates(
                symbol=symbol,
                observed_at=observed,
                reason_codes=exc.reason_codes,
                session_evidence=HigherTimeframeSessionEvidence.exact(
                    exc.session_issues
                ),
                symbol_native_daily_calendar_coverage_evidence=(
                    exc.native_daily_calendar_coverage_evidence
                ),
            ).symbol
        if sector_id is None or sector_name is None or sector_members is None:
            sector_gate = _unresolved_higher_timeframe_gate(
                subject=sector_id or "SECTOR",
                observed_at=observed,
                reason_codes=("QMT_SECTOR_HIGHER_TIMEFRAME_INPUT_UNAVAILABLE",),
                session_evidence=HigherTimeframeSessionEvidence.unavailable(),
            )
        else:
            try:
                sector_gate = self._sector_one(
                    sector_id=sector_id,
                    sector_name=sector_name,
                    sector_members=sector_members,
                    as_of=observed,
                    trading_sessions=sessions,
                    calendar_coverage_end=sessions[-1],
                )
            except HigherTimeframeDataUnavailable as exc:
                sector_gate = _unresolved_higher_timeframe_gate(
                    subject=sector_id,
                    observed_at=observed,
                    reason_codes=exc.reason_codes,
                    session_evidence=HigherTimeframeSessionEvidence.unavailable(),
                )
            except Exception:
                sector_gate = _unresolved_higher_timeframe_gate(
                    subject=sector_id,
                    observed_at=observed,
                    reason_codes=(
                        "QMT_SECTOR_HIGHER_TIMEFRAME_PROVIDER_UNAVAILABLE",
                    ),
                    session_evidence=HigherTimeframeSessionEvidence.unavailable(),
                )
        result = HigherTimeframeGateBundle(
            market=market,
            symbol=symbol_gate,
            sector=sector_gate,
        )
        self._bundle_cache[bundle_key] = result
        return result


def unresolved_higher_timeframe_gates(
    *,
    symbol: str,
    observed_at: datetime,
    reason_code: str | None = None,
    reason_codes: tuple[str, ...] = (),
    session_evidence: HigherTimeframeSessionEvidence | None = None,
    market_native_daily_calendar_coverage_evidence: (
        QmtNativeDailyCalendarCoverageEvidence | None
    ) = None,
    symbol_native_daily_calendar_coverage_evidence: (
        QmtNativeDailyCalendarCoverageEvidence | None
    ) = None,
    sector_subject: str | None = None,
) -> HigherTimeframeGateBundle:
    observed = normalize_datetime(observed_at, "observed_at")
    if reason_code is not None and reason_codes:
        raise ValueError("provide reason_code or reason_codes, not both")
    reasons = tuple(
        dict.fromkeys(
            value.strip()
            for value in ((reason_code,) if reason_code is not None else reason_codes)
            if value.strip()
        )
    )
    if not reasons:
        raise ValueError("at least one higher-timeframe reason code is required")
    evidence = session_evidence or HigherTimeframeSessionEvidence.unavailable()

    return HigherTimeframeGateBundle(
        market=_unresolved_higher_timeframe_gate(
            subject="MARKET",
            observed_at=observed,
            reason_codes=reasons,
            session_evidence=evidence,
            native_daily_calendar_coverage_evidence=(
                market_native_daily_calendar_coverage_evidence
            ),
        ),
        symbol=_unresolved_higher_timeframe_gate(
            subject=symbol,
            observed_at=observed,
            reason_codes=reasons,
            session_evidence=evidence,
            native_daily_calendar_coverage_evidence=(
                symbol_native_daily_calendar_coverage_evidence
            ),
        ),
        sector=_unresolved_higher_timeframe_gate(
            subject=sector_subject or "SECTOR",
            observed_at=observed,
            reason_codes=reasons,
            session_evidence=evidence,
        ),
    )


def _unresolved_higher_timeframe_gate(
    *,
    subject: str,
    observed_at: datetime,
    reason_codes: tuple[str, ...],
    session_evidence: HigherTimeframeSessionEvidence,
    native_daily_calendar_coverage_evidence: (
        QmtNativeDailyCalendarCoverageEvidence | None
    ) = None,
) -> HigherTimeframeGateEvidence:
    if not subject:
        raise ValueError("higher-timeframe subject is required")
    cause_document = (
        {"reason_code": reason_codes[0]}
        if len(reason_codes) == 1
        else {"reason_codes": reason_codes}
    )
    source = sha256_json(
        {
            "schema": "chanlun-higher-timeframe-provider-failure",
            "subject": subject,
            "observed_at": observed_at,
            "native_daily_calendar_coverage_evidence": (
                None
                if native_daily_calendar_coverage_evidence is None
                else native_daily_calendar_coverage_evidence.document()
            ),
            **cause_document,
        }
    )
    return HigherTimeframeGateEvidence(
        subject=subject,
        observed_at=observed_at,
        monthly="UNRESOLVED",
        weekly="UNRESOLVED",
        daily="UNRESOLVED",
        gate="UNRESOLVED",
        grade="UNRESOLVED",
        snapshot_id=source,
        source_revision=source,
        reason_codes=reason_codes,
        session_evidence=session_evidence,
        native_daily_calendar_coverage_evidence=(
            native_daily_calendar_coverage_evidence
        ),
    )


__all__ = (
    "HIGHER_TIMEFRAME_EFFECTIVENESS_AUDIT_SCHEMA",
    "HIGHER_TIMEFRAME_SESSION_EVIDENCE_CONTRACT_ID",
    "QMT_SECTOR_SAME_BASE_COVERAGE_EVIDENCE_CONTRACT_ID",
    "HigherTimeframeDataUnavailable",
    "HigherTimeframeGateBundle",
    "HigherTimeframeGateEvidence",
    "HigherTimeframePeriodDiagnostic",
    "HigherTimeframeSessionEvidence",
    "QMT_SECTOR_NATIVE_DAILY_RESEARCH_SOURCE_MODE",
    "QMT_SECTOR_SAME_BASE_SOURCE_MODE",
    "QmtHigherTimeframeGateSource",
    "QmtSectorSameBaseCoverageEvidence",
    "SectorHigherTimeframeGateResolution",
    "build_sector_higher_timeframe_gate_from_five_minute",
    "build_sector_higher_timeframe_research_gate_from_native_daily",
    "build_qmt_sector_same_base_coverage_evidence",
    "higher_timeframe_effectiveness_audit",
    "higher_timeframe_gate_evidence_from_envelope",
    "resolve_sector_higher_timeframe_gate",
    "sector_native_daily_research_bridge_contract",
    "unresolved_higher_timeframe_gates",
)
