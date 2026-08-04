"""Causal direct-recursive facts for sector-triggered individual stocks."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time
import hashlib
from typing import Sequence

import pandas as pd

from chanlun.core.strict_structure.models import ConstituentUnit, TrendType
from chanlun.decision_support.trading_system.backtest.fixed_year import (
    CausalDirectRecursiveDecisionFact,
    final_confirmed_structure_events,
    load_qmt_frame,
    qmt_factor_frame,
)
from chanlun.decision_support.trading_system.backtest.pit_metadata import (
    QmtFactorAt,
    SecurityMasterRecord,
    SectorMembershipChange,
)
from chanlun.decision_support.trading_system.models import StructuralPoint
from chanlun.decision_support.trading_system.v3_sector_first_trigger_plan import (
    SectorFirstTriggerLedger,
    sector_trigger_windows_for_current_member,
    sector_trigger_windows_for_memberships,
)
from chanlun.decision_support.trading_system.v3_recent_year_research import (
    RECENT_YEAR_SELECTION_PATH,
)


DIRECT_SYMBOL_FACT_SCHEMA = "chanlun-v3-sector-first-direct-symbol-facts/v1"


@dataclass(frozen=True, slots=True)
class SectorFirstDirectSymbolFacts:
    schema: str
    algorithm_revision: str
    source_revision: str
    trigger_ledger_sha256: str
    code: str
    requested_start: date
    requested_end: date
    effective_start: date
    source_start: datetime | None
    source_end: datetime | None
    one_minute_row_count: int
    sector_trigger_windows: tuple[tuple[datetime, datetime], ...]
    direct_decisions: tuple[CausalDirectRecursiveDecisionFact, ...]
    strategic_sell_points: tuple[StructuralPoint, ...]
    structural_points: tuple[StructuralPoint, ...]
    completed_units: tuple[ConstituentUnit, ...]
    completed_trends: tuple[TrendType, ...]
    point_anchor_unit_ids: tuple[tuple[str, str], ...]
    point_counts: tuple[tuple[str, int], ...]
    security_master: SecurityMasterRecord
    memberships: tuple[SectorMembershipChange, ...]
    factors: tuple[QmtFactorAt, ...]
    three_program_status: str = "UNRESOLVED"
    data_grade: str = "COMPONENT_ONLY"
    highest_status: str = "RESEARCH_ONLY"
    live_status: str = "LIVE_DISABLED"

    def __post_init__(self) -> None:
        if self.schema != DIRECT_SYMBOL_FACT_SCHEMA:
            raise ValueError("unsupported sector-first direct fact schema")
        if not self.trigger_ledger_sha256.startswith("sha256:"):
            raise ValueError("direct fact trigger ledger identity is invalid")
        if not self.requested_start <= self.effective_start <= self.requested_end:
            raise ValueError("invalid direct fact range")
        if self.code != self.security_master.code or not self.code:
            raise ValueError("direct fact security identity is invalid")
        if self.one_minute_row_count < 0:
            raise ValueError("one-minute row count cannot be negative")
        if (self.source_start is None) != (self.source_end is None):
            raise ValueError("source bounds must both be present or absent")
        if self.source_start is not None and self.source_start > self.source_end:
            raise ValueError("direct source range is inverted")
        if tuple(self.sector_trigger_windows) != tuple(
            sorted(set(self.sector_trigger_windows))
        ):
            raise ValueError("sector trigger windows must be sorted and unique")
        if any(left[0] > left[1] for left in self.sector_trigger_windows):
            raise ValueError("sector trigger window is inverted")
        if tuple(
            (row.first_seen_at, row.l0_point_id) for row in self.direct_decisions
        ) != tuple(
            sorted(
                (row.first_seen_at, row.l0_point_id)
                for row in self.direct_decisions
            )
        ):
            raise ValueError("direct decisions must be chronological")
        if self.point_counts != tuple(sorted(self.point_counts)):
            raise ValueError("direct point counts must be sorted")
        if self.structural_points != tuple(
            sorted(
                self.structural_points,
                key=lambda row: (row.available_at, row.point_id),
            )
        ):
            raise ValueError("direct structural points must be chronological")
        if self.completed_units != tuple(
            sorted(
                self.completed_units,
                key=lambda row: (row.available_at, row.unit_id),
            )
        ):
            raise ValueError("direct completed units must be chronological")
        if self.completed_trends != tuple(
            sorted(
                self.completed_trends,
                key=lambda row: (row.available_at, row.trend_id),
            )
        ):
            raise ValueError("direct completed trends must be chronological")
        if self.point_anchor_unit_ids != tuple(sorted(self.point_anchor_unit_ids)):
            raise ValueError("direct point anchors must be sorted")
        if {
            point_id for point_id, _unit_id in self.point_anchor_unit_ids
        } != {point.point_id for point in self.structural_points}:
            raise ValueError("direct point anchors must cover every structural point")
        known_units = {unit.unit_id for unit in self.completed_units}
        if any(
            unit_id not in known_units
            for _point_id, unit_id in self.point_anchor_unit_ids
        ):
            raise ValueError("direct point anchor references an unknown unit")
        if any(row.code != self.code for row in (*self.memberships, *self.factors)):
            raise ValueError("direct facts crossed a symbol identity")
        if self.three_program_status != "UNRESOLVED":
            raise ValueError("unsigned three-program facts cannot be promoted")

    @property
    def technical_entry_count(self) -> int:
        return sum(row.status == "PASS" for row in self.direct_decisions)

    @property
    def full_system_entry_count(self) -> int:
        # The unique strategy specification requires an independent signed
        # three-program adjudication.  Technical supply must remain visible,
        # but cannot be promoted while that fact is absent.
        return 0

    @property
    def rejection_counts(self) -> tuple[tuple[str, int], ...]:
        counts = Counter(
            reason
            for decision in self.direct_decisions
            for reason in decision.reason_codes
        )
        return tuple(sorted(counts.items()))


def _intersect_windows(
    windows: Sequence[tuple[datetime, datetime]],
    *,
    start_at: datetime,
    end_at: datetime,
) -> tuple[tuple[datetime, datetime], ...]:
    return tuple(
        (max(start, start_at), min(end, end_at))
        for start, end in windows
        if max(start, start_at) <= min(end, end_at)
    )


def _source_revision(
    *,
    frame: pd.DataFrame,
    code: str,
    trigger_ledger_sha256: str,
    memberships: Sequence[SectorMembershipChange],
    factors: Sequence[QmtFactorAt],
    current_sector_id: str | None = None,
) -> str:
    digest = hashlib.sha256()
    digest.update(code.encode("utf-8"))
    digest.update(trigger_ledger_sha256.encode("ascii"))
    digest.update(
        pd.util.hash_pandas_object(
            frame.reset_index(drop=True),
            index=False,
            categorize=False,
        ).to_numpy(dtype="uint64", copy=False).tobytes()
    )
    digest.update(repr(tuple(memberships)).encode("utf-8"))
    digest.update(repr(tuple(factors)).encode("utf-8"))
    if current_sector_id is not None:
        digest.update(current_sector_id.encode("utf-8"))
    digest.update(repr(tuple(sorted(frame.attrs.items()))).encode("utf-8"))
    return "sha256:" + digest.hexdigest()


def build_sector_first_direct_symbol_facts(
    *,
    code: str,
    warmup_start: date,
    requested_start: date,
    requested_end: date,
    effective_start: date,
    algorithm_revision: str,
    trigger_ledger: SectorFirstTriggerLedger,
    trigger_ledger_sha256: str,
    security_master: SecurityMasterRecord,
    memberships: Sequence[SectorMembershipChange],
    qmt_factors: Sequence[QmtFactorAt],
    current_sector_id: str | None = None,
) -> SectorFirstDirectSymbolFacts:
    """Build technical facts only inside sector-triggered entry windows.

    If a valid direct entry is found, a second causal pass from the earliest
    entry to the sample end records strategic sell points even when the sector
    later becomes ineligible.  Sector gates may block entries, never exits.
    """

    if not warmup_start <= requested_start <= effective_start <= requested_end:
        raise ValueError("invalid direct-symbol date boundaries")
    if security_master.code != code:
        raise ValueError("security master does not match direct symbol")
    if not trigger_ledger_sha256.startswith("sha256:"):
        raise ValueError("trigger ledger identity is required")
    if trigger_ledger.selection_path == RECENT_YEAR_SELECTION_PATH:
        if not current_sector_id:
            raise ValueError("current-sector replay requires one captured sector")
    elif current_sector_id is not None:
        raise ValueError("current-sector override requires the recent-year ledger")
    membership_rows = tuple(
        sorted(memberships, key=lambda row: (row.known_at, row.sector_id))
    )
    factor_rows = tuple(sorted(qmt_factors, key=lambda row: row.effective_on))
    factors = qmt_factor_frame(factor_rows)
    timezone = trigger_ledger.events[0].observed_at.tzinfo
    if timezone is None:
        raise ValueError("sector trigger ledger timezone is unavailable")
    end_at = datetime.combine(requested_end, time(15, 0), tzinfo=timezone)
    effective_at = datetime.combine(effective_start, time(9, 30), tzinfo=timezone)
    frame = load_qmt_frame(
        code,
        "1m",
        start_at=datetime.combine(warmup_start, time(9, 30), tzinfo=timezone),
        end_at=end_at,
        factors=factors,
    )
    source_revision = _source_revision(
        frame=frame,
        code=code,
        trigger_ledger_sha256=trigger_ledger_sha256,
        memberships=membership_rows,
        factors=factor_rows,
        current_sector_id=current_sector_id,
    )
    raw_windows = (
        sector_trigger_windows_for_current_member(
            ledger=trigger_ledger,
            security=security_master,
            sector_id=current_sector_id,
        )
        if current_sector_id is not None
        else sector_trigger_windows_for_memberships(
            ledger=trigger_ledger,
            security=security_master,
            memberships=membership_rows,
        )
    )
    windows = _intersect_windows(
        raw_windows,
        start_at=effective_at,
        end_at=end_at,
    )
    if frame.empty or not windows:
        return SectorFirstDirectSymbolFacts(
            schema=DIRECT_SYMBOL_FACT_SCHEMA,
            algorithm_revision=algorithm_revision,
            source_revision=source_revision,
            trigger_ledger_sha256=trigger_ledger_sha256,
            code=code,
            requested_start=requested_start,
            requested_end=requested_end,
            effective_start=effective_start,
            source_start=None,
            source_end=None,
            one_minute_row_count=len(frame),
            sector_trigger_windows=windows,
            direct_decisions=(),
            strategic_sell_points=(),
            structural_points=(),
            completed_units=(),
            completed_trends=(),
            point_anchor_unit_ids=(),
            point_counts=(),
            security_master=security_master,
            memberships=membership_rows,
            factors=factor_rows,
        )
    entry_ledger = final_confirmed_structure_events(
        code,
        "1m",
        frame,
        visibility_windows=windows,
    )
    decisions = entry_ledger.direct_recursive_decisions
    passing = tuple(row for row in decisions if row.status == "PASS")
    exit_points: tuple[StructuralPoint, ...] = ()
    all_points = entry_ledger.points
    all_units = entry_ledger.completed_units
    all_trends = entry_ledger.completed_trends
    all_anchors = entry_ledger.point_anchor_unit_ids
    if passing:
        exit_ledger = final_confirmed_structure_events(
            code,
            "1m",
            frame,
            visibility_windows=((passing[0].first_seen_at, end_at),),
        )
        exit_points = tuple(
            point
            for point in exit_ledger.points
            if point.recursive_level == 2 and point.side == "sell"
        )
        all_points = tuple(
            sorted(
                {point.point_id: point for point in (*all_points, *exit_ledger.points)}.values(),
                key=lambda point: (point.available_at, point.point_id),
            )
        )
        unit_values = {
            (unit.unit_id, unit.structural_level): unit
            for unit in (*entry_ledger.completed_units, *exit_ledger.completed_units)
        }
        all_units = tuple(
            sorted(
                unit_values.values(),
                key=lambda unit: (unit.available_at, unit.unit_id),
            )
        )
        trend_values = {
            trend.trend_id: trend
            for trend in (*entry_ledger.completed_trends, *exit_ledger.completed_trends)
        }
        all_trends = tuple(
            sorted(
                trend_values.values(),
                key=lambda trend: (trend.available_at, trend.trend_id),
            )
        )
        anchor_values = dict(entry_ledger.point_anchor_unit_ids)
        for point_id, unit_id in exit_ledger.point_anchor_unit_ids:
            previous = anchor_values.setdefault(point_id, unit_id)
            if previous != unit_id:
                raise ValueError("point anchor changed between causal passes")
        all_anchors = tuple(sorted(anchor_values.items()))
    counts = Counter(
        f"level{point.recursive_level}:{point.point_type}" for point in all_points
    )
    return SectorFirstDirectSymbolFacts(
        schema=DIRECT_SYMBOL_FACT_SCHEMA,
        algorithm_revision=algorithm_revision,
        source_revision=source_revision,
        trigger_ledger_sha256=trigger_ledger_sha256,
        code=code,
        requested_start=requested_start,
        requested_end=requested_end,
        effective_start=effective_start,
        source_start=frame["date"].iloc[0].to_pydatetime(),
        source_end=frame["date"].iloc[-1].to_pydatetime(),
        one_minute_row_count=len(frame),
        sector_trigger_windows=windows,
        direct_decisions=decisions,
        strategic_sell_points=exit_points,
        structural_points=all_points,
        completed_units=all_units,
        completed_trends=all_trends,
        point_anchor_unit_ids=all_anchors,
        point_counts=tuple(sorted(counts.items())),
        security_master=security_master,
        memberships=membership_rows,
        factors=factor_rows,
    )
__all__ = (
    "DIRECT_SYMBOL_FACT_SCHEMA",
    "SectorFirstDirectSymbolFacts",
    "build_sector_first_direct_symbol_facts",
)
