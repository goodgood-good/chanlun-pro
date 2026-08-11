from __future__ import annotations

"""Causal, read-only facts for the strict strategy ``ETF_PROXY`` path.

This module deliberately stops at the boundary of the frozen Chanlun
structure implementation.  It can build the point-in-time ETF basket and its
cross-sectional moving-average strength, completed day/week/month bars and
their MA5 values.  A high-timeframe risk snapshot is emitted only when a
separate frozen-structure adapter supplies all three certified risk states.
Missing publication timestamps, mappings or structure facts remain explicit
blockers; they are never replaced with a favourable default.
"""

from bisect import bisect_right
from dataclasses import dataclass, replace
from datetime import date, datetime, time
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Literal, Mapping, Sequence
from zoneinfo import ZoneInfo

from chanlun.core.strict_structure.base_profile import STRICT_STROKE_MODE
from chanlun.decision_support.fingerprints import normalize_datetime
from chanlun.decision_support.trading_system.parameters import (
    StrategyParameters,
    etf_parameter_snapshot,
)
from chanlun.decision_support.trading_system.selection import (
    AccountEntryGate,
    CandidateDecision,
    CandidateSnapshot,
    CompletedDailyClose,
    HigherTimeframeRiskSnapshot,
    RiskState,
    SectorStrengthSnapshot,
    SelectionResearchSnapshot,
    TechnicalEntrySnapshot,
    TradeabilitySnapshot,
    evaluate_candidate,
)


CN = ZoneInfo("Asia/Shanghai")
PeriodKind = Literal["D", "W", "M"]
FactGrade = Literal["FULL_SYSTEM_ELIGIBLE", "RESEARCH_ONLY", "UNRESOLVED"]
RiskMappingSupplyClass = Literal[
    "LOWER_STRUCTURE_UNAVAILABLE",
    "NO_LOWER_POINT_EVIDENCE",
    "ONLY_THIRD_CLASS_POINTS",
    "SELL12_OUTSIDE_TOP_FRACTAL",
    "SELL12_CENTER_INCOMPLETE",
    "HIGHEST_MAPPING_NOT_UNIQUE",
    "UNIQUE_MAPPING",
]
RiskDirectionalSupplyClass = Literal[
    "LOWER_STRUCTURE_UNAVAILABLE",
    "SELL12_PRESENT",
    "BUY12_PRESENT_SELL12_ABSENT",
    "NO_FIRST_SECOND_DIRECTIONAL_POINTS",
]
_MA_PERIODS = (5, 13, 21, 34, 55, 89, 144, 233)


def _canonical_symbol(symbol: str) -> str:
    value = symbol.strip().upper()
    if value.startswith(("SH.", "SZ.")):
        return value
    if value.endswith(".SH"):
        return "SH." + value[:-3]
    if value.endswith(".SZ"):
        return "SZ." + value[:-3]
    raise ValueError(f"unsupported A-share ETF symbol: {symbol!r}")


def _fingerprint(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class FactBlocker:
    field: str
    code: str
    detail: str

    def __post_init__(self) -> None:
        if not self.field or not self.code or not self.detail:
            raise ValueError("fact blocker fields are required")


@dataclass(frozen=True, slots=True)
class EtfTrackingMapping:
    symbol: str
    tracked_index: str
    known_at: datetime
    effective_from: datetime
    valid_until: datetime
    evidence_ids: tuple[str, ...]
    authoritative: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _canonical_symbol(self.symbol))
        known = normalize_datetime(self.known_at, "known_at")
        effective = normalize_datetime(self.effective_from, "effective_from")
        valid_until = normalize_datetime(self.valid_until, "valid_until")
        if known > effective or effective > valid_until:
            raise ValueError("ETF tracking mapping time order is invalid")
        if not self.tracked_index or not self.evidence_ids:
            raise ValueError("ETF tracking identity and evidence are required")
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("ETF tracking evidence IDs must be unique")
        object.__setattr__(self, "known_at", known)
        object.__setattr__(self, "effective_from", effective)
        object.__setattr__(self, "valid_until", valid_until)

    def visible_at(self, observed_at: datetime) -> bool:
        observed = normalize_datetime(observed_at, "observed_at")
        return (
            self.known_at <= observed
            and self.effective_from <= observed <= self.valid_until
        )


@dataclass(frozen=True, slots=True)
class PitBasketSnapshot:
    tracked_index: str
    candidate_session: date
    source_update_date: date
    members: tuple[str, ...]
    mapping_id: str
    source: str = "BaoStock.query_hs300_stocks"

    def __post_init__(self) -> None:
        if not self.tracked_index or not self.mapping_id:
            raise ValueError("basket identity is required")
        if self.source_update_date > self.candidate_session:
            raise ValueError("basket update cannot follow its candidate session")
        if self.members != tuple(sorted(set(self.members))):
            raise ValueError("basket members must be unique and sorted")


@dataclass(frozen=True, slots=True)
class EtfProxySelectionFacts:
    snapshot: SelectionResearchSnapshot | None
    basket: PitBasketSnapshot | None
    grade: FactGrade
    blockers: tuple[FactBlocker, ...]

    @property
    def full_system_eligible(self) -> bool:
        return self.grade == "FULL_SYSTEM_ELIGIBLE" and not self.blockers


@dataclass(frozen=True, slots=True)
class DailyMarketBar:
    session: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    known_at: datetime
    completed: bool = True

    def __post_init__(self) -> None:
        known = normalize_datetime(self.known_at, "known_at")
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError("market OHLC must be positive")
        if self.low > min(self.open, self.close) or self.high < max(
            self.open, self.close
        ):
            raise ValueError("market OHLC is inconsistent")
        if self.volume < 0:
            raise ValueError("market volume cannot be negative")
        object.__setattr__(self, "known_at", known)


@dataclass(frozen=True, slots=True)
class CompletedPeriodBar:
    period: PeriodKind
    period_key: str
    start_session: date
    end_session: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    known_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "known_at", normalize_datetime(self.known_at, "known_at")
        )
        if self.start_session > self.end_session:
            raise ValueError("completed period starts after it ends")


@dataclass(frozen=True, slots=True)
class RiskStructureStateFact:
    period: PeriodKind
    state: RiskState
    observed_at: datetime
    evidence_bar_end: datetime | None
    mapping_unique: bool
    mapped_center_id: str | None
    stroke_mode: str
    source_revision: str

    def __post_init__(self) -> None:
        observed = normalize_datetime(self.observed_at, "observed_at")
        object.__setattr__(self, "observed_at", observed)
        if self.evidence_bar_end is not None:
            evidence = normalize_datetime(self.evidence_bar_end, "evidence_bar_end")
            if evidence > observed:
                raise ValueError("risk evidence cannot follow its observation")
            object.__setattr__(self, "evidence_bar_end", evidence)
        if not self.source_revision:
            raise ValueError("risk structure source revision is required")
        if (
            self.mapping_unique
            and self.state != "NONE"
            and not self.mapped_center_id
        ):
            raise ValueError("a resolved active risk event requires its center ID")


@dataclass(frozen=True, slots=True)
class HigherTimeframeRiskFacts:
    snapshot: HigherTimeframeRiskSnapshot | None
    period_bars: tuple[tuple[PeriodKind, tuple[CompletedPeriodBar, ...]], ...]
    ma5: tuple[tuple[PeriodKind, Decimal | None], ...]
    blockers: tuple[FactBlocker, ...]

    @property
    def gate(self) -> str:
        return "UNRESOLVED" if self.snapshot is None else self.snapshot.gate


@dataclass(frozen=True, slots=True)
class BasketStrengthFacts:
    snapshot: SectorStrengthSnapshot
    basket: PitBasketSnapshot | None
    grade: FactGrade
    blockers: tuple[FactBlocker, ...]


@dataclass(frozen=True, slots=True)
class BenchmarkStructureRiskFacts:
    """M/W/D structure facts, including the explicit D -> 30m input gate."""

    states: tuple[RiskStructureStateFacts, ...]
    completed_30m_prefix_count: int
    blockers: tuple[FactBlocker, ...]

    def __post_init__(self) -> None:
        periods = tuple(value.fact.period for value in self.states)
        if periods != ("M", "W", "D"):
            raise ValueError("benchmark risk states must be ordered M/W/D")
        if self.completed_30m_prefix_count < 0:
            raise ValueError("completed 30m prefix count cannot be negative")


@dataclass(frozen=True, slots=True)
class EtfProxyCandidateDecisionFacts:
    """Auditable envelope around the unchanged strict strategy candidate decision core."""

    selection: EtfProxySelectionFacts
    anchor: BottomFractalAnchorFacts
    basket_strength: BasketStrengthFacts
    benchmark_structure: BenchmarkStructureRiskFacts
    market_risk: HigherTimeframeRiskFacts
    candidate_snapshot: CandidateSnapshot | None
    decision: CandidateDecision | None
    grade: FactGrade
    blockers: tuple[FactBlocker, ...]

    @property
    def full_system_eligible(self) -> bool:
        return self.grade == "FULL_SYSTEM_ELIGIBLE" and self.decision is not None


@dataclass(frozen=True, slots=True)
class BottomFractalAnchorFacts:
    anchor_session: date | None
    confirmation_time: datetime | None
    fractal_middle_session: date | None
    fractal_value: Decimal | None
    up_pen_start_present: bool
    stroke_mode: str
    source_revision: str
    blockers: tuple[FactBlocker, ...]
    warnings: tuple[FactBlocker, ...] = ()

    @property
    def resolved(self) -> bool:
        return self.anchor_session is not None and not self.blockers


@dataclass(frozen=True, slots=True)
class FrozenStructureBar:
    end_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    completed: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "end_at", normalize_datetime(self.end_at, "end_at"))
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError("frozen structure OHLC must be positive")
        if self.low > min(self.open, self.close) or self.high < max(
            self.open, self.close
        ):
            raise ValueError("frozen structure OHLC is inconsistent")
        if self.volume < 0:
            raise ValueError("frozen structure volume cannot be negative")


@dataclass(frozen=True, slots=True)
class RiskCenterPointEvidence:
    center_id: str
    center_level_rank: int
    center_completed: bool
    center_expanded: bool
    point_type: Literal["1sell", "2sell", "3sell", "3buy", "1buy", "2buy"]
    point_anchor_at: datetime
    point_available_at: datetime

    def __post_init__(self) -> None:
        anchor = normalize_datetime(self.point_anchor_at, "point_anchor_at")
        available = normalize_datetime(self.point_available_at, "point_available_at")
        if anchor > available:
            raise ValueError("risk point cannot be available before its anchor")
        if not self.center_id or self.center_level_rank < 0:
            raise ValueError("risk center identity and level rank are required")
        if self.point_type not in {
            "1sell",
            "2sell",
            "3sell",
            "3buy",
            "1buy",
            "2buy",
        }:
            raise ValueError("risk center point type is unsupported")
        object.__setattr__(self, "point_anchor_at", anchor)
        object.__setattr__(self, "point_available_at", available)


