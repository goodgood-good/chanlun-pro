"""Resolve causally complete 5-minute operational point dependency graphs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from chanlun.core.strict_structure.current_events import (
    TerminalSegmentReference,
    terminal_segment_reference,
)
from chanlun.core.strict_structure.models import (
    StrictPointEvidence,
    StrictPointStatus,
    StrictStructureResult,
)
from chanlun.decision_support.trading_system.operation_level import (
    is_five_minute_trade_level,
    is_one_minute_segment_level,
)


OperationalConfirmationBasis = Literal[
    "latest_completed_geometry",
    "dependency_chain_geometry",
]


@dataclass(frozen=True, slots=True)
class OperationalPointProjection:
    """One point promoted by a causally complete live geometry graph.

    ``latest_completed_geometry`` is the current trade event.  A projected
    second-class point may depend on an older first-class point whose audit
    lock is still pending; that ancestor is emitted as
    ``dependency_chain_geometry`` so chart state cannot show an approaching
    parent beside a confirmed child.
    """

    point_id: str
    confirmed_at: datetime
    confirmation_basis: OperationalConfirmationBasis
    terminal_segment: TerminalSegmentReference | None
    projected_parent_point_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.point_id:
            raise ValueError("operational point id is required")
        if self.confirmation_basis == "latest_completed_geometry":
            if (
                self.terminal_segment is None
                or self.terminal_segment.role != "latest_completed"
                or self.terminal_segment.state not in {"formed", "locked"}
            ):
                raise ValueError(
                    "latest completed projection requires terminal lineage"
                )
        elif self.confirmation_basis == "dependency_chain_geometry":
            if self.terminal_segment is not None:
                raise ValueError(
                    "dependency projection must not claim current terminal lineage"
                )
        else:
            raise ValueError("unsupported operational confirmation basis")
        if len(self.projected_parent_point_ids) != len(
            set(self.projected_parent_point_ids)
        ):
            raise ValueError("projected parent point ids must be unique")


def resolve_current_operational_point_graph(
    structure: StrictStructureResult,
    *,
    confirmed_points: tuple[StrictPointEvidence, ...],
    approaching_points: tuple[StrictPointEvidence, ...],
    source_frequency: str,
    include_one_minute_segment_level: bool = False,
) -> dict[str, OperationalPointProjection]:
    """Return current operational confirmations plus unresolved parent closure.

    The strict core deliberately keeps audit-unlocked points in
    ``approaching_points``.  Trading may promote the latest completed segment
    at its first causal geometry witness, but a child can only be promoted when
    every parent is already audit-confirmed or is itself backed by a completed
    projected segment.  The latter parents are returned in the same graph so
    all consumers expose a coherent lifecycle.
    """

    if not isinstance(structure, StrictStructureResult):
        raise TypeError("operational point graph requires strict structure")
    confirmed = tuple(confirmed_points)
    approaching = tuple(approaching_points)
    values = (*confirmed, *approaching)
    point_by_id = {point.point_id: point for point in values}
    if len(point_by_id) != len(values):
        raise ValueError("operational point graph requires unique point ids")
    if any(point.status is not StrictPointStatus.CONFIRMED for point in confirmed):
        raise ValueError("operational confirmed ledger contains provisional point")
    if any(point.status is not StrictPointStatus.APPROACHING for point in approaching):
        raise ValueError("operational approaching ledger contains confirmed point")

    units = {
        (level.structural_level, unit.unit_id): unit
        for level in structure.levels
        for unit in level.units
    }

    def completed_dependency_unit(point: StrictPointEvidence):
        unit = units.get((point.structural_level, point.anchor_unit_id))
        if unit is None or unit.forming:
            return None
        if not unit.locked and unit.formed_at is None:
            return None
        return unit

    def parent_closure(
        point: StrictPointEvidence,
        *,
        causal_ceiling: datetime,
        path: frozenset[str],
    ) -> tuple[tuple[OperationalPointProjection, ...], datetime] | None:
        parent_id = point.parent_point_id
        if parent_id is None:
            return (), point.anchor_at
        if parent_id in path:
            return None
        parent = point_by_id.get(parent_id)
        expected_parent_type = "1buy" if point.side == "buy" else "1sell"
        if (
            parent is None
            or point.point_type not in {"2buy", "2sell"}
            or parent.point_type != expected_parent_type
            or parent.side != point.side
            or parent.price_basis_revision != point.price_basis_revision
            or parent.available_at > causal_ceiling
        ):
            return None
        nested = parent_closure(
            parent,
            causal_ceiling=parent.available_at,
            path=path | {parent_id},
        )
        if nested is None:
            return None
        nested_projections, nested_floor = nested
        parent_floor = max(parent.available_at, nested_floor)
        if parent.status is StrictPointStatus.CONFIRMED:
            return nested_projections, parent_floor
        if completed_dependency_unit(parent) is None:
            return None
        dependency_ids = tuple(projection.point_id for projection in nested_projections)
        parent_projection = OperationalPointProjection(
            point_id=parent.point_id,
            confirmed_at=parent_floor,
            confirmation_basis="dependency_chain_geometry",
            terminal_segment=None,
            projected_parent_point_ids=dependency_ids,
        )
        return (*nested_projections, parent_projection), parent_floor

    projections: dict[str, OperationalPointProjection] = {}

    def merge(projection: OperationalPointProjection) -> None:
        previous = projections.get(projection.point_id)
        if previous is None:
            projections[projection.point_id] = projection
            return
        if previous == projection:
            return
        # A point directly anchored to the latest completed segment is the
        # current event and therefore supersedes its dependency-only role.
        if projection.confirmation_basis == "latest_completed_geometry":
            projections[projection.point_id] = projection
            return
        if previous.confirmation_basis == "latest_completed_geometry":
            return
        raise ValueError("operational point maps to conflicting projections")

    for point in approaching:
        eligible_level = is_five_minute_trade_level(
            source_frequency, point.structural_level
        ) or (
            include_one_minute_segment_level
            and is_one_minute_segment_level(
                source_frequency,
                point.structural_level,
            )
        )
        if not eligible_level:
            continue
        reference = terminal_segment_reference(
            structure,
            structural_level=point.structural_level,
            unit_id=point.anchor_unit_id,
        )
        if reference is None or reference.role != "latest_completed":
            continue
        unit = units.get((point.structural_level, point.anchor_unit_id))
        if unit is None or unit.formed_at is None:
            continue
        direct_confirmed_at = (
            point.available_at if reference.state == "formed" else unit.formed_at
        )
        resolved = parent_closure(
            point,
            causal_ceiling=point.available_at,
            path=frozenset({point.point_id}),
        )
        if resolved is None:
            # Fail closed: no child may become an operationally confirmed
            # point while its parent evidence is unresolved or still forming.
            continue
        dependencies, dependency_floor = resolved
        direct_confirmed_at = max(direct_confirmed_at, dependency_floor)
        for dependency in dependencies:
            merge(dependency)
        merge(
            OperationalPointProjection(
                point_id=point.point_id,
                confirmed_at=direct_confirmed_at,
                confirmation_basis="latest_completed_geometry",
                terminal_segment=reference,
                projected_parent_point_ids=tuple(
                    dependency.point_id for dependency in dependencies
                ),
            )
        )
    return projections


__all__ = (
    "OperationalConfirmationBasis",
    "OperationalPointProjection",
    "resolve_current_operational_point_graph",
)
