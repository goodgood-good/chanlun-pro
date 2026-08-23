"""无下单权限的人工复核缠论筛选。

程序可以缩小 QMT 全市场范围，并把严格递归引擎已经确认的30m环境、5m正式买卖点
和用于精确执行的1m区间套位置交给人工复核。它不得重新判断中枢、走势类型、递归级别或买卖点，
也不得创建订单。
历史评估因此只衡量筛选质量，不把人工复核队列冒充组合回测。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import re
from statistics import median
from typing import Literal, Mapping, Sequence

from chanlun.decision_support.fingerprints import normalize_datetime, sha256_json
from chanlun.decision_support.trading_system.a_share_minute_grid import (
    a_share_completed_one_minute_prefix_closes,
    validate_a_share_complete_session_closes,
    validate_a_share_completed_one_minute_prefix_closes,
)
from chanlun.decision_support.trading_system.file_lock import interprocess_file_lock
from chanlun.decision_support.trading_system.higher_timeframe_gate import (
    HIGHER_TIMEFRAME_SESSION_EVIDENCE_CONTRACT_ID,
    HigherTimeframePeriodDiagnostic,
    HigherTimeframeSessionEvidence,
    QMT_SECTOR_NATIVE_DAILY_RESEARCH_SOURCE_MODE,
    QMT_SECTOR_SAME_BASE_SOURCE_MODE,
    QmtSectorSameBaseCoverageEvidence,
    sector_native_daily_research_bridge_contract,
)
from chanlun.decision_support.trading_system.models import (
    EntryExecutionBoundary,
    parse_entry_execution_boundary_document,
)
from chanlun.decision_support.trading_system.position_recommendation import (
    PositionRecommendation,
    parse_position_recommendation_document,
)
from chanlun.decision_support.trading_system.qmt_higher_timeframe import (
    QMT_HIGHER_TIMEFRAME_WARMUP_EVIDENCE_CONTRACT_ID,
    QMT_HIGHER_TIMEFRAME_WARMUP_CONVERGENCE_PARAMETER_SET_ID,
    QmtHigherTimeframeWarmupEvidence,
)
from chanlun.decision_support.trading_system.qmt_native_daily_bridge import (
    QMT_NATIVE_DAILY_CALENDAR_COVERAGE_EVIDENCE_CONTRACT_ID,
    QMT_NATIVE_DAILY_RECONCILIATION_CONTRACT_ID,
    QmtNativeDailyCalendarCoverageEvidence,
    QmtNativeDailyReconciliationEvidence,
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
from chanlun.decision_support.trading_system.qmt_same_base_stream import (
    QmtMinuteSessionIssue,
)
from chanlun.decision_support.trading_system.etf_proxy_facts import (
    RiskMappingSupplyFacts,
)
from chanlun.decision_support.trading_system.selection import (
    HIGHER_TIMEFRAME_RISK_STATES,
    higher_timeframe_risk_gate,
)
from chanlun.decision_support.trading_system.signal_alignment import (
    unified_signal_alignment_contract,
)


AlertType = Literal[
    "POSSIBLE_5M_TRADE_BUY",
    "POSSIBLE_5M_TRADE_SELL",
    "POSSIBLE_30M_BUY",
    "POSSIBLE_30M_EXIT",
    "POSSIBLE_SELL_REVIEW",
    "POSSIBLE_5M_TACTICAL_SELL",
    "POSSIBLE_5M_TACTICAL_BUYBACK",
]
HUMAN_REVIEW_ALERT_TYPES = (
    "POSSIBLE_5M_TRADE_BUY",
    "POSSIBLE_5M_TRADE_SELL",
    "POSSIBLE_30M_BUY",
    "POSSIBLE_30M_EXIT",
    "POSSIBLE_SELL_REVIEW",
    "POSSIBLE_5M_TACTICAL_SELL",
    "POSSIBLE_5M_TACTICAL_BUYBACK",
)
ReviewConfidence = Literal["HIGH", "MEDIUM", "LOW", "UNRESOLVED"]
_RISK_GATES = frozenset({"GREEN", "AMBER", "RED", "UNRESOLVED"})

_FEEDBACK_LEDGER_SCHEMA = "chanlun-human-review-feedback-ledger"
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
HUMAN_REVIEW_SCREEN_SCHEMA = "chanlun-human-review-screen"
MONITOR_ONLY_WARNING_CODE = "MONITOR_ONLY_FORMAL_SELECTION_NOT_PASSED"
SECTOR_HIGHER_TIMEFRAME_REVIEW_EVIDENCE_SCHEMA = (
    "chanlun-human-review-sector-higher-timeframe-evidence"
)
MARKET_SYMBOL_HIGHER_TIMEFRAME_REVIEW_EVIDENCE_SCHEMA = (
    "chanlun-human-review-market-symbol-higher-timeframe-evidence"
)
SECTOR_RANKING_REVIEW_EVIDENCE_SCHEMA = "chanlun-human-review-sector-ranking-evidence"
_HIGHER_TIMEFRAME_PERIODS = ("M", "W", "D")
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
HIGHER_TIMEFRAME_REVIEW_SOURCE_SUPPORT_SCHEMA = (
    "chanlun-human-review-higher-timeframe-source-support"
)
_SECTOR_NATIVE_DAILY_RESEARCH_BLOCKER = (
    "QMT_SECTOR_NATIVE_DAILY_AND_5M_UNRECONCILED_RESEARCH_BRIDGE"
)

REVIEW_CHECKLIST = (
    "HUMAN_CONFIRM_30M_CONTEXT",
    "HUMAN_CONFIRM_SAME_LEVEL_AND_CENTER_DECOMPOSITION",
    "HUMAN_CONFIRM_30M_TREND_TYPE",
    "HUMAN_CONFIRM_5M_TRADE_POINT",
    "HUMAN_CONFIRM_1M_SEGMENT_DIFFERENCE",
    "HUMAN_CONFIRM_HIGHER_TIMEFRAME_RISK",
    "HUMAN_DEFINE_INVALIDATION_AND_ANY_PAPER_PLAN",
)


@dataclass(frozen=True, slots=True)
class HumanReviewScreeningParameters:
    signal_alignment_parameter_set_id: str
    schema: str = "chanlun-human-review-screening-parameters"
    event_study_horizons: tuple[int, ...] = (5, 10, 20)
    review_priority_bands: tuple[tuple[str, int, int, int], ...] = (
        ("BLOCKED", 8, 0, 19),
        ("NOT_ACTIONABLE", 30, 20, 39),
        ("UNRESOLVED", 30, 20, 39),
        ("CONDITIONAL", 55, 40, 69),
        ("RECOMMENDED", 72, 70, 89),
        ("STRUCTURAL_SELL_REVIEW", 82, 80, 89),
        ("MANUAL_ATTENTION_SELL_REVIEW", 92, 90, 100),
    )
    confidence_bonuses: tuple[tuple[str, int], ...] = (
        ("HIGH", 3),
        ("MEDIUM", 2),
        ("LOW", 1),
        ("UNRESOLVED", 0),
    )
    lifecycle_bonuses: tuple[tuple[str, int], ...] = (
        ("executable", 5),
        ("triggered", 5),
        ("observed", 3),
        ("armed", 3),
        ("formed", 2),
        ("approaching", 1),
    )
    exact_green_bonus: int = 2
    green_risk_gate_bonus: int = 1
    monitor_only_penalty: int = 2
    diagnostic_count_affects_priority: bool = False
    automated_order_authorized: bool = False
    human_confirmation_required: bool = True
    highest_status: str = "REVIEW_REQUIRED"
    live_status: str = "LIVE_DISABLED"

    def __post_init__(self) -> None:
        if (
            self.signal_alignment_parameter_set_id
            != unified_signal_alignment_contract().parameter_set_id
        ):
            raise ValueError("人工复核统一信号对齐身份发生变化")
        if self.event_study_horizons != (5, 10, 20):
            raise ValueError("human review event-study horizons changed")
        expected_bands = {
            "BLOCKED": (8, 0, 19),
            "NOT_ACTIONABLE": (30, 20, 39),
            "UNRESOLVED": (30, 20, 39),
            "CONDITIONAL": (55, 40, 69),
            "RECOMMENDED": (72, 70, 89),
            "STRUCTURAL_SELL_REVIEW": (82, 80, 89),
            "MANUAL_ATTENTION_SELL_REVIEW": (92, 90, 100),
        }
        if {
            status: (base, minimum, maximum)
            for status, base, minimum, maximum in self.review_priority_bands
        } != expected_bands:
            raise ValueError("human review priority bands changed")
        if self.diagnostic_count_affects_priority:
            raise ValueError("diagnostic volume cannot affect review priority")
        if (
            self.automated_order_authorized
            or not self.human_confirmation_required
            or self.highest_status != "REVIEW_REQUIRED"
            or self.live_status != "LIVE_DISABLED"
        ):
            raise ValueError("human review screening cannot enable trading")

    @property
    def parameter_set_id(self) -> str:
        return sha256_json(asdict(self))

    def document(self) -> dict[str, object]:
        return {**asdict(self), "parameter_set_id": self.parameter_set_id}


def human_review_screening_parameters() -> HumanReviewScreeningParameters:
    return HumanReviewScreeningParameters(
        signal_alignment_parameter_set_id=(
            unified_signal_alignment_contract().parameter_set_id
        )
    )


def _parse_qmt_higher_timeframe_warmup_evidence(
    raw: object,
) -> QmtHigherTimeframeWarmupEvidence:
    if not isinstance(raw, Mapping):
        raise ValueError("sector strict warmup evidence is malformed")
    expected_fields = {
        "contract_id",
        "required_daily_bar_count",
        "full_daily_bar_count",
        "suffix_daily_bar_count",
        "converged",
        "reason_code",
        "full_signature",
        "suffix_signature",
        "entry_disposition",
    }
    if set(raw) != expected_fields or raw.get("contract_id") != (
        QMT_HIGHER_TIMEFRAME_WARMUP_EVIDENCE_CONTRACT_ID
    ):
        raise ValueError("sector strict warmup evidence contract changed")
    if (
        type(raw.get("required_daily_bar_count")) is not int
        or type(raw.get("full_daily_bar_count")) is not int
        or type(raw.get("suffix_daily_bar_count")) is not int
        or type(raw.get("converged")) is not bool
        or not isinstance(raw.get("reason_code"), str)
        or _SHA256.fullmatch(str(raw.get("full_signature"))) is None
        or (
            raw.get("suffix_signature") is not None
            and _SHA256.fullmatch(str(raw.get("suffix_signature"))) is None
        )
    ):
        raise ValueError("sector strict warmup evidence values are invalid")
    try:
        evidence = QmtHigherTimeframeWarmupEvidence(
            required_daily_bar_count=int(raw["required_daily_bar_count"]),
            full_daily_bar_count=int(raw["full_daily_bar_count"]),
            suffix_daily_bar_count=int(raw["suffix_daily_bar_count"]),
            converged=bool(raw["converged"]),
            reason_code=str(raw["reason_code"]),  # type: ignore[arg-type]
            full_signature=str(raw["full_signature"]),
            suffix_signature=(
                None
                if raw["suffix_signature"] is None
                else str(raw["suffix_signature"])
            ),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("sector strict warmup evidence is inconsistent") from exc
    if dict(raw) != evidence.document():
        raise ValueError("sector strict warmup evidence semantics changed")
    return evidence


def _parse_warmup_convergence_evidence(
    raw: object,
    *,
    evidence_cutoff: datetime,
) -> WarmupConvergenceEnvelope:
    if not isinstance(raw, Mapping):
        raise ValueError("warmup convergence review evidence is malformed")
    try:
        evidence = WarmupConvergenceEnvelope.from_document(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("warmup convergence review evidence is invalid") from exc
    if (
        evidence.as_of != evidence_cutoff
        or evidence.frequency != "d"
        or evidence.parameter_set_id
        != QMT_HIGHER_TIMEFRAME_WARMUP_CONVERGENCE_PARAMETER_SET_ID
        or evidence.contract_id != WARMUP_CONVERGENCE_ENVELOPE_CONTRACT_ID
        or evidence.diagnostic_only is not True
        or evidence.active_gate_unchanged is not True
    ):
        raise ValueError("warmup convergence review identity changed")
    return evidence


def _bind_warmup_semantic_diagnostic_evidence(
    envelope: WarmupConvergenceEnvelope,
    raw: object,
    *,
    evidence_cutoff: datetime,
) -> WarmupConvergenceEnvelope:
    """Validate an optional sibling diagnostic and retain it in memory."""

    if raw is None:
        return envelope
    if not isinstance(raw, Mapping):
        raise ValueError("warmup semantic diagnostic review evidence is malformed")
    try:
        diagnostic = WarmupConvergenceDiagnosticEnvelope.from_document(raw)
        diagnostic.validate_against(envelope)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "warmup semantic diagnostic review evidence is invalid"
        ) from exc
    if (
        diagnostic.as_of != evidence_cutoff
        or diagnostic.frequency != "d"
        or diagnostic.parameter_set_id
        != QMT_HIGHER_TIMEFRAME_WARMUP_CONVERGENCE_PARAMETER_SET_ID
        or diagnostic.contract_id != WARMUP_CONVERGENCE_DIAGNOSTIC_CONTRACT_ID
        or diagnostic.diagnostic_only is not True
        or diagnostic.active_gate_unchanged is not True
        or diagnostic.live_status != "LIVE_DISABLED"
    ):
        raise ValueError("warmup semantic diagnostic review identity changed")
    return replace(envelope, diagnostic=diagnostic)


def _bind_warmup_mapping_supply_diagnostic_evidence(
    envelope: WarmupConvergenceEnvelope,
    raw: object,
    *,
    evidence_cutoff: datetime,
) -> WarmupConvergenceEnvelope:
    """Validate the optional point-level sibling against its parent hashes."""

    if raw is None:
        return envelope
    if envelope.diagnostic is None:
        raise ValueError("mapping supply diagnostic requires semantic evidence")
    if not isinstance(raw, Mapping):
        raise ValueError("warmup mapping supply review evidence is malformed")
    try:
        diagnostic = WarmupMappingSupplyDiagnosticEnvelope.from_document(raw)
        diagnostic.validate_against(envelope)
    except (TypeError, ValueError) as exc:
        raise ValueError("warmup mapping supply review evidence is invalid") from exc
    if (
        diagnostic.as_of != evidence_cutoff
        or diagnostic.frequency != "d"
        or diagnostic.parameter_set_id
        != QMT_HIGHER_TIMEFRAME_WARMUP_CONVERGENCE_PARAMETER_SET_ID
        or diagnostic.contract_id != WARMUP_MAPPING_SUPPLY_DIAGNOSTIC_CONTRACT_ID
        or diagnostic.diagnostic_only is not True
        or diagnostic.active_gate_unchanged is not True
        or diagnostic.live_status != "LIVE_DISABLED"
    ):
        raise ValueError("warmup mapping supply review identity changed")
    return replace(envelope, mapping_supply_diagnostic=diagnostic)


def _bind_warmup_structure_lineage_diagnostic_evidence(
    envelope: WarmupConvergenceEnvelope,
    raw: object,
    *,
    evidence_cutoff: datetime,
) -> WarmupConvergenceEnvelope:
    """Validate the optional structure sibling against all prior hashes."""

    if raw is None:
        return envelope
    if envelope.diagnostic is None or envelope.mapping_supply_diagnostic is None:
        raise ValueError("structure lineage requires semantic and supply evidence")
    if not isinstance(raw, Mapping):
        raise ValueError("warmup structure lineage review evidence is malformed")
    try:
        diagnostic = WarmupStructureLineageDiagnosticEnvelope.from_document(raw)
        diagnostic.validate_against(envelope)
    except (TypeError, ValueError) as exc:
        raise ValueError("warmup structure lineage review evidence is invalid") from exc
    if (
        diagnostic.as_of != evidence_cutoff
        or diagnostic.frequency != "d"
        or diagnostic.parameter_set_id
        != QMT_HIGHER_TIMEFRAME_WARMUP_CONVERGENCE_PARAMETER_SET_ID
        or diagnostic.contract_id != WARMUP_STRUCTURE_LINEAGE_DIAGNOSTIC_CONTRACT_ID
        or diagnostic.diagnostic_only is not True
        or diagnostic.active_gate_unchanged is not True
        or diagnostic.live_status != "LIVE_DISABLED"
    ):
        raise ValueError("warmup structure lineage review identity changed")
    return replace(envelope, structure_lineage_diagnostic=diagnostic)


@dataclass(frozen=True, slots=True)
class SectorHigherTimeframeReviewEvidence:
    """Portable current sector M/W/D decision and source proof."""

    source_mode: Literal[
        "PAGE_PARITY_SAME_5M_BASE",
        "NATIVE_DAILY_MWD_PLUS_5M_30M_UNRECONCILED_RESEARCH",
    ]
    strict_same_5m_warmup_evidence: QmtHigherTimeframeWarmupEvidence
    research_bridge_parameter_set_id: str | None
    strict_same_5m_source_coverage_evidence: QmtSectorSameBaseCoverageEvidence
    warmup_convergence_evidence: WarmupConvergenceEnvelope
    strict_same_5m_warmup_convergence_evidence: WarmupConvergenceEnvelope
    sector_id: str
    observed_at: datetime
    gate: str
    states: tuple[tuple[str, str], ...]
    reason_codes: tuple[str, ...]
    period_diagnostics: tuple[HigherTimeframePeriodDiagnostic, ...]
    schema: str = SECTOR_HIGHER_TIMEFRAME_REVIEW_EVIDENCE_SCHEMA
    live_status: str = "LIVE_DISABLED"

    def __post_init__(self) -> None:
        object.__setattr__(self, "states", tuple(self.states))
        object.__setattr__(self, "reason_codes", tuple(self.reason_codes))
        object.__setattr__(self, "period_diagnostics", tuple(self.period_diagnostics))
        if self.source_mode not in {
            QMT_SECTOR_SAME_BASE_SOURCE_MODE,
            QMT_SECTOR_NATIVE_DAILY_RESEARCH_SOURCE_MODE,
        }:
            raise ValueError("sector review source mode is invalid")
        if not isinstance(
            self.strict_same_5m_warmup_evidence,
            QmtHigherTimeframeWarmupEvidence,
        ):
            raise ValueError("sector review strict warmup evidence is required")
        if self.source_mode == QMT_SECTOR_NATIVE_DAILY_RESEARCH_SOURCE_MODE:
            expected_bridge = sector_native_daily_research_bridge_contract()[
                "parameter_set_id"
            ]
            if (
                self.research_bridge_parameter_set_id != expected_bridge
                or self.strict_same_5m_warmup_evidence.reason_code
                != "QMT_HIGHER_TIMEFRAME_WARMUP_HISTORY_INSUFFICIENT"
            ):
                raise ValueError("sector review research bridge is unsafe")
        elif self.research_bridge_parameter_set_id is not None:
            raise ValueError("same-base sector review cannot carry a research bridge")
        if self.live_status != "LIVE_DISABLED":
            raise ValueError("sector review evidence cannot enable live trading")
        if self.schema != SECTOR_HIGHER_TIMEFRAME_REVIEW_EVIDENCE_SCHEMA:
            raise ValueError("sector review evidence schema is unsupported")
        if (
            not isinstance(self.sector_id, str)
            or not self.sector_id
            or self.gate not in _RISK_GATES
        ):
            raise ValueError("sector review decision identity is incomplete")
        observed_at = normalize_datetime(self.observed_at, "observed_at")
        object.__setattr__(self, "observed_at", observed_at)
        coverage = self.strict_same_5m_source_coverage_evidence
        if not isinstance(coverage, QmtSectorSameBaseCoverageEvidence):
            raise ValueError("sector review strict 5m coverage is required")
        if (
            coverage.observed_at != observed_at
            or coverage.completed_daily_bar_count
            < self.strict_same_5m_warmup_evidence.full_daily_bar_count
            or coverage.required_daily_bar_count
            != self.strict_same_5m_warmup_evidence.required_daily_bar_count
            or coverage.warmup_converged
            != self.strict_same_5m_warmup_evidence.converged
            or coverage.warmup_reason_code
            != self.strict_same_5m_warmup_evidence.reason_code
        ):
            raise ValueError("sector review strict 5m coverage and warmup differ")
        convergence = self.warmup_convergence_evidence
        strict_convergence = self.strict_same_5m_warmup_convergence_evidence
        if (
            not isinstance(convergence, WarmupConvergenceEnvelope)
            or not isinstance(strict_convergence, WarmupConvergenceEnvelope)
            or convergence.as_of != observed_at
            or strict_convergence.as_of != observed_at
            or convergence.frequency != "d"
            or strict_convergence.frequency != "d"
        ):
            raise ValueError("sector review convergence evidence is required")
        if (
            self.source_mode == QMT_SECTOR_SAME_BASE_SOURCE_MODE
            and convergence != strict_convergence
        ):
            raise ValueError("same-base sector convergence evidence differs")
        if tuple(period for period, _state in self.states) != (
            _HIGHER_TIMEFRAME_PERIODS
        ):
            raise ValueError("sector review states must be ordered M/W/D")
        state_values = tuple(state for _period, state in self.states)
        unresolved = state_values == ("UNRESOLVED",) * 3
        if not unresolved and any(
            state not in HIGHER_TIMEFRAME_RISK_STATES for state in state_values
        ):
            raise ValueError("sector review state is invalid")
        if any(
            not isinstance(value, str) or not value for value in self.reason_codes
        ) or len(self.reason_codes) != len(set(self.reason_codes)):
            raise ValueError("sector review reasons must be unique")
        diagnostics = self.period_diagnostics
        if any(
            not isinstance(value, HigherTimeframePeriodDiagnostic)
            for value in diagnostics
        ):
            raise ValueError("sector review diagnostics are invalid")
        if unresolved:
            if self.gate != "UNRESOLVED" or not self.reason_codes:
                raise ValueError("unresolved sector review lacks a cause")
            # 与市场/标的证据相同，关闭失败的安全覆盖会移除有效板块状态，但必须保留
            # 可重放的原始月/周/日诊断；真正不可用的板块来源仍以空诊断元组表示。
        else:
            if (
                len(diagnostics) != 3
                or tuple(value.period for value in diagnostics)
                != _HIGHER_TIMEFRAME_PERIODS
                or tuple(value.state for value in diagnostics) != state_values
            ):
                raise ValueError("sector review states and diagnostics differ")
            expected_gate = higher_timeframe_risk_gate(
                states=state_values,  # type: ignore[arg-type]
                completed_ma5_available=True,
                mapping_unique=all(value.mapping_unique for value in diagnostics),
            )
            if (
                self.source_mode == QMT_SECTOR_NATIVE_DAILY_RESEARCH_SOURCE_MODE
                and expected_gate == "GREEN"
            ):
                expected_gate = "AMBER"
            if self.gate != expected_gate:
                raise ValueError("sector review gate cannot be reproduced")
        for diagnostic in diagnostics:
            if (
                diagnostic.evidence_bar_end is not None
                and diagnostic.evidence_bar_end > observed_at
            ) or (
                diagnostic.active_top_interval is not None
                and diagnostic.active_top_interval[1] > observed_at
            ):
                raise ValueError("sector review evidence contains future data")
            if any(code not in self.reason_codes for code in diagnostic.blocker_codes):
                raise ValueError("sector review diagnostic blocker was omitted")
            if (
                not diagnostic.mapping_unique
                and f"{diagnostic.period}_CENTER_MAPPING_UNRESOLVED"
                not in self.reason_codes
            ):
                raise ValueError("sector review mapping ambiguity was omitted")

    def _stable_document(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "source_mode": self.source_mode,
            "strict_same_5m_warmup_evidence": (
                self.strict_same_5m_warmup_evidence.document()
            ),
            "research_bridge_parameter_set_id": (self.research_bridge_parameter_set_id),
            "strict_same_5m_source_coverage_evidence": (
                self.strict_same_5m_source_coverage_evidence.document()
            ),
            "warmup_convergence_evidence": (
                self.warmup_convergence_evidence.document()
            ),
            "strict_same_5m_warmup_convergence_evidence": (
                self.strict_same_5m_warmup_convergence_evidence.document()
            ),
            "sector_id": self.sector_id,
            "observed_at": self.observed_at.isoformat(),
            "gate": self.gate,
            "states": {period: state for period, state in self.states},
            "reason_codes": list(self.reason_codes),
            "period_diagnostics": [
                value.document() for value in self.period_diagnostics
            ],
            "live_status": self.live_status,
        }

    @property
    def evidence_id(self) -> str:
        return sha256_json(self._stable_document())

    def document(self) -> dict[str, object]:
        stable = self._stable_document()
        return {**stable, "evidence_id": self.evidence_id}


def parse_sector_higher_timeframe_review_evidence(
    raw: object,
) -> SectorHigherTimeframeReviewEvidence:
    expected_fields = {
        "schema",
        "source_mode",
        "strict_same_5m_warmup_evidence",
        "research_bridge_parameter_set_id",
        "strict_same_5m_source_coverage_evidence",
        "warmup_convergence_evidence",
        "strict_same_5m_warmup_convergence_evidence",
        "sector_id",
        "observed_at",
        "gate",
        "states",
        "reason_codes",
        "period_diagnostics",
        "live_status",
        "evidence_id",
    }
    if (
        not isinstance(raw, Mapping)
        or set(raw) != expected_fields
        or raw.get("schema") != SECTOR_HIGHER_TIMEFRAME_REVIEW_EVIDENCE_SCHEMA
    ):
        raise ValueError("sector higher-timeframe review evidence is malformed")
    try:
        observed_at = normalize_datetime(
            datetime.fromisoformat(str(raw["observed_at"])),
            "sector_review.observed_at",
        )
        raw_states = raw["states"]
        raw_reasons = raw["reason_codes"]
        raw_coverage = raw["strict_same_5m_source_coverage_evidence"]
        if (
            not isinstance(raw_states, Mapping)
            or set(raw_states) != set(_HIGHER_TIMEFRAME_PERIODS)
            or not isinstance(raw_reasons, list)
            or any(not isinstance(value, str) or not value for value in raw_reasons)
            or not isinstance(raw_coverage, Mapping)
        ):
            raise ValueError("sector higher-timeframe decision evidence is invalid")
        evidence = SectorHigherTimeframeReviewEvidence(
            source_mode=str(raw["source_mode"]),  # type: ignore[arg-type]
            strict_same_5m_warmup_evidence=(
                _parse_qmt_higher_timeframe_warmup_evidence(
                    raw["strict_same_5m_warmup_evidence"]
                )
            ),
            research_bridge_parameter_set_id=(
                None
                if raw["research_bridge_parameter_set_id"] is None
                else str(raw["research_bridge_parameter_set_id"])
            ),
            strict_same_5m_source_coverage_evidence=(
                QmtSectorSameBaseCoverageEvidence.from_document(raw_coverage)
            ),
            warmup_convergence_evidence=_parse_warmup_convergence_evidence(
                raw["warmup_convergence_evidence"],
                evidence_cutoff=observed_at,
            ),
            strict_same_5m_warmup_convergence_evidence=(
                _parse_warmup_convergence_evidence(
                    raw["strict_same_5m_warmup_convergence_evidence"],
                    evidence_cutoff=observed_at,
                )
            ),
            sector_id=str(raw["sector_id"]),
            observed_at=observed_at,
            gate=str(raw["gate"]),
            states=tuple(
                (period, str(raw_states[period]))
                for period in _HIGHER_TIMEFRAME_PERIODS
            ),
            reason_codes=tuple(raw_reasons),
            period_diagnostics=_parse_review_period_diagnostics(
                raw["period_diagnostics"], evidence_cutoff=observed_at
            ),
            live_status=str(raw["live_status"]),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("sector higher-timeframe review evidence is invalid") from exc
    if raw.get("evidence_id") != evidence.evidence_id:
        raise ValueError("sector higher-timeframe review evidence identity changed")
    return evidence


def sector_higher_timeframe_review_evidence_from_risk(
    risk: Mapping[str, object],
    *,
    sector_id: str | None = None,
    observed_at: datetime | None = None,
) -> SectorHigherTimeframeReviewEvidence | None:
    fields = (
        "sector_higher_timeframe_source_mode",
        "sector_strict_same_5m_warmup_evidence",
        "sector_research_bridge_parameter_set_id",
    )
    present = tuple(field in risk for field in fields)
    if not any(present):
        return None
    if not all(present):
        raise ValueError("sector higher-timeframe risk source fields are incomplete")
    coverage_field = "sector_strict_same_5m_source_coverage_evidence"
    if coverage_field not in risk:
        raise ValueError("sector strict 5m coverage evidence is required")
    raw_coverage = risk[coverage_field]
    convergence_field = "sector_warmup_convergence_evidence"
    strict_convergence_field = "sector_strict_same_5m_warmup_convergence_evidence"
    convergence_present = (
        convergence_field in risk,
        strict_convergence_field in risk,
    )
    if not all(convergence_present):
        raise ValueError("sector convergence review evidence is required")
    if sector_id is None or observed_at is None:
        raise ValueError("sector review decision identity is incomplete")
    decision_fields = (
        "sector_gate",
        "sector_states",
        "sector_reason_codes",
        "sector_period_diagnostics",
    )
    if not all(field in risk for field in decision_fields):
        raise ValueError("sector higher-timeframe decision fields are incomplete")
    raw_states = risk["sector_states"]
    raw_reasons = risk["sector_reason_codes"]
    if (
        not isinstance(raw_states, Mapping)
        or set(raw_states) != set(_HIGHER_TIMEFRAME_PERIODS)
        or not isinstance(raw_reasons, list)
        or any(not isinstance(value, str) or not value for value in raw_reasons)
    ):
        raise ValueError("sector higher-timeframe decision evidence is malformed")
    cutoff = normalize_datetime(observed_at, "observed_at")
    if not isinstance(raw_coverage, Mapping):
        raise ValueError("sector strict 5m coverage evidence is malformed")
    coverage = QmtSectorSameBaseCoverageEvidence.from_document(raw_coverage)
    selected_convergence = _parse_warmup_convergence_evidence(
        risk[convergence_field],
        evidence_cutoff=cutoff,
    )
    strict_convergence = _parse_warmup_convergence_evidence(
        risk[strict_convergence_field],
        evidence_cutoff=cutoff,
    )
    diagnostic_contract_field = "warmup_convergence_diagnostic_contract_id"
    selected_diagnostic_field = "sector_warmup_convergence_diagnostic_evidence"
    strict_diagnostic_field = (
        "sector_strict_same_5m_warmup_convergence_diagnostic_evidence"
    )
    diagnostic_presence = (
        diagnostic_contract_field in risk,
        selected_diagnostic_field in risk,
        strict_diagnostic_field in risk,
    )
    if any(diagnostic_presence):
        if (
            not all(diagnostic_presence)
            or not all(convergence_present)
            or risk.get(diagnostic_contract_field)
            != WARMUP_CONVERGENCE_DIAGNOSTIC_CONTRACT_ID
            or selected_convergence is None
            or strict_convergence is None
        ):
            raise ValueError("sector semantic diagnostic fields are incomplete")
        selected_convergence = _bind_warmup_semantic_diagnostic_evidence(
            selected_convergence,
            risk[selected_diagnostic_field],
            evidence_cutoff=cutoff,
        )
        strict_convergence = _bind_warmup_semantic_diagnostic_evidence(
            strict_convergence,
            risk[strict_diagnostic_field],
            evidence_cutoff=cutoff,
        )
    supply_contract_field = "warmup_mapping_supply_diagnostic_contract_id"
    selected_supply_field = "sector_warmup_mapping_supply_diagnostic_evidence"
    strict_supply_field = (
        "sector_strict_same_5m_warmup_mapping_supply_diagnostic_evidence"
    )
    supply_presence = (
        supply_contract_field in risk,
        selected_supply_field in risk,
        strict_supply_field in risk,
    )
    if any(supply_presence):
        if (
            not all(supply_presence)
            or not all(diagnostic_presence)
            or risk.get(supply_contract_field)
            != WARMUP_MAPPING_SUPPLY_DIAGNOSTIC_CONTRACT_ID
            or selected_convergence is None
            or strict_convergence is None
        ):
            raise ValueError("sector mapping supply diagnostic fields are incomplete")
        selected_convergence = _bind_warmup_mapping_supply_diagnostic_evidence(
            selected_convergence,
            risk[selected_supply_field],
            evidence_cutoff=cutoff,
        )
        strict_convergence = _bind_warmup_mapping_supply_diagnostic_evidence(
            strict_convergence,
            risk[strict_supply_field],
            evidence_cutoff=cutoff,
        )
    lineage_contract_field = "warmup_structure_lineage_diagnostic_contract_id"
    selected_lineage_field = "sector_warmup_structure_lineage_diagnostic_evidence"
    strict_lineage_field = (
        "sector_strict_same_5m_warmup_structure_lineage_diagnostic_evidence"
    )
    lineage_presence = (
        lineage_contract_field in risk,
        selected_lineage_field in risk,
        strict_lineage_field in risk,
    )
    if any(lineage_presence):
        if (
            not all(lineage_presence)
            or not all(supply_presence)
            or risk.get(lineage_contract_field)
            != WARMUP_STRUCTURE_LINEAGE_DIAGNOSTIC_CONTRACT_ID
            or selected_convergence is None
            or strict_convergence is None
        ):
            raise ValueError(
                "sector structure lineage diagnostic fields are incomplete"
            )
        selected_convergence = _bind_warmup_structure_lineage_diagnostic_evidence(
            selected_convergence,
            risk[selected_lineage_field],
            evidence_cutoff=cutoff,
        )
        strict_convergence = _bind_warmup_structure_lineage_diagnostic_evidence(
            strict_convergence,
            risk[strict_lineage_field],
            evidence_cutoff=cutoff,
        )
    return SectorHigherTimeframeReviewEvidence(
        source_mode=str(risk[fields[0]]),  # type: ignore[arg-type]
        strict_same_5m_warmup_evidence=(
            _parse_qmt_higher_timeframe_warmup_evidence(risk[fields[1]])
        ),
        research_bridge_parameter_set_id=(
            None if risk[fields[2]] is None else str(risk[fields[2]])
        ),
        strict_same_5m_source_coverage_evidence=coverage,
        warmup_convergence_evidence=selected_convergence,
        strict_same_5m_warmup_convergence_evidence=strict_convergence,
        sector_id=sector_id,
        observed_at=cutoff,
        gate=str(risk["sector_gate"]),
        states=tuple(
            (period, str(raw_states[period])) for period in _HIGHER_TIMEFRAME_PERIODS
        ),
        reason_codes=tuple(raw_reasons),
        period_diagnostics=_parse_review_period_diagnostics(
            risk["sector_period_diagnostics"], evidence_cutoff=cutoff
        ),
    )


def _parse_review_period_diagnostics(
    raw: object,
    *,
    evidence_cutoff: datetime,
) -> tuple[HigherTimeframePeriodDiagnostic, ...]:
    """Parse the compact causal M/W/D explanation retained for a reviewer."""

    if not isinstance(raw, list) or len(raw) not in {0, 3}:
        raise ValueError("higher-timeframe review diagnostics are malformed")
    output: list[HigherTimeframePeriodDiagnostic] = []
    base_fields = {
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
    for expected_period, value in zip(_HIGHER_TIMEFRAME_PERIODS, raw):
        if not isinstance(value, Mapping):
            raise ValueError("higher-timeframe review diagnostic changed")
        expected_fields = (
            base_fields
            if value.get("state") == "NONE"
            else base_fields | {"mapping_supply"}
        )
        if set(value) != expected_fields:
            raise ValueError("higher-timeframe review diagnostic changed")
        if (
            value.get("period") != expected_period
            or not isinstance(value.get("state"), str)
            or value.get("state") not in HIGHER_TIMEFRAME_RISK_STATES
            or type(value.get("completed_bar_count")) is not int
            or int(value["completed_bar_count"]) < 0
            or type(value.get("mapping_unique")) is not bool
            or (
                value.get("mapped_center_id") is not None
                and (
                    not isinstance(value.get("mapped_center_id"), str)
                    or not str(value["mapped_center_id"])
                )
            )
            or _SHA256.fullmatch(str(value.get("source_revision"))) is None
        ):
            raise ValueError("higher-timeframe review diagnostic is invalid")
        tuple_fields: dict[str, tuple[str, ...]] = {}
        for field in (
            "mapping_candidate_ids",
            "blocker_codes",
            "warning_codes",
        ):
            values = value.get(field)
            if (
                not isinstance(values, list)
                or any(not isinstance(item, str) or not item for item in values)
                or len(values) != len(set(values))
            ):
                raise ValueError("higher-timeframe review diagnostic is invalid")
            tuple_fields[field] = tuple(values)
        evidence_bar_end = None
        if value.get("evidence_bar_end") is not None:
            evidence_bar_end = normalize_datetime(
                datetime.fromisoformat(str(value["evidence_bar_end"])),
                "higher_timeframe_review.evidence_bar_end",
            )
            if evidence_bar_end > evidence_cutoff:
                raise ValueError("higher-timeframe review used a future bar")
        active_interval = None
        raw_interval = value.get("active_top_interval")
        if raw_interval is not None:
            if not isinstance(raw_interval, list) or len(raw_interval) != 2:
                raise ValueError("higher-timeframe review interval is malformed")
            active_interval = tuple(
                normalize_datetime(
                    datetime.fromisoformat(str(item)),
                    "higher_timeframe_review.active_top_interval",
                )
                for item in raw_interval
            )
            if (
                active_interval[0] > active_interval[1]
                or active_interval[1] > evidence_cutoff
                or evidence_bar_end is None
                or active_interval[1] > evidence_bar_end
            ):
                raise ValueError("higher-timeframe review interval is invalid")
        state = str(value["state"])
        mapping_unique = bool(value["mapping_unique"])
        mapped_center_id = value.get("mapped_center_id")
        mapping_supply = None
        if value.get("mapping_supply") is not None:
            mapping_supply = RiskMappingSupplyFacts.from_document(
                value.get("mapping_supply")
            )
        if (state == "NONE" and mapping_supply is not None) or (
            state != "NONE" and mapping_supply is None
        ):
            raise ValueError("higher-timeframe mapping supply is inconsistent")
        if state == "NONE":
            if (
                active_interval is not None
                or not mapping_unique
                or mapped_center_id is not None
                or tuple_fields["mapping_candidate_ids"]
                or tuple_fields["blocker_codes"]
            ):
                raise ValueError("empty higher-timeframe state is inconsistent")
        elif (
            active_interval is None
            or evidence_bar_end is None
            or int(value["completed_bar_count"]) == 0
        ):
            raise ValueError("formed higher-timeframe state lacks evidence")
        if mapping_unique and state != "NONE" and not isinstance(mapped_center_id, str):
            raise ValueError("unique higher-timeframe mapping lacks a center")
        if not mapping_unique and (
            mapped_center_id is not None or not tuple_fields["blocker_codes"]
        ):
            raise ValueError("ambiguous higher-timeframe mapping is unexplained")
        diagnostic = HigherTimeframePeriodDiagnostic(
            period=expected_period,  # type: ignore[arg-type]
            state=state,
            completed_bar_count=int(value["completed_bar_count"]),
            evidence_bar_end=evidence_bar_end,
            active_top_interval=active_interval,  # type: ignore[arg-type]
            mapping_unique=mapping_unique,
            mapped_center_id=(
                None if mapped_center_id is None else str(mapped_center_id)
            ),
            mapping_candidate_ids=tuple_fields["mapping_candidate_ids"],
            blocker_codes=tuple_fields["blocker_codes"],
            warning_codes=tuple_fields["warning_codes"],
            source_revision=str(value["source_revision"]),
            mapping_supply=mapping_supply,
        )
        if diagnostic.document() != dict(value):
            raise ValueError("higher-timeframe review diagnostic semantics changed")
        output.append(diagnostic)
    return tuple(output)


def _parse_review_session_evidence(
    raw: object,
    *,
    evidence_cutoff: datetime,
) -> HigherTimeframeSessionEvidence:
    expected = {
        "contract_id",
        "status",
        "issue_count",
        "issues",
        "entry_disposition",
    }
    if not isinstance(raw, Mapping) or set(raw) != expected:
        raise ValueError("higher-timeframe review session evidence is malformed")
    status = raw.get("status")
    raw_issues = raw.get("issues")
    if (
        raw.get("contract_id") != HIGHER_TIMEFRAME_SESSION_EVIDENCE_CONTRACT_ID
        or status not in {"EXACT", "UNAVAILABLE"}
        or type(raw.get("issue_count")) is not int
        or not isinstance(raw_issues, list)
        or raw["issue_count"] != len(raw_issues)
        or (status == "UNAVAILABLE" and raw_issues)
    ):
        raise ValueError("higher-timeframe review session evidence is invalid")
    issues: list[QmtMinuteSessionIssue] = []
    for value in raw_issues:
        if not isinstance(value, Mapping):
            raise ValueError("higher-timeframe review session issue is malformed")
        try:
            issue = QmtMinuteSessionIssue(
                session=date.fromisoformat(str(value["session"])),
                code=str(value["code"]),  # type: ignore[arg-type]
                observed_rows=value["observed_rows"],  # type: ignore[arg-type]
                detail=str(value["detail"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                "higher-timeframe review session issue is invalid"
            ) from exc
        if (
            set(value) != set(issue.document())
            or dict(value) != issue.document()
            or issue.session > evidence_cutoff.date()
        ):
            raise ValueError("higher-timeframe review session issue semantics changed")
        issues.append(issue)
    try:
        evidence = HigherTimeframeSessionEvidence(
            status=status,  # type: ignore[arg-type]
            issues=tuple(issues),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "higher-timeframe review session evidence is inconsistent"
        ) from exc
    if evidence.document() != dict(raw):
        raise ValueError("higher-timeframe review session evidence semantics changed")
    return evidence


def _parse_review_native_daily_evidence(
    raw: object,
    *,
    evidence_cutoff: datetime,
    expected_symbol: str | None,
) -> QmtNativeDailyReconciliationEvidence:
    expected = {
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
    if not isinstance(raw, Mapping) or set(raw) != expected:
        raise ValueError("native-daily review evidence is malformed")
    symbol = raw.get("symbol")
    identities = raw.get("price_difference_identities")
    count_fields = (
        "native_daily_bar_count",
        "one_minute_daily_bar_count",
        "overlap_session_count",
    )
    if (
        raw.get("contract_id") != QMT_NATIVE_DAILY_RECONCILIATION_CONTRACT_ID
        or not isinstance(symbol, str)
        or not symbol
        or (expected_symbol is not None and symbol != expected_symbol)
        or any(type(raw.get(field)) is not int for field in count_fields)
        or any(int(raw[field]) <= 0 for field in count_fields)
        or not isinstance(identities, list)
        or any(not isinstance(value, str) or not value for value in identities)
        or len(identities) != len(set(identities))
        or type(raw.get("price_tolerance_quanta")) is not int
        or raw.get("price_tolerance_quanta") not in {0, 1}
        or type(raw.get("max_observed_price_difference_quanta")) is not int
        or int(raw["max_observed_price_difference_quanta"]) < 0
        or int(raw["max_observed_price_difference_quanta"])
        > int(raw["price_tolerance_quanta"])
    ):
        raise ValueError("native-daily review evidence is invalid")
    try:
        observed_at = normalize_datetime(
            datetime.fromisoformat(str(raw["observed_at"])),
            "native_daily_review.observed_at",
        )
        first = date.fromisoformat(str(raw["first_overlap_session"]))
        last = date.fromisoformat(str(raw["last_overlap_session"]))
    except (TypeError, ValueError) as exc:
        raise ValueError("native-daily review dates are invalid") from exc
    native_count, minute_count, overlap_count = (
        int(raw[field]) for field in count_fields
    )
    if (
        observed_at != evidence_cutoff
        or first > last
        or last > evidence_cutoff.date()
        or overlap_count > min(native_count, minute_count)
        or (overlap_count == 1) != (first == last)
    ):
        raise ValueError("native-daily review chronology is invalid")
    revision_fields = (
        "native_daily_content_revision",
        "one_minute_base_revision",
        "price_basis_revision",
        "trading_calendar_revision",
        "reconciled_source_revision",
    )
    if any(_SHA256.fullmatch(str(raw.get(field))) is None for field in revision_fields):
        raise ValueError("native-daily review source identity is invalid")
    difference_sessions: set[date] = set()
    for identity in identities:
        matched = re.fullmatch(
            r"([0-9]{4}-[0-9]{2}-[0-9]{2}):(open|high|low|close)",
            identity,
        )
        if matched is None:
            raise ValueError("native-daily price difference is malformed")
        session = date.fromisoformat(matched.group(1))
        if not first <= session <= last:
            raise ValueError("native-daily price difference escaped overlap")
        difference_sessions.add(session)
    if (
        raw.get("price_difference_count") != len(identities)
        or raw.get("price_difference_session_count") != len(difference_sessions)
        or raw.get("all_overlap_ohlcv_equal") is not (not identities)
        or raw.get("all_overlap_ohlcv_within_declared_tolerance") is not True
        or raw.get("native_daily_role") != "LEFT_HISTORY_BEFORE_ONE_MINUTE_BASE_ONLY"
        or raw.get("intraday_role") != "ONE_MINUTE_DERIVED_30M_AND_DAILY_TAIL"
        or raw.get("live_status") != "LIVE_DISABLED"
    ):
        raise ValueError("native-daily review semantics changed")
    try:
        evidence = QmtNativeDailyReconciliationEvidence(
            symbol=symbol,
            observed_at=observed_at,
            native_daily_bar_count=native_count,
            one_minute_daily_bar_count=minute_count,
            overlap_session_count=overlap_count,
            first_overlap_session=first.isoformat(),
            last_overlap_session=last.isoformat(),
            native_daily_content_revision=str(raw["native_daily_content_revision"]),
            one_minute_base_revision=str(raw["one_minute_base_revision"]),
            price_basis_revision=str(raw["price_basis_revision"]),
            trading_calendar_revision=str(raw["trading_calendar_revision"]),
            price_tolerance_quanta=int(raw["price_tolerance_quanta"]),
            price_difference_identities=tuple(identities),
            max_observed_price_difference_quanta=int(
                raw["max_observed_price_difference_quanta"]
            ),
            reconciled_source_revision=str(raw["reconciled_source_revision"]),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("native-daily review evidence is inconsistent") from exc
    if evidence.document() != dict(raw):
        raise ValueError("native-daily review evidence cannot be reproduced")
    return evidence


def _parse_review_native_daily_calendar_coverage(
    raw: object,
    *,
    evidence_cutoff: datetime,
    expected_symbol: str | None,
) -> QmtNativeDailyCalendarCoverageEvidence:
    if not isinstance(raw, Mapping):
        raise ValueError("native-daily calendar review evidence is malformed")
    try:
        evidence = QmtNativeDailyCalendarCoverageEvidence.from_document(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("native-daily calendar review evidence is invalid") from exc
    if (
        evidence.observed_at != evidence_cutoff
        or (expected_symbol is not None and evidence.symbol != expected_symbol)
        or evidence.native_last_session > evidence_cutoff.date()
        or evidence.calendar_last_session > evidence_cutoff.date()
    ):
        raise ValueError("native-daily calendar review chronology is invalid")
    return evidence


@dataclass(frozen=True, slots=True)
class HigherTimeframeReviewSourceSupport:
    """Portable data-sufficiency and source-reconciliation proof for one side."""

    subject: Literal["MARKET", "SYMBOL"]
    session_evidence: HigherTimeframeSessionEvidence | None = None
    warmup_evidence: QmtHigherTimeframeWarmupEvidence | None = None
    warmup_convergence_evidence: WarmupConvergenceEnvelope | None = None
    native_daily_reconciliation_evidence: (
        QmtNativeDailyReconciliationEvidence | None
    ) = None
    native_daily_calendar_coverage_evidence: (
        QmtNativeDailyCalendarCoverageEvidence | None
    ) = None
    schema: str = HIGHER_TIMEFRAME_REVIEW_SOURCE_SUPPORT_SCHEMA
    live_status: str = "LIVE_DISABLED"

    def __post_init__(self) -> None:
        if (
            self.subject not in {"MARKET", "SYMBOL"}
            or self.schema != HIGHER_TIMEFRAME_REVIEW_SOURCE_SUPPORT_SCHEMA
            or self.live_status != "LIVE_DISABLED"
            or all(
                value is None
                for value in (
                    self.session_evidence,
                    self.warmup_evidence,
                    self.warmup_convergence_evidence,
                    self.native_daily_reconciliation_evidence,
                    self.native_daily_calendar_coverage_evidence,
                )
            )
        ):
            raise ValueError("higher-timeframe review source support is invalid")
        if self.session_evidence is not None and not isinstance(
            self.session_evidence,
            HigherTimeframeSessionEvidence,
        ):
            raise ValueError("higher-timeframe session support is invalid")
        if self.warmup_evidence is not None and not isinstance(
            self.warmup_evidence,
            QmtHigherTimeframeWarmupEvidence,
        ):
            raise ValueError("higher-timeframe warmup support is invalid")
        if self.warmup_convergence_evidence is not None and not isinstance(
            self.warmup_convergence_evidence,
            WarmupConvergenceEnvelope,
        ):
            raise ValueError("higher-timeframe convergence support is invalid")
        if self.native_daily_reconciliation_evidence is not None and not isinstance(
            self.native_daily_reconciliation_evidence,
            QmtNativeDailyReconciliationEvidence,
        ):
            raise ValueError("higher-timeframe native-daily support is invalid")
        coverage = self.native_daily_calendar_coverage_evidence
        if coverage is not None and not isinstance(
            coverage,
            QmtNativeDailyCalendarCoverageEvidence,
        ):
            raise ValueError(
                "higher-timeframe native-daily calendar support is invalid"
            )

    def validate_side(self, *, gate: str, reason_codes: tuple[str, ...]) -> None:
        session = self.session_evidence
        reason_issue_codes = _SESSION_ISSUE_CODES.intersection(reason_codes)
        if session is None:
            if reason_issue_codes:
                raise ValueError("higher-timeframe session support is missing")
        elif session.status == "EXACT":
            issue_codes = {value.code for value in session.issues}
            if (
                issue_codes != reason_issue_codes
                or not issue_codes.issubset(set(reason_codes))
                or (issue_codes and gate != "UNRESOLVED")
            ):
                raise ValueError("higher-timeframe session support contradicts gate")
        elif reason_issue_codes or gate != "UNRESOLVED":
            raise ValueError("unavailable session support contradicts gate")

        warmup = self.warmup_evidence
        blocking = _QMT_MWD_WARMUP_BLOCKING_CODES.intersection(reason_codes)
        if warmup is None:
            if blocking:
                raise ValueError("higher-timeframe warmup support is missing")
        elif warmup.converged:
            if blocking or warmup.reason_code in reason_codes:
                raise ValueError("stable warmup support contradicts reasons")
        elif warmup.reason_code not in reason_codes or gate != "UNRESOLVED":
            raise ValueError("failed warmup support contradicts gate")

        coverage = self.native_daily_calendar_coverage_evidence
        calendar_blocked = "QMT_NATIVE_DAILY_TRADING_CALENDAR_MISMATCH" in reason_codes
        if coverage is None:
            if calendar_blocked:
                raise ValueError("native-daily calendar support is missing")
        elif coverage.status == "EXACT":
            if calendar_blocked or self.native_daily_reconciliation_evidence is None:
                raise ValueError("exact native-daily calendar support is inconsistent")
        elif (
            not calendar_blocked
            or gate != "UNRESOLVED"
            or self.native_daily_reconciliation_evidence is not None
        ):
            raise ValueError("failed native-daily calendar support contradicts gate")

    def _stable_document(self) -> dict[str, object]:
        document = {
            "schema": self.schema,
            "subject": self.subject,
            "session_evidence": (
                None
                if self.session_evidence is None
                else self.session_evidence.document()
            ),
            "warmup_evidence": (
                None
                if self.warmup_evidence is None
                else self.warmup_evidence.document()
            ),
            "native_daily_reconciliation_evidence": (
                None
                if self.native_daily_reconciliation_evidence is None
                else self.native_daily_reconciliation_evidence.document()
            ),
            "live_status": self.live_status,
        }
        document["native_daily_calendar_coverage_evidence"] = (
            None
            if self.native_daily_calendar_coverage_evidence is None
            else self.native_daily_calendar_coverage_evidence.document()
        )
        document["warmup_convergence_evidence"] = (
            None
            if self.warmup_convergence_evidence is None
            else self.warmup_convergence_evidence.document()
        )
        return document

    @property
    def support_id(self) -> str:
        return sha256_json(self._stable_document())

    def document(self) -> dict[str, object]:
        stable = self._stable_document()
        return {**stable, "support_id": self.support_id}


@dataclass(frozen=True, slots=True)
class HigherTimeframeReviewSideEvidence:
    """One market or symbol M/W/D gate with independently replayable reasons."""

    subject: Literal["MARKET", "SYMBOL"]
    gate: str
    states: tuple[tuple[str, str], ...]
    reason_codes: tuple[str, ...]
    period_diagnostics: tuple[HigherTimeframePeriodDiagnostic, ...]
    source_support: HigherTimeframeReviewSourceSupport | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "states", tuple(self.states))
        object.__setattr__(self, "reason_codes", tuple(self.reason_codes))
        object.__setattr__(
            self,
            "period_diagnostics",
            tuple(self.period_diagnostics),
        )
        if self.subject not in {"MARKET", "SYMBOL"} or self.gate not in _RISK_GATES:
            raise ValueError("higher-timeframe review side is invalid")
        if tuple(period for period, _state in self.states) != (
            _HIGHER_TIMEFRAME_PERIODS
        ):
            raise ValueError("higher-timeframe review states must be ordered M/W/D")
        state_values = tuple(state for _period, state in self.states)
        unresolved = state_values == ("UNRESOLVED",) * 3
        if not unresolved and any(
            state not in HIGHER_TIMEFRAME_RISK_STATES for state in state_values
        ):
            raise ValueError("higher-timeframe review state is invalid")
        if any(
            not isinstance(value, str) or not value for value in self.reason_codes
        ) or len(self.reason_codes) != len(set(self.reason_codes)):
            raise ValueError("higher-timeframe review reasons must be unique")
        diagnostics = self.period_diagnostics
        if any(
            not isinstance(value, HigherTimeframePeriodDiagnostic)
            for value in diagnostics
        ):
            raise ValueError("higher-timeframe review diagnostics are invalid")
        if diagnostics and tuple(value.period for value in diagnostics) != (
            _HIGHER_TIMEFRAME_PERIODS
        ):
            raise ValueError("higher-timeframe diagnostics must be ordered M/W/D")
        if unresolved:
            if self.gate != "UNRESOLVED" or not self.reason_codes:
                raise ValueError("unresolved higher-timeframe gate lacks a cause")
            # 当安全覆盖（例如预热分歧）移除原本可重放的决策快照时，有效月/周/日状态
            # 会统一压平为 UNRESOLVED。必须保留原始周期诊断，用于解释结构引擎在关闭
            # 失败前看到的事实，也是高周期审计契约的明确要求。不可用来源使用空诊断
            # 元组，因此两种情况可凭证据区分，而非依靠推断。
        else:
            if (
                len(diagnostics) != 3
                or tuple(value.state for value in diagnostics) != state_values
            ):
                raise ValueError("higher-timeframe states and diagnostics differ")
            expected_gate = higher_timeframe_risk_gate(
                states=state_values,  # type: ignore[arg-type]
                completed_ma5_available=True,
                mapping_unique=all(value.mapping_unique for value in diagnostics),
            )
            if self.gate != expected_gate:
                raise ValueError("higher-timeframe review gate cannot be reproduced")
        for diagnostic in diagnostics:
            if any(code not in self.reason_codes for code in diagnostic.blocker_codes):
                raise ValueError("higher-timeframe diagnostic blocker was omitted")
            if (
                not diagnostic.mapping_unique
                and f"{diagnostic.period}_CENTER_MAPPING_UNRESOLVED"
                not in self.reason_codes
            ):
                raise ValueError("higher-timeframe mapping ambiguity was omitted")
        support = self.source_support
        if support is not None:
            if (
                not isinstance(support, HigherTimeframeReviewSourceSupport)
                or support.subject != self.subject
            ):
                raise ValueError("higher-timeframe source support side changed")
            support.validate_side(
                gate=self.gate,
                reason_codes=self.reason_codes,
            )
        elif (
            _SESSION_ISSUE_CODES.intersection(self.reason_codes)
            or _QMT_MWD_WARMUP_BLOCKING_CODES.intersection(self.reason_codes)
            or "QMT_NATIVE_DAILY_TRADING_CALENDAR_MISMATCH" in self.reason_codes
        ):
            raise ValueError("higher-timeframe source support was omitted")

    def document(self) -> dict[str, object]:
        document: dict[str, object] = {
            "subject": self.subject,
            "gate": self.gate,
            "states": {period: state for period, state in self.states},
            "reason_codes": list(self.reason_codes),
            "period_diagnostics": [
                value.document() for value in self.period_diagnostics
            ],
        }
        if self.source_support is not None:
            document["source_support"] = self.source_support.document()
        return document


@dataclass(frozen=True, slots=True)
class MarketSymbolHigherTimeframeReviewEvidence:
    """Portable market + symbol M/W/D evidence bound to one review alert."""

    symbol: str
    observed_at: datetime
    market: HigherTimeframeReviewSideEvidence
    symbol_evidence: HigherTimeframeReviewSideEvidence
    schema: str = MARKET_SYMBOL_HIGHER_TIMEFRAME_REVIEW_EVIDENCE_SCHEMA
    live_status: str = "LIVE_DISABLED"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "observed_at",
            normalize_datetime(self.observed_at, "observed_at"),
        )
        if (
            not self.symbol
            or not isinstance(self.market, HigherTimeframeReviewSideEvidence)
            or not isinstance(
                self.symbol_evidence,
                HigherTimeframeReviewSideEvidence,
            )
            or self.market.subject != "MARKET"
            or self.symbol_evidence.subject != "SYMBOL"
            or self.schema != MARKET_SYMBOL_HIGHER_TIMEFRAME_REVIEW_EVIDENCE_SCHEMA
            or self.live_status != "LIVE_DISABLED"
        ):
            raise ValueError("market/symbol higher-timeframe evidence is invalid")
        for side in (self.market, self.symbol_evidence):
            for diagnostic in side.period_diagnostics:
                if (
                    diagnostic.evidence_bar_end is not None
                    and diagnostic.evidence_bar_end > self.observed_at
                ) or (
                    diagnostic.active_top_interval is not None
                    and diagnostic.active_top_interval[1] > self.observed_at
                ):
                    raise ValueError("market/symbol evidence contains future data")
            support = side.source_support
            if support is None:
                continue
            convergence = support.warmup_convergence_evidence
            if convergence is not None and (
                convergence.as_of != self.observed_at
                or convergence.frequency != "d"
                or convergence.parameter_set_id
                != QMT_HIGHER_TIMEFRAME_WARMUP_CONVERGENCE_PARAMETER_SET_ID
            ):
                raise ValueError(
                    "market/symbol warmup convergence support is mismatched"
                )
            session = support.session_evidence
            if session is not None and any(
                issue.session > self.observed_at.date() for issue in session.issues
            ):
                raise ValueError("market/symbol session support contains future data")
            native = support.native_daily_reconciliation_evidence
            if native is not None and (
                native.observed_at != self.observed_at
                or (side.subject == "SYMBOL" and native.symbol != self.symbol)
            ):
                raise ValueError("market/symbol native-daily support is mismatched")
            calendar_coverage = support.native_daily_calendar_coverage_evidence
            if calendar_coverage is not None and (
                calendar_coverage.observed_at != self.observed_at
                or calendar_coverage.native_last_session > self.observed_at.date()
                or calendar_coverage.calendar_last_session > self.observed_at.date()
                or (
                    side.subject == "SYMBOL" and calendar_coverage.symbol != self.symbol
                )
            ):
                raise ValueError(
                    "market/symbol native-daily calendar support is mismatched"
                )

    def _stable_document(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "symbol": self.symbol,
            "observed_at": self.observed_at.isoformat(),
            "market": self.market.document(),
            "symbol_evidence": self.symbol_evidence.document(),
            "live_status": self.live_status,
        }

    @property
    def evidence_id(self) -> str:
        return sha256_json(self._stable_document())

    def document(self) -> dict[str, object]:
        stable = self._stable_document()
        return {**stable, "evidence_id": self.evidence_id}


def _parse_review_source_support(
    raw: object,
    *,
    expected_subject: Literal["MARKET", "SYMBOL"],
    evidence_cutoff: datetime,
    expected_symbol: str | None,
) -> HigherTimeframeReviewSourceSupport:
    common = {
        "schema",
        "subject",
        "session_evidence",
        "warmup_evidence",
        "native_daily_reconciliation_evidence",
        "live_status",
        "support_id",
    }
    if not isinstance(raw, Mapping):
        raise ValueError("higher-timeframe review source support is malformed")
    expected = common | {
        "native_daily_calendar_coverage_evidence",
        "warmup_convergence_evidence",
    }
    if (
        set(raw) != expected
        or raw.get("subject") != expected_subject
        or raw.get("schema") != HIGHER_TIMEFRAME_REVIEW_SOURCE_SUPPORT_SCHEMA
    ):
        raise ValueError("higher-timeframe review source support is malformed")
    try:
        support = HigherTimeframeReviewSourceSupport(
            subject=expected_subject,
            session_evidence=(
                None
                if raw["session_evidence"] is None
                else _parse_review_session_evidence(
                    raw["session_evidence"],
                    evidence_cutoff=evidence_cutoff,
                )
            ),
            warmup_evidence=(
                None
                if raw["warmup_evidence"] is None
                else _parse_qmt_higher_timeframe_warmup_evidence(raw["warmup_evidence"])
            ),
            warmup_convergence_evidence=(
                None
                if raw["warmup_convergence_evidence"] is None
                else _parse_warmup_convergence_evidence(
                    raw["warmup_convergence_evidence"],
                    evidence_cutoff=evidence_cutoff,
                )
            ),
            native_daily_reconciliation_evidence=(
                None
                if raw["native_daily_reconciliation_evidence"] is None
                else _parse_review_native_daily_evidence(
                    raw["native_daily_reconciliation_evidence"],
                    evidence_cutoff=evidence_cutoff,
                    expected_symbol=expected_symbol,
                )
            ),
            native_daily_calendar_coverage_evidence=(
                None
                if raw["native_daily_calendar_coverage_evidence"] is None
                else _parse_review_native_daily_calendar_coverage(
                    raw["native_daily_calendar_coverage_evidence"],
                    evidence_cutoff=evidence_cutoff,
                    expected_symbol=expected_symbol,
                )
            ),
            schema=str(raw["schema"]),
            live_status=str(raw["live_status"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("higher-timeframe review source support is invalid") from exc
    if raw.get("support_id") != support.support_id:
        raise ValueError("higher-timeframe review source support identity changed")
    return support


def _review_source_supports_from_risk(
    risk: Mapping[str, object],
    *,
    symbol: str,
    evidence_cutoff: datetime,
) -> dict[str, HigherTimeframeReviewSourceSupport | None]:
    values: dict[str, dict[str, object | None]] = {
        prefix: {
            "session_evidence": None,
            "warmup_evidence": None,
            "warmup_convergence_evidence": None,
            "native_daily_reconciliation_evidence": None,
            "native_daily_calendar_coverage_evidence": None,
        }
        for prefix in ("market", "symbol")
    }

    session_fields = (
        "session_evidence_contract_id",
        "market_session_evidence",
        "sector_session_evidence",
        "symbol_session_evidence",
    )
    present = tuple(field in risk for field in session_fields)
    if any(present):
        if not all(present) or risk.get(session_fields[0]) != (
            HIGHER_TIMEFRAME_SESSION_EVIDENCE_CONTRACT_ID
        ):
            raise ValueError("higher-timeframe session support is incomplete")
        # 可移植市场/标的证据不重复板块侧，但仍消费同一个原子上游契约。被忽略成员
        # 也必须校验，避免四字段扩展被部分改写后仅因选中两侧形式正确而获准。
        _parse_review_session_evidence(
            risk["sector_session_evidence"],
            evidence_cutoff=evidence_cutoff,
        )
        for prefix in values:
            values[prefix]["session_evidence"] = _parse_review_session_evidence(
                risk[f"{prefix}_session_evidence"],
                evidence_cutoff=evidence_cutoff,
            )

    calendar_fields = (
        "native_daily_calendar_coverage_contract_id",
        "market_native_daily_calendar_coverage_evidence",
        "sector_native_daily_calendar_coverage_evidence",
        "symbol_native_daily_calendar_coverage_evidence",
    )
    present = tuple(field in risk for field in calendar_fields)
    if any(present):
        if (
            not all(present)
            or risk.get(calendar_fields[0])
            != QMT_NATIVE_DAILY_CALENDAR_COVERAGE_EVIDENCE_CONTRACT_ID
            or risk.get("sector_native_daily_calendar_coverage_evidence") is not None
        ):
            raise ValueError("native-daily calendar review support is incomplete")
        for prefix in values:
            raw = risk[f"{prefix}_native_daily_calendar_coverage_evidence"]
            values[prefix]["native_daily_calendar_coverage_evidence"] = (
                None
                if raw is None
                else _parse_review_native_daily_calendar_coverage(
                    raw,
                    evidence_cutoff=evidence_cutoff,
                    expected_symbol=(symbol if prefix == "symbol" else None),
                )
            )

    warmup_fields = (
        "warmup_evidence_contract_id",
        "market_warmup_evidence",
        "sector_warmup_evidence",
        "symbol_warmup_evidence",
    )
    present = tuple(field in risk for field in warmup_fields)
    if any(present):
        if not all(present) or risk.get(warmup_fields[0]) != (
            QMT_HIGHER_TIMEFRAME_WARMUP_EVIDENCE_CONTRACT_ID
        ):
            raise ValueError("higher-timeframe warmup support is incomplete")
        sector_warmup = risk["sector_warmup_evidence"]
        if sector_warmup is not None:
            _parse_qmt_higher_timeframe_warmup_evidence(sector_warmup)
        for prefix in values:
            raw = risk[f"{prefix}_warmup_evidence"]
            values[prefix]["warmup_evidence"] = (
                None
                if raw is None
                else _parse_qmt_higher_timeframe_warmup_evidence(raw)
            )

    convergence_fields = (
        "warmup_convergence_contract_id",
        "market_warmup_convergence_evidence",
        "sector_warmup_convergence_evidence",
        "symbol_warmup_convergence_evidence",
    )
    convergence_present = tuple(field in risk for field in convergence_fields)
    if any(convergence_present):
        if (
            not all(convergence_present)
            or risk.get(convergence_fields[0])
            != WARMUP_CONVERGENCE_ENVELOPE_CONTRACT_ID
        ):
            raise ValueError("higher-timeframe convergence support is incomplete")
        sector_convergence = risk["sector_warmup_convergence_evidence"]
        if sector_convergence is None:
            # 可移植市场/标的转换器允许不含板块决策字段的风险片段；此形态下原子扩展
            # 中被忽略的板块成员可为 ``None``，但显式已解析板块仍不能丢失证据。
            if risk.get("sector_gate") not in {None, "UNRESOLVED"}:
                raise ValueError("resolved sector lost convergence evidence")
        else:
            _parse_warmup_convergence_evidence(
                sector_convergence,
                evidence_cutoff=evidence_cutoff,
            )
        for prefix in values:
            raw = risk[f"{prefix}_warmup_convergence_evidence"]
            if raw is None:
                if risk.get(f"{prefix}_gate") != "UNRESOLVED":
                    raise ValueError(f"resolved {prefix} lost convergence evidence")
            else:
                values[prefix]["warmup_convergence_evidence"] = (
                    _parse_warmup_convergence_evidence(
                        raw,
                        evidence_cutoff=evidence_cutoff,
                    )
                )

    diagnostic_fields = (
        "warmup_convergence_diagnostic_contract_id",
        "market_warmup_convergence_diagnostic_evidence",
        "sector_warmup_convergence_diagnostic_evidence",
        "symbol_warmup_convergence_diagnostic_evidence",
    )
    diagnostic_present = tuple(field in risk for field in diagnostic_fields)
    strict_diagnostic_field = (
        "sector_strict_same_5m_warmup_convergence_diagnostic_evidence"
    )
    if any(diagnostic_present):
        if (
            not all(diagnostic_present)
            or not all(convergence_present)
            or risk.get(diagnostic_fields[0])
            != WARMUP_CONVERGENCE_DIAGNOSTIC_CONTRACT_ID
        ):
            raise ValueError(
                "higher-timeframe semantic diagnostic support is incomplete"
            )
        sector_envelope_raw = risk["sector_warmup_convergence_evidence"]
        sector_diagnostic_raw = risk["sector_warmup_convergence_diagnostic_evidence"]
        if sector_envelope_raw is None:
            if sector_diagnostic_raw is not None:
                raise ValueError("sector diagnostic has no convergence envelope")
        else:
            sector_envelope = _parse_warmup_convergence_evidence(
                sector_envelope_raw,
                evidence_cutoff=evidence_cutoff,
            )
            _bind_warmup_semantic_diagnostic_evidence(
                sector_envelope,
                sector_diagnostic_raw,
                evidence_cutoff=evidence_cutoff,
            )
        for prefix in values:
            envelope = values[prefix]["warmup_convergence_evidence"]
            raw_diagnostic = risk[f"{prefix}_warmup_convergence_diagnostic_evidence"]
            if envelope is None:
                if raw_diagnostic is not None:
                    raise ValueError(f"{prefix} diagnostic has no convergence envelope")
            else:
                values[prefix]["warmup_convergence_evidence"] = (
                    _bind_warmup_semantic_diagnostic_evidence(
                        envelope,  # type: ignore[arg-type]
                        raw_diagnostic,
                        evidence_cutoff=evidence_cutoff,
                    )
                )
        if strict_diagnostic_field in risk:
            strict_envelope_raw = risk.get(
                "sector_strict_same_5m_warmup_convergence_evidence"
            )
            if strict_envelope_raw is None:
                if risk.get(strict_diagnostic_field) is not None:
                    raise ValueError(
                        "strict sector diagnostic has no convergence envelope"
                    )
            else:
                strict_envelope = _parse_warmup_convergence_evidence(
                    strict_envelope_raw,
                    evidence_cutoff=evidence_cutoff,
                )
                _bind_warmup_semantic_diagnostic_evidence(
                    strict_envelope,
                    risk.get(strict_diagnostic_field),
                    evidence_cutoff=evidence_cutoff,
                )
    elif strict_diagnostic_field in risk:
        raise ValueError("strict sector semantic diagnostic lost its contract")

    supply_diagnostic_fields = (
        "warmup_mapping_supply_diagnostic_contract_id",
        "market_warmup_mapping_supply_diagnostic_evidence",
        "sector_warmup_mapping_supply_diagnostic_evidence",
        "symbol_warmup_mapping_supply_diagnostic_evidence",
    )
    supply_diagnostic_present = tuple(
        field in risk for field in supply_diagnostic_fields
    )
    strict_supply_diagnostic_field = (
        "sector_strict_same_5m_warmup_mapping_supply_diagnostic_evidence"
    )
    if any(supply_diagnostic_present):
        if (
            not all(supply_diagnostic_present)
            or not all(diagnostic_present)
            or risk.get(supply_diagnostic_fields[0])
            != WARMUP_MAPPING_SUPPLY_DIAGNOSTIC_CONTRACT_ID
        ):
            raise ValueError(
                "higher-timeframe mapping supply diagnostic support is incomplete"
            )
        sector_envelope_raw = risk["sector_warmup_convergence_evidence"]
        sector_semantic_raw = risk["sector_warmup_convergence_diagnostic_evidence"]
        sector_supply_raw = risk["sector_warmup_mapping_supply_diagnostic_evidence"]
        if sector_envelope_raw is None:
            if sector_supply_raw is not None:
                raise ValueError("sector mapping supply has no convergence envelope")
        else:
            sector_envelope = _parse_warmup_convergence_evidence(
                sector_envelope_raw,
                evidence_cutoff=evidence_cutoff,
            )
            sector_envelope = _bind_warmup_semantic_diagnostic_evidence(
                sector_envelope,
                sector_semantic_raw,
                evidence_cutoff=evidence_cutoff,
            )
            _bind_warmup_mapping_supply_diagnostic_evidence(
                sector_envelope,
                sector_supply_raw,
                evidence_cutoff=evidence_cutoff,
            )
        for prefix in values:
            envelope = values[prefix]["warmup_convergence_evidence"]
            raw_supply = risk[f"{prefix}_warmup_mapping_supply_diagnostic_evidence"]
            if envelope is None:
                if raw_supply is not None:
                    raise ValueError(
                        f"{prefix} mapping supply has no convergence envelope"
                    )
            else:
                values[prefix]["warmup_convergence_evidence"] = (
                    _bind_warmup_mapping_supply_diagnostic_evidence(
                        envelope,  # type: ignore[arg-type]
                        raw_supply,
                        evidence_cutoff=evidence_cutoff,
                    )
                )
        if strict_supply_diagnostic_field in risk:
            strict_envelope_raw = risk.get(
                "sector_strict_same_5m_warmup_convergence_evidence"
            )
            if strict_envelope_raw is None:
                if risk.get(strict_supply_diagnostic_field) is not None:
                    raise ValueError(
                        "strict sector mapping supply has no convergence envelope"
                    )
            else:
                strict_envelope = _parse_warmup_convergence_evidence(
                    strict_envelope_raw,
                    evidence_cutoff=evidence_cutoff,
                )
                strict_envelope = _bind_warmup_semantic_diagnostic_evidence(
                    strict_envelope,
                    risk.get(strict_diagnostic_field),
                    evidence_cutoff=evidence_cutoff,
                )
                _bind_warmup_mapping_supply_diagnostic_evidence(
                    strict_envelope,
                    risk.get(strict_supply_diagnostic_field),
                    evidence_cutoff=evidence_cutoff,
                )
    elif strict_supply_diagnostic_field in risk:
        raise ValueError("strict sector mapping supply diagnostic lost its contract")

    lineage_diagnostic_fields = (
        "warmup_structure_lineage_diagnostic_contract_id",
        "market_warmup_structure_lineage_diagnostic_evidence",
        "sector_warmup_structure_lineage_diagnostic_evidence",
        "symbol_warmup_structure_lineage_diagnostic_evidence",
    )
    lineage_diagnostic_present = tuple(
        field in risk for field in lineage_diagnostic_fields
    )
    strict_lineage_diagnostic_field = (
        "sector_strict_same_5m_warmup_structure_lineage_diagnostic_evidence"
    )
    if any(lineage_diagnostic_present):
        if (
            not all(lineage_diagnostic_present)
            or not all(supply_diagnostic_present)
            or risk.get(lineage_diagnostic_fields[0])
            != WARMUP_STRUCTURE_LINEAGE_DIAGNOSTIC_CONTRACT_ID
        ):
            raise ValueError("higher-timeframe structure lineage support is incomplete")
        sector_envelope_raw = risk["sector_warmup_convergence_evidence"]
        sector_lineage_raw = risk["sector_warmup_structure_lineage_diagnostic_evidence"]
        if sector_envelope_raw is None:
            if sector_lineage_raw is not None:
                raise ValueError("sector structure lineage has no convergence envelope")
        else:
            sector_envelope = _parse_warmup_convergence_evidence(
                sector_envelope_raw,
                evidence_cutoff=evidence_cutoff,
            )
            sector_envelope = _bind_warmup_semantic_diagnostic_evidence(
                sector_envelope,
                risk["sector_warmup_convergence_diagnostic_evidence"],
                evidence_cutoff=evidence_cutoff,
            )
            sector_envelope = _bind_warmup_mapping_supply_diagnostic_evidence(
                sector_envelope,
                risk["sector_warmup_mapping_supply_diagnostic_evidence"],
                evidence_cutoff=evidence_cutoff,
            )
            _bind_warmup_structure_lineage_diagnostic_evidence(
                sector_envelope,
                sector_lineage_raw,
                evidence_cutoff=evidence_cutoff,
            )
        for prefix in values:
            envelope = values[prefix]["warmup_convergence_evidence"]
            raw_lineage = risk[f"{prefix}_warmup_structure_lineage_diagnostic_evidence"]
            if envelope is None:
                if raw_lineage is not None:
                    raise ValueError(
                        f"{prefix} structure lineage has no convergence envelope"
                    )
            else:
                values[prefix]["warmup_convergence_evidence"] = (
                    _bind_warmup_structure_lineage_diagnostic_evidence(
                        envelope,  # type: ignore[arg-type]
                        raw_lineage,
                        evidence_cutoff=evidence_cutoff,
                    )
                )
        if strict_lineage_diagnostic_field in risk:
            strict_envelope_raw = risk.get(
                "sector_strict_same_5m_warmup_convergence_evidence"
            )
            if strict_envelope_raw is None:
                if risk.get(strict_lineage_diagnostic_field) is not None:
                    raise ValueError(
                        "strict sector structure lineage has no convergence envelope"
                    )
            else:
                strict_envelope = _parse_warmup_convergence_evidence(
                    strict_envelope_raw,
                    evidence_cutoff=evidence_cutoff,
                )
                strict_envelope = _bind_warmup_semantic_diagnostic_evidence(
                    strict_envelope,
                    risk.get(strict_diagnostic_field),
                    evidence_cutoff=evidence_cutoff,
                )
                strict_envelope = _bind_warmup_mapping_supply_diagnostic_evidence(
                    strict_envelope,
                    risk.get(strict_supply_diagnostic_field),
                    evidence_cutoff=evidence_cutoff,
                )
                _bind_warmup_structure_lineage_diagnostic_evidence(
                    strict_envelope,
                    risk.get(strict_lineage_diagnostic_field),
                    evidence_cutoff=evidence_cutoff,
                )
    elif strict_lineage_diagnostic_field in risk:
        raise ValueError("strict sector structure lineage diagnostic lost its contract")

    native_fields = (
        "native_daily_reconciliation_contract_id",
        "market_native_daily_reconciliation_evidence",
        "sector_native_daily_reconciliation_evidence",
        "symbol_native_daily_reconciliation_evidence",
    )
    present = tuple(field in risk for field in native_fields)
    if any(present):
        if (
            not all(present)
            or risk.get(native_fields[0]) != QMT_NATIVE_DAILY_RECONCILIATION_CONTRACT_ID
            or risk.get("sector_native_daily_reconciliation_evidence") is not None
        ):
            raise ValueError("native-daily review support is incomplete")
        for prefix in values:
            raw = risk[f"{prefix}_native_daily_reconciliation_evidence"]
            values[prefix]["native_daily_reconciliation_evidence"] = (
                None
                if raw is None
                else _parse_review_native_daily_evidence(
                    raw,
                    evidence_cutoff=evidence_cutoff,
                    expected_symbol=(symbol if prefix == "symbol" else None),
                )
            )

    output: dict[str, HigherTimeframeReviewSourceSupport | None] = {}
    for prefix, support_values in values.items():
        output[prefix] = (
            None
            if all(value is None for value in support_values.values())
            else HigherTimeframeReviewSourceSupport(
                subject="MARKET" if prefix == "market" else "SYMBOL",
                session_evidence=support_values["session_evidence"],  # type: ignore[arg-type]
                warmup_evidence=support_values["warmup_evidence"],  # type: ignore[arg-type]
                warmup_convergence_evidence=support_values[
                    "warmup_convergence_evidence"
                ],  # type: ignore[arg-type]
                native_daily_reconciliation_evidence=support_values[
                    "native_daily_reconciliation_evidence"
                ],  # type: ignore[arg-type]
                native_daily_calendar_coverage_evidence=support_values[
                    "native_daily_calendar_coverage_evidence"
                ],  # type: ignore[arg-type]
            )
        )
    return output


def _review_side_from_risk(
    risk: Mapping[str, object],
    *,
    prefix: Literal["market", "symbol"],
    evidence_cutoff: datetime,
    source_support: HigherTimeframeReviewSourceSupport | None,
) -> HigherTimeframeReviewSideEvidence:
    raw_states = risk.get(f"{prefix}_states")
    raw_reasons = risk.get(f"{prefix}_reason_codes")
    if (
        not isinstance(raw_states, Mapping)
        or set(raw_states) != set(_HIGHER_TIMEFRAME_PERIODS)
        or not isinstance(raw_reasons, list)
        or any(not isinstance(value, str) or not value for value in raw_reasons)
        or len(raw_reasons) != len(set(raw_reasons))
    ):
        raise ValueError("market/symbol higher-timeframe risk is incomplete")
    return HigherTimeframeReviewSideEvidence(
        subject="MARKET" if prefix == "market" else "SYMBOL",
        gate=str(risk.get(f"{prefix}_gate") or "UNRESOLVED"),
        states=tuple(
            (period, str(raw_states[period])) for period in _HIGHER_TIMEFRAME_PERIODS
        ),
        reason_codes=tuple(raw_reasons),
        period_diagnostics=_parse_review_period_diagnostics(
            risk.get(f"{prefix}_period_diagnostics"),
            evidence_cutoff=evidence_cutoff,
        ),
        source_support=source_support,
    )


def market_symbol_higher_timeframe_review_evidence_from_risk(
    risk: Mapping[str, object],
    *,
    symbol: str,
    observed_at: datetime,
) -> MarketSymbolHigherTimeframeReviewEvidence:
    observed = normalize_datetime(observed_at, "observed_at")
    supports = _review_source_supports_from_risk(
        risk,
        symbol=symbol,
        evidence_cutoff=observed,
    )
    return MarketSymbolHigherTimeframeReviewEvidence(
        symbol=symbol,
        observed_at=observed,
        market=_review_side_from_risk(
            risk,
            prefix="market",
            evidence_cutoff=observed,
            source_support=supports["market"],
        ),
        symbol_evidence=_review_side_from_risk(
            risk,
            prefix="symbol",
            evidence_cutoff=observed,
            source_support=supports["symbol"],
        ),
    )


def parse_market_symbol_higher_timeframe_review_evidence(
    raw: object,
) -> MarketSymbolHigherTimeframeReviewEvidence:
    expected = {
        "schema",
        "symbol",
        "observed_at",
        "market",
        "symbol_evidence",
        "live_status",
        "evidence_id",
    }
    if not isinstance(raw, Mapping) or set(raw) != expected:
        raise ValueError("market/symbol higher-timeframe evidence is malformed")
    try:
        observed = normalize_datetime(
            datetime.fromisoformat(str(raw["observed_at"])),
            "observed_at",
        )

        def parse_side(
            value: object,
            *,
            expected_subject: Literal["MARKET", "SYMBOL"],
        ) -> HigherTimeframeReviewSideEvidence:
            base_fields = {
                "subject",
                "gate",
                "states",
                "reason_codes",
                "period_diagnostics",
            }
            if not isinstance(value, Mapping) or set(value) not in {
                frozenset(base_fields),
                frozenset((*base_fields, "source_support")),
            }:
                raise ValueError("higher-timeframe review side is malformed")
            states = value.get("states")
            reasons = value.get("reason_codes")
            if (
                value.get("subject") != expected_subject
                or not isinstance(states, Mapping)
                or set(states) != set(_HIGHER_TIMEFRAME_PERIODS)
                or not isinstance(reasons, list)
                or any(not isinstance(item, str) or not item for item in reasons)
                or len(reasons) != len(set(reasons))
            ):
                raise ValueError("higher-timeframe review side is malformed")
            return HigherTimeframeReviewSideEvidence(
                subject=expected_subject,
                gate=str(value.get("gate") or "UNRESOLVED"),
                states=tuple(
                    (period, str(states[period]))
                    for period in _HIGHER_TIMEFRAME_PERIODS
                ),
                reason_codes=tuple(reasons),
                period_diagnostics=_parse_review_period_diagnostics(
                    value.get("period_diagnostics"),
                    evidence_cutoff=observed,
                ),
                source_support=(
                    None
                    if "source_support" not in value
                    else _parse_review_source_support(
                        value["source_support"],
                        expected_subject=expected_subject,
                        evidence_cutoff=observed,
                        expected_symbol=(
                            str(raw["symbol"]) if expected_subject == "SYMBOL" else None
                        ),
                    )
                ),
            )

        evidence = MarketSymbolHigherTimeframeReviewEvidence(
            symbol=str(raw["symbol"]),
            observed_at=observed,
            market=parse_side(raw["market"], expected_subject="MARKET"),
            symbol_evidence=parse_side(
                raw["symbol_evidence"],
                expected_subject="SYMBOL",
            ),
            schema=str(raw["schema"]),
            live_status=str(raw["live_status"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("market/symbol higher-timeframe evidence is invalid") from exc
    if raw.get("evidence_id") != evidence.evidence_id:
        raise ValueError("market/symbol higher-timeframe evidence identity changed")
    return evidence


@dataclass(frozen=True, slots=True)
class SectorRankingReviewEvidence:
    """Complete explanation of why one sector occupied its review rank."""

    sector_id: str
    sector_name: str
    observed_at: datetime
    eligible: bool
    hard_block: bool
    regime: Literal["supportive", "neutral", "hostile"]
    ordinal: int | None
    rank_score: int
    rank_components: tuple[tuple[str, int], ...]
    reason_codes: tuple[str, ...]
    horizontal_strength: Decimal | None = None
    horizontal_rank: int | None = None
    strength_observed_at: datetime | None = None
    strength_anchor_session: date | None = None
    strength_member_count: int = 0
    strength_source_revision: str | None = None
    strength_evidence_revision: str | None = None
    sector_catalog_revision: str | None = None
    strength_reason_codes: tuple[str, ...] = ()
    schema: str = SECTOR_RANKING_REVIEW_EVIDENCE_SCHEMA
    live_status: str = "LIVE_DISABLED"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "observed_at",
            normalize_datetime(self.observed_at, "observed_at"),
        )
        if self.strength_observed_at is not None:
            object.__setattr__(
                self,
                "strength_observed_at",
                normalize_datetime(
                    self.strength_observed_at,
                    "strength_observed_at",
                ),
            )
        if (
            not self.sector_id
            or not self.sector_name
            or self.schema != SECTOR_RANKING_REVIEW_EVIDENCE_SCHEMA
            or self.live_status != "LIVE_DISABLED"
        ):
            raise ValueError("sector ranking review identity is invalid")
        if self.eligible == self.hard_block:
            raise ValueError("sector ranking eligibility is inconsistent")
        if self.regime not in {"supportive", "neutral", "hostile"}:
            raise ValueError("sector ranking regime is invalid")
        if self.eligible:
            if type(self.ordinal) is not int or self.ordinal <= 0:
                raise ValueError("eligible sector ranking requires an ordinal")
        elif self.ordinal is not None:
            raise ValueError("hard-blocked sector cannot carry an ordinal")
        if type(self.rank_score) is not int or self.rank_score < 0:
            raise ValueError("sector ranking score is invalid")
        components = tuple(self.rank_components)
        names = tuple(name for name, _value in components)
        if (
            any(
                not isinstance(name, str)
                or not name
                or type(value) is not int
                or value < 0
                for name, value in components
            )
            or len(names) != len(set(names))
            or components != tuple(sorted(components))
        ):
            raise ValueError("sector ranking components are invalid")
        if sum(value for _name, value in components) != self.rank_score:
            raise ValueError("sector ranking components do not sum")
        if self.eligible and not components:
            raise ValueError("eligible sector ranking detail is missing")
        for field in ("reason_codes", "strength_reason_codes"):
            values = tuple(getattr(self, field))
            if any(not isinstance(value, str) or not value for value in values) or len(
                values
            ) != len(set(values)):
                raise ValueError(f"{field} must contain unique reason codes")
        resolved = self.horizontal_strength is not None
        if resolved != (self.horizontal_rank is not None):
            raise ValueError(
                "sector strength and horizontal rank must resolve together"
            )
        if self.horizontal_strength is not None and (
            not self.horizontal_strength.is_finite()
            or type(self.horizontal_rank) is not int
            or self.horizontal_rank <= 0
        ):
            raise ValueError("sector horizontal strength is invalid")
        if self.strength_member_count < 0:
            raise ValueError("sector strength member count is invalid")
        if resolved and (
            self.strength_anchor_session is None
            or self.strength_member_count <= 0
            or self.strength_source_revision is None
        ):
            raise ValueError("resolved sector strength provenance is incomplete")
        if (
            self.strength_observed_at is not None
            and self.strength_observed_at > self.observed_at
        ):
            raise ValueError("sector strength contains future evidence")
        if (
            self.strength_anchor_session is not None
            and self.strength_anchor_session > self.observed_at.date()
        ):
            raise ValueError("sector strength anchor is in the future")
        for value in (
            self.strength_source_revision,
            self.strength_evidence_revision,
            self.sector_catalog_revision,
        ):
            if value is not None and _SHA256.fullmatch(value) is None:
                raise ValueError("sector strength identity is invalid")
        if self.sector_catalog_revision is None:
            raise ValueError("sector ranking catalog identity is missing")

    def _stable_document(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "sector_id": self.sector_id,
            "sector_name": self.sector_name,
            "observed_at": self.observed_at.isoformat(),
            "eligible": self.eligible,
            "hard_block": self.hard_block,
            "regime": self.regime,
            "ordinal": self.ordinal,
            "rank_score": self.rank_score,
            "rank_components": dict(self.rank_components),
            "reason_codes": list(self.reason_codes),
            "horizontal_strength": (
                None
                if self.horizontal_strength is None
                else format(self.horizontal_strength, "f")
            ),
            "horizontal_rank": self.horizontal_rank,
            "strength_observed_at": (
                None
                if self.strength_observed_at is None
                else self.strength_observed_at.isoformat()
            ),
            "strength_anchor_session": (
                None
                if self.strength_anchor_session is None
                else self.strength_anchor_session.isoformat()
            ),
            "strength_member_count": self.strength_member_count,
            "strength_source_revision": self.strength_source_revision,
            "strength_evidence_revision": self.strength_evidence_revision,
            "sector_catalog_revision": self.sector_catalog_revision,
            "strength_reason_codes": list(self.strength_reason_codes),
            "live_status": self.live_status,
        }

    @property
    def evidence_id(self) -> str:
        return sha256_json(self._stable_document())

    def document(self) -> dict[str, object]:
        stable = self._stable_document()
        return {**stable, "evidence_id": self.evidence_id}


def parse_sector_ranking_review_evidence(
    raw: object,
) -> SectorRankingReviewEvidence:
    expected = {
        "schema",
        "sector_id",
        "sector_name",
        "observed_at",
        "eligible",
        "hard_block",
        "regime",
        "ordinal",
        "rank_score",
        "rank_components",
        "reason_codes",
        "horizontal_strength",
        "horizontal_rank",
        "strength_observed_at",
        "strength_anchor_session",
        "strength_member_count",
        "strength_source_revision",
        "strength_evidence_revision",
        "sector_catalog_revision",
        "strength_reason_codes",
        "live_status",
        "evidence_id",
    }
    if not isinstance(raw, Mapping) or set(raw) != expected:
        raise ValueError("sector ranking review evidence is malformed")
    components = raw.get("rank_components")
    reasons = raw.get("reason_codes")
    strength_reasons = raw.get("strength_reason_codes")
    if (
        not isinstance(components, Mapping)
        or not isinstance(reasons, list)
        or not isinstance(strength_reasons, list)
        or type(raw.get("eligible")) is not bool
        or type(raw.get("hard_block")) is not bool
        or type(raw.get("rank_score")) is not int
        or type(raw.get("strength_member_count")) is not int
    ):
        raise ValueError("sector ranking review evidence values are invalid")
    try:
        evidence = SectorRankingReviewEvidence(
            sector_id=str(raw["sector_id"]),
            sector_name=str(raw["sector_name"]),
            observed_at=datetime.fromisoformat(str(raw["observed_at"])),
            eligible=bool(raw["eligible"]),
            hard_block=bool(raw["hard_block"]),
            regime=str(raw["regime"]),  # type: ignore[arg-type]
            ordinal=(None if raw["ordinal"] is None else int(raw["ordinal"])),
            rank_score=int(raw["rank_score"]),
            rank_components=tuple(
                sorted((str(name), int(value)) for name, value in components.items())
            ),
            reason_codes=tuple(str(value) for value in reasons),
            horizontal_strength=(
                None
                if raw["horizontal_strength"] is None
                else Decimal(str(raw["horizontal_strength"]))
            ),
            horizontal_rank=(
                None if raw["horizontal_rank"] is None else int(raw["horizontal_rank"])
            ),
            strength_observed_at=(
                None
                if raw["strength_observed_at"] is None
                else datetime.fromisoformat(str(raw["strength_observed_at"]))
            ),
            strength_anchor_session=(
                None
                if raw["strength_anchor_session"] is None
                else date.fromisoformat(str(raw["strength_anchor_session"]))
            ),
            strength_member_count=int(raw["strength_member_count"]),
            strength_source_revision=(
                None
                if raw["strength_source_revision"] is None
                else str(raw["strength_source_revision"])
            ),
            strength_evidence_revision=(
                None
                if raw["strength_evidence_revision"] is None
                else str(raw["strength_evidence_revision"])
            ),
            sector_catalog_revision=(
                None
                if raw["sector_catalog_revision"] is None
                else str(raw["sector_catalog_revision"])
            ),
            strength_reason_codes=tuple(str(value) for value in strength_reasons),
            schema=str(raw["schema"]),
            live_status=str(raw["live_status"]),
        )
    except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
        raise ValueError("sector ranking review evidence is invalid") from exc
    if raw.get("evidence_id") != evidence.evidence_id:
        raise ValueError("sector ranking review evidence identity changed")
    return evidence


def sector_ranking_review_evidence_from_live_sector(
    raw: Mapping[str, object],
    *,
    observed_at: datetime,
    strength_evidence_revision: str | None = None,
    sector_catalog_revision: str | None = None,
) -> SectorRankingReviewEvidence:
    """Retain the already-validated live sector ordering facts."""

    components = raw.get("rank_components")
    reasons = raw.get("reason_codes")
    strength_reasons = raw.get("strength_reason_codes")
    if (
        not isinstance(components, Mapping)
        or not isinstance(reasons, list)
        or not isinstance(strength_reasons, list)
        or type(raw.get("eligible")) is not bool
        or type(raw.get("hard_block")) is not bool
        or type(raw.get("rank_score")) is not int
        or type(raw.get("strength_member_count")) is not int
    ):
        raise ValueError("live sector ranking evidence is incomplete")
    try:
        return SectorRankingReviewEvidence(
            sector_id=str(raw["sector_id"]),
            sector_name=str(raw["sector_name"]),
            observed_at=observed_at,
            eligible=bool(raw["eligible"]),
            hard_block=bool(raw["hard_block"]),
            regime=str(raw["regime"]),  # type: ignore[arg-type]
            ordinal=(None if raw.get("rank") is None else int(raw["rank"])),
            rank_score=int(raw["rank_score"]),
            rank_components=tuple(
                sorted((str(name), int(value)) for name, value in components.items())
            ),
            reason_codes=tuple(str(value) for value in reasons),
            horizontal_strength=(
                None
                if raw.get("horizontal_strength") is None
                else Decimal(str(raw["horizontal_strength"]))
            ),
            horizontal_rank=(
                None
                if raw.get("horizontal_rank") is None
                else int(raw["horizontal_rank"])
            ),
            # 权威批次对每个成员分类、均值和跨板块排序共用一个决策时间；紧凑板块记录
            # 不重复该时钟，因此在此显式绑定，不能静默替换为信号结构时间。
            strength_observed_at=observed_at,
            strength_anchor_session=(
                None
                if raw.get("strength_anchor_session") is None
                else date.fromisoformat(str(raw["strength_anchor_session"]))
            ),
            strength_member_count=int(raw["strength_member_count"]),
            strength_source_revision=(
                None
                if raw.get("strength_source_revision") is None
                else str(raw["strength_source_revision"])
            ),
            strength_evidence_revision=strength_evidence_revision,
            sector_catalog_revision=sector_catalog_revision,
            strength_reason_codes=tuple(str(value) for value in strength_reasons),
        )
    except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
        raise ValueError("live sector ranking evidence is invalid") from exc


def sector_ranking_review_evidence_from_candidate_audit(
    raw: Mapping[str, object],
    *,
    observed_at: datetime,
) -> SectorRankingReviewEvidence | None:
    """Build complete ranking evidence from one current candidate audit row."""

    available = raw.get("sector_ranking_available")
    if type(available) is not bool:
        raise ValueError("candidate sector ranking availability is missing")
    if not available:
        return None
    components = raw.get("sector_rank_components")
    reasons = raw.get("sector_rank_reason_codes")
    strength_reasons = raw.get("sector_strength_reason_codes")
    if (
        not isinstance(components, Mapping)
        or not isinstance(reasons, (tuple, list))
        or not isinstance(strength_reasons, (tuple, list))
        or type(raw.get("sector_eligible")) is not bool
        or type(raw.get("sector_hard_block")) is not bool
        or type(raw.get("sector_rank_score")) is not int
        or type(raw.get("sector_strength_member_count")) is not int
    ):
        raise ValueError("candidate sector ranking evidence is incomplete")
    try:
        strength_observed = raw.get("sector_strength_observed_at")
        anchor = raw.get("sector_strength_anchor_session")
        return SectorRankingReviewEvidence(
            sector_id=str(raw["sector_id"]),
            sector_name=str(raw["sector_name"]),
            observed_at=observed_at,
            eligible=bool(raw["sector_eligible"]),
            hard_block=bool(raw["sector_hard_block"]),
            regime=str(raw["sector_regime"]),  # type: ignore[arg-type]
            ordinal=(
                None
                if raw.get("sector_ordinal") is None
                else int(raw["sector_ordinal"])
            ),
            rank_score=int(raw["sector_rank_score"]),
            rank_components=tuple(
                sorted((str(name), int(value)) for name, value in components.items())
            ),
            reason_codes=tuple(str(value) for value in reasons),
            horizontal_strength=(
                None
                if raw.get("sector_horizontal_strength") is None
                else Decimal(str(raw["sector_horizontal_strength"]))
            ),
            horizontal_rank=(
                None
                if raw.get("sector_horizontal_rank") is None
                else int(raw["sector_horizontal_rank"])
            ),
            strength_observed_at=(
                None
                if strength_observed is None
                else normalize_datetime(
                    strength_observed,
                    "sector_strength_observed_at",
                )
            ),
            strength_anchor_session=(
                None
                if anchor is None
                else anchor
                if isinstance(anchor, date) and not isinstance(anchor, datetime)
                else date.fromisoformat(str(anchor))
            ),
            strength_member_count=int(raw["sector_strength_member_count"]),
            strength_source_revision=(
                None
                if raw.get("sector_strength_source_revision") is None
                else str(raw["sector_strength_source_revision"])
            ),
            strength_evidence_revision=(
                None
                if raw.get("sector_strength_evidence_revision") is None
                else str(raw["sector_strength_evidence_revision"])
            ),
            sector_catalog_revision=(
                None
                if raw.get("sector_catalog_revision") is None
                else str(raw["sector_catalog_revision"])
            ),
            strength_reason_codes=tuple(str(value) for value in strength_reasons),
        )
    except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
        raise ValueError("candidate sector ranking evidence is invalid") from exc


def review_priority(
    *,
    confidence: ReviewConfidence,
    exact_green: bool,
    market_risk_gate: str,
    sector_risk_gate: str,
    symbol_risk_gate: str,
    warning_count: int,
    position_status: str | None = None,
    side: str | None = None,
    selection_sources: tuple[str, ...] = (),
    lifecycle_stage: str | None = None,
    monitor_only: bool = False,
    parameters: HumanReviewScreeningParameters | None = None,
) -> int:
    """Return a stable review-urgency score, never a synthetic trade score.

    Structural review state owns the priority band. Confidence, lifecycle and advisory
    risk gates may only order candidates *inside* that band.  Diagnostic volume
    is deliberately accepted for audit compatibility but cannot lower priority:
    otherwise a better documented candidate is ranked below a hard block merely
    because it carries more evidence codes.
    """

    values = parameters or human_review_screening_parameters()
    if type(warning_count) is not int or warning_count < 0:
        raise ValueError("human review warning count is invalid")
    if side not in {None, "buy", "sell"}:
        raise ValueError("human review priority side is invalid")
    if any(not isinstance(value, str) or not value for value in selection_sources):
        raise ValueError("human review selection sources are invalid")

    actionable_sell_review = bool(
        side == "sell"
        and lifecycle_stage in {"triggered", "executable", "active"}
        and (position_status in {"CONDITIONAL", "RECOMMENDED"} or exact_green)
    )
    manual_attention_sources = {
        "MANUAL_ATTENTION_MONITOR",
        # 只为旧归档读取保留；新决策文档不再生成这两个来源码。
        "HOLDING_MONITOR",
        "VIRTUAL_HOLDING_MONITOR",
    }
    if actionable_sell_review and manual_attention_sources.intersection(
        selection_sources
    ):
        effective_status = "MANUAL_ATTENTION_SELL_REVIEW"
    elif actionable_sell_review:
        effective_status = "STRUCTURAL_SELL_REVIEW"
    elif position_status is not None:
        effective_status = position_status
    elif exact_green:
        effective_status = "RECOMMENDED"
    elif confidence == "MEDIUM":
        effective_status = "CONDITIONAL"
    else:
        effective_status = "NOT_ACTIONABLE"

    bands = {
        status: (base, minimum, maximum)
        for status, base, minimum, maximum in values.review_priority_bands
    }
    if effective_status not in bands:
        raise ValueError("human review position status is invalid")
    base, minimum, maximum = bands[effective_status]
    score = base + dict(values.confidence_bonuses)[confidence]
    score += values.exact_green_bonus if exact_green else 0
    score += values.green_risk_gate_bonus * sum(
        gate == "GREEN"
        for gate in (market_risk_gate, sector_risk_gate, symbol_risk_gate)
    )
    score += dict(values.lifecycle_bonuses).get(lifecycle_stage, 0)
    score -= values.monitor_only_penalty if monitor_only else 0
    return min(maximum, max(minimum, score))


@dataclass(frozen=True, slots=True)
class HumanReviewAlert:
    symbol: str
    alert_type: AlertType
    signal_at: datetime
    review_available_at: datetime
    source_point_id: str
    structure_snapshot_id: str
    sector_id: str | None
    confidence: ReviewConfidence
    review_priority: int
    reference_price: Decimal | None
    structural_invalidation_price: Decimal | None
    market_risk_gate: str
    sector_risk_gate: str
    symbol_risk_gate: str
    warning_codes: tuple[str, ...]
    source_fact_ids: tuple[str, ...]
    screening_parameter_set_id: str
    signal_alignment_parameter_set_id: str
    sector_higher_timeframe_evidence: SectorHigherTimeframeReviewEvidence | None = None
    market_symbol_higher_timeframe_evidence: (
        MarketSymbolHigherTimeframeReviewEvidence | None
    ) = None
    sector_ranking_evidence: SectorRankingReviewEvidence | None = None
    entry_confirmation_bar_closed_at: datetime | None = None
    entry_price_cap: Decimal | None = None
    entry_valid_until: datetime | None = None
    entry_boundary_evidence_id: str | None = None
    entry_execution_boundary: EntryExecutionBoundary | None = None
    position_recommendation: PositionRecommendation | None = None
    review_checklist: tuple[str, ...] = REVIEW_CHECKLIST
    status: str = "REVIEW_REQUIRED"
    automated_action_authorized: bool = False
    live_status: str = "LIVE_DISABLED"

    def __post_init__(self) -> None:
        for field in ("signal_at", "review_available_at"):
            object.__setattr__(
                self,
                field,
                normalize_datetime(getattr(self, field), field),
            )
        if self.entry_confirmation_bar_closed_at is not None:
            object.__setattr__(
                self,
                "entry_confirmation_bar_closed_at",
                normalize_datetime(
                    self.entry_confirmation_bar_closed_at,
                    "entry_confirmation_bar_closed_at",
                ),
            )
        if self.entry_valid_until is not None:
            object.__setattr__(
                self,
                "entry_valid_until",
                normalize_datetime(self.entry_valid_until, "entry_valid_until"),
            )
        for field in ("warning_codes", "source_fact_ids", "review_checklist"):
            values = tuple(getattr(self, field))
            object.__setattr__(self, field, values)
            if len(values) != len(set(values)):
                raise ValueError(f"{field} must be unique")
        if self.alert_type not in HUMAN_REVIEW_ALERT_TYPES:
            raise ValueError("human review alert type is invalid")
        if self.review_available_at < self.signal_at:
            raise ValueError("human review cannot predate its source signal")
        if not 0 <= self.review_priority <= 100:
            raise ValueError("human review priority is outside 0..100")
        if any(
            gate not in _RISK_GATES
            for gate in (
                self.market_risk_gate,
                self.sector_risk_gate,
                self.symbol_risk_gate,
            )
        ):
            raise ValueError("human review risk gate is invalid")
        if not self.symbol or not self.source_point_id or not self.source_fact_ids:
            raise ValueError("human review alert provenance is incomplete")
        source_evidence = self.sector_higher_timeframe_evidence
        if source_evidence is not None and (
            not isinstance(source_evidence, SectorHigherTimeframeReviewEvidence)
            or source_evidence.evidence_id not in self.source_fact_ids
        ):
            raise ValueError("human review sector source evidence is not fact-bound")
        if source_evidence is not None and (
            source_evidence.sector_id != self.sector_id
            or source_evidence.observed_at != self.signal_at
            or source_evidence.gate != self.sector_risk_gate
        ):
            raise ValueError("human review sector decision evidence is not fact-bound")
        if source_evidence is None:
            if _SECTOR_NATIVE_DAILY_RESEARCH_BLOCKER in self.warning_codes:
                raise ValueError("human review research bridge evidence is missing")
        elif source_evidence.source_mode == (
            QMT_SECTOR_NATIVE_DAILY_RESEARCH_SOURCE_MODE
        ):
            if (
                self.sector_risk_gate == "GREEN"
                or _SECTOR_NATIVE_DAILY_RESEARCH_BLOCKER not in self.warning_codes
            ):
                raise ValueError("human review research bridge gate is inconsistent")
        elif _SECTOR_NATIVE_DAILY_RESEARCH_BLOCKER in self.warning_codes:
            raise ValueError("same-base human review carries research blocker")
        market_symbol_evidence = self.market_symbol_higher_timeframe_evidence
        if market_symbol_evidence is not None and (
            not isinstance(
                market_symbol_evidence,
                MarketSymbolHigherTimeframeReviewEvidence,
            )
            or market_symbol_evidence.symbol != self.symbol
            or market_symbol_evidence.observed_at != self.signal_at
            or market_symbol_evidence.market.gate != self.market_risk_gate
            or market_symbol_evidence.symbol_evidence.gate != self.symbol_risk_gate
            or market_symbol_evidence.evidence_id not in self.source_fact_ids
        ):
            raise ValueError(
                "human review market/symbol higher-timeframe evidence is not fact-bound"
            )
        ranking_evidence = self.sector_ranking_evidence
        if ranking_evidence is not None and (
            not isinstance(ranking_evidence, SectorRankingReviewEvidence)
            or self.sector_id != ranking_evidence.sector_id
            or ranking_evidence.observed_at > self.review_available_at
            or ranking_evidence.observed_at < self.signal_at
            or ranking_evidence.evidence_id not in self.source_fact_ids
        ):
            raise ValueError("human review sector ranking evidence is not fact-bound")
        if self.reference_price is not None and self.reference_price <= 0:
            raise ValueError("human review reference price must be positive")
        if (
            self.structural_invalidation_price is not None
            and self.structural_invalidation_price <= 0
        ):
            raise ValueError("human review invalidation price must be positive")
        boundary_values = (
            self.entry_confirmation_bar_closed_at,
            self.entry_price_cap,
            self.entry_valid_until,
            self.entry_boundary_evidence_id,
        )
        if any(value is not None for value in boundary_values) and any(
            value is None for value in boundary_values
        ):
            raise ValueError("human review entry execution boundary is incomplete")
        if self.entry_price_cap is not None and (
            not isinstance(self.entry_price_cap, Decimal)
            or not self.entry_price_cap.is_finite()
            or self.entry_price_cap <= 0
            or self.entry_valid_until is None
            or self.entry_confirmation_bar_closed_at is None
            or self.entry_valid_until < self.entry_confirmation_bar_closed_at
            or _SHA256.fullmatch(str(self.entry_boundary_evidence_id)) is None
        ):
            raise ValueError("human review entry execution boundary is invalid")
        if self.entry_execution_boundary is not None and (
            not isinstance(
                self.entry_execution_boundary,
                EntryExecutionBoundary,
            )
            or self.entry_execution_boundary.symbol != self.symbol
            or self.entry_execution_boundary.confirmation_bar_closed_at
            != self.entry_confirmation_bar_closed_at
            or self.entry_execution_boundary.raw_high != self.entry_price_cap
            or self.entry_execution_boundary.entry_valid_until != self.entry_valid_until
            or self.entry_execution_boundary.evidence_id
            != self.entry_boundary_evidence_id
            or self.entry_execution_boundary.evidence_id not in self.source_fact_ids
        ):
            raise ValueError(
                "human review full entry execution boundary does not match"
            )
        if self.position_recommendation is not None:
            expected_side = (
                "buy"
                if self.alert_type
                in {"POSSIBLE_5M_TRADE_BUY", "POSSIBLE_30M_BUY", "POSSIBLE_5M_TACTICAL_BUYBACK"}
                else "sell"
            )
            if (
                not isinstance(self.position_recommendation, PositionRecommendation)
                or self.position_recommendation.side != expected_side
                or self.position_recommendation.manual_confirmation_required is not True
                or self.position_recommendation.automated_order_authorized is not False
            ):
                raise ValueError("human review position recommendation is invalid")
        if (
            self.screening_parameter_set_id
            != human_review_screening_parameters().parameter_set_id
            or self.signal_alignment_parameter_set_id
            != unified_signal_alignment_contract().parameter_set_id
        ):
            raise ValueError("人工复核提醒的严格参数绑定发生变化")
        if (
            self.review_checklist != REVIEW_CHECKLIST
            or self.status != "REVIEW_REQUIRED"
            or self.automated_action_authorized
            or self.live_status != "LIVE_DISABLED"
        ):
            raise ValueError("human review alert cannot authorize an action")

    @property
    def candidate_id(self) -> str:
        return _human_review_candidate_id(self)

    @property
    def signal_lifecycle_id(self) -> str:
        """返回跨不可变每日筛选快照保持稳定的 setup 身份。"""

        return sha256_json(
            {
                "schema": "chanlun-human-review-signal-lifecycle",
                "symbol": self.symbol,
                "alert_type": self.alert_type,
                "source_point_id": self.source_point_id,
                "screening_parameter_set_id": self.screening_parameter_set_id,
                "signal_alignment_parameter_set_id": self.signal_alignment_parameter_set_id,
            }
        )


def _human_review_candidate_id(
    alert: HumanReviewAlert,
) -> str:
    """重新计算当前候选身份。"""

    # ``dataclasses.asdict(alert)`` 会递归复制每棵 M/W/D 证据树，而下方随后又会用
    # 各证据对象的规范文档替换副本。全市场队列会因此无意义地复制数千万个值；
    # 这里保留浅层字段映射，只规范化嵌套证据对象。
    stable = {
        field: getattr(alert, field) for field in HumanReviewAlert.__dataclass_fields__
    }
    if alert.position_recommendation is None:
        # Keep candidate identities from reports written before the optional
        # recommendation field was introduced stable and readable.
        stable.pop("position_recommendation", None)
    else:
        stable["position_recommendation"] = alert.position_recommendation.document()
    if alert.sector_higher_timeframe_evidence is not None:
        stable["sector_higher_timeframe_evidence"] = (
            alert.sector_higher_timeframe_evidence.document()
        )
    if alert.market_symbol_higher_timeframe_evidence is not None:
        # 来源支持证据以纯 ``date`` 保存时点化交易日；应绑定其已规范化的可移植文档，
        # 而不是把 ``asdict`` 日期交给通用规范指纹助手。
        stable["market_symbol_higher_timeframe_evidence"] = (
            alert.market_symbol_higher_timeframe_evidence.document()
        )
    if alert.sector_ranking_evidence is not None:
        # 规范指纹助手有意不提供通用纯日期表示；使用证据自身的规范文档，使锚定交易日
        # 按 ISO 日期哈希，并同时绑定嵌套证据身份。
        stable["sector_ranking_evidence"] = alert.sector_ranking_evidence.document()
    if alert.entry_execution_boundary is not None:
        stable["entry_execution_boundary"] = alert.entry_execution_boundary.document()
    return sha256_json(stable)


def parse_human_review_alert(
    raw: object,
    *,
    sector_evidence_cache: dict[
        str,
        tuple[object, SectorHigherTimeframeReviewEvidence],
    ]
    | None = None,
    market_symbol_evidence_cache: dict[
        str,
        tuple[object, MarketSymbolHigherTimeframeReviewEvidence],
    ]
    | None = None,
    sector_ranking_evidence_cache: dict[
        str,
        tuple[object, SectorRankingReviewEvidence],
    ]
    | None = None,
) -> HumanReviewAlert:
    """Parse and hash-verify one portable review alert document."""

    alert_fields = tuple(HumanReviewAlert.__dataclass_fields__)
    full_fields = set(alert_fields)
    payload_fields = set(raw) if isinstance(raw, Mapping) else set()
    expected_fields = full_fields | {"candidate_id", "signal_lifecycle_id"}
    legacy_fields = expected_fields - {"position_recommendation"}
    if not isinstance(raw, Mapping) or payload_fields not in {
        frozenset(expected_fields),
        frozenset(legacy_fields),
    }:
        raise ValueError("human review candidate is malformed")
    values = {field: raw.get(field) for field in alert_fields}
    try:
        for field in ("signal_at", "review_available_at"):
            values[field] = datetime.fromisoformat(str(values[field]))
        for field in (
            "entry_confirmation_bar_closed_at",
            "entry_valid_until",
        ):
            if field in values and values[field] is not None:
                values[field] = datetime.fromisoformat(str(values[field]))
        for field in (
            "reference_price",
            "structural_invalidation_price",
            "entry_price_cap",
        ):
            if field not in values:
                continue
            values[field] = (
                None if values[field] is None else Decimal(str(values[field]))
            )
        if values.get("entry_execution_boundary") is not None:
            values["entry_execution_boundary"] = (
                parse_entry_execution_boundary_document(
                    values["entry_execution_boundary"]
                )
            )
        if values.get("position_recommendation") is not None:
            values["position_recommendation"] = (
                parse_position_recommendation_document(
                    values["position_recommendation"]
                )
            )
        if values.get("sector_higher_timeframe_evidence") is not None:
            raw_evidence = values["sector_higher_timeframe_evidence"]
            evidence_key = (
                str(raw_evidence.get("evidence_id"))
                if isinstance(raw_evidence, Mapping)
                else ""
            )
            cached = (
                None
                if sector_evidence_cache is None
                else sector_evidence_cache.get(evidence_key)
            )
            if cached is not None and raw_evidence == cached[0]:
                values["sector_higher_timeframe_evidence"] = cached[1]
            else:
                parsed_sector = parse_sector_higher_timeframe_review_evidence(
                    raw_evidence
                )
                values["sector_higher_timeframe_evidence"] = parsed_sector
                if sector_evidence_cache is not None:
                    sector_evidence_cache[evidence_key] = (
                        raw_evidence,
                        parsed_sector,
                    )
        if values.get("market_symbol_higher_timeframe_evidence") is not None:
            raw_evidence = values["market_symbol_higher_timeframe_evidence"]
            evidence_key = (
                str(raw_evidence.get("evidence_id"))
                if isinstance(raw_evidence, Mapping)
                else ""
            )
            cached = (
                None
                if market_symbol_evidence_cache is None
                else market_symbol_evidence_cache.get(evidence_key)
            )
            if cached is not None and raw_evidence == cached[0]:
                values["market_symbol_higher_timeframe_evidence"] = cached[1]
            else:
                parsed_market_symbol = (
                    parse_market_symbol_higher_timeframe_review_evidence(raw_evidence)
                )
                values["market_symbol_higher_timeframe_evidence"] = parsed_market_symbol
                if market_symbol_evidence_cache is not None:
                    market_symbol_evidence_cache[evidence_key] = (
                        raw_evidence,
                        parsed_market_symbol,
                    )
        if values.get("sector_ranking_evidence") is not None:
            raw_evidence = values["sector_ranking_evidence"]
            evidence_key = (
                str(raw_evidence.get("evidence_id"))
                if isinstance(raw_evidence, Mapping)
                else ""
            )
            cached = (
                None
                if sector_ranking_evidence_cache is None
                else sector_ranking_evidence_cache.get(evidence_key)
            )
            if cached is not None and raw_evidence == cached[0]:
                values["sector_ranking_evidence"] = cached[1]
            else:
                parsed_ranking = parse_sector_ranking_review_evidence(raw_evidence)
                values["sector_ranking_evidence"] = parsed_ranking
                if sector_ranking_evidence_cache is not None:
                    sector_ranking_evidence_cache[evidence_key] = (
                        raw_evidence,
                        parsed_ranking,
                    )
        for field in ("warning_codes", "source_fact_ids", "review_checklist"):
            values[field] = tuple(values[field])
        alert = HumanReviewAlert(**values)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("human review candidate is malformed") from exc
    if raw.get("candidate_id") != alert.candidate_id:
        raise ValueError("human review candidate identity changed")
    lifecycle = raw.get("signal_lifecycle_id")
    if lifecycle != alert.signal_lifecycle_id:
        raise ValueError("human review lifecycle identity changed")
    return alert


def human_review_alert_document(alert: HumanReviewAlert) -> dict[str, object]:
    """Return a portable alert whose full entry evidence is self-contained."""

    stable = {
        field: getattr(alert, field) for field in HumanReviewAlert.__dataclass_fields__
    }
    if alert.position_recommendation is None:
        stable.pop("position_recommendation", None)
    else:
        stable["position_recommendation"] = alert.position_recommendation.document()
    if alert.sector_higher_timeframe_evidence is not None:
        stable["sector_higher_timeframe_evidence"] = (
            alert.sector_higher_timeframe_evidence.document()
        )
    if alert.market_symbol_higher_timeframe_evidence is not None:
        stable["market_symbol_higher_timeframe_evidence"] = (
            alert.market_symbol_higher_timeframe_evidence.document()
        )
    if alert.sector_ranking_evidence is not None:
        stable["sector_ranking_evidence"] = alert.sector_ranking_evidence.document()
    if alert.entry_execution_boundary is not None:
        stable["entry_execution_boundary"] = alert.entry_execution_boundary.document()
    return stable


def validate_human_review_screen_document(
    payload: Mapping[str, object],
) -> tuple[HumanReviewAlert, ...]:
    """Validate one review-only report for every page and write adapter.

    This deliberately defines the common minimum boundary shared by historical,
    live and forward reports.  Profile-specific analytics may add fields, but
    no consumer may weaken the hash, no-order or candidate identity checks.
    """

    claimed = payload.get("content_sha256")
    stable = {key: payload[key] for key in payload if key != "content_sha256"}
    if _SHA256.fullmatch(str(claimed)) is None or claimed != sha256_json(stable):
        raise ValueError("human_review_report_hash_mismatch")
    if (
        payload.get("schema") != HUMAN_REVIEW_SCREEN_SCHEMA
        or payload.get("data_grade") != "HUMAN_REVIEW_SCREENING"
        or payload.get("highest_status") != "REVIEW_REQUIRED"
        or payload.get("live_status") != "LIVE_DISABLED"
        or payload.get("human_confirmation_required") is not True
        or payload.get("automated_order_authorized") is not False
        or payload.get("portfolio_backtest_performed") is not False
        or payload.get("portfolio_performance_evaluable") is not False
        or payload.get("orders_created") != 0
        or payload.get("fills_created") != 0
        or payload.get("positions_created") != 0
    ):
        raise ValueError("human_review_report_boundary_invalid")
    queue = payload.get("review_queue")
    if not isinstance(queue, list):
        raise ValueError("human_review_queue_missing")
    sector_evidence_cache: dict[
        str,
        tuple[object, SectorHigherTimeframeReviewEvidence],
    ] = {}
    market_symbol_evidence_cache: dict[
        str,
        tuple[object, MarketSymbolHigherTimeframeReviewEvidence],
    ] = {}
    sector_ranking_evidence_cache: dict[
        str,
        tuple[object, SectorRankingReviewEvidence],
    ] = {}
    try:
        alerts = tuple(
            parse_human_review_alert(
                value,
                sector_evidence_cache=sector_evidence_cache,
                market_symbol_evidence_cache=market_symbol_evidence_cache,
                sector_ranking_evidence_cache=sector_ranking_evidence_cache,
            )
            for value in queue
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("human_review_candidate_malformed") from exc
    # ``parse_human_review_alert`` 已经重算并匹配了每一项。
    # 候选身份已完成认证；此处读取已认证原始身份，可避免仅为查重再次哈希同一大型证据树。
    identities = tuple(str(value["candidate_id"]) for value in queue)
    if len(identities) != len(set(identities)):
        raise ValueError("human_review_candidate_duplicate")
    return alerts


@dataclass(frozen=True, slots=True)
class ReviewPriceBar:
    observed_at: datetime
    high: Decimal
    low: Decimal
    close: Decimal
    complete: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "observed_at",
            normalize_datetime(self.observed_at, "observed_at"),
        )
        if self.high <= 0 or self.low <= 0 or self.close <= 0:
            raise ValueError("review price bar values must be positive")
        if self.low > self.high or not self.low <= self.close <= self.high:
            raise ValueError("review price bar range is invalid")


@dataclass(frozen=True, slots=True)
class ReviewEventStudyObservation:
    candidate_id: str
    symbol: str
    horizon_sessions: int
    reference_at: datetime | None
    reference_price: Decimal | None
    end_session: date | None
    close_return: Decimal | None
    maximum_favorable_excursion: Decimal | None
    maximum_adverse_excursion: Decimal | None
    invalidation_observed: bool | None
    first_invalidation_at: datetime | None
    complete: bool
    reason_code: str | None


def evaluate_review_alert(
    alert: HumanReviewAlert,
    bars: Sequence[ReviewPriceBar],
    *,
    parameters: HumanReviewScreeningParameters | None = None,
    trading_sessions: Sequence[date] | None = None,
    require_complete_one_minute_sessions: bool = False,
) -> tuple[ReviewEventStudyObservation, ...]:
    """Evaluate a screen after the fact without feeding outcomes into it."""

    values = parameters or human_review_screening_parameters()
    calendar = None if trading_sessions is None else tuple(trading_sessions)
    if calendar is not None and (
        calendar != tuple(sorted(set(calendar)))
        or any(not isinstance(value, date) for value in calendar)
    ):
        raise ValueError("review event-study trading calendar is invalid")
    if require_complete_one_minute_sessions and calendar is None:
        raise ValueError("complete 1m event study requires a trading calendar")
    completed = tuple(
        sorted(
            (bar for bar in bars if bar.complete),
            key=lambda value: value.observed_at,
        )
    )
    if len({bar.observed_at for bar in completed}) != len(completed):
        raise ValueError("review event-study bars contain duplicate timestamps")
    known = tuple(
        bar for bar in completed if bar.observed_at <= alert.review_available_at
    )
    # 信号当日剩余盘中区间不是完整前向交易日；排除后，“5 个交易日”才表示复核者首次
    # 收到提醒之后的五个完整交易日。
    future = tuple(
        bar
        for bar in completed
        if bar.observed_at.date() > alert.review_available_at.date()
    )
    reference_reason = "NO_CAUSAL_REFERENCE_BAR"
    if require_complete_one_minute_sessions:
        expected_reference_closes = a_share_completed_one_minute_prefix_closes(
            alert.review_available_at
        )
        signal_session_bars = tuple(
            bar
            for bar in known
            if bar.observed_at.date() == alert.review_available_at.date()
        )
        try:
            validate_a_share_completed_one_minute_prefix_closes(
                tuple(bar.observed_at for bar in signal_session_bars),
                not_after=alert.review_available_at,
            )
        except ValueError:
            reference = None
            reference_reason = "INCOMPLETE_CAUSAL_REFERENCE_SESSION_GRID"
        else:
            reference = (
                None if not expected_reference_closes else signal_session_bars[-1]
            )
    else:
        reference = None if not known else known[-1]
    # ``alert.reference_price`` 是复制得到的最细粒度因果结构锚点。
    # ``alert.reference_price`` 来自 5m 正式买卖点的因果结构锚点，
    # 不是市场报价。后验收益必须从复核时实际可知的最后一根已完成 1m 收盘价开始；
    # 强制二者相等会错误拒绝结构锚点低于或高于最新收盘价的正常候选。
    observed_future_sessions = {bar.observed_at.date() for bar in future}
    if calendar is None:
        sessions = tuple(sorted(observed_future_sessions))
    else:
        sessions = tuple(
            value for value in calendar if value > alert.review_available_at.date()
        )
        if observed_future_sessions - set(sessions):
            raise ValueError("review bars escaped the trading calendar")
    output: list[ReviewEventStudyObservation] = []
    for horizon in values.event_study_horizons:
        if reference is None or len(sessions) < horizon:
            output.append(
                ReviewEventStudyObservation(
                    candidate_id=alert.candidate_id,
                    symbol=alert.symbol,
                    horizon_sessions=horizon,
                    reference_at=None if reference is None else reference.observed_at,
                    reference_price=None if reference is None else reference.close,
                    end_session=None,
                    close_return=None,
                    maximum_favorable_excursion=None,
                    maximum_adverse_excursion=None,
                    invalidation_observed=None,
                    first_invalidation_at=None,
                    complete=False,
                    reason_code=(
                        reference_reason
                        if reference is None
                        else "INSUFFICIENT_FUTURE_SESSIONS"
                    ),
                )
            )
            continue
        end_session = sessions[horizon - 1]
        target_sessions = frozenset(sessions[:horizon])
        if require_complete_one_minute_sessions:
            try:
                for session in sessions[:horizon]:
                    validate_a_share_complete_session_closes(
                        tuple(
                            bar.observed_at
                            for bar in future
                            if bar.observed_at.date() == session
                        ),
                        session=session,
                    )
            except ValueError:
                output.append(
                    ReviewEventStudyObservation(
                        candidate_id=alert.candidate_id,
                        symbol=alert.symbol,
                        horizon_sessions=horizon,
                        reference_at=reference.observed_at,
                        reference_price=reference.close,
                        end_session=end_session,
                        close_return=None,
                        maximum_favorable_excursion=None,
                        maximum_adverse_excursion=None,
                        invalidation_observed=None,
                        first_invalidation_at=None,
                        complete=False,
                        reason_code="INCOMPLETE_FUTURE_SESSION_GRID",
                    )
                )
                continue
        window = tuple(
            bar for bar in future if bar.observed_at.date() in target_sessions
        )
        end_bar = tuple(bar for bar in window if bar.observed_at.date() == end_session)[
            -1
        ]
        first_invalidation = next(
            (
                bar.observed_at
                for bar in window
                if alert.structural_invalidation_price is not None
                # 严格策略第 5.6 节：三买回试在相等时仍有效（low >= ZG）；只有已完成
                # 已完成 K 线最低价严格低于 ZG，才能证明重新进入中枢。
                and bar.low < alert.structural_invalidation_price
            ),
            None,
        )
        output.append(
            ReviewEventStudyObservation(
                candidate_id=alert.candidate_id,
                symbol=alert.symbol,
                horizon_sessions=horizon,
                reference_at=reference.observed_at,
                reference_price=reference.close,
                end_session=end_session,
                close_return=end_bar.close / reference.close - Decimal("1"),
                maximum_favorable_excursion=(
                    max(bar.high for bar in window) / reference.close - Decimal("1")
                ),
                maximum_adverse_excursion=(
                    min(bar.low for bar in window) / reference.close - Decimal("1")
                ),
                invalidation_observed=(
                    None
                    if alert.structural_invalidation_price is None
                    else first_invalidation is not None
                ),
                first_invalidation_at=first_invalidation,
                complete=True,
                reason_code=None,
            )
        )
    return tuple(output)


def summarize_event_study(
    observations: Sequence[ReviewEventStudyObservation],
) -> dict[str, object]:
    output: dict[str, object] = {}
    horizons = sorted({value.horizon_sessions for value in observations})
    for horizon in horizons:
        values = tuple(
            value
            for value in observations
            if value.horizon_sessions == horizon and value.complete
        )
        returns = tuple(
            value.close_return for value in values if value.close_return is not None
        )
        favorable = tuple(
            value.maximum_favorable_excursion
            for value in values
            if value.maximum_favorable_excursion is not None
        )
        adverse = tuple(
            value.maximum_adverse_excursion
            for value in values
            if value.maximum_adverse_excursion is not None
        )
        invalidations = tuple(
            value.invalidation_observed
            for value in values
            if value.invalidation_observed is not None
        )
        false_positive_proxies = tuple(
            bool(value.invalidation_observed) or value.close_return <= 0
            for value in values
            if value.close_return is not None
        )
        output[str(horizon)] = {
            "eligible_count": len(values),
            "pending_count": sum(
                value.horizon_sessions == horizon and not value.complete
                for value in observations
            ),
            "mean_close_return": (
                None if not returns else sum(returns, Decimal("0")) / len(returns)
            ),
            "median_close_return": None if not returns else median(returns),
            "positive_close_rate": (
                None
                if not returns
                else Decimal(sum(value > 0 for value in returns)) / len(returns)
            ),
            "mean_maximum_favorable_excursion": (
                None if not favorable else sum(favorable, Decimal("0")) / len(favorable)
            ),
            "mean_maximum_adverse_excursion": (
                None if not adverse else sum(adverse, Decimal("0")) / len(adverse)
            ),
            "invalidation_rate": (
                None
                if not invalidations
                else Decimal(sum(bool(value) for value in invalidations))
                / len(invalidations)
            ),
            "false_positive_proxy_rate": (
                None
                if not false_positive_proxies
                else Decimal(sum(false_positive_proxies)) / len(false_positive_proxies)
            ),
        }
    return output


@dataclass(frozen=True, slots=True)
class HumanReviewFeedback:
    candidate_id: str
    source_screen_content_sha256: str
    reviewer: str
    reviewed_at: datetime
    center_judgement: Literal["CONFIRMED", "REJECTED", "UNCERTAIN"]
    trend_judgement: Literal["UP", "DOWN", "CONSOLIDATION", "UNCERTAIN"]
    level_judgement: Literal["30M", "5M", "1M", "OTHER", "UNCERTAIN"]
    point_judgement: Literal[
        "BUY_1",
        "BUY_2",
        "BUY_3",
        "SELL_1",
        "SELL_2",
        "SELL_3",
        "NONE",
        "UNCERTAIN",
    ]
    disposition: Literal["WATCH", "REJECT", "PAPER_OBSERVE", "NEEDS_MORE_DATA"]
    decomposition_judgement: Literal[
        "SAME_LEVEL", "CENTER", "COMBINED", "UNCERTAIN"
    ] = "UNCERTAIN"
    center_expansion_judgement: Literal["CONFIRMED", "REJECTED", "UNCERTAIN"] = (
        "UNCERTAIN"
    )
    nine_segment_upgrade_judgement: Literal["CONFIRMED", "REJECTED", "UNCERTAIN"] = (
        "UNCERTAIN"
    )
    locator_judgement: Literal["CONFIRMED", "REJECTED", "UNCERTAIN"] = "UNCERTAIN"
    notes: str = ""
    request_id: str | None = None
    signal_lifecycle_id: str | None = None
    automated_action_authorized: bool = False
    live_status: str = "LIVE_DISABLED"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "reviewed_at",
            normalize_datetime(self.reviewed_at, "reviewed_at"),
        )
        if _SHA256.fullmatch(self.candidate_id) is None:
            raise ValueError("human review candidate identity is invalid")
        if _SHA256.fullmatch(self.source_screen_content_sha256) is None:
            raise ValueError("human review source report identity is invalid")
        if not self.reviewer.strip():
            raise ValueError("human reviewer identity is required")
        if self.request_id is not None and (
            not self.request_id.strip()
            or len(self.request_id) > 128
            or re.fullmatch(r"[A-Za-z0-9._:-]+", self.request_id) is None
        ):
            raise ValueError("human review request identity is invalid")
        if (
            self.signal_lifecycle_id is not None
            and _SHA256.fullmatch(self.signal_lifecycle_id) is None
        ):
            raise ValueError("human review signal lifecycle identity is invalid")
        if self.center_judgement not in {"CONFIRMED", "REJECTED", "UNCERTAIN"}:
            raise ValueError("human center judgement is invalid")
        if self.trend_judgement not in {
            "UP",
            "DOWN",
            "CONSOLIDATION",
            "UNCERTAIN",
        }:
            raise ValueError("human trend judgement is invalid")
        if self.level_judgement not in {
            "30M",
            "5M",
            "1M",
            "OTHER",
            "UNCERTAIN",
        }:
            raise ValueError("human level judgement is invalid")
        if self.point_judgement not in {
            "BUY_1",
            "BUY_2",
            "BUY_3",
            "SELL_1",
            "SELL_2",
            "SELL_3",
            "NONE",
            "UNCERTAIN",
        }:
            raise ValueError("human point judgement is invalid")
        if self.disposition not in {
            "WATCH",
            "REJECT",
            "PAPER_OBSERVE",
            "NEEDS_MORE_DATA",
        }:
            raise ValueError("human review disposition is invalid")
        if self.decomposition_judgement not in {
            "SAME_LEVEL",
            "CENTER",
            "COMBINED",
            "UNCERTAIN",
        }:
            raise ValueError("human decomposition judgement is invalid")
        for field in (
            "center_expansion_judgement",
            "nine_segment_upgrade_judgement",
            "locator_judgement",
        ):
            if getattr(self, field) not in {
                "CONFIRMED",
                "REJECTED",
                "UNCERTAIN",
            }:
                raise ValueError(f"human {field} is invalid")
        if self.automated_action_authorized or self.live_status != "LIVE_DISABLED":
            raise ValueError("human feedback cannot authorize an automated action")

    @property
    def feedback_id(self) -> str:
        values = asdict(self)
        if self.request_id is not None:
            # 网络重试会获得新的服务器墙钟时间；身份绑定显式请求键，同时在账本载荷中
            # 保留首次获准写入的时间戳。
            values["reviewed_at"] = "IDEMPOTENT_REQUEST_TIMESTAMP"
        return sha256_json(values)


def validate_human_review_feedback_causality(
    feedback: HumanReviewFeedback,
    alert: HumanReviewAlert,
    *,
    source_screen_content_sha256: str,
) -> None:
    """Bind a new judgement to evidence already available to its reviewer."""

    if _SHA256.fullmatch(source_screen_content_sha256) is None:
        raise ValueError("human review source report identity is invalid")
    if (
        feedback.candidate_id != alert.candidate_id
        or feedback.source_screen_content_sha256 != source_screen_content_sha256
        or feedback.signal_lifecycle_id != alert.signal_lifecycle_id
    ):
        raise ValueError("human review feedback provenance changed")
    if feedback.reviewed_at < alert.review_available_at:
        raise ValueError("human review feedback predates available evidence")


def _jsonable(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    return value


def _feedback_ledger_document(
    entries: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    document: dict[str, object] = {
        "schema": _FEEDBACK_LEDGER_SCHEMA,
        "entries": [dict(value) for value in entries],
        "automated_order_authorized": False,
        "live_status": "LIVE_DISABLED",
    }
    document["content_sha256"] = sha256_json(document)
    return document


def load_human_review_feedback_ledger(path: Path) -> dict[str, object]:
    """Load and verify the complete append-only feedback hash chain."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != _FEEDBACK_LEDGER_SCHEMA
    ):
        raise ValueError("unsupported human review feedback ledger")
    if (
        payload.get("automated_order_authorized") is not False
        or payload.get("live_status") != "LIVE_DISABLED"
    ):
        raise ValueError("human review feedback ledger cannot authorize trading")
    claimed_content_sha256 = payload.get("content_sha256")
    stable_document = dict(payload)
    stable_document.pop("content_sha256", None)
    if claimed_content_sha256 != sha256_json(stable_document):
        raise ValueError("human review feedback ledger content hash mismatch")

    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list):
        raise ValueError("human review feedback ledger entries must be a list")
    feedback_fields = tuple(HumanReviewFeedback.__dataclass_fields__)
    chain_fields = {
        "feedback_id",
        "previous_entry_sha256",
        "entry_sha256",
    }
    expected_feedback_fields = frozenset(feedback_fields)
    previous: str | None = None
    for index, raw_entry in enumerate(raw_entries):
        raw_fields = (
            frozenset(raw_entry) - chain_fields
            if isinstance(raw_entry, dict)
            else frozenset()
        )
        if (
            not isinstance(raw_entry, dict)
            or raw_fields != expected_feedback_fields
            or frozenset(raw_entry) != raw_fields | chain_fields
        ):
            raise ValueError(f"human review feedback ledger entry {index} is malformed")
        if raw_entry.get("previous_entry_sha256") != previous:
            raise ValueError(
                f"human review feedback ledger chain broke at entry {index}"
            )
        stable_entry = dict(raw_entry)
        claimed_entry_sha256 = stable_entry.pop("entry_sha256")
        if claimed_entry_sha256 != sha256_json(stable_entry):
            raise ValueError(
                f"human review feedback ledger hash mismatch at entry {index}"
            )
        feedback_values = {field: raw_entry[field] for field in feedback_fields}
        try:
            feedback_values["reviewed_at"] = datetime.fromisoformat(
                str(feedback_values["reviewed_at"])
            )
            feedback = HumanReviewFeedback(**feedback_values)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"human review feedback ledger payload invalid at entry {index}"
            ) from exc
        identity_values = {field: getattr(feedback, field) for field in feedback_fields}
        if identity_values.get("request_id") is not None:
            identity_values["reviewed_at"] = "IDEMPOTENT_REQUEST_TIMESTAMP"
        expected_feedback_id = sha256_json(identity_values)
        if raw_entry.get("feedback_id") != expected_feedback_id:
            raise ValueError(
                f"human review feedback identity mismatch at entry {index}"
            )
        previous = str(claimed_entry_sha256)
    return payload