@dataclass(frozen=True, slots=True)
class RiskMappingPointEvidenceFacts:
    """Stable, causal identity for one retained lower-level point.

    ``point_id`` deliberately excludes observation-specific mapping state
    (whether this point lies inside the *current* top-fractal interval and
    whether its center is the highest candidate).  The same structural point
    can therefore be de-duplicated across successive top-fractal events while
    those event-relative facts remain explicit alongside it.
    """

    point_id: str
    source_symbol: str
    source_frequency: str
    center_id: str
    center_level_rank: int
    center_completed: bool
    center_expanded: bool
    point_type: Literal["1sell", "2sell", "3sell", "3buy"]
    point_anchor_at: datetime
    point_available_at: datetime
    inside_active_top_interval: bool
    highest_mapping_candidate: bool

    @staticmethod
    def identity(
        *,
        source_symbol: str,
        source_frequency: str,
        center_id: str,
        center_level_rank: int,
        point_type: str,
        point_anchor_at: datetime,
        point_available_at: datetime,
    ) -> str:
        return _fingerprint(
            {
                "contract": "chanlun-risk-mapping-point-identity",
                "source_symbol": source_symbol,
                "source_frequency": source_frequency,
                "center_id": center_id,
                "center_level_rank": center_level_rank,
                "point_type": point_type,
                "point_anchor_at": point_anchor_at,
                "point_available_at": point_available_at,
            }
        )

    def __post_init__(self) -> None:
        symbol = self.source_symbol.strip()
        frequency = self.source_frequency.strip()
        anchor = normalize_datetime(self.point_anchor_at, "point_anchor_at")
        available = normalize_datetime(
            self.point_available_at, "point_available_at"
        )
        if not symbol or not frequency or not self.center_id:
            raise ValueError("risk mapping point source and center are required")
        if self.center_level_rank < 0:
            raise ValueError("risk mapping point center rank cannot be negative")
        if self.point_type not in {"1sell", "2sell", "3sell", "3buy"}:
            raise ValueError("risk mapping point type is unsupported")
        if anchor > available:
            raise ValueError("risk mapping point cannot be available before its anchor")
        expected = self.identity(
            source_symbol=symbol,
            source_frequency=frequency,
            center_id=self.center_id,
            center_level_rank=self.center_level_rank,
            point_type=self.point_type,
            point_anchor_at=anchor,
            point_available_at=available,
        )
        if self.point_id != expected:
            raise ValueError("risk mapping point identity does not match its evidence")
        if self.highest_mapping_candidate and (
            self.point_type not in {"1sell", "2sell"}
            or not self.center_completed
            or not self.inside_active_top_interval
        ):
            raise ValueError("highest mapping candidate is not eligible evidence")
        object.__setattr__(self, "source_symbol", symbol)
        object.__setattr__(self, "source_frequency", frequency)
        object.__setattr__(self, "point_anchor_at", anchor)
        object.__setattr__(self, "point_available_at", available)

    def document(self) -> dict[str, object]:
        return {
            "point_id": self.point_id,
            "source_symbol": self.source_symbol,
            "source_frequency": self.source_frequency,
            "center_id": self.center_id,
            "center_level_rank": self.center_level_rank,
            "center_completed": self.center_completed,
            "center_expanded": self.center_expanded,
            "point_type": self.point_type,
            "point_anchor_at": self.point_anchor_at.isoformat(),
            "point_available_at": self.point_available_at.isoformat(),
            "inside_active_top_interval": self.inside_active_top_interval,
            "highest_mapping_candidate": self.highest_mapping_candidate,
        }

    @classmethod
    def from_document(cls, raw: object) -> "RiskMappingPointEvidenceFacts":
        expected = {
            "point_id",
            "source_symbol",
            "source_frequency",
            "center_id",
            "center_level_rank",
            "center_completed",
            "center_expanded",
            "point_type",
            "point_anchor_at",
            "point_available_at",
            "inside_active_top_interval",
            "highest_mapping_candidate",
        }
        if not isinstance(raw, Mapping) or set(raw) != expected:
            raise ValueError("risk mapping point evidence document is malformed")
        boolean_fields = {
            "center_completed",
            "center_expanded",
            "inside_active_top_interval",
            "highest_mapping_candidate",
        }
        if any(type(raw.get(field)) is not bool for field in boolean_fields):
            raise ValueError("risk mapping point boolean evidence is malformed")
        if type(raw.get("center_level_rank")) is not int:
            raise ValueError("risk mapping point center rank is malformed")
        try:
            anchor = datetime.fromisoformat(str(raw["point_anchor_at"]))
            available = datetime.fromisoformat(str(raw["point_available_at"]))
        except (TypeError, ValueError) as exc:
            raise ValueError("risk mapping point time evidence is malformed") from exc
        return cls(
            point_id=str(raw["point_id"]),
            source_symbol=str(raw["source_symbol"]),
            source_frequency=str(raw["source_frequency"]),
            center_id=str(raw["center_id"]),
            center_level_rank=int(raw["center_level_rank"]),
            center_completed=bool(raw["center_completed"]),
            center_expanded=bool(raw["center_expanded"]),
            point_type=str(raw["point_type"]),  # type: ignore[arg-type]
            point_anchor_at=anchor,
            point_available_at=available,
            inside_active_top_interval=bool(raw["inside_active_top_interval"]),
            highest_mapping_candidate=bool(raw["highest_mapping_candidate"]),
        )


@dataclass(frozen=True, slots=True)
class RiskDiagnosticBuyPointEvidenceFacts:
    """Stable identity for a diagnostic-only first/second buy.

    This contract is intentionally separate from
    :class:`RiskMappingPointEvidenceFacts`.  A diagnostic buy can be opened on
    a causal chart for human review, but it can never become a top-fractal
    mapping candidate, alter a risk gate, or authorize an order.
    """

    point_id: str
    source_symbol: str
    source_frequency: str
    center_id: str
    center_level_rank: int
    center_completed: bool
    center_expanded: bool
    point_type: Literal["1buy", "2buy"]
    point_anchor_at: datetime
    point_available_at: datetime
    inside_active_top_interval: bool

    @staticmethod
    def identity(
        *,
        source_symbol: str,
        source_frequency: str,
        center_id: str,
        center_level_rank: int,
        point_type: str,
        point_anchor_at: datetime,
        point_available_at: datetime,
    ) -> str:
        return _fingerprint(
            {
                "contract": "chanlun-risk-diagnostic-buy-point-identity",
                "source_symbol": source_symbol,
                "source_frequency": source_frequency,
                "center_id": center_id,
                "center_level_rank": center_level_rank,
                "point_type": point_type,
                "point_anchor_at": point_anchor_at,
                "point_available_at": point_available_at,
            }
        )

    def __post_init__(self) -> None:
        symbol = self.source_symbol.strip()
        frequency = self.source_frequency.strip()
        anchor = normalize_datetime(self.point_anchor_at, "point_anchor_at")
        available = normalize_datetime(
            self.point_available_at, "point_available_at"
        )
        if not symbol or not frequency or not self.center_id:
            raise ValueError("diagnostic buy point source and center are required")
        if self.center_level_rank < 0:
            raise ValueError("diagnostic buy point center rank cannot be negative")
        if self.point_type not in {"1buy", "2buy"}:
            raise ValueError("diagnostic buy point type is unsupported")
        if anchor > available:
            raise ValueError(
                "diagnostic buy point cannot be available before its anchor"
            )
        expected = self.identity(
            source_symbol=symbol,
            source_frequency=frequency,
            center_id=self.center_id,
            center_level_rank=self.center_level_rank,
            point_type=self.point_type,
            point_anchor_at=anchor,
            point_available_at=available,
        )
        if self.point_id != expected:
            raise ValueError(
                "diagnostic buy point identity does not match its evidence"
            )
        object.__setattr__(self, "source_symbol", symbol)
        object.__setattr__(self, "source_frequency", frequency)
        object.__setattr__(self, "point_anchor_at", anchor)
        object.__setattr__(self, "point_available_at", available)

    def document(self) -> dict[str, object]:
        return {
            "point_id": self.point_id,
            "source_symbol": self.source_symbol,
            "source_frequency": self.source_frequency,
            "center_id": self.center_id,
            "center_level_rank": self.center_level_rank,
            "center_completed": self.center_completed,
            "center_expanded": self.center_expanded,
            "point_type": self.point_type,
            "point_anchor_at": self.point_anchor_at.isoformat(),
            "point_available_at": self.point_available_at.isoformat(),
            "inside_active_top_interval": self.inside_active_top_interval,
            "diagnostic_only": True,
            "mapping_eligible": False,
        }

    @classmethod
    def from_document(cls, raw: object) -> "RiskDiagnosticBuyPointEvidenceFacts":
        expected = {
            "point_id",
            "source_symbol",
            "source_frequency",
            "center_id",
            "center_level_rank",
            "center_completed",
            "center_expanded",
            "point_type",
            "point_anchor_at",
            "point_available_at",
            "inside_active_top_interval",
            "diagnostic_only",
            "mapping_eligible",
        }
        if not isinstance(raw, Mapping) or set(raw) != expected:
            raise ValueError("diagnostic buy point evidence document is malformed")
        boolean_fields = {
            "center_completed",
            "center_expanded",
            "inside_active_top_interval",
            "diagnostic_only",
            "mapping_eligible",
        }
        if any(type(raw.get(field)) is not bool for field in boolean_fields):
            raise ValueError("diagnostic buy point boolean evidence is malformed")
        if raw.get("diagnostic_only") is not True or raw.get(
            "mapping_eligible"
        ) is not False:
            raise ValueError("diagnostic buy point was promoted into mapping evidence")
        if type(raw.get("center_level_rank")) is not int:
            raise ValueError("diagnostic buy point center rank is malformed")
        try:
            anchor = datetime.fromisoformat(str(raw["point_anchor_at"]))
            available = datetime.fromisoformat(str(raw["point_available_at"]))
        except (TypeError, ValueError) as exc:
            raise ValueError("diagnostic buy point time evidence is malformed") from exc
        return cls(
            point_id=str(raw["point_id"]),
            source_symbol=str(raw["source_symbol"]),
            source_frequency=str(raw["source_frequency"]),
            center_id=str(raw["center_id"]),
            center_level_rank=int(raw["center_level_rank"]),
            center_completed=bool(raw["center_completed"]),
            center_expanded=bool(raw["center_expanded"]),
            point_type=str(raw["point_type"]),  # type: ignore[arg-type]
            point_anchor_at=anchor,
            point_available_at=available,
            inside_active_top_interval=bool(raw["inside_active_top_interval"]),
        )


