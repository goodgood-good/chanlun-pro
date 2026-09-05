"""Resolve live events from the actual terminal strict-structure lineage."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from chanlun.core.strict_structure.models import (
    ConstituentUnit,
    DivergenceEvidence,
    SourceKind,
    StrictEvidenceResult,
    StrictPointEvidence,
    StrictStructureResult,
)


TerminalSegmentRole = Literal["latest_unfinished", "latest_completed"]
TerminalSegmentState = Literal["forming", "formed", "locked"]


@dataclass(frozen=True, slots=True)
class TerminalSegmentReference:
    """Stable description of one of the two live-tail structure units.

    At physical level zero the unit is a Chan segment.  At a recursive level
    it is the corresponding trend-type unit.  The role deliberately describes
    geometric completion separately from the causal/non-repainting lock.
    """

    role: TerminalSegmentRole
    structural_level: int
    unit_id: str
    source_kind: SourceKind
    direction: Literal["up", "down"]
    state: TerminalSegmentState
    market_start: datetime
    market_end: datetime
    available_at: datetime

    def __post_init__(self) -> None:
        if self.role not in {"latest_unfinished", "latest_completed"}:
            raise ValueError("invalid terminal segment role")
        if type(self.structural_level) is not int or self.structural_level < 0:
            raise ValueError("terminal segment level must be non-negative")
        if not isinstance(self.unit_id, str) or not self.unit_id:
            raise ValueError("terminal segment id is required")
        object.__setattr__(self, "source_kind", SourceKind(self.source_kind))
        if self.direction not in {"up", "down"}:
            raise ValueError("terminal segment direction is invalid")
        if self.state not in {"forming", "formed", "locked"}:
            raise ValueError("terminal segment state is invalid")
        if self.role == "latest_unfinished" and self.state != "forming":
            raise ValueError("latest unfinished segment must still be forming")
        if self.role == "latest_completed" and self.state == "forming":
            raise ValueError("latest completed segment cannot still be forming")
        if self.market_end < self.market_start:
            raise ValueError("terminal segment end cannot precede its start")
        if self.available_at < self.market_end:
            raise ValueError("terminal segment availability cannot precede its end")


@dataclass(frozen=True, slots=True)
class TerminalSegmentWindow:
    """The only two structure units allowed to feed a current trade view."""

    structural_level: int
    latest_unfinished: TerminalSegmentReference | None
    latest_completed: TerminalSegmentReference | None

    def __post_init__(self) -> None:
        if type(self.structural_level) is not int or self.structural_level < 0:
            raise ValueError("terminal segment window level must be non-negative")
        for reference, role in (
            (self.latest_unfinished, "latest_unfinished"),
            (self.latest_completed, "latest_completed"),
        ):
            if reference is not None and (
                reference.structural_level != self.structural_level
                or reference.role != role
            ):
                raise ValueError("terminal segment window reference mismatch")

    @property
    def references(self) -> tuple[TerminalSegmentReference, ...]:
        return tuple(
            reference
            for reference in (self.latest_unfinished, self.latest_completed)
            if reference is not None
        )


@dataclass(frozen=True, slots=True)
class CurrentStrictEvents:
    """Formal events whose anchors still belong to the live terminal window."""

    points: tuple[StrictPointEvidence, ...]
    divergences: tuple[DivergenceEvidence, ...]


def _reference(
    unit: ConstituentUnit,
    role: TerminalSegmentRole,
) -> TerminalSegmentReference:
    state: TerminalSegmentState = (
        "forming" if unit.forming else "locked" if unit.locked else "formed"
    )
    return TerminalSegmentReference(
        role=role,
        structural_level=unit.structural_level,
        unit_id=unit.unit_id,
        source_kind=unit.source_kind,
        direction=unit.direction,
        state=state,
        market_start=unit.market_start,
        market_end=unit.market_end,
        available_at=unit.available_at,
    )


def terminal_segment_windows(
    structure: StrictStructureResult,
) -> tuple[TerminalSegmentWindow, ...]:
    """Return the latest forming and latest geometrically completed unit.

    ``locked=False`` is not synonymous with unfinished.  Confirmation can
    cascade several segments late, so older geometrically completed segments
    may remain unlocked.  ``forming`` identifies the sole unfinished tail;
    the last non-forming unit is the latest completed segment regardless of
    whether its non-repainting lock has arrived.
    """

    windows: list[TerminalSegmentWindow] = []
    for level in structure.levels:
        forming = tuple(unit for unit in level.units if unit.forming)
        if len(forming) > 1 or (forming and forming[0] is not level.units[-1]):
            raise ValueError("only the terminal strict unit may still be forming")
        completed = next(
            (unit for unit in reversed(level.units) if not unit.forming),
            None,
        )
        windows.append(
            TerminalSegmentWindow(
                structural_level=level.structural_level,
                latest_unfinished=(
                    None
                    if not forming
                    else _reference(forming[0], "latest_unfinished")
                ),
                latest_completed=(
                    None
                    if completed is None
                    else _reference(completed, "latest_completed")
                ),
            )
        )
    return tuple(windows)


def terminal_segment_reference(
    structure: StrictStructureResult,
    *,
    structural_level: int,
    unit_id: str,
) -> TerminalSegmentReference | None:
    """Resolve an exact unit id against the two-unit live-tail window."""

    for window in terminal_segment_windows(structure):
        if window.structural_level != structural_level:
            continue
        return next(
            (
                reference
                for reference in window.references
                if reference.unit_id == unit_id
            ),
            None,
        )
    return None


def current_strict_point_evidence(
    structure: StrictStructureResult,
    points,
) -> tuple[StrictPointEvidence, ...]:
    """Keep only points anchored to the two actual terminal structure units."""

    terminal_ids = {
        (reference.structural_level, reference.unit_id)
        for window in terminal_segment_windows(structure)
        for reference in window.references
    }
    return tuple(
        point
        for point in points
        if (point.structural_level, point.anchor_unit_id) in terminal_ids
    )


def current_strict_events(
    evidence: StrictEvidenceResult,
) -> CurrentStrictEvents:
    """Return current points/divergences without consulting historical age.

    The complete evidence ledger remains untouched for charting and audit.
    Selection, monitoring and notifications consume only events linked to the
    latest unfinished segment or the latest completed segment at that level.
    """

    points = current_strict_point_evidence(
        evidence.structure,
        evidence.confirmed_points,
    )
    referenced_divergence_ids = {
        point.divergence.divergence_id
        for point in points
        if point.divergence is not None
    }
    terminal_ids = {
        (reference.structural_level, reference.unit_id)
        for window in terminal_segment_windows(evidence.structure)
        for reference in window.references
    }
    divergences = tuple(
        divergence
        for divergence in evidence.divergences
        if divergence.divergence_id in referenced_divergence_ids
        or (
            divergence.structural_level,
            divergence.signal_unit_id,
        )
        in terminal_ids
    )
    return CurrentStrictEvents(points=points, divergences=divergences)


__all__ = (
    "CurrentStrictEvents",
    "TerminalSegmentReference",
    "TerminalSegmentRole",
    "TerminalSegmentState",
    "TerminalSegmentWindow",
    "current_strict_events",
    "current_strict_point_evidence",
    "terminal_segment_reference",
    "terminal_segment_windows",
)
