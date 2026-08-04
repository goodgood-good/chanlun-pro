"""Causal research approximation for Chanlun technical buy/sell locations.

The strict recursive implementation remains valuable as structure evidence,
but its exact parent/child proof is too brittle to be the only way a research
program may recognize an operational point.  This module therefore keeps the
30m/5m/1m roles and consumes only already-confirmed structural points, while
making the approximation boundary explicit and hash-identifiable.

It never changes the structure core, never back-dates a point, and can never
promote a result beyond ``RESEARCH_ONLY / LIVE_DISABLED``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Mapping, Sequence

from chanlun.decision_support.fingerprints import normalize_datetime, sha256_json
from chanlun.decision_support.trading_system.backtest.fixed_year import (
    CausalDirectRecursiveDecisionFact,
)
from chanlun.decision_support.trading_system.models import StructuralPoint


ApproximationStatus = Literal["PASS", "REJECT"]
ApproximationConfidence = Literal["HIGH", "MEDIUM", "LOW"]


@dataclass(frozen=True, slots=True)
class TechnicalApproximationParameters:
    """Frozen operational interpretation of an approximate point.

    A raw recursive level-2 third buy is the 30m setup.  The program then
    waits for the first confirmed raw level-0 buy point, including a third
    buy, for at most ten trading sessions.  Expansion and unresolved
    nine-segment evidence are warnings because the user explicitly wants a
    usable approximation rather than a proof that the recursive partition is
    unique.
    """

    schema: str = "chanlun-v3-technical-point-approximation-parameters/v1"
    strategic_frequency: str = "30m"
    tactical_frequency: str = "5m"
    locator_frequency: str = "1m"
    strategic_recursive_level: int = 2
    tactical_recursive_level: int = 1
    locator_recursive_level: int = 0
    strategic_setup_point_types: tuple[str, ...] = ("3buy",)
    entry_locator_point_types: tuple[str, ...] = ("1buy", "2buy", "3buy")
    tactical_locator_point_types: tuple[str, ...] = (
        "1buy",
        "2buy",
        "3buy",
        "1sell",
        "2sell",
        "3sell",
    )
    max_entry_locator_wait_sessions: int = 10
    max_tactical_locator_wait_sessions: int = 3
    setup_invalidation_rule: str = (
        "COMPLETED_ADJUSTED_1M_CLOSE_AT_OR_BELOW_STRATEGIC_INVALIDATION"
    )
    strategic_exit_rule: str = (
        "FIRST_COMPLETED_1M_INVALIDATION_OR_CONFIRMED_RAW_LEVEL2_SELL"
    )
    tactical_rule: str = "RAW_LEVEL1_POINT_PLUS_NEARBY_RAW_LEVEL0_SAME_SIDE_POINT"
    expansion_handling: str = "WARNING_NOT_BLOCKER"
    nine_segment_handling: str = "WARNING_NOT_BLOCKER"
    status_ceiling: str = "RESEARCH_ONLY"
    live_status: str = "LIVE_DISABLED"

    def __post_init__(self) -> None:
        if (
            self.strategic_frequency,
            self.tactical_frequency,
            self.locator_frequency,
            self.strategic_recursive_level,
            self.tactical_recursive_level,
            self.locator_recursive_level,
        ) != ("30m", "5m", "1m", 2, 1, 0):
            raise ValueError("technical approximation timeframe mapping changed")
        if self.strategic_setup_point_types != ("3buy",):
            raise ValueError("technical approximation setup types changed")
        if self.entry_locator_point_types != ("1buy", "2buy", "3buy"):
            raise ValueError("technical approximation entry locators changed")
        if (
            self.max_entry_locator_wait_sessions != 10
            or self.max_tactical_locator_wait_sessions != 3
        ):
            raise ValueError("technical approximation wait windows changed")
        if (
            self.expansion_handling != "WARNING_NOT_BLOCKER"
            or self.nine_segment_handling != "WARNING_NOT_BLOCKER"
        ):
            raise ValueError("technical approximation warning policy changed")
        if self.status_ceiling != "RESEARCH_ONLY" or self.live_status != "LIVE_DISABLED":
            raise ValueError("technical approximation cannot enable live trading")

    @property
    def parameter_set_id(self) -> str:
        return sha256_json(asdict(self))

    def document(self) -> dict[str, object]:
        return {**asdict(self), "parameter_set_id": self.parameter_set_id}


def technical_approximation_parameters() -> TechnicalApproximationParameters:
    return TechnicalApproximationParameters()


@dataclass(frozen=True, slots=True)
class TechnicalApproximationAlignmentContract:
    technical_parameter_set_id: str
    schema: str = "chanlun-v3-technical-point-approximation-alignment/v1"
    contract_id: str = "V3_APPROXIMATE_30M_5M_1M_CONFIRMED_POINTS_V1"
    level_relation_mode: str = "CAUSAL_CONFIRMED_POINT_APPROXIMATION"
    status_ceiling: str = "RESEARCH_ONLY"
    live_status: str = "LIVE_DISABLED"

    def __post_init__(self) -> None:
        expected = technical_approximation_parameters().parameter_set_id
        if self.technical_parameter_set_id != expected:
            raise ValueError("technical approximation parameter identity changed")
        if self.status_ceiling != "RESEARCH_ONLY" or self.live_status != "LIVE_DISABLED":
            raise ValueError("technical approximation alignment cannot enable live")

    @property
    def parameter_set_id(self) -> str:
        return sha256_json(asdict(self))


def technical_approximation_alignment_contract(
) -> TechnicalApproximationAlignmentContract:
    parameters = technical_approximation_parameters()
    return TechnicalApproximationAlignmentContract(
        technical_parameter_set_id=parameters.parameter_set_id
    )


@dataclass(frozen=True, slots=True)
class ApproximateTechnicalEntryDecision:
    symbol: str
    strategic_point_id: str
    setup_at: datetime
    status: ApproximationStatus
    locator_point_id: str | None
    locator_point_type: str | None
    locator_at: datetime | None
    locator_delay_sessions: int | None
    confidence: ApproximationConfidence | None
    reason_codes: tuple[str, ...]
    warning_codes: tuple[str, ...]
    strict_structure_snapshot_id: str
    parameter_set_id: str
    data_grade: str = "RESEARCH_APPROXIMATION"
    highest_status: str = "RESEARCH_ONLY"
    live_status: str = "LIVE_DISABLED"

    def __post_init__(self) -> None:
        object.__setattr__(self, "setup_at", normalize_datetime(self.setup_at, "setup_at"))
        if self.locator_at is not None:
            object.__setattr__(
                self,
                "locator_at",
                normalize_datetime(self.locator_at, "locator_at"),
            )
        for field in ("reason_codes", "warning_codes"):
            values = tuple(getattr(self, field))
            object.__setattr__(self, field, values)
            if len(values) != len(set(values)):
                raise ValueError(f"{field} must be unique")
        if not self.symbol or not self.strategic_point_id:
            raise ValueError("technical approximation decision identity is required")
        if not self.strict_structure_snapshot_id.startswith("sha256:"):
            raise ValueError("technical approximation requires a strict snapshot")
        if not self.parameter_set_id.startswith("sha256:"):
            raise ValueError("technical approximation parameter identity is required")
        locator = (
            self.locator_point_id,
            self.locator_point_type,
            self.locator_at,
            self.locator_delay_sessions,
            self.confidence,
        )
        if self.status == "PASS":
            if any(value is None for value in locator) or self.reason_codes:
                raise ValueError("passing technical approximation requires one locator")
        elif any(value is not None for value in locator) or not self.reason_codes:
            raise ValueError("rejected technical approximation requires reasons only")
        if self.highest_status != "RESEARCH_ONLY" or self.live_status != "LIVE_DISABLED":
            raise ValueError("technical approximation decision cannot enable live")

    @property
    def decision_id(self) -> str:
        return sha256_json(asdict(self))


@dataclass(frozen=True, slots=True)
class ApproximateChanlunEntryChain:
    """Replay provenance for the explicitly approximate entry path."""

    strategic_point_id: str
    strategic_center_id: str
    strategic_anchor_unit_id: str
    locator_point_id: str
    locator_anchor_unit_id: str
    locator_point_type: str
    setup_at: datetime
    decision_at: datetime
    locator_delay_sessions: int
    confirmation_bar_high: Decimal
    structural_invalidation_price: Decimal
    strict_structure_snapshot_id: str
    technical_parameter_set_id: str
    warning_codes: tuple[str, ...]
    provenance_fact_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        for field in ("setup_at", "decision_at"):
            object.__setattr__(
                self,
                field,
                normalize_datetime(getattr(self, field), field),
            )
        for field in ("warning_codes", "provenance_fact_ids"):
            values = tuple(getattr(self, field))
            object.__setattr__(self, field, values)
            if len(values) != len(set(values)):
                raise ValueError(f"{field} must be unique")
        identities = (
            self.strategic_point_id,
            self.strategic_center_id,
            self.strategic_anchor_unit_id,
            self.locator_point_id,
            self.locator_anchor_unit_id,
            self.strict_structure_snapshot_id,
            self.technical_parameter_set_id,
        )
        if any(not value for value in identities) or not self.provenance_fact_ids:
            raise ValueError("approximate entry chain provenance is incomplete")
        if self.decision_at < self.setup_at or self.locator_delay_sessions < 0:
            raise ValueError("approximate entry chain timing is invalid")
        if self.confirmation_bar_high <= 0 or self.structural_invalidation_price <= 0:
            raise ValueError("approximate entry execution boundaries must be positive")
        if self.locator_point_type not in {"1buy", "2buy", "3buy"}:
            raise ValueError("approximate entry locator type is invalid")

    @property
    def chain_id(self) -> str:
        return sha256_json(asdict(self))

    @property
    def l2_confirmation_bar_high(self) -> Decimal:
        """Compatibility name used by the shared candidate builder."""

        return self.confirmation_bar_high


_WAIVABLE_STRICT_REASONS = frozenset(
    {
        "ACTIVE_CENTER_EXPANSION_RECLASSIFYING",
        "UNRESOLVED_NINE_SEGMENT_RECLASSIFICATION",
        "NO_L2_1M_LOCATOR_IN_DIRECT_FIRST_RETURN",
        "L2_1M_SECOND_BUY_REQUIRES_SIGNED_EVIDENCE",
    }
)


def _session_distance(
    sessions: Sequence[date],
    start: date,
    end: date,
) -> int | None:
    values = tuple(sessions)
    if values != tuple(sorted(set(values))):
        raise ValueError("technical approximation sessions must be sorted and unique")
    positions = {value: index for index, value in enumerate(values)}
    if start not in positions or end not in positions or positions[end] < positions[start]:
        return None
    return positions[end] - positions[start]


def approximate_technical_entry_decision(
    *,
    strict_decision: CausalDirectRecursiveDecisionFact,
    strategic_point: StructuralPoint,
    structural_points: Sequence[StructuralPoint],
    trading_sessions: Sequence[date],
    parameters: TechnicalApproximationParameters | None = None,
) -> ApproximateTechnicalEntryDecision:
    """Choose the first causal lower-level point after one 30m setup."""

    params = parameters or technical_approximation_parameters()
    if (
        strategic_point.point_id != strict_decision.l0_point_id
        or strategic_point.recursive_level != params.strategic_recursive_level
        or strategic_point.point_type not in params.strategic_setup_point_types
        or strategic_point.side != "buy"
    ):
        return ApproximateTechnicalEntryDecision(
            symbol=strategic_point.code,
            strategic_point_id=strict_decision.l0_point_id,
            setup_at=strict_decision.first_seen_at,
            status="REJECT",
            locator_point_id=None,
            locator_point_type=None,
            locator_at=None,
            locator_delay_sessions=None,
            confidence=None,
            reason_codes=("REJECT_APPROXIMATE_STRATEGIC_SETUP_INVALID",),
            warning_codes=(),
            strict_structure_snapshot_id=strict_decision.structure_snapshot_id,
            parameter_set_id=params.parameter_set_id,
        )
    unsupported = tuple(
        reason
        for reason in strict_decision.reason_codes
        if reason not in _WAIVABLE_STRICT_REASONS
    )
    warnings = tuple(
        f"APPROXIMATION_WARNING_{reason}"
        for reason in strict_decision.reason_codes
        if reason in _WAIVABLE_STRICT_REASONS
    )
    if unsupported:
        return ApproximateTechnicalEntryDecision(
            symbol=strategic_point.code,
            strategic_point_id=strategic_point.point_id,
            setup_at=strict_decision.first_seen_at,
            status="REJECT",
            locator_point_id=None,
            locator_point_type=None,
            locator_at=None,
            locator_delay_sessions=None,
            confidence=None,
            reason_codes=("REJECT_UNSUPPORTED_STRICT_STRUCTURE_FAILURE", *unsupported),
            warning_codes=warnings,
            strict_structure_snapshot_id=strict_decision.structure_snapshot_id,
            parameter_set_id=params.parameter_set_id,
        )

    points = {point.point_id: point for point in structural_points}
    exact = (
        None
        if strict_decision.aligned_entry_chain is None
        else points.get(strict_decision.aligned_entry_chain.l2_locator_point_id)
    )
    if exact is not None:
        locator = exact
        delay = 0
        confidence: ApproximationConfidence = "HIGH"
    else:
        priority = {"1buy": 0, "2buy": 1, "3buy": 2}
        candidates = tuple(
            sorted(
                (
                    point
                    for point in structural_points
                    if point.recursive_level == params.locator_recursive_level
                    and point.point_type in params.entry_locator_point_types
                    and point.side == "buy"
                    and point.available_at >= strict_decision.first_seen_at
                ),
                key=lambda point: (
                    point.available_at,
                    priority[point.point_type],
                    point.point_id,
                ),
            )
        )
        if not candidates:
            return ApproximateTechnicalEntryDecision(
                symbol=strategic_point.code,
                strategic_point_id=strategic_point.point_id,
                setup_at=strict_decision.first_seen_at,
                status="REJECT",
                locator_point_id=None,
                locator_point_type=None,
                locator_at=None,
                locator_delay_sessions=None,
                confidence=None,
                reason_codes=("REJECT_NO_CAUSAL_APPROXIMATE_1M_BUY_LOCATOR",),
                warning_codes=warnings,
                strict_structure_snapshot_id=strict_decision.structure_snapshot_id,
                parameter_set_id=params.parameter_set_id,
            )
        locator = candidates[0]
        distance = _session_distance(
            trading_sessions,
            strict_decision.first_seen_at.date(),
            locator.available_at.date(),
        )
        if distance is None or distance > params.max_entry_locator_wait_sessions:
            return ApproximateTechnicalEntryDecision(
                symbol=strategic_point.code,
                strategic_point_id=strategic_point.point_id,
                setup_at=strict_decision.first_seen_at,
                status="REJECT",
                locator_point_id=None,
                locator_point_type=None,
                locator_at=None,
                locator_delay_sessions=None,
                confidence=None,
                reason_codes=("REJECT_APPROXIMATE_1M_LOCATOR_WAIT_EXCEEDED",),
                warning_codes=warnings,
                strict_structure_snapshot_id=strict_decision.structure_snapshot_id,
                parameter_set_id=params.parameter_set_id,
            )
        delay = distance
        confidence = (
            "MEDIUM"
            if distance <= params.max_tactical_locator_wait_sessions
            and "UNRESOLVED_NINE_SEGMENT_RECLASSIFICATION"
            not in strict_decision.reason_codes
            else "LOW"
        )
    return ApproximateTechnicalEntryDecision(
        symbol=strategic_point.code,
        strategic_point_id=strategic_point.point_id,
        setup_at=strict_decision.first_seen_at,
        status="PASS",
        locator_point_id=locator.point_id,
        locator_point_type=locator.point_type,
        locator_at=locator.available_at,
        locator_delay_sessions=delay,
        confidence=confidence,
        reason_codes=(),
        warning_codes=warnings,
        strict_structure_snapshot_id=strict_decision.structure_snapshot_id,
        parameter_set_id=params.parameter_set_id,
    )


def bind_approximate_entry_chain(
    *,
    decision: ApproximateTechnicalEntryDecision,
    strategic_point: StructuralPoint,
    locator_point: StructuralPoint,
    point_anchor_unit_ids: Mapping[str, str],
    confirmation_bar_high: Decimal,
) -> ApproximateChanlunEntryChain:
    if decision.status != "PASS" or decision.locator_point_id != locator_point.point_id:
        raise ValueError("only a passing approximate decision can bind a chain")
    strategic_anchor = point_anchor_unit_ids.get(strategic_point.point_id)
    locator_anchor = point_anchor_unit_ids.get(locator_point.point_id)
    if strategic_anchor is None or locator_anchor is None or strategic_point.center_id is None:
        raise ValueError("approximate entry source provenance is incomplete")
    assert decision.locator_at is not None
    assert decision.locator_delay_sessions is not None
    return ApproximateChanlunEntryChain(
        strategic_point_id=strategic_point.point_id,
        strategic_center_id=strategic_point.center_id,
        strategic_anchor_unit_id=strategic_anchor,
        locator_point_id=locator_point.point_id,
        locator_anchor_unit_id=locator_anchor,
        locator_point_type=locator_point.point_type,
        setup_at=decision.setup_at,
        decision_at=max(decision.setup_at, decision.locator_at),
        locator_delay_sessions=decision.locator_delay_sessions,
        confirmation_bar_high=confirmation_bar_high,
        structural_invalidation_price=Decimal(
            str(strategic_point.structure_invalidation_price)
        ),
        strict_structure_snapshot_id=decision.strict_structure_snapshot_id,
        technical_parameter_set_id=decision.parameter_set_id,
        warning_codes=decision.warning_codes,
        provenance_fact_ids=tuple(
            dict.fromkeys(
                (
                    decision.strict_structure_snapshot_id,
                    strategic_point.point_id,
                    strategic_point.center_id,
                    strategic_anchor,
                    locator_point.point_id,
                    locator_anchor,
                )
            )
        ),
    )


__all__ = (
    "ApproximateChanlunEntryChain",
    "ApproximateTechnicalEntryDecision",
    "TechnicalApproximationAlignmentContract",
    "TechnicalApproximationParameters",
    "approximate_technical_entry_decision",
    "bind_approximate_entry_chain",
    "technical_approximation_alignment_contract",
    "technical_approximation_parameters",
)