@dataclass(frozen=True, slots=True)
class RiskMappingSupplyFacts:
    """Lossless explanation of the top-fractal mapping supply.

    The frozen strict strategy mapping still accepts only a completed lower center carrying
    a first/second sell whose anchor falls inside the three high-period
    fractal bars.  These counts explain *why* that mapping did or did not
    exist; they never promote a third-class point or relax the interval.
    """

    classification: RiskMappingSupplyClass
    lower_structure_available: bool
    point_evidence_count: int
    point_type_counts: tuple[tuple[str, int], ...]
    completed_sell12_count: int
    in_top_interval_sell12_count: int
    completed_in_top_interval_sell12_count: int
    incomplete_in_top_interval_sell12_count: int
    outside_top_interval_sell12_count: int
    highest_candidate_center_count: int
    point_evidence: tuple[RiskMappingPointEvidenceFacts, ...]
    # Buy-side first/second points are diagnostic only.  They explain a
    # directional supply imbalance but never enter the frozen top-fractal
    # first/second-sell selector or the stable mapping-point identity table.
    diagnostic_buy_point_type_counts: tuple[tuple[str, int], ...]
    diagnostic_buy_point_evidence: tuple[RiskDiagnosticBuyPointEvidenceFacts, ...]

    def __post_init__(self) -> None:
        expected_types = ("1sell", "2sell", "3sell", "3buy")
        if tuple(name for name, _count in self.point_type_counts) != expected_types:
            raise ValueError("risk mapping point counts must be ordered 1/2/3sell/3buy")
        counts = tuple(count for _name, count in self.point_type_counts)
        scalar_counts = (
            self.point_evidence_count,
            self.completed_sell12_count,
            self.in_top_interval_sell12_count,
            self.completed_in_top_interval_sell12_count,
            self.incomplete_in_top_interval_sell12_count,
            self.outside_top_interval_sell12_count,
            self.highest_candidate_center_count,
            *counts,
        )
        if any(type(value) is not int or value < 0 for value in scalar_counts):
            raise ValueError("risk mapping supply counts must be non-negative integers")
        if sum(counts) != self.point_evidence_count:
            raise ValueError("risk mapping point-type counts do not reconcile")
        sell12_count = counts[0] + counts[1]
        if (
            self.completed_sell12_count > sell12_count
            or self.in_top_interval_sell12_count
            + self.outside_top_interval_sell12_count
            != sell12_count
            or self.completed_in_top_interval_sell12_count
            + self.incomplete_in_top_interval_sell12_count
            != self.in_top_interval_sell12_count
            or self.completed_in_top_interval_sell12_count
            > self.completed_sell12_count
        ):
            raise ValueError("risk mapping sell-point counts do not reconcile")
        details = tuple(self.point_evidence)
        if any(
            not isinstance(value, RiskMappingPointEvidenceFacts) for value in details
        ):
            raise ValueError("risk mapping point evidence is malformed")
        if len(details) != self.point_evidence_count:
            raise ValueError("risk mapping point identities do not reconcile")
        if len({value.point_id for value in details}) != len(details):
            raise ValueError("risk mapping point identities must be unique")
        detail_counts = tuple(
            sum(value.point_type == point_type for value in details)
            for point_type in expected_types
        )
        if detail_counts != counts:
            raise ValueError("risk mapping point details changed type counts")
        detailed_sell12 = tuple(
            value for value in details if value.point_type in {"1sell", "2sell"}
        )
        detailed_inside = tuple(
            value for value in detailed_sell12 if value.inside_active_top_interval
        )
        if (
            sum(value.center_completed for value in detailed_sell12)
            != self.completed_sell12_count
            or len(detailed_inside) != self.in_top_interval_sell12_count
            or sum(value.center_completed for value in detailed_inside)
            != self.completed_in_top_interval_sell12_count
        ):
            raise ValueError("risk mapping point details changed sell counts")
        highest_centers = {
            value.center_id for value in details if value.highest_mapping_candidate
        }
        if len(highest_centers) != self.highest_candidate_center_count:
            raise ValueError("risk mapping point details changed candidate centers")
        object.__setattr__(self, "point_evidence", details)

        diagnostic = tuple(self.diagnostic_buy_point_type_counts)
        if tuple(name for name, _count in diagnostic) != ("1buy", "2buy"):
            raise ValueError("diagnostic buy-point counts must be ordered 1buy/2buy")
        diagnostic_counts = tuple(count for _name, count in diagnostic)
        if any(type(value) is not int or value < 0 for value in diagnostic_counts):
            raise ValueError(
                "diagnostic buy-point counts must be non-negative integers"
            )
        if not self.lower_structure_available and any(diagnostic_counts):
            raise ValueError(
                "unavailable lower structure cannot carry diagnostic buy points"
            )
        object.__setattr__(self, "diagnostic_buy_point_type_counts", diagnostic)

        diagnostic_details = tuple(self.diagnostic_buy_point_evidence)
        if any(
            not isinstance(value, RiskDiagnosticBuyPointEvidenceFacts)
            for value in diagnostic_details
        ):
            raise ValueError("diagnostic buy-point evidence is malformed")
        if len({value.point_id for value in diagnostic_details}) != len(
            diagnostic_details
        ):
            raise ValueError("diagnostic buy-point identities must be unique")
        diagnostic_detail_counts = tuple(
            sum(value.point_type == point_type for value in diagnostic_details)
            for point_type in ("1buy", "2buy")
        )
        if diagnostic_detail_counts != diagnostic_counts:
            raise ValueError("diagnostic buy-point identities do not reconcile")
        object.__setattr__(self, "diagnostic_buy_point_evidence", diagnostic_details)
        expected_class: RiskMappingSupplyClass
        if not self.lower_structure_available:
            if any(scalar_counts):
                raise ValueError("unavailable lower structure cannot carry mapping evidence")
            expected_class = "LOWER_STRUCTURE_UNAVAILABLE"
        elif self.point_evidence_count == 0:
            expected_class = "NO_LOWER_POINT_EVIDENCE"
        elif sell12_count == 0:
            expected_class = "ONLY_THIRD_CLASS_POINTS"
        elif self.in_top_interval_sell12_count == 0:
            expected_class = "SELL12_OUTSIDE_TOP_FRACTAL"
        elif self.completed_in_top_interval_sell12_count == 0:
            expected_class = "SELL12_CENTER_INCOMPLETE"
        elif self.highest_candidate_center_count > 1:
            expected_class = "HIGHEST_MAPPING_NOT_UNIQUE"
        elif self.highest_candidate_center_count == 1:
            expected_class = "UNIQUE_MAPPING"
        else:
            raise ValueError("completed in-interval sell evidence lost its candidate center")
        if self.classification != expected_class:
            raise ValueError(
                "risk mapping supply classification is inconsistent: "
                f"expected {expected_class}, got {self.classification}"
            )

    @property
    def diagnostic_directional_classification(
        self,
    ) -> RiskDirectionalSupplyClass:
        if not self.lower_structure_available:
            return "LOWER_STRUCTURE_UNAVAILABLE"
        sell12_count = self.point_type_counts[0][1] + self.point_type_counts[1][1]
        if sell12_count:
            return "SELL12_PRESENT"
        if sum(count for _name, count in self.diagnostic_buy_point_type_counts):
            return "BUY12_PRESENT_SELL12_ABSENT"
        return "NO_FIRST_SECOND_DIRECTIONAL_POINTS"

    def document(self) -> dict[str, object]:
        document: dict[str, object] = {
            "classification": self.classification,
            "lower_structure_available": self.lower_structure_available,
            "point_evidence_count": self.point_evidence_count,
            "point_type_counts": dict(self.point_type_counts),
            "completed_sell12_count": self.completed_sell12_count,
            "in_top_interval_sell12_count": self.in_top_interval_sell12_count,
            "completed_in_top_interval_sell12_count": (
                self.completed_in_top_interval_sell12_count
            ),
            "incomplete_in_top_interval_sell12_count": (
                self.incomplete_in_top_interval_sell12_count
            ),
            "outside_top_interval_sell12_count": (
                self.outside_top_interval_sell12_count
            ),
            "highest_candidate_center_count": self.highest_candidate_center_count,
        }
        document["point_evidence"] = [
            value.document() for value in self.point_evidence
        ]
        document["diagnostic_buy_point_type_counts"] = dict(
            self.diagnostic_buy_point_type_counts
        )
        document["diagnostic_directional_classification"] = (
            self.diagnostic_directional_classification
        )
        document["diagnostic_buy_point_evidence"] = [
            value.document() for value in self.diagnostic_buy_point_evidence
        ]
        return document

    @classmethod
    def from_document(cls, raw: object) -> "RiskMappingSupplyFacts":
        expected = {
            "classification",
            "lower_structure_available",
            "point_evidence_count",
            "point_type_counts",
            "completed_sell12_count",
            "in_top_interval_sell12_count",
            "completed_in_top_interval_sell12_count",
            "incomplete_in_top_interval_sell12_count",
            "outside_top_interval_sell12_count",
            "highest_candidate_center_count",
        }
        expected |= {
            "point_evidence",
            "diagnostic_buy_point_type_counts",
            "diagnostic_directional_classification",
            "diagnostic_buy_point_evidence",
        }
        if not isinstance(raw, Mapping) or set(raw) != expected:
            raise ValueError("risk mapping supply document is malformed")
        point_counts = raw.get("point_type_counts")
        ordered_types = ("1sell", "2sell", "3sell", "3buy")
        if not isinstance(point_counts, Mapping) or set(point_counts) != set(
            ordered_types
        ):
            raise ValueError("risk mapping point counts are malformed")
        if type(raw.get("lower_structure_available")) is not bool:
            raise ValueError("risk mapping lower-structure availability is malformed")
        integer_fields = expected - {
            "classification",
            "lower_structure_available",
            "point_type_counts",
            "point_evidence",
            "diagnostic_buy_point_type_counts",
            "diagnostic_directional_classification",
            "diagnostic_buy_point_evidence",
        }
        if any(type(raw.get(field)) is not int for field in integer_fields) or any(
            type(point_counts.get(name)) is not int for name in ordered_types
        ):
            raise ValueError("risk mapping supply counts are malformed")
        raw_evidence = raw.get("point_evidence")
        if not isinstance(raw_evidence, list):
            raise ValueError("risk mapping point evidence must be a list")
        point_evidence = tuple(
            RiskMappingPointEvidenceFacts.from_document(value)
            for value in raw_evidence
        )
        raw_diagnostic = raw.get("diagnostic_buy_point_type_counts")
        if not isinstance(raw_diagnostic, Mapping) or set(raw_diagnostic) != {
            "1buy",
            "2buy",
        }:
            raise ValueError("diagnostic buy-point counts are malformed")
        if any(
            type(raw_diagnostic.get(name)) is not int for name in ("1buy", "2buy")
        ):
            raise ValueError("diagnostic buy-point counts are malformed")
        diagnostic_counts = tuple(
            (name, int(raw_diagnostic[name])) for name in ("1buy", "2buy")
        )
        raw_diagnostic_evidence = raw.get("diagnostic_buy_point_evidence")
        if not isinstance(raw_diagnostic_evidence, list):
            raise ValueError("diagnostic buy-point evidence must be a list")
        diagnostic_evidence = tuple(
            RiskDiagnosticBuyPointEvidenceFacts.from_document(value)
            for value in raw_diagnostic_evidence
        )
        result = cls(
            classification=str(raw["classification"]),  # type: ignore[arg-type]
            lower_structure_available=bool(raw["lower_structure_available"]),
            point_evidence_count=int(raw["point_evidence_count"]),
            point_type_counts=tuple(
                (name, int(point_counts[name])) for name in ordered_types
            ),
            completed_sell12_count=int(raw["completed_sell12_count"]),
            in_top_interval_sell12_count=int(
                raw["in_top_interval_sell12_count"]
            ),
            completed_in_top_interval_sell12_count=int(
                raw["completed_in_top_interval_sell12_count"]
            ),
            incomplete_in_top_interval_sell12_count=int(
                raw["incomplete_in_top_interval_sell12_count"]
            ),
            outside_top_interval_sell12_count=int(
                raw["outside_top_interval_sell12_count"]
            ),
            highest_candidate_center_count=int(
                raw["highest_candidate_center_count"]
            ),
            point_evidence=point_evidence,
            diagnostic_buy_point_type_counts=diagnostic_counts,
            diagnostic_buy_point_evidence=diagnostic_evidence,
        )
        if raw.get("diagnostic_directional_classification") != (
            result.diagnostic_directional_classification
        ):
            raise ValueError("diagnostic directional classification is inconsistent")
        return result


@dataclass(frozen=True, slots=True)
class RiskStructureStateFacts:
    fact: RiskStructureStateFact
    active_top_interval: tuple[datetime, datetime] | None
    mapped_center_id: str | None
    mapping_candidate_ids: tuple[str, ...]
    blockers: tuple[FactBlocker, ...]
    warnings: tuple[FactBlocker, ...] = ()
    mapping_supply: RiskMappingSupplyFacts | None = None


@dataclass(frozen=True, slots=True)
class QmtCorporateActionEvent:
    event_id: str
    effective_on: date
    available_at: datetime
    dr: Decimal
    interest: Decimal
    stock_bonus: Decimal
    stock_gift: Decimal
    allot_num: Decimal
    allot_price: Decimal
    gugai: Decimal

    def __post_init__(self) -> None:
        available = normalize_datetime(self.available_at, "available_at")
        if not self.event_id or self.dr <= 0:
            raise ValueError("QMT corporate-action identity and positive dr are required")
        if available.date() != self.effective_on:
            raise ValueError("QMT event availability must be on its effective session")
        object.__setattr__(self, "available_at", available)


@dataclass(frozen=True, slots=True)
class QmtCorporateActionLedger:
    symbol: str
    snapshot_content_sha256: str
    source_store_sha256: str
    events: tuple[QmtCorporateActionEvent, ...]
    authority_attestation_id: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _canonical_symbol(self.symbol))
        effective = tuple(event.effective_on for event in self.events)
        if effective != tuple(sorted(set(effective))):
            raise ValueError("QMT corporate actions must be unique and chronological")
        if not self.snapshot_content_sha256.startswith("sha256:") or not self.source_store_sha256.startswith(
            "sha256:"
        ):
            raise ValueError("QMT corporate-action source hashes are required")

    def visible_events(self, observed_at: datetime) -> tuple[QmtCorporateActionEvent, ...]:
        observed = normalize_datetime(observed_at, "observed_at")
        return tuple(event for event in self.events if event.available_at <= observed)


@dataclass(frozen=True, slots=True)
class QmtCorporateActionLedgerFacts:
    ledger: QmtCorporateActionLedger | None
    grade: FactGrade
    blockers: tuple[FactBlocker, ...]


@dataclass(frozen=True, slots=True)
class CausalAdjustedStructureBars:
    bars: tuple[FrozenStructureBar, ...]
    price_basis_revision: str
    applied_event_ids: tuple[str, ...]
    grade: FactGrade
    blockers: tuple[FactBlocker, ...]


def structure_bars_from_daily(
    rows: Sequence[DailyMarketBar],
) -> tuple[FrozenStructureBar, ...]:
    return tuple(
        FrozenStructureBar(
            end_at=row.known_at,
            open=row.open,
            high=row.high,
            low=row.low,
            close=row.close,
            volume=row.volume,
            completed=row.completed,
        )
        for row in rows
    )


def structure_bars_from_periods(
    rows: Sequence[CompletedPeriodBar],
) -> tuple[FrozenStructureBar, ...]:
    return tuple(
        FrozenStructureBar(
            end_at=row.known_at,
            open=row.open,
            high=row.high,
            low=row.low,
            close=row.close,
            volume=row.volume,
        )
        for row in rows
    )