def _append_human_review_feedback_unlocked(
    path: Path,
    feedback: HumanReviewFeedback,
) -> dict[str, object]:
    """Append a hash-chained human judgement; duplicates are idempotent."""

    if path.is_file():
        payload = load_human_review_feedback_ledger(path)
        entries = list(payload.get("entries") or ())
    else:
        entries = []
    if feedback.request_id is not None:
        request_match = next(
            (
                row
                for row in entries
                if row.get("request_id") == feedback.request_id
                and row.get("reviewer") == feedback.reviewer
            ),
            None,
        )
        if request_match is not None:
            existing_values = {
                field: request_match[field]
                for field in HumanReviewFeedback.__dataclass_fields__
                if field in request_match and field != "reviewed_at"
            }
            same_values = all(
                existing == _jsonable(getattr(feedback, field))
                for field, existing in existing_values.items()
            )
            if not same_values:
                raise ValueError(
                    "human review request identity was reused with different values"
                )
            return payload
    if any(row.get("feedback_id") == feedback.feedback_id for row in entries):
        return payload
    previous = None if not entries else entries[-1]["entry_sha256"]
    stable = {
        **_jsonable(asdict(feedback)),
        "feedback_id": feedback.feedback_id,
        "previous_entry_sha256": previous,
    }
    entry = {**stable, "entry_sha256": sha256_json(stable)}
    entries.append(entry)
    document = _feedback_ledger_document(entries)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
    return document