def _qmt_snapshot_content_sha256(payload: Mapping[str, object]) -> str:
    stable = {
        key: value
        for key, value in payload.items()
        if key not in {"generated_at", "content_sha256"}
    }
    encoded = json.dumps(
        stable,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def load_qmt_corporate_action_ledger(
    path: Path,
    *,
    symbol: str,
    expected_source_store_sha256: str | None = None,
    authority_attestation_id: str | None = None,
) -> QmtCorporateActionLedgerFacts:
    """Load one effective-dated QMT ledger without assuming empty means none."""

    canonical = _canonical_symbol(symbol)
    provider_symbol = canonical.split(".", 1)[1] + "." + canonical.split(".", 1)[0]
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("QMT corporate-action snapshot must be an object")
    blockers: list[FactBlocker] = []
    if payload.get("schema") != "chanlun-qmt-etf-corporate-actions":
        blockers.append(
            FactBlocker(
                "qmt_corporate_action_schema",
                "QMT_CORPORATE_ACTION_SCHEMA_UNSUPPORTED",
                str(payload.get("schema")),
            )
        )
    computed_hash = _qmt_snapshot_content_sha256(payload)
    recorded_hash = str(payload.get("content_sha256", ""))
    if computed_hash != recorded_hash:
        blockers.append(
            FactBlocker(
                "qmt_corporate_action_snapshot_hash",
                "QMT_CORPORATE_ACTION_SNAPSHOT_HASH_MISMATCH",
                f"recorded={recorded_hash}; computed={computed_hash}",
            )
        )
    source_hash = str(payload.get("source_store_sha256", ""))
    if not source_hash.startswith("sha256:"):
        blockers.append(
            FactBlocker(
                "qmt_corporate_action_source_store",
                "QMT_CORPORATE_ACTION_SOURCE_HASH_MISSING",
                source_hash or "MISSING",
            )
        )
    if (
        expected_source_store_sha256 is not None
        and source_hash != expected_source_store_sha256
    ):
        blockers.append(
            FactBlocker(
                "qmt_corporate_action_source_store",
                "QMT_CORPORATE_ACTION_SOURCE_HASH_CHANGED",
                f"expected={expected_source_store_sha256}; observed={source_hash}",
            )
        )
    instruments = payload.get("instruments")
    if not isinstance(instruments, list):
        instruments = []
    matches = tuple(
        item
        for item in instruments
        if isinstance(item, Mapping)
        and str(item.get("code", "")).upper() == provider_symbol
    )
    if len(matches) != 1:
        blockers.append(
            FactBlocker(
                "qmt_corporate_action_instrument",
                "QMT_CORPORATE_ACTION_INSTRUMENT_NOT_UNIQUE",
                f"symbol={provider_symbol}; matches={len(matches)}",
            )
        )
        return QmtCorporateActionLedgerFacts(None, "UNRESOLVED", tuple(blockers))
    instrument = matches[0]
    if instrument.get("status") != "EFFECTIVE_DATED_EVENTS_AVAILABLE":
        blockers.append(
            FactBlocker(
                "qmt_corporate_action_status",
                "QMT_CORPORATE_ACTION_EVENTS_UNAVAILABLE_UNKNOWN_NOT_NONE",
                str(instrument.get("status")),
            )
        )
    expected_columns = (
        "time",
        "interest",
        "stockBonus",
        "stockGift",
        "allotNum",
        "allotPrice",
        "gugai",
        "dr",
    )
    if tuple(instrument.get("provider_columns", ())) != expected_columns:
        blockers.append(
            FactBlocker(
                "qmt_corporate_action_fields",
                "QMT_CORPORATE_ACTION_FIELDS_MISMATCH",
                repr(instrument.get("provider_columns")),
            )
        )
    raw_events = instrument.get("events")
    if not isinstance(raw_events, list):
        raw_events = []
    events: list[QmtCorporateActionEvent] = []
    for raw_event in raw_events:
        if not isinstance(raw_event, Mapping) or not isinstance(
            raw_event.get("raw"), Mapping
        ):
            blockers.append(
                FactBlocker(
                    "qmt_corporate_action_event",
                    "QMT_CORPORATE_ACTION_EVENT_MALFORMED",
                    repr(raw_event),
                )
            )
            continue
        if raw_event.get("availability_policy") != (
            "EFFECTIVE_SESSION_OPEN_RESEARCH_ASSUMPTION"
        ):
            blockers.append(
                FactBlocker(
                    "qmt_corporate_action_availability",
                    "QMT_CORPORATE_ACTION_AVAILABILITY_POLICY_MISMATCH",
                    str(raw_event.get("availability_policy")),
                )
            )
            continue
        effective_on = date.fromisoformat(str(raw_event["effective_on"]))
        raw = raw_event["raw"]
        event_id = _fingerprint(
            {
                "symbol": canonical,
                "effective_on": effective_on,
                "raw": raw,
                "snapshot": recorded_hash,
            }
        )
        try:
            events.append(
                QmtCorporateActionEvent(
                    event_id=event_id,
                    effective_on=effective_on,
                    available_at=datetime.combine(
                        effective_on, time(9, 30), tzinfo=CN
                    ),
                    dr=Decimal(str(raw["dr"])),
                    interest=Decimal(str(raw["interest"])),
                    stock_bonus=Decimal(str(raw["stockBonus"])),
                    stock_gift=Decimal(str(raw["stockGift"])),
                    allot_num=Decimal(str(raw["allotNum"])),
                    allot_price=Decimal(str(raw["allotPrice"])),
                    gugai=Decimal(str(raw["gugai"])),
                )
            )
        except (KeyError, ValueError, ArithmeticError) as exc:
            blockers.append(
                FactBlocker(
                    "qmt_corporate_action_event",
                    "QMT_CORPORATE_ACTION_EVENT_INVALID",
                    f"effective_on={effective_on}; error={exc}",
                )
            )
    fatal = tuple(
        blocker
        for blocker in blockers
        if blocker.code
        not in {"QMT_CORPORATE_ACTION_AUTHORITY_ATTESTATION_MISSING"}
    )
    if fatal:
        return QmtCorporateActionLedgerFacts(None, "UNRESOLVED", tuple(blockers))
    if authority_attestation_id is None:
        blockers.append(
            FactBlocker(
                "qmt_corporate_action_authority",
                "QMT_CORPORATE_ACTION_AUTHORITY_ATTESTATION_MISSING",
                "effective-session availability is a research assumption",
            )
        )
    ledger = QmtCorporateActionLedger(
        symbol=canonical,
        snapshot_content_sha256=recorded_hash,
        source_store_sha256=source_hash,
        events=tuple(events),
        authority_attestation_id=authority_attestation_id,
    )
    return QmtCorporateActionLedgerFacts(
        ledger=ledger,
        grade=(
            "FULL_SYSTEM_ELIGIBLE"
            if authority_attestation_id is not None
            else "RESEARCH_ONLY"
        ),
        blockers=tuple(blockers),
    )


def apply_qmt_causal_adjustments(
    bars: Sequence[FrozenStructureBar],
    *,
    ledger_facts: QmtCorporateActionLedgerFacts,
    decision_time: datetime,
) -> CausalAdjustedStructureBars:
    decision = normalize_datetime(decision_time, "decision_time")
    if ledger_facts.ledger is None:
        return CausalAdjustedStructureBars(
            bars=(),
            price_basis_revision="UNRESOLVED",
            applied_event_ids=(),
            grade="UNRESOLVED",
            blockers=ledger_facts.blockers,
        )
    visible_events = ledger_facts.ledger.visible_events(decision)
    output: list[FrozenStructureBar] = []
    applied_ids: set[str] = set()
    for bar in bars:
        if not bar.completed or bar.end_at > decision:
            continue
        multiplier = Decimal("1")
        for event in visible_events:
            if event.effective_on <= bar.end_at.date():
                multiplier *= event.dr
                applied_ids.add(event.event_id)
        output.append(
            FrozenStructureBar(
                end_at=bar.end_at,
                open=bar.open * multiplier,
                high=bar.high * multiplier,
                low=bar.low * multiplier,
                close=bar.close * multiplier,
                volume=bar.volume,
            )
        )
    revision = _fingerprint(
        {
            "symbol": ledger_facts.ledger.symbol,
            "snapshot": ledger_facts.ledger.snapshot_content_sha256,
            "decision": decision,
            "events": sorted(applied_ids),
            "mode": "QMT_EFFECTIVE_DR_CAUSAL_FORWARD",
        }
    )
    return CausalAdjustedStructureBars(
        bars=tuple(output),
        price_basis_revision=revision,
        applied_event_ids=tuple(sorted(applied_ids)),
        grade=ledger_facts.grade,
        blockers=ledger_facts.blockers,
    )


def select_unique_top_center_mapping(
    evidence: Sequence[RiskCenterPointEvidence],
    *,
    interval_start: datetime,
    interval_end: datetime,
    decision_time: datetime,
) -> tuple[str | None, tuple[str, ...]]:
    """Select the highest unique completed center containing a 1/2 sell.

    Both high-fractal interval boundaries are inclusive, matching the strict strategy
    specification.  A lower-level center is ignored whenever at least one
    candidate exists at a higher structural rank.
    """

    start = normalize_datetime(interval_start, "interval_start")
    end = normalize_datetime(interval_end, "interval_end")
    decision = normalize_datetime(decision_time, "decision_time")
    if start > end or end > decision:
        raise ValueError("risk mapping interval is not causally visible")
    candidates = tuple(
        row
        for row in evidence
        if row.center_completed
        and row.point_type in {"1sell", "2sell"}
        and start <= row.point_anchor_at <= end
        and row.point_available_at <= decision
    )
    if not candidates:
        return None, ()
    highest = max(row.center_level_rank for row in candidates)
    ids = tuple(
        sorted(
            {
                row.center_id
                for row in candidates
                if row.center_level_rank == highest
            }
        )
    )
    return (ids[0] if len(ids) == 1 else None), ids


def _strict_structure_state(
    bars: Sequence[FrozenStructureBar],
    *,
    symbol: str,
    frequency: str,
    decision_time: datetime,
    structure_price_quantum: Decimal,
    price_basis_revision: str,
):
    import pandas as pd

    from chanlun.core.cl import CL
    from chanlun.decision_support.trading_system.runtime_config import (
        strict_cl_config,
    )

    decision = normalize_datetime(decision_time, "decision_time")
    visible = tuple(
        bar for bar in bars if bar.completed and bar.end_at <= decision
    )
    ends = tuple(bar.end_at for bar in visible)
    if ends != tuple(sorted(set(ends))):
        raise ValueError("frozen structure bars must be unique and chronological")
    config = strict_cl_config(
        structure_price_quantum=structure_price_quantum,
        price_basis_revision=price_basis_revision,
    )
    frame = pd.DataFrame(
        (
            {
                "code": symbol,
                "date": bar.end_at,
                "open": float(bar.open),
                "high": float(bar.high),
                "low": float(bar.low),
                "close": float(bar.close),
                "volume": float(bar.volume),
            }
            for bar in visible
        )
    )
    if frame.empty:
        return None
    return CL(symbol, frequency, config, market="a").process_klines(frame)


def _fractal_times(fractal: object) -> tuple[datetime, datetime]:
    raw = tuple(
        value
        for included in fractal.klines
        if included is not None
        for value in included.klines
    )
    if not raw:
        raise ValueError("frozen fractal has no source bars")
    return (
        normalize_datetime(min(value.date for value in raw), "fractal_start"),
        normalize_datetime(max(value.date for value in raw), "fractal_end"),
    )


def _lower_risk_evidence(
    state: object,
    *,
    frequency: str,
    decision_time: datetime,
) -> tuple[RiskCenterPointEvidence, ...]:
    """Map the unified strict structure snapshot into high-risk evidence.

    The completed high-timeframe fractal and its lower-timeframe center/point
    mapping use the same canonical strict stroke and recursive structure
    authority as charting, screening and replay.
    """

    from chanlun.core.strict_structure.center_relation import (
        classify_center_relation,
    )
    from chanlun.core.strict_structure.models import (
        CenterRelation,
        CenterState,
    )

    decision = normalize_datetime(decision_time, "decision_time")
    getter = getattr(state, "get_strict_evidence", None)
    if not callable(getter):
        raise ValueError("lower risk state has no strict evidence authority")
    evidence = getter()
    if (
        getattr(evidence, "source_frequency", None) != frequency
        or normalize_datetime(
            getattr(evidence, "source_closed_at", None),
            "source_closed_at",
        )
        > decision
    ):
        raise ValueError("lower strict evidence context is inconsistent")

    centers: dict[str, object] = {}
    expanded_ids: set[str] = set()
    for level in evidence.structure.levels:
        level_centers = tuple(level.center_result.centers)
        for center in level_centers:
            previous = centers.setdefault(center.center_id, center)
            if previous is not center:
                raise ValueError("strict lower center ids are duplicated")
        for previous, current in zip(level_centers, level_centers[1:]):
            if classify_center_relation(previous, current) is CenterRelation.UPGRADE:
                expanded_ids.update((previous.center_id, current.center_id))

    output: dict[tuple[str, str, datetime, datetime], RiskCenterPointEvidence] = {}
    for point in evidence.confirmed_points:
        available = normalize_datetime(point.available_at, "point_available_at")
        if available > decision:
            raise ValueError("lower strict evidence contains a future point")
        if point.point_type not in {
            "1sell",
            "2sell",
            "3sell",
            "3buy",
            "1buy",
            "2buy",
        }:
            continue
        if point.center_id is None:
            raise ValueError("strict risk point has no center identity")
        center = centers.get(point.center_id)
        if center is None or center.structural_level != point.structural_level:
            raise ValueError("strict risk point references an unavailable center")
        anchor = normalize_datetime(point.anchor_at, "point_anchor_at")
        fact = RiskCenterPointEvidence(
            center_id=center.center_id,
            center_level_rank=center.structural_level,
            center_completed=center.state is not CenterState.ONGOING,
            center_expanded=center.center_id in expanded_ids,
            point_type=point.point_type,
            point_anchor_at=anchor,
            point_available_at=available,
        )
        output[(center.center_id, point.point_type, anchor, available)] = fact
    return tuple(
        sorted(
            output.values(),
            key=lambda row: (
                row.point_available_at,
                row.center_level_rank,
                row.center_id,
                row.point_type,
            ),
        )
    )


def _risk_mapping_supply_facts(
    evidence: Sequence[RiskCenterPointEvidence],
    *,
    diagnostic_buy_evidence: Sequence[RiskCenterPointEvidence] = (),
    lower_structure_available: bool,
    interval: tuple[datetime, datetime],
    mapping_candidate_ids: tuple[str, ...],
    source_symbol: str,
    source_frequency: str,
) -> RiskMappingSupplyFacts:
    if not source_symbol or not source_frequency:
        raise ValueError("risk mapping point source identity is required")
    if any(
        row.point_type not in {"1sell", "2sell", "3sell", "3buy"}
        for row in evidence
    ):
        raise ValueError("mapping evidence contains a diagnostic-only point")
    if any(
        row.point_type not in {"1buy", "2buy"}
        for row in diagnostic_buy_evidence
    ):
        raise ValueError("diagnostic buy evidence contains a mapping point")
    counts = tuple(
        (point_type, sum(row.point_type == point_type for row in evidence))
        for point_type in ("1sell", "2sell", "3sell", "3buy")
    )
    sell12 = tuple(row for row in evidence if row.point_type in {"1sell", "2sell"})
    in_interval = tuple(
        row
        for row in sell12
        if interval[0] <= row.point_anchor_at <= interval[1]
    )
    completed_in_interval = tuple(row for row in in_interval if row.center_completed)
    classification: RiskMappingSupplyClass
    if not lower_structure_available:
        classification = "LOWER_STRUCTURE_UNAVAILABLE"
    elif not evidence:
        classification = "NO_LOWER_POINT_EVIDENCE"
    elif not sell12:
        classification = "ONLY_THIRD_CLASS_POINTS"
    elif not in_interval:
        classification = "SELL12_OUTSIDE_TOP_FRACTAL"
    elif not completed_in_interval:
        classification = "SELL12_CENTER_INCOMPLETE"
    elif len(mapping_candidate_ids) > 1:
        classification = "HIGHEST_MAPPING_NOT_UNIQUE"
    else:
        classification = "UNIQUE_MAPPING"
    point_evidence = tuple(
            RiskMappingPointEvidenceFacts(
                point_id=RiskMappingPointEvidenceFacts.identity(
                    source_symbol=source_symbol,
                    source_frequency=source_frequency,
                    center_id=row.center_id,
                    center_level_rank=row.center_level_rank,
                    point_type=row.point_type,
                    point_anchor_at=row.point_anchor_at,
                    point_available_at=row.point_available_at,
                ),
                source_symbol=source_symbol,
                source_frequency=source_frequency,
                center_id=row.center_id,
                center_level_rank=row.center_level_rank,
                center_completed=row.center_completed,
                center_expanded=row.center_expanded,
                point_type=row.point_type,
                point_anchor_at=row.point_anchor_at,
                point_available_at=row.point_available_at,
                inside_active_top_interval=(
                    interval[0] <= row.point_anchor_at <= interval[1]
                ),
                highest_mapping_candidate=(
                    row.center_id in mapping_candidate_ids
                    and row.center_completed
                    and row.point_type in {"1sell", "2sell"}
                    and interval[0] <= row.point_anchor_at <= interval[1]
                ),
            )
            for row in evidence
        )
    diagnostic_point_evidence = tuple(
            RiskDiagnosticBuyPointEvidenceFacts(
                point_id=RiskDiagnosticBuyPointEvidenceFacts.identity(
                    source_symbol=source_symbol,
                    source_frequency=source_frequency,
                    center_id=row.center_id,
                    center_level_rank=row.center_level_rank,
                    point_type=row.point_type,
                    point_anchor_at=row.point_anchor_at,
                    point_available_at=row.point_available_at,
                ),
                source_symbol=source_symbol,
                source_frequency=source_frequency,
                center_id=row.center_id,
                center_level_rank=row.center_level_rank,
                center_completed=row.center_completed,
                center_expanded=row.center_expanded,
                point_type=row.point_type,
                point_anchor_at=row.point_anchor_at,
                point_available_at=row.point_available_at,
                inside_active_top_interval=(
                    interval[0] <= row.point_anchor_at <= interval[1]
                ),
            )
            for row in diagnostic_buy_evidence
        )
    return RiskMappingSupplyFacts(
        classification=classification,
        lower_structure_available=lower_structure_available,
        point_evidence_count=len(evidence),
        point_type_counts=counts,
        completed_sell12_count=sum(row.center_completed for row in sell12),
        in_top_interval_sell12_count=len(in_interval),
        completed_in_top_interval_sell12_count=len(completed_in_interval),
        incomplete_in_top_interval_sell12_count=(
            len(in_interval) - len(completed_in_interval)
        ),
        outside_top_interval_sell12_count=len(sell12) - len(in_interval),
        highest_candidate_center_count=len(mapping_candidate_ids),
        point_evidence=point_evidence,
        diagnostic_buy_point_type_counts=tuple(
            (
                point_type,
                sum(
                    row.point_type == point_type
                    for row in diagnostic_buy_evidence
                ),
            )
            for point_type in ("1buy", "2buy")
        ),
        diagnostic_buy_point_evidence=diagnostic_point_evidence,
    )


def build_risk_structure_state_fact(
    *,
    period: PeriodKind,
    high_frequency: str,
    lower_frequency: str,
    high_bars: Sequence[FrozenStructureBar],
    lower_bars: Sequence[FrozenStructureBar],
    decision_time: datetime,
    symbol: str,
    structure_lineage_sink: dict[str, object] | None = None,
    structure_price_quantum: Decimal = Decimal("0.01"),
    price_basis_revision: str | None = None,
) -> RiskStructureStateFacts:
    """Build one strict strategy high-timeframe risk state from one structure authority.

    High-period fractals, lower centers and points all come from the canonical
    strict recursive evidence endpoint used by charting, screening and replay.
    The adapter only performs the strict strategy mapping: the highest unique
    completed lower center carrying a first/second sell inside the inclusive
    high-fractal interval. Ambiguity becomes ``FORMED_UNRESOLVED``.
    """

    decision = normalize_datetime(decision_time, "decision_time")
    if (
        not isinstance(structure_price_quantum, Decimal)
        or not structure_price_quantum.is_finite()
        or structure_price_quantum <= 0
    ):
        raise ValueError("structure_price_quantum must be a positive Decimal")
    resolved_price_basis = price_basis_revision or _fingerprint(
        {
            "schema": "chanlun-risk-frozen-price-basis",
            "symbol": symbol,
            "mode": "CALLER_FROZEN_POINT_IN_TIME_PRICES",
            "stroke_mode": STRICT_STROKE_MODE,
        }
    )
    if (
        not isinstance(resolved_price_basis, str)
        or not resolved_price_basis.strip()
        or resolved_price_basis != resolved_price_basis.strip()
    ):
        raise ValueError("price_basis_revision must be a non-empty string")
    source_revision = _fingerprint(
        {
            "period": period,
            "high_frequency": high_frequency,
            "lower_frequency": lower_frequency,
            "decision": decision,
            "stroke_mode": STRICT_STROKE_MODE,
            "center_point_authority": "STRICT_RECURSIVE_EVIDENCE",
            "structure_price_quantum": str(structure_price_quantum),
            "price_basis_revision": resolved_price_basis,
            "high": [
                (bar.end_at, bar.open, bar.high, bar.low, bar.close, bar.volume)
                for bar in high_bars
                if bar.completed and bar.end_at <= decision
            ],
            "lower": [
                (bar.end_at, bar.open, bar.high, bar.low, bar.close, bar.volume)
                for bar in lower_bars
                if bar.completed and bar.end_at <= decision
            ],
        }
    )
    high_state = _strict_structure_state(
        high_bars,
        symbol=symbol,
        frequency=high_frequency,
        decision_time=decision,
        structure_price_quantum=structure_price_quantum,
        price_basis_revision=resolved_price_basis,
    )
    completed_tops: list[tuple[datetime, object, tuple[datetime, datetime]]] = []
    if high_state is not None:
        for fractal in high_state.get_fxs():
            if fractal.type != "ding" or not fractal.done:
                continue
            interval = _fractal_times(fractal)
            if interval[1] <= decision:
                completed_tops.append((interval[1], fractal, interval))
    if not completed_tops:
        fact = RiskStructureStateFact(
            period=period,
            state="NONE",
            observed_at=decision,
            evidence_bar_end=None,
            mapping_unique=True,
            mapped_center_id=None,
            stroke_mode=STRICT_STROKE_MODE,
            source_revision=source_revision,
        )
        return RiskStructureStateFacts(fact, None, None, (), ())

    top_confirmation, top, interval = max(completed_tops, key=lambda row: row[0])
    # A later, completed opposite high-timeframe pen closes this top event.
    down_pen_end: datetime | None = None
    for line in high_state.get_bis():
        if (
            line.type == "down"
            and line.is_done()
            and line.locked_at is not None
            and line.start.type == "ding"
            and line.start.k.k_index == top.k.k_index
            and line.end.type == "di"
        ):
            locked = normalize_datetime(line.locked_at, "down_pen_locked_at")
            if locked <= decision:
                down_pen_end = locked
                break
    if down_pen_end is not None:
        fact = RiskStructureStateFact(
            period=period,
            state="NONE",
            observed_at=decision,
            evidence_bar_end=down_pen_end,
            mapping_unique=True,
            mapped_center_id=None,
            stroke_mode=STRICT_STROKE_MODE,
            source_revision=source_revision,
        )
        return RiskStructureStateFacts(fact, None, None, (), ())

    lower_state = _strict_structure_state(
        lower_bars,
        symbol=symbol,
        frequency=lower_frequency,
        decision_time=decision,
        structure_price_quantum=structure_price_quantum,
        price_basis_revision=resolved_price_basis,
    )
    all_evidence = (
        ()
        if lower_state is None
        else _lower_risk_evidence(
            lower_state,
            frequency=lower_frequency,
            decision_time=decision,
        )
    )
    # First/second buys are retained only as read-only directional supply
    # diagnostics.  They cannot enter the frozen top-fractal mapping, stable
    # mapping-point identities, later third-point lifecycle, or gate state.
    evidence = tuple(
        row
        for row in all_evidence
        if row.point_type in {"1sell", "2sell", "3sell", "3buy"}
    )
    diagnostic_buy_evidence = tuple(
        row for row in all_evidence if row.point_type in {"1buy", "2buy"}
    )
    mapped, candidates = select_unique_top_center_mapping(
        evidence,
        interval_start=interval[0],
        interval_end=interval[1],
        decision_time=decision,
    )
    mapping_supply = _risk_mapping_supply_facts(
        evidence,
        diagnostic_buy_evidence=diagnostic_buy_evidence,
        lower_structure_available=lower_state is not None,
        interval=interval,
        mapping_candidate_ids=candidates,
        source_symbol=symbol,
        source_frequency=lower_frequency,
    )
    if structure_lineage_sink is not None and lower_state is not None:
        from chanlun.decision_support.trading_system.warmup_structure_lineage import (
            capture_warmup_structure_lineage_snapshot,
        )

        structure_lineage_sink[period] = (
            capture_warmup_structure_lineage_snapshot(
                period=period,
                source_symbol=symbol,
                source_frequency=lower_frequency,
                source_bars=lower_bars,
                state=lower_state,
                mapping_supply=mapping_supply,
            )
        )
    if mapped is None:
        code = (
            "NO_COMPLETED_LOWER_1SELL_2SELL_CENTER_IN_TOP_FRACTAL"
            if not candidates
            else "HIGHEST_LOWER_CENTER_MAPPING_NOT_UNIQUE"
        )
        fact = RiskStructureStateFact(
            period=period,
            state="FORMED_UNRESOLVED",
            observed_at=decision,
            evidence_bar_end=top_confirmation,
            mapping_unique=False,
            mapped_center_id=None,
            stroke_mode=STRICT_STROKE_MODE,
            source_revision=source_revision,
        )
        return RiskStructureStateFacts(
            fact=fact,
            active_top_interval=interval,
            mapped_center_id=None,
            mapping_candidate_ids=candidates,
            blockers=(
                FactBlocker(
                    f"{period}_center_mapping",
                    code,
                    f"candidate_ids={candidates!r}",
                ),
            ),
            mapping_supply=mapping_supply,
        )

    later_events = tuple(
        row
        for row in evidence
        if row.center_id == mapped
        and row.point_type in {"3sell", "3buy"}
        and row.point_available_at >= top_confirmation
    )
    warnings: list[FactBlocker] = []
    state: RiskState = "FORMED"
    evidence_end = top_confirmation
    for event in later_events:
        if event.point_type == "3sell":
            if event.center_expanded:
                warnings.append(
                    FactBlocker(
                        f"{period}_center_extension",
                        "THIRD_SELL_IGNORED_AFTER_CENTER_EXTENSION",
                        event.center_id,
                    )
                )
                continue
            state = "PEN_RISK_CONFIRMED"
            evidence_end = event.point_available_at
            break
        state = "RESOLVED_CONTINUATION"
        evidence_end = event.point_available_at
        break
    fact = RiskStructureStateFact(
        period=period,
        state=state,
        observed_at=decision,
        evidence_bar_end=evidence_end,
        mapping_unique=True,
        mapped_center_id=mapped,
        stroke_mode=STRICT_STROKE_MODE,
        source_revision=source_revision,
    )
    return RiskStructureStateFacts(
        fact=fact,
        active_top_interval=interval,
        mapped_center_id=mapped,
        mapping_candidate_ids=candidates,
        blockers=(),
        warnings=tuple(warnings),
        mapping_supply=mapping_supply,
    )


def _period_key(session: date, period: PeriodKind) -> str:
    if period == "D":
        return session.isoformat()
    if period == "W":
        year, week, _weekday = session.isocalendar()
        return f"{year:04d}-W{week:02d}"
    if period == "M":
        return f"{session.year:04d}-{session.month:02d}"
    raise ValueError(f"unsupported period: {period}")


def aggregate_completed_period_bars(
    rows: Sequence[DailyMarketBar],
    *,
    trading_sessions: Sequence[date],
    decision_time: datetime,
    period: PeriodKind,
    calendar_coverage_end: date,
) -> tuple[CompletedPeriodBar, ...]:
    """Aggregate only fully completed exchange-calendar periods.

    For a week/month, having its latest observed daily row is not enough.  The
    supplied calendar must cover the entire calendar period and the bar for
    that period's final trading session must already be visible.
    """

    decision = normalize_datetime(decision_time, "decision_time")
    sessions = tuple(trading_sessions)
    if sessions != tuple(sorted(set(sessions))):
        raise ValueError("trading sessions must be unique and chronological")
    daily = tuple(
        row
        for row in rows
        if row.completed
        and row.known_at <= decision
        and row.session <= decision.date()
    )
    daily_sessions = tuple(row.session for row in daily)
    if daily_sessions != tuple(sorted(set(daily_sessions))):
        raise ValueError("daily bars must be unique and chronological")
    row_by_session = {row.session: row for row in daily}
    calendar_groups: dict[str, list[date]] = {}
    for session in sessions:
        calendar_groups.setdefault(_period_key(session, period), []).append(session)
    row_groups: dict[str, list[DailyMarketBar]] = {}
    for row in daily:
        row_groups.setdefault(_period_key(row.session, period), []).append(row)

    output: list[CompletedPeriodBar] = []
    for key, group in sorted(row_groups.items()):
        expected = calendar_groups.get(key, [])
        if not expected:
            continue
        if period == "W":
            period_calendar_end = max(expected) if expected else group[-1].session
            # Calendar coverage through Sunday proves no later trading day was
            # silently omitted from the supplied calendar.
            calendar_period_end = date.fromisocalendar(
                group[-1].session.isocalendar().year,
                group[-1].session.isocalendar().week,
                7,
            )
        elif period == "M":
            if group[-1].session.month == 12:
                next_month = date(group[-1].session.year + 1, 1, 1)
            else:
                next_month = date(
                    group[-1].session.year, group[-1].session.month + 1, 1
                )
            calendar_period_end = date.fromordinal(next_month.toordinal() - 1)
            period_calendar_end = max(expected)
        else:
            calendar_period_end = group[-1].session
            period_calendar_end = max(expected)
        if calendar_coverage_end < calendar_period_end:
            continue
        if period_calendar_end > decision.date():
            continue
        if period_calendar_end not in row_by_session:
            continue
        expected_visible = tuple(
            value for value in expected if value <= period_calendar_end
        )
        if any(value not in row_by_session for value in expected_visible):
            continue
        ordered = tuple(row_by_session[value] for value in expected_visible)
        output.append(
            CompletedPeriodBar(
                period=period,
                period_key=key,
                start_session=ordered[0].session,
                end_session=ordered[-1].session,
                open=ordered[0].open,
                high=max(row.high for row in ordered),
                low=min(row.low for row in ordered),
                close=ordered[-1].close,
                volume=sum((row.volume for row in ordered), Decimal("0")),
                known_at=ordered[-1].known_at,
            )
        )
    return tuple(output)


def completed_period_ma5(rows: Sequence[CompletedPeriodBar]) -> Decimal | None:
    if len(rows) < 5:
        return None
    return sum((row.close for row in rows[-5:]), Decimal("0")) / Decimal("5")


def latest_completed_bottom_fractal_anchor(
    rows: Sequence[DailyMarketBar],
    *,
    decision_time: datetime,
    symbol: str,
) -> BottomFractalAnchorFacts:
    """Read the latest completed daily bottom fractal from the frozen core.

    The adapter consumes the canonical strict stroke distance mode; it neither
    changes nor reimplements inclusion, fractal or stroke logic.
    The third included K-line's final raw bar is the causal confirmation time.
    """

    decision = normalize_datetime(decision_time, "decision_time")
    visible = tuple(
        row
        for row in rows
        if row.completed
        and row.known_at <= decision
        and row.session <= decision.date()
    )
    sessions = tuple(row.session for row in visible)
    if sessions != tuple(sorted(set(sessions))):
        raise ValueError("daily anchor bars must be unique and chronological")
    source_revision = _fingerprint(
        {
            "symbol": symbol,
            "decision_time": decision,
            "stroke_mode": STRICT_STROKE_MODE,
            "bars": [
                (
                    row.session,
                    row.open,
                    row.high,
                    row.low,
                    row.close,
                    row.volume,
                    row.known_at,
                )
                for row in visible
            ],
        }
    )
    if len(visible) < 3:
        return BottomFractalAnchorFacts(
            None,
            None,
            None,
            None,
            False,
            STRICT_STROKE_MODE,
            source_revision,
            (
                FactBlocker(
                    "broad_market_daily_fractal",
                    "DAILY_FRACTAL_HISTORY_INSUFFICIENT",
                    f"completed_daily_bars={len(visible)}",
                ),
            ),
            (),
        )

    # Import lazily so data-only consumers do not initialize the structure
    # engine.  These are read-only calls into the frozen implementation.
    import pandas as pd

    from chanlun.core.cl import CL
    from chanlun.core.strict_structure.base_profile import strict_base_config

    config = strict_base_config()
    frame = pd.DataFrame(
        (
            {
                "code": symbol,
                "date": row.known_at,
                "open": float(row.open),
                "high": float(row.high),
                "low": float(row.low),
                "close": float(row.close),
                "volume": float(row.volume),
            }
            for row in visible
        )
    )
    state = CL(symbol, "d", config, market="a").process_klines(frame)
    candidates: list[tuple[datetime, object]] = []
    for fractal in state.get_fxs():
        if fractal.type != "di" or not fractal.done:
            continue
        raw = tuple(
            value
            for included in fractal.klines
            if included is not None
            for value in included.klines
        )
        if not raw:
            continue
        confirmation = normalize_datetime(max(value.date for value in raw), "fx_end")
        if confirmation <= decision:
            candidates.append((confirmation, fractal))
    if not candidates:
        return BottomFractalAnchorFacts(
            None,
            None,
            None,
            None,
            False,
            STRICT_STROKE_MODE,
            source_revision,
            (
                FactBlocker(
                    "broad_market_daily_fractal",
                    "COMPLETED_DAILY_BOTTOM_FRACTAL_MISSING",
                    decision.isoformat(),
                ),
            ),
            (),
        )
    latest_time = max(value[0] for value in candidates)
    latest = tuple(fractal for end, fractal in candidates if end == latest_time)
    if len(latest) != 1:
        return BottomFractalAnchorFacts(
            None,
            None,
            None,
            None,
            False,
            STRICT_STROKE_MODE,
            source_revision,
            (
                FactBlocker(
                    "broad_market_daily_fractal",
                    "DAILY_BOTTOM_FRACTAL_NOT_UNIQUE",
                    latest_time.isoformat(),
                ),
            ),
            (),
        )
    fractal = latest[0]
    up_pen_start = any(
        line.type == "up"
        and line.start.type == "di"
        and line.start.k.k_index == fractal.k.k_index
        and line.start.val == fractal.val
        for line in state.get_bis()
    )
    warnings: tuple[FactBlocker, ...] = ()
    if not up_pen_start:
        warnings = (
            FactBlocker(
                "broad_market_daily_up_pen_validation",
                "BOTTOM_FRACTAL_UP_PEN_START_NOT_MATERIALIZED_YET",
                "anchor remains the completed bottom-fractal third bar; "
                + latest_time.isoformat(),
            ),
        )
    middle_time = normalize_datetime(fractal.k.date, "fx_middle")
    return BottomFractalAnchorFacts(
        anchor_session=latest_time.date(),
        confirmation_time=latest_time,
        fractal_middle_session=middle_time.date(),
        fractal_value=Decimal(str(fractal.val)),
        up_pen_start_present=up_pen_start,
        stroke_mode=STRICT_STROKE_MODE,
        source_revision=source_revision,
        blockers=(),
        warnings=warnings,
    )


def build_higher_timeframe_risk_facts(
    daily_bars: Sequence[DailyMarketBar],
    *,
    trading_sessions: Sequence[date],
    calendar_coverage_end: date,
    decision_time: datetime,
    structure_states: Sequence[RiskStructureStateFact],
    snapshot_id: str,
) -> HigherTimeframeRiskFacts:
    decision = normalize_datetime(decision_time, "decision_time")
    grouped: list[tuple[PeriodKind, tuple[CompletedPeriodBar, ...]]] = []
    ma5: list[tuple[PeriodKind, Decimal | None]] = []
    blockers: list[FactBlocker] = []
    for period in ("M", "W", "D"):
        values = aggregate_completed_period_bars(
            daily_bars,
            trading_sessions=trading_sessions,
            decision_time=decision,
            period=period,
            calendar_coverage_end=calendar_coverage_end,
        )
        average = completed_period_ma5(values)
        grouped.append((period, values))
        ma5.append((period, average))
        if average is None:
            blockers.append(
                FactBlocker(
                    field=f"{period}_ma5",
                    code=f"{period}_COMPLETED_MA5_UNAVAILABLE",
                    detail=f"completed_{period}_bars={len(values)}",
                )
            )

    by_period = {state.period: state for state in structure_states}
    if len(by_period) != len(tuple(structure_states)):
        raise ValueError("duplicate high-timeframe risk state")
    for period in ("M", "W", "D"):
        state = by_period.get(period)
        if state is None:
            blockers.append(
                FactBlocker(
                    field=f"{period}_risk_state",
                    code=f"{period}_FROZEN_STRUCTURE_RISK_FACT_MISSING",
                    detail="no certified read-only center-mapping fact was supplied",
                )
            )
            continue
        if state.observed_at > decision:
            blockers.append(
                FactBlocker(
                    field=f"{period}_risk_state",
                    code=f"{period}_RISK_FACT_FROM_FUTURE",
                    detail=state.observed_at.isoformat(),
                )
            )
        if state.stroke_mode != STRICT_STROKE_MODE:
            blockers.append(
                FactBlocker(
                    field=f"{period}_stroke_mode",
                    code=f"{period}_NON_STRICT_STROKE_RISK_FACT",
                    detail=state.stroke_mode,
                )
            )
        if not state.mapping_unique:
            blockers.append(
                FactBlocker(
                    field=f"{period}_center_mapping",
                    code=f"{period}_CENTER_MAPPING_UNRESOLVED",
                    detail=state.mapped_center_id or "UNRESOLVED",
                )
            )

    average_map = dict(ma5)
    required = {"M", "W", "D"}
    if required.difference(by_period) or any(value is None for value in average_map.values()):
        snapshot = None
    else:
        states = tuple(by_period[period] for period in ("M", "W", "D"))
        visible_and_strict = all(
            state.observed_at <= decision
            and state.stroke_mode == STRICT_STROKE_MODE
            for state in states
        )
        snapshot = (
            HigherTimeframeRiskSnapshot(
                snapshot_id=snapshot_id,
                observed_at=decision,
                monthly=by_period["M"].state,
                weekly=by_period["W"].state,
                daily=by_period["D"].state,
                monthly_ma5=average_map["M"],
                weekly_ma5=average_map["W"],
                daily_ma5=average_map["D"],
                mapping_unique=visible_and_strict
                and all(state.mapping_unique for state in states),
            )
            if visible_and_strict
            else None
        )
    return HigherTimeframeRiskFacts(
        snapshot=snapshot,
        period_bars=tuple(grouped),
        ma5=tuple(ma5),
        blockers=tuple(blockers),
    )


def build_benchmark_structure_risk_facts(
    daily_bars: Sequence[DailyMarketBar],
    *,
    trading_sessions: Sequence[date],
    calendar_coverage_end: date,
    decision_time: datetime,
    completed_30m_bars: Sequence[FrozenStructureBar],
    symbol: str = "CSI.000300",
    structure_lineage_sink: dict[str, object] | None = None,
) -> BenchmarkStructureRiskFacts:
    """Build M->W, W->D and D->30m facts from the current prefix.

    The 30-minute bars are an explicit caller-owned input.  Only completed
    bars whose end time is visible at ``decision_time`` are consumed.  An
    absent prefix, or one that does not span the active daily top-fractal
    interval, makes the D mapping unresolved; it is never converted to a
    favourable ``NONE``/``GREEN`` default.
    """

    decision = normalize_datetime(decision_time, "decision_time")
    visible_30m = tuple(
        bar
        for bar in completed_30m_bars
        if bar.completed and bar.end_at <= decision
    )
    visible_ends = tuple(bar.end_at for bar in visible_30m)
    if visible_ends != tuple(sorted(set(visible_ends))):
        raise ValueError("completed CSI300 30m bars must be unique and chronological")

    daily_structure = structure_bars_from_daily(daily_bars)
    weekly_structure = structure_bars_from_periods(
        aggregate_completed_period_bars(
            daily_bars,
            trading_sessions=trading_sessions,
            decision_time=decision,
            period="W",
            calendar_coverage_end=calendar_coverage_end,
        )
    )
    monthly_structure = structure_bars_from_periods(
        aggregate_completed_period_bars(
            daily_bars,
            trading_sessions=trading_sessions,
            decision_time=decision,
            period="M",
            calendar_coverage_end=calendar_coverage_end,
        )
    )
    monthly = build_risk_structure_state_fact(
        period="M",
        high_frequency="m",
        lower_frequency="w",
        high_bars=monthly_structure,
        lower_bars=weekly_structure,
        decision_time=decision,
        symbol=symbol,
        structure_lineage_sink=structure_lineage_sink,
    )
    weekly = build_risk_structure_state_fact(
        period="W",
        high_frequency="w",
        lower_frequency="d",
        high_bars=weekly_structure,
        lower_bars=daily_structure,
        decision_time=decision,
        symbol=symbol,
        structure_lineage_sink=structure_lineage_sink,
    )
    daily = build_risk_structure_state_fact(
        period="D",
        high_frequency="d",
        lower_frequency="30m",
        high_bars=daily_structure,
        lower_bars=visible_30m,
        decision_time=decision,
        symbol=symbol,
        structure_lineage_sink=structure_lineage_sink,
    )

    input_blockers: list[FactBlocker] = []
    if not visible_30m:
        input_blockers.append(
            FactBlocker(
                "D_lower_structure_30m",
                "CSI300_COMPLETED_30M_BARS_MISSING",
                f"decision_time={decision.isoformat()}; visible_completed_bars=0",
            )
        )
    elif daily.active_top_interval is not None:
        interval_start, interval_end = daily.active_top_interval
        if visible_30m[0].end_at > interval_start or visible_30m[-1].end_at < interval_end:
            input_blockers.append(
                FactBlocker(
                    "D_lower_structure_30m",
                    "CSI300_30M_COVERAGE_DOES_NOT_SPAN_D_TOP_FRACTAL",
                    (
                        f"coverage={visible_30m[0].end_at.isoformat()}.."
                        f"{visible_30m[-1].end_at.isoformat()}; "
                        f"required={interval_start.isoformat()}.."
                        f"{interval_end.isoformat()}"
                    ),
                )
            )

    if input_blockers:
        active = daily.active_top_interval is not None
        daily = RiskStructureStateFacts(
            fact=replace(
                daily.fact,
                state=("FORMED_UNRESOLVED" if active else daily.fact.state),
                mapping_unique=False,
                mapped_center_id=None,
            ),
            active_top_interval=daily.active_top_interval,
            mapped_center_id=None,
            mapping_candidate_ids=daily.mapping_candidate_ids,
            blockers=daily.blockers + tuple(input_blockers),
            warnings=daily.warnings,
            mapping_supply=daily.mapping_supply,
        )
    states = (monthly, weekly, daily)
    return BenchmarkStructureRiskFacts(
        states=states,
        completed_30m_prefix_count=len(visible_30m),
        blockers=tuple(
            blocker for value in states for blocker in value.blockers
        ),
    )


def member_ma_strength_category_fast(
    rows: Sequence[CompletedDailyClose],
    *,
    anchor_session: date,
    decision_time: datetime,
) -> int:
    decision = normalize_datetime(decision_time, "decision_time")
    visible = tuple(
        row
        for row in rows
        if row.completed
        and row.known_at <= decision
        and row.session <= decision.date()
    )
    if len(visible) < 5:
        return 1
    prefix = [Decimal("0")]
    for row in visible:
        prefix.append(prefix[-1] + row.close)
    for ordinal, period in enumerate(_MA_PERIODS, start=1):
        conquered = False
        for index, row in enumerate(visible):
            if row.session < anchor_session or index + 1 < period:
                continue
            average = (prefix[index + 1] - prefix[index + 1 - period]) / Decimal(
                period
            )
            if row.close > average:
                conquered = True
                break
        if not conquered:
            return ordinal
    return 9


class EtfProxyPitRepository:
    """Read-only adapter over ``etf_proxy_pit.sqlite3``.

    The repository intentionally resolves a basket only for an exact stored
    candidate session.  Carrying the latest known row forward would silently
    fill unobserved index changes between sparse snapshots.
    """

    def __init__(self, database: Path, *, tracked_index: str = "CSI.000300"):
        self.database = Path(database)
        self.tracked_index = tracked_index

    def _connect(self) -> sqlite3.Connection:
        uri = f"file:{self.database.resolve().as_posix()}?mode=ro"
        return sqlite3.connect(uri, uri=True)

    def available_membership_sessions(self) -> tuple[date, ...]:
        with self._connect() as connection:
            return tuple(
                date.fromisoformat(row[0])
                for row in connection.execute(
                    "SELECT DISTINCT candidate_session FROM memberships ORDER BY 1"
                )
            )

    def exact_basket(self, session: date) -> PitBasketSnapshot | None:
        with self._connect() as connection:
            rows = tuple(
                connection.execute(
                    """
                    SELECT source_update_date, code
                    FROM memberships
                    WHERE candidate_session=?
                    ORDER BY code
                    """,
                    (session.isoformat(),),
                )
            )
        if not rows:
            return None
        update_dates = {date.fromisoformat(str(row[0])) for row in rows}
        if len(update_dates) != 1:
            raise ValueError("basket snapshot contains conflicting update dates")
        members = tuple(str(row[1]) for row in rows)
        mapping_id = _fingerprint(
            {
                "tracked_index": self.tracked_index,
                "candidate_session": session,
                "source_update_date": next(iter(update_dates)),
                "members": members,
            }
        )
        return PitBasketSnapshot(
            tracked_index=self.tracked_index,
            candidate_session=session,
            source_update_date=next(iter(update_dates)),
            members=members,
            mapping_id=mapping_id,
        )

    def build_selection_facts(
        self,
        mapping: EtfTrackingMapping,
        *,
        decision_time: datetime,
        reviewer: str,
        signature: str,
    ) -> EtfProxySelectionFacts:
        decision = normalize_datetime(decision_time, "decision_time")
        blockers: list[FactBlocker] = []
        if mapping.tracked_index != self.tracked_index:
            blockers.append(
                FactBlocker(
                    "tracked_index",
                    "ETF_TRACKED_INDEX_MISMATCH",
                    f"mapping={mapping.tracked_index}; PIT={self.tracked_index}",
                )
            )
        if not mapping.visible_at(decision):
            blockers.append(
                FactBlocker(
                    "tracking_mapping",
                    "ETF_TRACKING_MAPPING_NOT_VISIBLE",
                    decision.isoformat(),
                )
            )
        basket = self.exact_basket(decision.date())
        if basket is None:
            blockers.append(
                FactBlocker(
                    "point_in_time_basket",
                    "EXACT_DECISION_SESSION_BASKET_MISSING",
                    decision.date().isoformat(),
                )
            )
            return EtfProxySelectionFacts(
                snapshot=None,
                basket=None,
                grade="UNRESOLVED",
                blockers=tuple(blockers),
            )
        if len(basket.members) != 300:
            blockers.append(
                FactBlocker(
                    "point_in_time_basket",
                    "CSI300_BASKET_MEMBER_COUNT_INVALID",
                    f"members={len(basket.members)}",
                )
            )
        # The cache has a source update date but no intraday publication
        # timestamp.  Use the source day's close as a conservative visibility
        # boundary and keep the certification downgrade explicit.
        basket_known_at = datetime.combine(
            basket.source_update_date, time(15, 0), tzinfo=CN
        )
        if basket_known_at > decision:
            blockers.append(
                FactBlocker(
                    "basket_known_at",
                    "BASKET_SOURCE_UPDATE_NOT_YET_VISIBLE",
                    basket_known_at.isoformat(),
                )
            )
        blockers.append(
            FactBlocker(
                "basket_publication_time",
                "BASKET_INTRADAY_PUBLICATION_TIMESTAMP_UNAVAILABLE",
                "only source_update_date is stored",
            )
        )
        if not mapping.authoritative:
            blockers.append(
                FactBlocker(
                    "tracking_mapping_authority",
                    "ETF_TRACKING_MAPPING_NOT_AUTHORITATIVE",
                    ",".join(mapping.evidence_ids),
                )
            )
        snapshot = None
        fatal_codes = {
            "ETF_TRACKED_INDEX_MISMATCH",
            "ETF_TRACKING_MAPPING_NOT_VISIBLE",
            "CSI300_BASKET_MEMBER_COUNT_INVALID",
            "BASKET_SOURCE_UPDATE_NOT_YET_VISIBLE",
        }
        if not fatal_codes.intersection(blocker.code for blocker in blockers):
            effective = max(mapping.effective_from, basket_known_at)
            valid_until = min(
                mapping.valid_until,
                datetime.combine(
                    basket.candidate_session, time(23, 59, 59), tzinfo=CN
                ),
            )
            if effective <= valid_until:
                snapshot = SelectionResearchSnapshot(
                    snapshot_id=_fingerprint(
                        {
                            "symbol": mapping.symbol,
                            "basket": basket.mapping_id,
                            "mapping": mapping.evidence_ids,
                            "decision_session": decision.date(),
                        }
                    ),
                    symbol=mapping.symbol,
                    path="ETF_PROXY",
                    effective_at=effective,
                    known_at=max(mapping.known_at, basket_known_at),
                    valid_until=valid_until,
                    reviewer=reviewer,
                    signature=signature,
                    official_evidence_ids=mapping.evidence_ids,
                    industry_opportunity_status="NOT_APPLICABLE",
                    fundamental_role="ETF_PROXY",
                    relative_value_status="ETF_PROXY",
                    point_in_time_total_market_cap=None,
                    peer_set_id=None,
                    basket_mapping_id=basket.mapping_id,
                )
        grade: FactGrade = (
            "FULL_SYSTEM_ELIGIBLE"
            if snapshot is not None and not blockers
            else "RESEARCH_ONLY"
            if snapshot is not None
            else "UNRESOLVED"
        )
        return EtfProxySelectionFacts(snapshot, basket, grade, tuple(blockers))

    def _calendar(self, *, end: date) -> tuple[date, ...]:
        with self._connect() as connection:
            return tuple(
                date.fromisoformat(row[0])
                for row in connection.execute(
                    """
                    SELECT calendar_date FROM trading_calendar
                    WHERE is_trading_day='1' AND calendar_date<=?
                    ORDER BY calendar_date
                    """,
                    (end.isoformat(),),
                )
            )

    def _member_closes(
        self,
        connection: sqlite3.Connection,
        *,
        code: str,
        decision_time: datetime,
        required_sessions: tuple[date, ...],
    ) -> tuple[tuple[CompletedDailyClose, ...], str, tuple[FactBlocker, ...]]:
        if not required_sessions:
            return (), "UNEXPLAINED_GAP", (
                FactBlocker(
                    f"member:{code}:daily_history",
                    "REQUIRED_TRADING_SESSION_RANGE_EMPTY",
                    decision_time.date().isoformat(),
                ),
            )
        master = connection.execute(
            "SELECT ipo_date, out_date FROM security_master WHERE code=?", (code,)
        ).fetchone()
        if master is None:
            return (), "UNEXPLAINED_GAP", (
                FactBlocker(
                    f"member:{code}",
                    "MEMBER_SECURITY_MASTER_MISSING",
                    code,
                ),
            )
        listed_on = date.fromisoformat(str(master[0]))
        out_date = date.fromisoformat(str(master[1])) if master[1] else None
        rows = tuple(
            connection.execute(
                """
                SELECT session, close, trade_status
                FROM daily_bars
                WHERE code=? AND session<=?
                ORDER BY session
                """,
                (code, decision_time.date().isoformat()),
            )
        )
        factors = tuple(
            connection.execute(
                """
                SELECT effective_on, backward_factor
                FROM adjustment_factors
                WHERE code=? AND effective_on<=?
                ORDER BY effective_on
                """,
                (code, decision_time.date().isoformat()),
            )
        )
        factor_dates = tuple(date.fromisoformat(str(row[0])) for row in factors)
        blocker_list: list[FactBlocker] = []
        closes: list[CompletedDailyClose] = []
        row_dates: set[date] = set()
        missing_factor_sessions: list[date] = []
        last_trade_status = "1"
        for raw_session, raw_close, trade_status in rows:
            session = date.fromisoformat(str(raw_session))
            known_at = datetime.combine(session, time(15, 0), tzinfo=CN)
            if known_at > decision_time:
                continue
            row_dates.add(session)
            last_trade_status = str(trade_status)
            factor_pos = bisect_right(factor_dates, session) - 1
            if factor_pos < 0:
                missing_factor_sessions.append(session)
                continue
            close = Decimal(str(raw_close)) * Decimal(str(factors[factor_pos][1]))
            closes.append(
                CompletedDailyClose(
                    session=session,
                    close=close,
                    known_at=known_at,
                )
            )
        if missing_factor_sessions:
            blocker_list.append(
                FactBlocker(
                    f"member:{code}:adjustment",
                    "CAUSAL_BACKWARD_FACTOR_MISSING",
                    (
                        f"count={len(missing_factor_sessions)}; "
                        f"first={missing_factor_sessions[0].isoformat()}; "
                        f"last={missing_factor_sessions[-1].isoformat()}"
                    ),
                )
            )
        expected = tuple(
            session
            for session in required_sessions
            if session >= listed_on and (out_date is None or session <= out_date)
        )
        missing = tuple(session for session in expected if session not in row_dates)
        if missing:
            blocker_list.append(
                FactBlocker(
                    f"member:{code}:daily_history",
                    "UNEXPLAINED_MEMBER_DAILY_GAP",
                    f"count={len(missing)}; first={missing[0].isoformat()}",
                )
            )
            history_status = "UNEXPLAINED_GAP"
        elif listed_on > required_sessions[0]:
            history_status = "NEW_LISTING"
        elif last_trade_status == "0":
            history_status = "SUSPENDED"
        else:
            history_status = "COMPLETE"
        return tuple(closes), history_status, tuple(blocker_list)

    def build_basket_strength_facts(
        self,
        *,
        decision_time: datetime,
        anchor_session: date | None,
        rank: int = 1,
    ) -> BasketStrengthFacts:
        decision = normalize_datetime(decision_time, "decision_time")
        basket = self.exact_basket(decision.date())
        blockers: list[FactBlocker] = []
        if basket is None:
            blockers.append(
                FactBlocker(
                    "point_in_time_basket",
                    "EXACT_DECISION_SESSION_BASKET_MISSING",
                    decision.date().isoformat(),
                )
            )
        if anchor_session is None:
            blockers.append(
                FactBlocker(
                    "broad_market_rebound_anchor",
                    "UNIQUE_COMPLETED_DAILY_BOTTOM_FRACTAL_ANCHOR_MISSING",
                    "the frozen fractal adapter did not supply an anchor",
                )
            )
        if basket is None or anchor_session is None:
            snapshot = SectorStrengthSnapshot(
                snapshot_id=_fingerprint(
                    {
                        "decision": decision,
                        "basket": None if basket is None else basket.mapping_id,
                        "anchor": anchor_session,
                    }
                ),
                sector_id=f"BROAD_BASKET_STRENGTH:{self.tracked_index}",
                observed_at=decision,
                anchor_session=anchor_session or decision.date(),
                member_count=0,
                categories=(),
                strength=None,
                rank=None,
                unresolved_reasons=tuple(blocker.code for blocker in blockers),
            )
            return BasketStrengthFacts(
                snapshot=snapshot,
                basket=basket,
                grade="UNRESOLVED",
                blockers=tuple(blockers),
            )

        calendar = self._calendar(end=decision.date())
        anchor_position = bisect_right(calendar, anchor_session) - 1
        if anchor_position < 0:
            required_sessions = calendar
        else:
            required_sessions = calendar[max(0, anchor_position - 232) :]
        if not required_sessions:
            blockers.append(
                FactBlocker(
                    "trading_calendar",
                    "TRADING_CALENDAR_RANGE_EMPTY",
                    decision.date().isoformat(),
                )
            )
        categories: list[tuple[str, int]] = []
        with self._connect() as connection:
            for code in basket.members:
                closes, status, member_blockers = self._member_closes(
                    connection,
                    code=code,
                    decision_time=decision,
                    required_sessions=required_sessions,
                )
                blockers.extend(member_blockers)
                if status == "UNEXPLAINED_GAP":
                    categories.append((code, 1))
                    continue
                categories.append(
                    (
                        code,
                        member_ma_strength_category_fast(
                            closes,
                            anchor_session=anchor_session,
                            decision_time=decision,
                        ),
                    )
                )
        categories.sort(key=lambda value: value[0])
        # The table has dated factors but no publication timestamp.  The
        # backward factors are safe for causal calculation after filtering by
        # effective date, yet this missing field prevents full certification.
        blockers.append(
            FactBlocker(
                "adjustment_factor_known_at",
                "ADJUSTMENT_FACTOR_PUBLICATION_TIMESTAMP_UNAVAILABLE",
                "effective_on and backward_factor are stored; known_at is not",
            )
        )
        unresolved = tuple(
            blocker.code
            for blocker in blockers
            if blocker.code
            not in {"ADJUSTMENT_FACTOR_PUBLICATION_TIMESTAMP_UNAVAILABLE"}
        )
        if unresolved:
            snapshot = SectorStrengthSnapshot(
                snapshot_id=_fingerprint(
                    {
                        "basket": basket.mapping_id,
                        "anchor": anchor_session,
                        "decision": decision,
                        "unresolved": unresolved,
                    }
                ),
                sector_id=f"BROAD_BASKET_STRENGTH:{self.tracked_index}",
                observed_at=decision,
                anchor_session=anchor_session,
                member_count=len(categories),
                categories=tuple((symbol, 1) for symbol, _ in categories),
                strength=None,
                rank=None,
                unresolved_reasons=unresolved,
            )
            grade: FactGrade = "UNRESOLVED"
        else:
            strength = sum(
                (Decimal(category) for _symbol, category in categories), Decimal("0")
            ) / Decimal(len(categories))
            snapshot = SectorStrengthSnapshot(
                snapshot_id=_fingerprint(
                    {
                        "basket": basket.mapping_id,
                        "anchor": anchor_session,
                        "decision": decision,
                        "categories": categories,
                    }
                ),
                sector_id=f"BROAD_BASKET_STRENGTH:{self.tracked_index}",
                observed_at=decision,
                anchor_session=anchor_session,
                member_count=len(categories),
                categories=tuple(categories),
                strength=strength,
                rank=rank,
            )
            grade = "RESEARCH_ONLY"
        return BasketStrengthFacts(snapshot, basket, grade, tuple(blockers))


def build_etf_proxy_candidate_decision(
    repository: EtfProxyPitRepository,
    mapping: EtfTrackingMapping,
    *,
    decision_time: datetime,
    benchmark_daily_bars: Sequence[DailyMarketBar],
    benchmark_completed_30m_bars: Sequence[FrozenStructureBar],
    trading_sessions: Sequence[date],
    calendar_coverage_end: date,
    tradeability: TradeabilitySnapshot,
    sector_risk: HigherTimeframeRiskSnapshot | None,
    symbol_risk: HigherTimeframeRiskSnapshot | None,
    technical: TechnicalEntrySnapshot,
    account: AccountEntryGate,
    reviewer: str,
    signature: str,
    parameters: StrategyParameters | None = None,
    market: str = "A",
) -> EtfProxyCandidateDecisionFacts:
    """Build one strict ETF_PROXY candidate at an arbitrary decision time.

    Selection, membership and basket strength are regenerated for the exact
    decision session.  The unchanged :func:`evaluate_candidate` core performs
    the final rule checks.  Sector and symbol risk remain separate caller
    facts as required by strict strategy; this adapter never aliases the broad-market risk
    into those labels.
    """

    decision_time = normalize_datetime(decision_time, "decision_time")
    actual_parameters = parameters or etf_parameter_snapshot()
    blockers: list[FactBlocker] = []
    if actual_parameters.selection_path != "ETF_PROXY":
        blockers.append(
            FactBlocker(
                "parameter_selection_path",
                "ETF_PROXY_PARAMETER_SNAPSHOT_REQUIRED",
                actual_parameters.selection_path,
            )
        )
    selection = repository.build_selection_facts(
        mapping,
        decision_time=decision_time,
        reviewer=reviewer,
        signature=signature,
    )
    anchor = latest_completed_bottom_fractal_anchor(
        benchmark_daily_bars,
        decision_time=decision_time,
        symbol=repository.tracked_index,
    )
    basket_strength = repository.build_basket_strength_facts(
        decision_time=decision_time,
        anchor_session=anchor.anchor_session,
    )
    benchmark_structure = build_benchmark_structure_risk_facts(
        benchmark_daily_bars,
        trading_sessions=trading_sessions,
        calendar_coverage_end=calendar_coverage_end,
        decision_time=decision_time,
        completed_30m_bars=benchmark_completed_30m_bars,
        symbol=repository.tracked_index,
    )
    market_risk = build_higher_timeframe_risk_facts(
        benchmark_daily_bars,
        trading_sessions=trading_sessions,
        calendar_coverage_end=calendar_coverage_end,
        decision_time=decision_time,
        structure_states=tuple(
            value.fact for value in benchmark_structure.states
        ),
        snapshot_id=_fingerprint(
            {
                "kind": "ETF_PROXY_MARKET_RISK",
                "tracked_index": repository.tracked_index,
                "decision_time": decision_time,
                "structure_revisions": tuple(
                    value.fact.source_revision
                    for value in benchmark_structure.states
                ),
            }
        ),
    )
    blockers.extend(selection.blockers)
    blockers.extend(anchor.blockers)
    blockers.extend(anchor.warnings)
    blockers.extend(basket_strength.blockers)
    blockers.extend(benchmark_structure.blockers)
    blockers.extend(market_risk.blockers)
    if tradeability.symbol != mapping.symbol:
        blockers.append(
            FactBlocker(
                "tradeability_symbol",
                "ETF_PROXY_TRADEABILITY_SYMBOL_MISMATCH",
                f"mapping={mapping.symbol}; tradeability={tradeability.symbol}",
            )
        )
    if sector_risk is None:
        blockers.append(
            FactBlocker(
                "sector_risk",
                "ETF_PROXY_SECTOR_RISK_FACT_MISSING",
                "strict strategy forbids aliasing broad-market risk into sector risk",
            )
        )
    if symbol_risk is None:
        blockers.append(
            FactBlocker(
                "symbol_risk",
                "ETF_PROXY_SYMBOL_RISK_FACT_MISSING",
                "the ETF's own M/W/D risk snapshot was not supplied",
            )
        )
    if selection.snapshot is None:
        blockers.append(
            FactBlocker(
                "selection_snapshot",
                "ETF_PROXY_SELECTION_SNAPSHOT_UNAVAILABLE",
                decision_time.date().isoformat(),
            )
        )
    if market_risk.snapshot is None:
        blockers.append(
            FactBlocker(
                "market_risk",
                "ETF_PROXY_MARKET_RISK_SNAPSHOT_UNAVAILABLE",
                decision_time.isoformat(),
            )
        )

    candidate: CandidateSnapshot | None = None
    evaluated: CandidateDecision | None = None
    can_evaluate = (
        actual_parameters.selection_path == "ETF_PROXY"
        and selection.snapshot is not None
        and market_risk.snapshot is not None
        and sector_risk is not None
        and symbol_risk is not None
        and tradeability.symbol == mapping.symbol
    )
    if can_evaluate:
        candidate = CandidateSnapshot(
            symbol=mapping.symbol,
            market=market,
            sector_id=basket_strength.snapshot.sector_id,
            decision_time=decision_time,
            research=selection.snapshot,
            tradeability=tradeability,
            market_risk=market_risk.snapshot,
            sector_risk=sector_risk,
            symbol_risk=symbol_risk,
            sector_strength=basket_strength.snapshot,
            technical=technical,
            account=account,
        )
        evaluated = evaluate_candidate(candidate, actual_parameters)

    if evaluated is None:
        grade: FactGrade = "UNRESOLVED"
    elif (
        selection.grade == "FULL_SYSTEM_ELIGIBLE"
        and basket_strength.grade == "FULL_SYSTEM_ELIGIBLE"
        and not blockers
    ):
        grade = "FULL_SYSTEM_ELIGIBLE"
    else:
        grade = "RESEARCH_ONLY"
    return EtfProxyCandidateDecisionFacts(
        selection=selection,
        anchor=anchor,
        basket_strength=basket_strength,
        benchmark_structure=benchmark_structure,
        market_risk=market_risk,
        candidate_snapshot=candidate,
        decision=evaluated,
        grade=grade,
        blockers=tuple(blockers),
    )


__all__ = (
    "BasketStrengthFacts",
    "BenchmarkStructureRiskFacts",
    "BottomFractalAnchorFacts",
    "CompletedPeriodBar",
    "CausalAdjustedStructureBars",
    "DailyMarketBar",
    "EtfProxyPitRepository",
    "EtfProxyCandidateDecisionFacts",
    "EtfProxySelectionFacts",
    "EtfTrackingMapping",
    "FactBlocker",
    "FrozenStructureBar",
    "HigherTimeframeRiskFacts",
    "PitBasketSnapshot",
    "QmtCorporateActionEvent",
    "QmtCorporateActionLedger",
    "QmtCorporateActionLedgerFacts",
    "RiskCenterPointEvidence",
    "RiskDiagnosticBuyPointEvidenceFacts",
    "RiskMappingPointEvidenceFacts",
    "RiskMappingSupplyFacts",
    "RiskStructureStateFact",
    "RiskStructureStateFacts",
    "aggregate_completed_period_bars",
    "apply_qmt_causal_adjustments",
    "build_higher_timeframe_risk_facts",
    "build_benchmark_structure_risk_facts",
    "build_etf_proxy_candidate_decision",
    "build_risk_structure_state_fact",
    "completed_period_ma5",
    "latest_completed_bottom_fractal_anchor",
    "load_qmt_corporate_action_ledger",
    "member_ma_strength_category_fast",
    "select_unique_top_center_mapping",
    "structure_bars_from_daily",
    "structure_bars_from_periods",
)