def append_human_review_feedback(
    path: Path,
    feedback: HumanReviewFeedback,
) -> dict[str, object]:
    """Append feedback atomically across web, CLI and scheduled processes."""

    with interprocess_file_lock(path.with_suffix(path.suffix + ".lock")):
        return _append_human_review_feedback_unlocked(path, feedback)


__all__ = (
    "HumanReviewAlert",
    "HumanReviewFeedback",
    "HumanReviewScreeningParameters",
    "HUMAN_REVIEW_SCREEN_SCHEMA",
    "HigherTimeframeReviewSideEvidence",
    "HigherTimeframeReviewSourceSupport",
    "HIGHER_TIMEFRAME_REVIEW_SOURCE_SUPPORT_SCHEMA",
    "MARKET_SYMBOL_HIGHER_TIMEFRAME_REVIEW_EVIDENCE_SCHEMA",
    "MarketSymbolHigherTimeframeReviewEvidence",
    "MONITOR_ONLY_WARNING_CODE",
    "REVIEW_CHECKLIST",
    "ReviewEventStudyObservation",
    "ReviewPriceBar",
    "SECTOR_HIGHER_TIMEFRAME_REVIEW_EVIDENCE_SCHEMA",
    "SectorHigherTimeframeReviewEvidence",
    "append_human_review_feedback",
    "evaluate_review_alert",
    "human_review_alert_document",
    "human_review_screening_parameters",
    "load_human_review_feedback_ledger",
    "parse_human_review_alert",
    "parse_market_symbol_higher_timeframe_review_evidence",
    "parse_sector_higher_timeframe_review_evidence",
    "review_priority",
    "market_symbol_higher_timeframe_review_evidence_from_risk",
    "sector_higher_timeframe_review_evidence_from_risk",
    "summarize_event_study",
    "validate_human_review_feedback_causality",
    "validate_human_review_screen_document",
)
