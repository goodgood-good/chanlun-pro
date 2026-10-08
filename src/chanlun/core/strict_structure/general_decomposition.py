"""Source-bounded associative views of one locked segment path.

Lesson 54 permits distinct, internally contiguous groupings of the same
physical path.  This module materializes a declared grouping and its price
geometry without promoting a three-segment group to a formal completed trend
type or issuing a third-class point without that type's completion evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal

from chanlun.core.strict_structure.identity import stable_structure_id
from chanlun.core.strict_structure.center_relation import classify_center_relation
from chanlun.core.strict_structure.models import (
    CenterLevelResult,
    CenterState,
    ConstituentUnit,
    SourceKind,
    StrictLevelResult,
    StrictPointEvidence,
    StrictStructureResult,
    TrendCenter,
    TrendState,
    TrendKind,
    TrendType,
    combined_extreme_market_time,
)


@dataclass(frozen=True, slots=True)
class AssociativeComponent:
    component_id: str
    structural_level: int
    member_unit_ids: tuple[str, str, str]
    core_zd_tick: int
    core_zg_tick: int
    price_low_tick: int
    price_high_tick: int
    market_start: datetime
    market_end: datetime
    available_at: datetime


@dataclass(frozen=True, slots=True)
class AssociativeParent:
    center_id: str
    structural_level: int
    component_ids: tuple[str, str, str]
    zd_tick: int
    zg_tick: int
    observed_at: datetime
    formal_status: str = "requires_completed_child_type_certificates"


@dataclass(frozen=True, slots=True)
class AssociativeFollowup:
    departure_member_ids: tuple[str, ...]
    departure_status: str
    return_unit_id: str | None
    return_geometry: str
    formal_third_point: bool = False


@dataclass(frozen=True, slots=True)
class AssociativeView:
    view_id: str
    source_structural_level: int
    price_basis_revision: str
    source_unit_ids: tuple[str, ...]
    prefix_unit_ids: tuple[str, ...]
    components: tuple[AssociativeComponent, ...]
    suffix_unit_ids: tuple[str, ...]
    parent_centers: tuple[AssociativeParent, ...]
    latest_parent_followup: AssociativeFollowup | None
    as_of: datetime


@dataclass(frozen=True, slots=True)
class ThreeSegmentCoreObservation:
    observation_id: str
    structural_level: int
    price_basis_revision: str
    member_unit_ids: tuple[str, str, str]
    zd_tick: int
    zg_tick: int
    market_formed_at: datetime
    available_at: datetime


@dataclass(frozen=True, slots=True)
class SegmentizedCenterPath:
    view_id: str
    source_start_unit_id: str
    source_unit_ids: tuple[str, ...]
    derived_unit_ids: tuple[str, ...]
    center_result: CenterLevelResult
    as_of: datetime


@dataclass(frozen=True, slots=True)
class DeclaredGeneralSignalView:
    view_id: str
    source_unit_ids: tuple[str, ...]
    structure: StrictStructureResult
    third_class_points: tuple[StrictPointEvidence, ...]
    trend_partition_status: str
    trend_partition_reason: str | None
    trend_identity_scope: str
    pending_movement_source_unit_ids: tuple[tuple[str, ...], ...]
    unqualified_prefix_unit_ids: tuple[str, ...]
    unqualified_tail_unit_ids: tuple[str, ...]
    as_of: datetime


@dataclass(frozen=True, slots=True)
class DeclaredChildTypeCertificate:
    view_id: str
    unit: ConstituentUnit
    source_unit_ids: tuple[str, ...]
    evidence_ref: str
    qualification_kind: str = "author_bounded_case"
    strict_trend: TrendType | None = None
    confirmation_witness_at: datetime | None = None
    availability_witness_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class DeclaredUpgradeChildPath:
    view_id: str
    source_trend_id: str
    review: dict[str, object]
    certificates: tuple[DeclaredChildTypeCertificate, ...]
    combined_units: tuple[ConstituentUnit, ...]
    protected_source_unit: ConstituentUnit | None
    geometry_equivalent: bool
    clock_not_earlier_than_protected: bool
    as_of: datetime


@dataclass(frozen=True, slots=True)
class DeclaredPartitionCoverage:
    view_id: str
    source_structural_level: int
    source_unit_ids: tuple[str, ...]
    child_type_ids: tuple[str, ...]
    qualified_source_unit_ids: tuple[str, ...]
    unqualified_prefix_unit_ids: tuple[str, ...]
    unqualified_tail_unit_ids: tuple[str, ...]
    coverage_status: str
    strict_completed_only: bool
    as_of: datetime


@dataclass(frozen=True, slots=True)
class DeclaredParentView:
    view_id: str
    source_unit_ids: tuple[str, ...]
    child_certificates: tuple[DeclaredChildTypeCertificate, ...]
    partition_coverage: DeclaredPartitionCoverage
    parent_path_audit: "DeclaredParentPathAudit"
    parent_center: TrendCenter
    third_class_points: tuple[StrictPointEvidence, ...]
    unqualified_prefix_unit_ids: tuple[str, ...]
    unqualified_tail_unit_ids: tuple[str, ...]
    unqualified_tail_geometry: str
    protected_center_relation: "ProtectedCenterRelation"
    as_of: datetime


@dataclass(frozen=True, slots=True)
class ProtectedCenterRelation:
    status: str
    checked_center_ids: tuple[str, ...]
    matching_center_id: str | None
    overlapping_center_ids: tuple[str, ...]
    overlapping_source_unit_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DeclaredParentPathAudit:
    view_id: str
    partition_coverage: DeclaredPartitionCoverage
    parent_center_ids: tuple[str, ...]
    protected_relations: tuple[ProtectedCenterRelation, ...]
    connector_child_type_ids: tuple[tuple[str, ...], ...]
    parent_path_status: str
    as_of: datetime


@dataclass(frozen=True, slots=True)
class GapBarEvidence:
    """One closed bar from a declared, price-basis-pinned source window."""

    bar_id: str
    closed_at: datetime
    low_tick: int
    high_tick: int
    price_basis_revision: str


@dataclass(frozen=True, slots=True)
class GeneralCenterConnectorObservation:
    previous_center_id: str
    current_center_id: str
    structural_level: int
    price_basis_revision: str
    connector_kind: str
    connector_source_ids: tuple[str, ...]
    market_start: datetime
    market_end: datetime
    start_tick: int
    end_tick: int
    known_at: datetime
    market_source_ref: str
    source_adjacency_scope: str


def observe_explicit_general_center_connector(
    previous: TrendCenter,
    current: TrendCenter,
    *,
    as_of: datetime,
    market_source_ref: str,
    lower_segments: tuple[ConstituentUnit, ...] = (),
    source_bars: tuple[GapBarEvidence, ...] = (),
    gap_after_bar_index: int | None = None,
) -> GeneralCenterConnectorObservation:
    """Record an explicit lower segment or direct gap without creating a type.

    L099:37-56 permits these connectors in a general decomposition. This
    bounded observation checks the submitted source window and center boundary;
    it never supplies a missing completed child type or formal third point.
    """

    if (
        not isinstance(market_source_ref, str) or not market_source_ref.strip()
        or as_of.tzinfo is None or as_of.utcoffset() is None
        or previous.center_id == current.center_id
        or previous.structural_level != current.structural_level
        or previous.source_kind is not current.source_kind
        or previous.price_basis_revision != current.price_basis_revision
        or previous.available_at > as_of or current.available_at > as_of
    ):
        raise ValueError("general connector requires distinct, available same-grade centers and a source")
    left = (previous.completion_return_unit
            or (previous.supersession_bridge_units[-1]
                if previous.supersession_bridge_units else previous.body_units[-1]))
    right = current.entry_unit or current.body_units[0]
    if left.market_end >= right.market_start:
        raise ValueError("general connector requires a positive interval between centers")
    segments = tuple(lower_segments)
    bars = tuple(source_bars)
    if bool(segments) == bool(bars):
        raise ValueError("provide exactly one lower-segment or gap source")

    if segments:
        if gap_after_bar_index is not None:
            raise ValueError("a segment connector cannot borrow a gap index")
        occupied = {unit.unit_id for center in (previous, current)
                    for unit in (*center.body_units,
                                 *center.failed_departure_units,
                                 *center.supersession_bridge_units,
                                 *((center.entry_unit,) if center.entry_unit else ()),
                                 *((center.completion_leave_unit,) if center.completion_leave_unit else ()),
                                 *((center.completion_return_unit,) if center.completion_return_unit else ()))}
        if (any(not isinstance(unit, ConstituentUnit) for unit in segments)
                or len({unit.unit_id for unit in segments}) != len(segments)
                or any(unit.unit_id in occupied
                       or unit.source_kind is not SourceKind.SEGMENT
                       or unit.structural_level > previous.structural_level
                       or unit.price_basis_revision != previous.price_basis_revision
                       or not unit.locked or unit.available_at > as_of
                       for unit in segments)):
            raise ValueError("lower connector segments lack independent locked source proof")
        chain = (left, *segments, right)
        if any(a.end_tick != b.start_tick or a.market_end != b.market_start
               for a, b in zip(chain, chain[1:])):
            raise ValueError("lower connector segments must cover the entire center boundary")
        kind = "lower_segment_path"
        source_ids = tuple(unit.unit_id for unit in segments)
        adjacency_scope = "submitted_locked_segment_chain"
        observed_at = max(unit.available_at for unit in segments)
    else:
        if (type(gap_after_bar_index) is not int
                or not 0 <= gap_after_bar_index < len(bars) - 1
                or any(not isinstance(bar, GapBarEvidence) for bar in bars)
                or len({bar.bar_id for bar in bars}) != len(bars)
                or any(not bar.bar_id or bar.price_basis_revision != previous.price_basis_revision
                       or bar.closed_at.tzinfo is None or bar.closed_at.utcoffset() is None
                       or type(bar.low_tick) is not int or type(bar.high_tick) is not int
                       or bar.low_tick > bar.high_tick or bar.closed_at > as_of
                       for bar in bars)
                or any(a.closed_at >= b.closed_at for a, b in zip(bars, bars[1:]))):
            raise ValueError("gap requires an ordered, basis-pinned closed-bar window")
        before, after = bars[gap_after_bar_index:gap_after_bar_index + 2]
        if (before.closed_at != left.market_end
                or after.closed_at != right.market_start
                or not before.low_tick <= left.end_tick <= before.high_tick
                or not after.low_tick <= right.start_tick <= after.high_tick
                or not (before.high_tick < after.low_tick
                        or after.high_tick < before.low_tick)):
            raise ValueError("bars do not prove a direct non-overlapping gap between centers")
        kind = "direct_price_gap"
        source_ids = (before.bar_id, after.bar_id)
        adjacency_scope = "adjacent_in_submitted_closed_bar_window"
        observed_at = after.closed_at

    return GeneralCenterConnectorObservation(
        previous_center_id=previous.center_id,
        current_center_id=current.center_id,
        structural_level=previous.structural_level,
        price_basis_revision=previous.price_basis_revision,
        connector_kind=kind, connector_source_ids=source_ids,
        market_start=left.market_end, market_end=right.market_start,
        start_tick=left.end_tick, end_tick=right.start_tick,
        known_at=max(previous.available_at, current.available_at, observed_at),
        market_source_ref=market_source_ref,
        source_adjacency_scope=adjacency_scope,
    )


def general_center_connector_observation_payload(
    observation: GeneralCenterConnectorObservation,
) -> dict:
    return {
        "schema": "chanlun-general-center-connector-observation-v1",
        "previous_center_id": observation.previous_center_id,
        "current_center_id": observation.current_center_id,
        "structural_level": observation.structural_level,
        "price_basis_revision": observation.price_basis_revision,
        "connector_kind": observation.connector_kind,
        "connector_source_ids": list(observation.connector_source_ids),
        "market_start": int(observation.market_start.timestamp()),
        "market_end": int(observation.market_end.timestamp()),
        "start_tick": observation.start_tick,
        "end_tick": observation.end_tick,
        "known_at": int(observation.known_at.timestamp()),
        "market_source_ref": observation.market_source_ref,
        "source_adjacency_scope": observation.source_adjacency_scope,
        "rule_source_ref": "L099:37-56",
        "formal_parent_type_certified": False,
        "higher_center_certified": False,
        "third_point_certified": False,
        "tradable": False,
    }


def _overlap(units: tuple[ConstituentUnit, ...]) -> tuple[int, int] | None:
    zd = max(unit.low_tick for unit in units)
    zg = min(unit.high_tick for unit in units)
    return (zd, zg) if zd <= zg else None


def _validate_source(units: tuple[ConstituentUnit, ...], as_of: datetime) -> None:
    if not units:
        raise ValueError("associative view requires source units")
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must have an explicit timezone")
    first = units[0]
    if first.source_kind is not SourceKind.SEGMENT:
        raise ValueError("associative view requires completed segments")
    if len({unit.unit_id for unit in units}) != len(units):
        raise ValueError("source unit ids must be unique")
    for unit in units:
        if (
            unit.source_kind is not SourceKind.SEGMENT
            or unit.structural_level != first.structural_level
            or unit.price_basis_revision != first.price_basis_revision
            or not unit.locked
            or any(
                moment.tzinfo is None or moment.utcoffset() is None
                for moment in (
                    unit.market_start, unit.market_end,
                    unit.confirmed_at, unit.available_at,
                )
            )
            or unit.available_at > as_of
        ):
            raise ValueError("source units must be one locked causal segment chain")
    for previous, current in zip(units, units[1:]):
        if (
            previous.end_tick != current.start_tick
            or previous.direction == current.direction
            or current.market_start != previous.market_end
        ):
            raise ValueError("source segments must alternate and connect")


def _validate_parent_source(
    units: tuple[ConstituentUnit, ...], as_of: datetime,
) -> None:
    """Validate one locked child-source grade for a declared recursive parent."""
    if not units:
        raise ValueError("declared parent requires source units")
    if units[0].source_kind is SourceKind.SEGMENT:
        _validate_source(units, as_of)
        return
    if (
        units[0].source_kind is not SourceKind.TREND_TYPE
        or units[0].structural_level < 1
    ):
        raise ValueError("declared parent requires completed lower trend units")
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must have an explicit timezone")
    if any(
        unit.confirmed_at is None
        or any(
            moment.tzinfo is None or moment.utcoffset() is None
            for moment in (
                unit.market_start, unit.market_end,
                unit.confirmed_at, unit.available_at,
            )
        )
        for unit in units
    ):
        raise ValueError("higher parent source requires timezone-aware completed units")
    from chanlun.core.strict_structure.center_machine import validate_unit_sequence

    validate_unit_sequence(
        units, units[0].structural_level, SourceKind.TREND_TYPE
    )
    if any(
        not unit.locked
        or unit.available_at > as_of
        for unit in units
    ):
        raise ValueError("higher parent source must be one locked causal trend-unit chain")


def observe_three_segment_cores(
    units: tuple[ConstituentUnit, ...], *, as_of: datetime,
) -> tuple[ThreeSegmentCoreObservation, ...]:
    """Record every locked three-segment overlap, without choosing a view.

    This exposes the minimal center geometry used at d2 in lesson 54.  A
    historical triple can belong to an alternative association; its overlap
    does not by itself supply a completed child TrendType or a tradable point.
    """

    values = tuple(units)
    if not values:
        return ()
    _validate_source(values, as_of)
    output = []
    for start in range(len(values) - 2):
        members = values[start : start + 3]
        core = _overlap(members)
        if core is None:
            continue
        ids = tuple(unit.unit_id for unit in members)
        output.append(ThreeSegmentCoreObservation(
            observation_id=stable_structure_id(
                "chanlun-three-locked-segment-core-v1",
                values[0].price_basis_revision, values[0].structural_level,
                ids, core,
            ),
            structural_level=values[0].structural_level,
            price_basis_revision=values[0].price_basis_revision,
            member_unit_ids=ids,
            zd_tick=core[0], zg_tick=core[1],
            market_formed_at=members[-1].market_end,
            available_at=max(unit.available_at for unit in members),
        ))
    return tuple(output)


def three_segment_core_payload(observation: ThreeSegmentCoreObservation) -> dict:
    return {
        "schema": "chanlun-three-segment-core-observation-v1",
        "observation_id": observation.observation_id,
        "structural_level": observation.structural_level,
        "price_basis_revision": observation.price_basis_revision,
        "member_unit_ids": list(observation.member_unit_ids),
        "core": {"zd_tick": observation.zd_tick, "zg_tick": observation.zg_tick},
        "market_formed_at": int(observation.market_formed_at.timestamp()),
        "available_at": int(observation.available_at.timestamp()),
        "formal_center_id": None,
        "tradable": False,
        "evidence_scope": "three_complete_segment_overlap_before_view_selection",
    }


def materialize_segmentized_center_path(
    units: tuple[ConstituentUnit, ...],
    *,
    view_id: str,
    view_start_unit_id: str,
    as_of: datetime,
) -> SegmentizedCenterPath:
    """Run the three-member lifecycle under L054's declared base assumption.

    The caller asserts that each locked segment is the completed elementary
    lower type used in this one view.  This must not be fed into the canonical
    same-level engine as if every segment were universally a TrendType.
    """

    from chanlun.core.strict_structure.center_machine import calculate_centers

    values = tuple(units)
    if not view_id or not view_id.strip():
        raise ValueError("view_id is required")
    _validate_source(values, as_of)
    positions = {unit.unit_id: index for index, unit in enumerate(values)}
    if view_start_unit_id not in positions:
        raise ValueError("view start is absent from the source prefix")
    start_index = positions[view_start_unit_id]
    source = values[start_index:]
    derived = tuple(
        replace(
            unit,
            unit_id=stable_structure_id(
                "chanlun-l054-segmentized-child-v1", view_id, unit.unit_id,
                unit.price_basis_revision,
            ),
            source_kind=SourceKind.TREND_TYPE,
            child_ids=(unit.unit_id,),
        )
        for unit in source
    )
    result = calculate_centers(
        derived, source[0].structural_level, SourceKind.TREND_TYPE
    )
    return SegmentizedCenterPath(
        view_id=view_id,
        source_start_unit_id=view_start_unit_id,
        source_unit_ids=tuple(unit.unit_id for unit in source),
        derived_unit_ids=tuple(unit.unit_id for unit in derived),
        center_result=result,
        as_of=as_of,
    )


def segmentized_center_path_payload(path: SegmentizedCenterPath) -> dict:
    from chanlun.core.strict_structure.point_rules import classify_third_class_geometry

    def third_class_geometry(center) -> dict | None:
        leave = center.completion_leave_unit
        ret = center.completion_return_unit
        if leave is None or ret is None:
            return None
        classified = classify_third_class_geometry(
            zd_tick=center.zd_tick, zg_tick=center.zg_tick,
            leave=leave, return_unit=ret,
        )
        if classified is None:
            return None
        _direction, point_type, side, anchor_tick, invalidation_tick, _variant = classified
        return {
            "point_type": point_type,
            "side": side,
            "anchor_source_unit_id": ret.child_ids[0],
            "anchor_tick": anchor_tick,
            "invalidation_tick": invalidation_tick,
            "formal_strict_point": False,
            "evidence_scope": "declared_segmentized_child_type_geometry",
        }

    return {
        "schema": "chanlun-l054-segmentized-center-path-v1",
        "view_id": path.view_id,
        "source_start_unit_id": path.source_start_unit_id,
        "source_unit_ids": list(path.source_unit_ids),
        "derived_unit_ids": list(path.derived_unit_ids),
        "as_of": int(path.as_of.timestamp()),
        "centers": [
            {
                "center_id": center.center_id,
                "structural_level": center.structural_level,
                "source_kind": center.source_kind.value,
                "initial_source_member_ids": [
                    unit.child_ids[0] for unit in center.initial_units
                ],
                "core": {"zd_tick": center.zd_tick, "zg_tick": center.zg_tick},
                "state": center.state.value,
                "established_at": int(center.established_at.timestamp()),
                "completed_at": (
                    None if center.completed_at is None
                    else int(center.completed_at.timestamp())
                ),
                "third_class_geometry": third_class_geometry(center),
            }
            for center in path.center_result.centers
        ],
        "source_policy": "l054_declared_segment_as_completed_child_type",
        "canonical_same_level_center": False,
        "tradable": False,
    }


def build_declared_three_core_signal_view(
    units: tuple[ConstituentUnit, ...],
    *,
    view_id: str,
    view_start_unit_id: str,
    as_of: datetime,
    price_quantum: Decimal,
) -> DeclaredGeneralSignalView:
    """Place the selected three-core path in an isolated formal view ledger.

    The resulting StrictPointEvidence is formal within this declared source
    policy.  It is not inserted into the canonical same-level point ledger.
    """

    from chanlun.core.strict_structure.center_machine import (
        calculate_three_core_segment_centers,
        validate_unit_sequence,
    )
    from chanlun.core.strict_structure.signals import StrictSignalEngine
    from chanlun.core.strict_structure.trend_assembler import assemble_trend_types

    values = tuple(units)
    if not view_id or not view_id.strip():
        raise ValueError("view_id is required")
    if not values:
        raise ValueError("declared segment view requires source units")
    validate_unit_sequence(values, values[0].structural_level, SourceKind.SEGMENT)
    locked_prefix = tuple(unit for unit in values if unit.locked)
    _validate_source(locked_prefix, as_of)
    if any(unit.available_at > as_of for unit in values):
        raise ValueError("declared segment view cannot use future preview units")
    positions = {unit.unit_id: index for index, unit in enumerate(values)}
    if view_start_unit_id not in positions:
        raise ValueError("view start is absent from source prefix")
    start_index = positions[view_start_unit_id]
    source = values[start_index:]
    if source[0].structural_level != 0:
        raise ValueError("declared segment base must occupy structural level zero")
    center_result = calculate_three_core_segment_centers(
        source, source[0].structural_level
    )
    try:
        assembly = assemble_trend_types(
            center_result.centers, source, source[0].structural_level
        )
    except ValueError as exc:
        if str(exc) != "trend must contain every center body and bridge unit":
            raise
        # The center ledger is still valid, but the current fixed-direction
        # trend assembler would detach a three-core seed member at the open
        # history boundary. Preserve centers and hold type certification.
        assembly = None
        trend_partition_status = "pending_source_ownership"
        trend_partition_reason = str(exc)
    else:
        trend_partition_status = (
            "assembled_with_pending_movements"
            if assembly.pending_movements else "assembled"
        )
        trend_partition_reason = None
    # General association may recombine a previously complete segment path
    # when a later center becomes established (L099). A fixed same-level
    # assembler's LOCKED label is therefore not transferable to this view.
    provisional_trends = (
        () if assembly is None else tuple(
            replace(trend, state=TrendState.COMPLETE)
            if trend.state is TrendState.LOCKED else trend
            for trend in assembly.current_trends
        )
    )
    level = StrictLevelResult(
        structural_level=source[0].structural_level,
        units=source, center_result=center_result,
        trend_types=provisional_trends,
        completed_trends=(),
        decomposition_boundaries=(),
        decomposition_mode="general_declared", view_id=view_id,
    )
    structure = StrictStructureResult(
        schema="chanlun-structure",
        price_basis_revision=source[0].price_basis_revision,
        levels=(level,),
    )
    points = StrictSignalEngine(
        structure=structure, price_quantum=price_quantum,
    ).third_class_points()
    occupied = tuple(
        unit
        for center in center_result.centers
        for unit in (
            *center.body_units,
            *center.failed_departure_units,
            *center.supersession_bridge_units,
            *((center.pending_leave_unit,) if center.pending_leave_unit is not None else ()),
            *((center.completion_leave_unit,) if center.completion_leave_unit is not None else ()),
            *((center.completion_return_unit,) if center.completion_return_unit is not None else ()),
        )
    )
    last_owned_index = max(
        (positions[unit.unit_id] for unit in occupied),
        default=start_index - 1,
    )
    return DeclaredGeneralSignalView(
        view_id=view_id,
        source_unit_ids=tuple(unit.unit_id for unit in source),
        structure=structure,
        third_class_points=points,
        trend_partition_status=trend_partition_status,
        trend_partition_reason=trend_partition_reason,
        trend_identity_scope="recomposable_general_view",
        pending_movement_source_unit_ids=(
            () if assembly is None else tuple(
                tuple(unit.unit_id for unit in partition.constituent_units)
                for partition in assembly.pending_movements
            )
        ),
        unqualified_prefix_unit_ids=tuple(
            unit.unit_id for unit in values[:start_index]
        ),
        unqualified_tail_unit_ids=tuple(
            unit.unit_id for unit in values[last_owned_index + 1:]
        ),
        as_of=as_of,
    )


def declared_general_signal_view_payload(view: DeclaredGeneralSignalView) -> dict:
    level = view.structure.levels[0]
    return {
        "schema": "chanlun-declared-general-signal-view-v2",
        "view_id": view.view_id,
        "decomposition_mode": level.decomposition_mode,
        "source_policy": "l054_declared_completed_elementary_segment_base",
        "source_unit_ids": list(view.source_unit_ids),
        "unqualified_prefix_unit_ids": list(view.unqualified_prefix_unit_ids),
        "unqualified_tail_unit_ids": list(view.unqualified_tail_unit_ids),
        "full_path_partition_certified": False,
        "trend_partition_status": view.trend_partition_status,
        "trend_partition_reason": view.trend_partition_reason,
        "trend_identity_scope": view.trend_identity_scope,
        "pending_movement_source_unit_ids": [
            list(ids) for ids in view.pending_movement_source_unit_ids
        ],
        "as_of": int(view.as_of.timestamp()),
        "centers": [
            {
                "center_id": center.center_id,
                "formation_rule": center.formation_rule,
                "source_member_unit_ids": [
                    unit.unit_id for unit in center.initial_units
                ],
                "core": {"zd_tick": center.zd_tick, "zg_tick": center.zg_tick},
                "state": center.state.value,
                "established_at": int(center.established_at.timestamp()),
                "completed_at": (
                    None if center.completed_at is None
                    else int(center.completed_at.timestamp())
                ),
            }
            for center in level.center_result.centers
        ],
        "center_previews": [
            {
                "formal_center_id": preview.formal_center_id,
                "formation_rule": preview.formation_rule,
                "source_member_unit_ids": list(preview.unit_ids),
                "core": (
                    None if preview.zd_tick is None else
                    {"zd_tick": preview.zd_tick, "zg_tick": preview.zg_tick}
                ),
                "state": preview.state.value,
                "available_at": int(preview.available_at.timestamp()),
                "completion_leave_unit_id": preview.completion_leave_unit_id,
                "completion_return_unit_id": preview.completion_return_unit_id,
                "tradable": False,
            }
            for preview in level.center_result.previews
        ],
        "trend_types": [
            {
                "trend_id": trend.trend_id,
                "kind": trend.kind.value if trend.centers else "geometric_movement",
                "formal_type_qualified": bool(trend.centers),
                "direction": trend.trend_direction,
                "trend_direction": trend.trend_direction,
                "movement_orientation": trend.movement_orientation,
                "state": trend.state.value,
                "center_ids": [center.center_id for center in trend.centers],
                "source_member_unit_ids": [
                    unit.unit_id for unit in trend.constituent_units
                ],
                "market_end": int(trend.market_end.timestamp()),
                "confirmed_at": (
                    None if trend.confirmed_at is None
                    else int(trend.confirmed_at.timestamp())
                ),
                "completion_witness_unit_ids": [
                    unit.unit_id for unit in trend.completion_witness_units
                ],
                "available_at": int(trend.available_at.timestamp()),
            }
            for trend in level.trend_types
        ],
        "third_class_points": [
            {
                "point_id": point.point_id,
                "point_type": point.point_type,
                "center_id": point.center_id,
                "anchor_source_unit_id": point.anchor_unit_id,
                "anchor_at": int(point.anchor_at.timestamp()),
                "available_at": int(point.available_at.timestamp()),
                "status": point.status.value,
            }
            for point in view.third_class_points
        ],
        "canonical_same_level_output": False,
        "tradable": False,
    }


def qualify_declared_child_type(
    source_units: tuple[ConstituentUnit, ...],
    *,
    view_id: str,
    evidence_ref: str,
    as_of: datetime,
    confirmation_witness_at: datetime | None = None,
    availability_witness_at: datetime | None = None,
) -> DeclaredChildTypeCertificate:
    """Represent one author-bounded completed child type with exact source IDs.

    The certificate is not a substitute for a production TrendType proof;
    consumers of this qualification must remain outside canonical trading.
    """

    values = tuple(source_units)
    if not view_id or not view_id.strip() or not evidence_ref:
        raise ValueError("declared type requires a view and source evidence")
    _validate_parent_source(values, as_of)
    if (confirmation_witness_at is None) != (availability_witness_at is None):
        raise ValueError("declared child qualification requires both witness clocks")
    source_confirmed_at = max(value.confirmed_at for value in values)
    source_available_at = max(value.available_at for value in values)
    if confirmation_witness_at is not None:
        if (
            any(
                moment.tzinfo is None or moment.utcoffset() is None
                for moment in (confirmation_witness_at, availability_witness_at)
            )
            or confirmation_witness_at < source_confirmed_at
            or availability_witness_at < max(
                source_available_at, confirmation_witness_at
            )
            or availability_witness_at > as_of
        ):
            raise ValueError("declared child witness clocks are not causal")
    first, last = values[0], values[-1]
    direction = (
        "up" if last.end_tick > first.start_tick
        else "down" if last.end_tick < first.start_tick
        else first.direction
    )
    unit_ids = tuple(value.unit_id for value in values)
    confirmation = confirmation_witness_at or source_confirmed_at
    availability = availability_witness_at or source_available_at
    child = ConstituentUnit(
        unit_id=stable_structure_id(
            "chanlun-declared-recursive-child-type-v1"
            if first.source_kind is SourceKind.TREND_TYPE
            else "chanlun-declared-child-type-v2"
            if confirmation_witness_at is not None
            else "chanlun-declared-child-type-v1",
            view_id, first.price_basis_revision, unit_ids, evidence_ref,
            *((first.structural_level, first.source_kind.value)
              if first.source_kind is SourceKind.TREND_TYPE else ()),
            *((
                int(confirmation.timestamp()), int(availability.timestamp())
            ) if confirmation_witness_at is not None else ()),
        ),
        structural_level=first.structural_level + 1,
        source_kind=SourceKind.TREND_TYPE,
        price_basis_revision=first.price_basis_revision,
        direction=direction,
        start_tick=first.start_tick,
        end_tick=last.end_tick,
        low_tick=min(value.low_tick for value in values),
        high_tick=max(value.high_tick for value in values),
        market_start=first.market_start,
        market_end=last.market_end,
        confirmed_at=confirmation,
        available_at=availability,
        locked=True,
        child_ids=unit_ids,
        low_market_time=combined_extreme_market_time(values, "low"),
        high_market_time=combined_extreme_market_time(values, "high"),
    )
    return DeclaredChildTypeCertificate(
        view_id=view_id, unit=child,
        source_unit_ids=unit_ids, evidence_ref=evidence_ref,
        confirmation_witness_at=confirmation_witness_at,
        availability_witness_at=availability_witness_at,
    )


def qualify_locked_trend_type(
    trend: TrendType,
    *,
    view_id: str,
    as_of: datetime,
) -> DeclaredChildTypeCertificate:
    """Bind a genuinely locked lower TrendType to one general-view ledger."""

    from chanlun.core.strict_structure.unit_adapter import trend_type_to_unit

    if not view_id or not view_id.strip():
        raise ValueError("view_id is required")
    if trend.state is not TrendState.LOCKED or trend.available_at > as_of:
        raise ValueError("strict child type must be locked and causally available")
    child = trend_type_to_unit(trend)
    return DeclaredChildTypeCertificate(
        view_id=view_id,
        unit=child,
        source_unit_ids=tuple(unit.unit_id for unit in trend.constituent_units),
        evidence_ref=f"strict_trend:{trend.trend_id}",
        qualification_kind="strict_locked_trend_type",
        strict_trend=trend,
    )


def review_declared_upgrade_split(
    trend: TrendType, *, view_id: str, as_of: datetime,
) -> dict[str, object]:
    """Prove local source ownership for one proposed center-by-center split.

    L054:145-157 requires a lower-type connector between same-grade centers.
    The review uses each completed center's independent leave and first return
    as a proposed boundary. It never selects this view for the canonical path,
    rewrites an existing divergence, or promotes a parent center or point.
    """

    if not view_id or not view_id.strip():
        raise ValueError("upgrade split review requires a declared view_id")
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("upgrade split review requires an aware cutoff")
    if trend.kind is not TrendKind.CONSOLIDATION or len(trend.centers) < 2:
        raise ValueError("upgrade split review requires a multi-center consolidation")
    values = tuple(trend.constituent_units)
    positions = {unit.unit_id: index for index, unit in enumerate(values)}
    if len(positions) != len(values):
        raise ValueError("upgrade split source ids must be unique")
    base = {
        "schema": "chanlun-declared-upgrade-split-review-v2",
        "view_id": view_id,
        "source_trend_id": trend.trend_id,
        "source_structural_level": trend.structural_level,
        "price_basis_revision": trend.price_basis_revision,
        "source_unit_ids": [unit.unit_id for unit in values],
        "center_ids": [center.center_id for center in trend.centers],
        "center_relations": [
            classify_center_relation(previous, current).value
            for previous, current in zip(trend.centers, trend.centers[1:])
        ],
        "as_of": int(as_of.timestamp()),
        "current_endpoint_contract_compatible": None,
        "full_path_partition_certified": False,
        "higher_center_formal": False,
        "formal_point_from_review": False,
        "tradable": False,
    }


    if (
        trend.state is not TrendState.LOCKED
        or trend.available_at > as_of
        or trend.terminal_divergence is not None
        or any(not unit.locked or unit.available_at > as_of for unit in values)
    ):
        return {
            **base, "status": "pending_locked_source_or_divergence_boundary",
            "connector_unit_ids": [], "candidate_child_types": [],
            "source_partition": None,
        }

    starts = [0]
    connectors = []
    for previous, current in zip(trend.centers, trend.centers[1:]):
        leave = previous.completion_leave_unit
        returned = previous.completion_return_unit
        if (
            previous.state is not CenterState.COMPLETED
            or leave is None or returned is None
            or not leave.locked or not returned.locked
            or leave.available_at > as_of or returned.available_at > as_of
            or leave.unit_id not in positions or returned.unit_id not in positions
            or current.body_units[0].unit_id not in positions
            or leave.unit_id in {
                unit.unit_id for unit in (
                    *current.body_units, *current.failed_departure_units,
                    *current.supersession_bridge_units,
                )
            }
            or positions[returned.unit_id] != positions[leave.unit_id] + 1
            or positions[returned.unit_id] <= starts[-1]
            or positions[returned.unit_id] > positions[current.body_units[0].unit_id]
        ):
            return {
                **base, "status": "pending_same_level_connector_or_parent_upgrade",
                "blocking_center_pair_ids": [
                    previous.center_id, current.center_id,
                ],
                "connector_unit_ids": connectors,
                "candidate_child_types": [], "source_partition": None,
            }
        starts.append(positions[returned.unit_id])
        connectors.append(leave.unit_id)

    from chanlun.core.strict_structure.trend_assembler import _build

    candidates = []
    for offset, center in enumerate(trend.centers):
        members = values[starts[offset] : (
            starts[offset + 1] if offset + 1 < len(starts) else len(values)
        )]
        closed_at = center.structural_closed_at
        if not members or closed_at is None:
            return {
                **base, "status": "pending_center_completion",
                "connector_unit_ids": connectors,
                "candidate_child_types": [], "source_partition": None,
            }
        witness = trend if offset == len(trend.centers) - 1 else trend.centers[offset + 1]
        confirmed_at = max(
            closed_at,
            trend.confirmed_at if witness is trend else witness.established_at,
        )
        available_at = max(
            center.available_at, witness.available_at,
            *(unit.available_at for unit in members),
        )
        try:
            candidate = _build(
                (center,), members, trend.structural_level,
                TrendState.LOCKED, confirmed_at, available_at,
            )
        except ValueError as exc:
            return {
                **base, "status": "single_center_source_slice_invalid",
                "failure_reason": str(exc),
                "failed_center_id": center.center_id,
                "connector_unit_ids": connectors,
                "candidate_child_types": [], "source_partition": None,
            }
        candidates.append(candidate)
    owned = tuple(
        unit.unit_id for candidate in candidates
        for unit in candidate.constituent_units
    )
    if owned != tuple(unit.unit_id for unit in values) or any(
        previous.end_tick != current.start_tick
        or previous.market_end != current.market_start
        for previous, current in zip(candidates, candidates[1:])
    ):
        raise ValueError("declared upgrade candidates do not own one connected path")
    return {
        **base,
        "status": "local_connected_completed_source_partition",
        "current_endpoint_contract_compatible": all(
            len(candidate.constituent_units) % 2 == 1
            and candidate.constituent_units[0].direction == candidate.direction
            and candidate.constituent_units[-1].direction == candidate.direction
            for candidate in candidates
        ),
        "connector_unit_ids": connectors,
        "candidate_child_types": [
            {
                "candidate_id": stable_structure_id(
                    "chanlun-declared-upgrade-split-candidate-v1",
                    view_id, candidate.trend_id,
                ),
                "center_id": candidate.centers[0].center_id,
                "source_unit_ids": [
                    unit.unit_id for unit in candidate.constituent_units
                ],
                "movement_orientation": candidate.direction,
                "trend_direction": None,
                "source_unit_count": len(candidate.constituent_units),
                "directional_extrema_compatible": (
                    len(candidate.constituent_units) % 2 == 1
                    and candidate.constituent_units[0].direction == candidate.direction
                    and candidate.constituent_units[-1].direction == candidate.direction
                ),
                "confirmed_at": int(candidate.confirmed_at.timestamp()),
                "known_at": int(candidate.available_at.timestamp()),
            }
            for candidate in candidates
        ],
        "source_partition": {
            "source_unit_ids": [unit.unit_id for unit in values],
            "candidate_source_unit_ids": [
                [unit.unit_id for unit in candidate.constituent_units]
                for candidate in candidates
            ],
            "coverage_status": "source_covered_by_declared_candidates",
            "strict_completed_only": False,
            "general_view_legality_proven": False,
        },
        "candidate_identity_scope": "declared_repartition_only",
    }


def materialize_declared_upgrade_child_path(
    trend: TrendType, *, view_id: str, as_of: datetime,
) -> DeclaredUpgradeChildPath:
    """Build a view-scoped lower-type path with pinned completion clocks.

    Each source slice is taken from the validated local split review. A raw
    segment-only qualification would backdate the child type before its center
    and successor evidence was known, so both witness clocks are mandatory.
    The combined units remain declared observations, not canonical parent
    source or a trade certificate.
    """

    from chanlun.core.strict_structure.same_level_decomposition import (
        combine_same_level_trends,
    )
    from chanlun.core.strict_structure.unit_adapter import trend_type_to_unit

    review = review_declared_upgrade_split(trend, view_id=view_id, as_of=as_of)
    if review["status"] != "local_connected_completed_source_partition":
        return DeclaredUpgradeChildPath(
            view_id=view_id, source_trend_id=trend.trend_id, review=review,
            certificates=(), combined_units=(), protected_source_unit=None,
            geometry_equivalent=False,
            clock_not_earlier_than_protected=False, as_of=as_of,
        )
    by_id = {unit.unit_id: unit for unit in trend.constituent_units}
    certificates = tuple(
        qualify_declared_child_type(
            tuple(by_id[unit_id] for unit_id in candidate["source_unit_ids"]),
            view_id=view_id,
            evidence_ref=f"declared_upgrade_split:{candidate['candidate_id']}",
            as_of=as_of,
            confirmation_witness_at=datetime.fromtimestamp(
                candidate["confirmed_at"], timezone.utc,
            ),
            availability_witness_at=datetime.fromtimestamp(
                candidate["known_at"], timezone.utc,
            ),
        )
        for candidate in review["candidate_child_types"]
    )
    coverage = certify_declared_child_partition(
        trend.constituent_units, certificates, view_id=view_id, as_of=as_of,
    )
    if coverage.coverage_status != "source_covered_by_completed_types":
        raise ValueError("declared upgrade path lost exact source coverage")
    combined = combine_same_level_trends(
        tuple(certificate.unit for certificate in certificates), frozenset(),
    ).units
    protected = trend_type_to_unit(trend)
    geometry_fields = (
        "direction", "start_tick", "end_tick", "low_tick", "high_tick",
        "market_start", "market_end",
    )
    geometry_equal = len(combined) == 1 and all(
        getattr(combined[0], field) == getattr(protected, field)
        for field in geometry_fields
    )
    clock_not_earlier = geometry_equal and combined[0].available_at >= protected.available_at
    return DeclaredUpgradeChildPath(
        view_id=view_id, source_trend_id=trend.trend_id, review=review,
        certificates=certificates, combined_units=combined,
        protected_source_unit=protected,
        geometry_equivalent=geometry_equal,
        clock_not_earlier_than_protected=clock_not_earlier,
        as_of=as_of,
    )


def declared_upgrade_child_path_payload(path: DeclaredUpgradeChildPath) -> dict[str, object]:
    """Serialize the alternative clock proof without publishing formal IDs."""

    combined = path.combined_units
    protected = path.protected_source_unit
    return {
        **path.review,
        "schema": "chanlun-declared-upgrade-child-path-v1",
        "causal_combination": (
            None if not combined else {
                "declared_child_unit_ids": [
                    certificate.unit.unit_id for certificate in path.certificates
                ],
                "combined_unit_ids": [unit.unit_id for unit in combined],
                "combined_known_at": max(
                    int(unit.available_at.timestamp()) for unit in combined
                ),
                "protected_source_unit_id": (
                    None if protected is None else protected.unit_id
                ),
                "protected_known_at": (
                    None if protected is None else int(protected.available_at.timestamp())
                ),
                "geometry_equivalent": path.geometry_equivalent,
                "clock_not_earlier_than_protected": (
                    path.clock_not_earlier_than_protected
                ),
                "full_path_partition_certified": False,
                "higher_center_formal": False,
                "tradable": False,
            }
        ),
    }


def replay_declared_upgrade_parent_geometry(
    path: DeclaredUpgradeChildPath,
    parent_level: StrictLevelResult,
    *,
    as_of: datetime,
) -> dict[str, object]:
    """Compare a view-scoped source substitution with frozen parent geometry.

    This replays center geometry and third-class price geometry only. Distinct
    source IDs create distinct center/point identities; no formal parent or
    trading signal is imported into the canonical ledger.
    """

    from chanlun.core.strict_structure.center_machine import (
        calculate_centers, validate_unit_sequence,
    )
    from chanlun.core.strict_structure.point_rules import classify_third_class_geometry

    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("declared parent replay requires an aware cutoff")
    if parent_level.structural_level != path.review["source_structural_level"] + 1:
        raise ValueError("declared parent replay requires adjacent grades")
    base = {
        "schema": "chanlun-declared-upgrade-parent-geometry-replay-v1",
        "view_id": path.view_id,
        "source_trend_id": path.source_trend_id,
        "parent_grade": parent_level.structural_level,
        "as_of": int(as_of.timestamp()),
        "full_path_partition_certified": False,
        "formal_parent_center_identity_reused": False,
        "formal_third_point_reissued": False,
        "tradable": False,
    }
    protected = path.protected_source_unit
    combined = path.combined_units[0] if len(path.combined_units) == 1 else None
    geometry_fields = (
        "direction", "start_tick", "end_tick", "low_tick", "high_tick",
        "market_start", "market_end",
    )
    exact_path_still_valid = bool(
        combined is not None and protected is not None
        and path.geometry_equivalent
        and path.clock_not_earlier_than_protected
        and path.as_of <= as_of
        and combined.available_at <= as_of
        and combined.available_at >= protected.available_at
        and all(
            getattr(combined, field) == getattr(protected, field)
            for field in geometry_fields
        )
        and len(path.certificates) == len(path.review["candidate_child_types"])
        and all(certificate.view_id == path.view_id for certificate in path.certificates)
        and combined.child_ids == tuple(
            certificate.unit.unit_id for certificate in path.certificates
        )
    )
    if not exact_path_still_valid:
        return {
            **base, "status": "pending_declared_child_path_or_clock",
            "canonical_center_ids": [], "declared_center_ids": [],
            "center_geometry_and_clock_equal": False,
            "third_class_geometry_equal": False,
        }
    source = tuple(parent_level.units)
    positions = [
        index for index, unit in enumerate(source)
        if unit.unit_id == path.protected_source_unit.unit_id
    ]
    if len(positions) != 1:
        embedded = any(
            path.protected_source_unit.unit_id in unit.child_ids
            for unit in source
        )
        return {
            **base,
            "status": (
                "protected_member_embedded_in_combined_parent_unit"
                if embedded else "protected_member_absent_from_parent_source"
            ),
            "canonical_center_ids": [], "declared_center_ids": [],
            "center_geometry_and_clock_equal": False,
            "third_class_geometry_equal": False,
        }
    offset = positions[0]
    substituted = (*source[:offset], path.combined_units[0], *source[offset + 1:])
    validate_unit_sequence(
        substituted, parent_level.structural_level, SourceKind.TREND_TYPE,
    )
    declared = calculate_centers(
        substituted, parent_level.structural_level, SourceKind.TREND_TYPE,
    ).centers
    canonical = tuple(parent_level.center_result.centers)

    def unit_geometry(unit):
        return None if unit is None else (
            unit.direction, unit.start_tick, unit.end_tick,
            unit.low_tick, unit.high_tick, unit.market_start, unit.market_end,
            unit.available_at,
        )

    def center_geometry(center):
        return (
            center.state, center.zd_tick, center.zg_tick,
            center.dd_tick, center.gg_tick,
            center.body_start_market_time, center.last_touch_market_time,
            center.established_at, center.completed_at, center.available_at,
            tuple(unit_geometry(unit) for unit in center.body_units),
            unit_geometry(center.completion_leave_unit),
            unit_geometry(center.completion_return_unit),
        )

    def third_geometry(center):
        leave, returned = center.completion_leave_unit, center.completion_return_unit
        if leave is None or returned is None:
            return None
        classified = classify_third_class_geometry(
            zd_tick=center.zd_tick, zg_tick=center.zg_tick,
            leave=leave, return_unit=returned,
        )
        if classified is None:
            return None
        direction, point_type, side, anchor, invalidation, variant = classified
        return (
            direction, point_type, side, anchor, invalidation, variant.value,
            center.available_at,
        )

    center_equal = len(canonical) == len(declared) and all(
        center_geometry(left) == center_geometry(right)
        for left, right in zip(canonical, declared)
    )
    point_equal = len(canonical) == len(declared) and all(
        third_geometry(left) == third_geometry(right)
        for left, right in zip(canonical, declared)
    )
    return {
        **base,
        "status": (
            "parent_center_and_third_class_geometry_preserved"
            if center_equal and point_equal else
            "declared_parent_geometry_or_clock_differs"
        ),
        "substituted_parent_unit_index": offset,
        "canonical_center_ids": [center.center_id for center in canonical],
        "declared_center_ids": [center.center_id for center in declared],
        "center_geometry_and_clock_equal": center_equal,
        "third_class_geometry_equal": point_equal,
        "third_class_geometries": [
            None if third_geometry(center) is None else {
                "point_type": third_geometry(center)[1],
                "side": third_geometry(center)[2],
                "anchor_tick": third_geometry(center)[3],
                "invalidation_tick": third_geometry(center)[4],
                "known_at": int(third_geometry(center)[6].timestamp()),
            }
            for center in declared
        ],
    }

def certify_declared_child_partition(
    source_units: tuple[ConstituentUnit, ...],
    certificates: tuple[DeclaredChildTypeCertificate, ...],
    *,
    view_id: str,
    as_of: datetime,
) -> DeclaredPartitionCoverage:
    """Certify exact connected source coverage, without asserting view legality.

    A completed-type partition is a necessary input to a general association.
    Price overlap, even with complete source coverage, does not select its
    parent centers or settle the ownership of a competing established view.
    """

    values = tuple(source_units)
    declared = tuple(certificates)
    if not view_id or not view_id.strip():
        raise ValueError("view_id is required")
    _validate_parent_source(values, as_of)
    positions = {unit.unit_id: index for index, unit in enumerate(values)}
    first_index = None
    last_index = None
    qualified_ids = []
    previous_child = None
    for certificate in declared:
        if certificate.view_id != view_id or certificate.qualification_kind not in (
            "author_bounded_case", "strict_locked_trend_type"
        ):
            raise ValueError("child certificate belongs to another qualification view")
        child = certificate.unit
        if child.price_basis_revision != values[0].price_basis_revision:
            raise ValueError("child certificate crosses price basis")
        if (
            child.structural_level != values[0].structural_level + 1
            or child.source_kind is not SourceKind.TREND_TYPE
        ):
            raise ValueError("child certificate belongs to another source level")
        if child.available_at > as_of:
            raise ValueError("child certificate is not yet available")
        if not certificate.source_unit_ids:
            raise ValueError("child certificate requires source members")
        try:
            indexes = tuple(positions[item] for item in certificate.source_unit_ids)
        except KeyError as exc:
            raise ValueError("child certificate has missing source members") from exc
        if indexes != tuple(range(indexes[0], indexes[0] + len(indexes))):
            raise ValueError("child type members must be contiguous")
        if last_index is not None and indexes[0] != last_index + 1:
            raise ValueError("child types may not reuse or skip source members")
        if child.child_ids != certificate.source_unit_ids:
            raise ValueError("child type lineage does not match its certificate")
        source_members = tuple(values[index] for index in indexes)
        if certificate.qualification_kind == "author_bounded_case":
            if certificate.strict_trend is not None:
                raise ValueError("author case cannot borrow a strict trend identity")
            expected = qualify_declared_child_type(
                source_members, view_id=view_id,
                evidence_ref=certificate.evidence_ref, as_of=as_of,
                confirmation_witness_at=certificate.confirmation_witness_at,
                availability_witness_at=certificate.availability_witness_at,
            )
        else:
            trend = certificate.strict_trend
            if trend is None or trend.constituent_units != source_members:
                raise ValueError("strict certificate is missing exact trend lineage")
            expected = qualify_locked_trend_type(
                trend, view_id=view_id, as_of=as_of,
            )
        if child != expected.unit or certificate.evidence_ref != expected.evidence_ref:
            raise ValueError("child certificate geometry or time differs from source")
        if previous_child is not None and (
            previous_child.end_tick != child.start_tick
            or child.market_start != previous_child.market_end
        ):
            # Source coverage is a necessary, view-scoped condition. L038's
            # same-level consolidation + consolidation connection has no
            # formal trend-direction alternation requirement; parent-center
            # geometry and legality are proved separately.
            raise ValueError("child types must connect in price and market time")
        if first_index is None:
            first_index = indexes[0]
        last_index = indexes[-1]
        qualified_ids.extend(certificate.source_unit_ids)
        previous_child = child

    prefix = values[:first_index] if first_index is not None else ()
    tail = values[last_index + 1:] if last_index is not None else values
    coverage_status = (
        "no_completed_types" if not declared
        else "prefix_and_tail_unqualified" if prefix and tail
        else "prefix_unqualified" if prefix
        else "tail_pending" if tail
        else "source_covered_by_completed_types"
    )
    return DeclaredPartitionCoverage(
        view_id=view_id,
        source_structural_level=values[0].structural_level,
        source_unit_ids=tuple(unit.unit_id for unit in values),
        child_type_ids=tuple(certificate.unit.unit_id for certificate in declared),
        qualified_source_unit_ids=tuple(qualified_ids),
        unqualified_prefix_unit_ids=tuple(unit.unit_id for unit in prefix),
        unqualified_tail_unit_ids=tuple(unit.unit_id for unit in tail),
        coverage_status=coverage_status,
        strict_completed_only=bool(declared) and all(
            certificate.qualification_kind == "strict_locked_trend_type"
            for certificate in declared
        ),
        as_of=as_of,
    )


def declared_partition_coverage_payload(coverage: DeclaredPartitionCoverage) -> dict:
    return {
        "schema": "chanlun-declared-child-partition-coverage-v1",
        "view_id": coverage.view_id,
        "source_structural_level": coverage.source_structural_level,
        "source_unit_ids": list(coverage.source_unit_ids),
        "child_type_ids": list(coverage.child_type_ids),
        "qualified_source_unit_ids": list(coverage.qualified_source_unit_ids),
        "unqualified_prefix_unit_ids": list(coverage.unqualified_prefix_unit_ids),
        "unqualified_tail_unit_ids": list(coverage.unqualified_tail_unit_ids),
        "coverage_status": coverage.coverage_status,
        "strict_completed_only": coverage.strict_completed_only,
        "as_of": int(coverage.as_of.timestamp()),
        "general_view_legality_proven": False,
        "tradable": False,
    }


def classify_parent_against_protected_centers(
    parent: TrendCenter,
    protected_centers: tuple[TrendCenter, ...],
) -> ProtectedCenterRelation:
    """Prevent a shifted view from silently owning an established center.

    Exact same-member parents retain one identity; partial raw-member overlap
    calls for an explicit general-view repartition, not a second independent
    formal center.  A completed return witness is intentionally not body
    ownership and may start a later center.
    """

    protected = tuple(protected_centers)
    if not protected:
        return ProtectedCenterRelation(
            status="no_protected_centers_supplied",
            checked_center_ids=(), matching_center_id=None,
            overlapping_center_ids=(), overlapping_source_unit_ids=(),
        )
    if any(
        not child.child_ids
        for center in (parent, *protected) for child in center.body_units
    ):
        return ProtectedCenterRelation(
            status="lineage_unavailable",
            checked_center_ids=tuple(center.center_id for center in protected),
            matching_center_id=None,
            overlapping_center_ids=(), overlapping_source_unit_ids=(),
        )

    def raw_body_ids(center: TrendCenter) -> set[str]:
        return {
            source_id
            for child in center.body_units
            for source_id in (child.child_ids or (child.unit_id,))
        }

    candidate_raw = raw_body_ids(parent)
    candidate_seed = tuple(unit.child_ids for unit in parent.initial_units)
    matching = None
    overlapping = []
    overlapping_raw = set()
    for existing in protected:
        if (
            existing.structural_level != parent.structural_level
            or existing.source_kind is not parent.source_kind
            or existing.price_basis_revision != parent.price_basis_revision
        ):
            raise ValueError("protected center uses another structural context")
        existing_seed = tuple(unit.child_ids for unit in existing.initial_units)
        if (
            existing_seed == candidate_seed
            and (existing.zd_tick, existing.zg_tick) == (parent.zd_tick, parent.zg_tick)
        ):
            matching = existing.center_id
            continue
        shared = candidate_raw & raw_body_ids(existing)
        if shared:
            overlapping.append(existing.center_id)
            overlapping_raw.update(shared)
    status = (
        "overlap_requires_repartition" if overlapping
        else "same_existing_center" if matching is not None
        else "disjoint_new_center_candidate"
    )
    return ProtectedCenterRelation(
        status=status,
        checked_center_ids=tuple(center.center_id for center in protected),
        matching_center_id=matching,
        overlapping_center_ids=tuple(overlapping),
        overlapping_source_unit_ids=tuple(sorted(overlapping_raw)),
    )


def audit_declared_parent_path(
    source_units: tuple[ConstituentUnit, ...],
    certificates: tuple[DeclaredChildTypeCertificate, ...],
    *,
    view_id: str,
    as_of: datetime,
    protected_parent_centers: tuple[TrendCenter, ...] = (),
) -> DeclaredParentPathAudit:
    """Replay selected parent centers and audit preservation and connectors.

    This checks necessary path structure only. Alternative associations may
    still require a full view-selection proof before any trading promotion.
    """
    from chanlun.core.strict_structure.center_machine import calculate_centers

    values = tuple(source_units)
    declared = tuple(certificates)
    protected = tuple(protected_parent_centers)
    coverage = certify_declared_child_partition(
        values, declared, view_id=view_id, as_of=as_of
    )
    parent_level = values[0].structural_level + 1
    for existing in protected:
        if existing.available_at > as_of:
            raise ValueError("protected center is unavailable at this as_of")
        if (
            existing.structural_level != parent_level
            or existing.source_kind is not SourceKind.TREND_TYPE
            or existing.price_basis_revision != values[0].price_basis_revision
        ):
            raise ValueError("protected center uses another structural context")
    child_units = tuple(certificate.unit for certificate in declared)
    centers = calculate_centers(
        child_units, parent_level, SourceKind.TREND_TYPE
    ).centers
    relations = tuple(
        classify_parent_against_protected_centers(center, protected)
        for center in centers
    )
    positions = {unit.unit_id: index for index, unit in enumerate(child_units)}
    connectors = tuple(
        tuple(
            unit.unit_id for unit in child_units[
                positions[previous.body_units[-1].unit_id] + 1:
                positions[current.body_units[0].unit_id]
            ]
        )
        for previous, current in zip(centers, centers[1:])
    )
    matched = {
        relation.matching_center_id
        for relation in relations if relation.matching_center_id is not None
    }
    qualified_source = set(coverage.qualified_source_unit_ids)
    missing_protected = any(
        existing.center_id not in matched
        and bool(raw_ids := {
            source_id for child in existing.body_units
            for source_id in child.child_ids
        })
        and raw_ids <= qualified_source
        for existing in protected
    )
    status = (
        "protected_lineage_unavailable"
        if any(relation.status == "lineage_unavailable" for relation in relations)
        else "protected_repartition_required"
        if any(relation.status == "overlap_requires_repartition" for relation in relations)
        else "same_level_connector_missing" if any(not ids for ids in connectors)
        else "protected_center_missing" if missing_protected
        else "no_parent_center_yet" if not centers
        else "source_partition_pending"
        if coverage.coverage_status != "source_covered_by_completed_types"
        else "structurally_consistent"
    )
    return DeclaredParentPathAudit(
        view_id=view_id,
        partition_coverage=coverage,
        parent_center_ids=tuple(center.center_id for center in centers),
        protected_relations=relations,
        connector_child_type_ids=connectors,
        parent_path_status=status,
        as_of=as_of,
    )


def declared_parent_path_audit_payload(audit: DeclaredParentPathAudit) -> dict:
    return {
        "schema": "chanlun-declared-parent-path-audit-v1",
        "view_id": audit.view_id,
        "as_of": int(audit.as_of.timestamp()),
        "source_partition": declared_partition_coverage_payload(
            audit.partition_coverage
        ),
        "parent_center_ids": list(audit.parent_center_ids),
        "protected_relation_statuses": [
            relation.status for relation in audit.protected_relations
        ],
        "protected_relations": [
            {
                "status": relation.status,
                "checked_center_ids": list(relation.checked_center_ids),
                "matching_center_id": relation.matching_center_id,
                "overlapping_center_ids": list(relation.overlapping_center_ids),
                "overlapping_source_unit_ids": list(
                    relation.overlapping_source_unit_ids
                ),
            }
            for relation in audit.protected_relations
        ],
        "connector_child_type_ids": [
            list(ids) for ids in audit.connector_child_type_ids
        ],
        "parent_path_status": audit.parent_path_status,
        "general_view_legality_proven": False,
        "tradable": False,
    }


def build_declared_parent_view(
    source_units: tuple[ConstituentUnit, ...],
    *,
    view_id: str,
    initial_certificates: tuple[DeclaredChildTypeCertificate, ...],
    as_of: datetime,
    price_quantum: Decimal,
    departure_certificate: DeclaredChildTypeCertificate | None = None,
    return_certificate: DeclaredChildTypeCertificate | None = None,
    protected_parent_centers: tuple[TrendCenter, ...] = (),
) -> DeclaredParentView:
    """Form one parent and its point only from certified view-specific types."""

    from chanlun.core.strict_structure.center_machine import (
        advance_center,
        establish_center,
    )
    from chanlun.core.strict_structure.signals import StrictSignalEngine

    values = tuple(source_units)
    _validate_parent_source(values, as_of)
    if any(center.available_at > as_of for center in protected_parent_centers):
        raise ValueError("protected center is unavailable at this as_of")
    initial = tuple(initial_certificates)
    if len(initial) != 3:
        raise ValueError("parent requires exactly three completed child certificates")
    if return_certificate is not None and departure_certificate is None:
        raise ValueError("return certificate requires a departure certificate")
    certificates = (
        *initial,
        *((departure_certificate,) if departure_certificate is not None else ()),
        *((return_certificate,) if return_certificate is not None else ()),
    )
    path_audit = audit_declared_parent_path(
        values, certificates, view_id=view_id, as_of=as_of,
        protected_parent_centers=tuple(protected_parent_centers),
    )
    coverage = path_audit.partition_coverage
    child_units = tuple(certificate.unit for certificate in certificates)
    parent_structural_level = values[0].structural_level + 1
    parent = establish_center(
        child_units[:3], parent_structural_level, SourceKind.TREND_TYPE
    )
    if parent is None:
        raise ValueError("three completed child types do not form a parent center")
    for child in child_units[3:]:
        parent, _event = advance_center(parent, child)
    if (
        not path_audit.parent_center_ids
        or path_audit.parent_center_ids[0] != parent.center_id
    ):
        raise ValueError("declared parent differs from full child-path replay")
    protected_relation = path_audit.protected_relations[0]
    child_level = StrictLevelResult(
        structural_level=values[0].structural_level, units=values,
        center_result=CenterLevelResult(
            structural_level=values[0].structural_level,
            price_basis_revision=values[0].price_basis_revision,
            centers=(), previews=(), events=(),
            locked_unit_count=len(values), replay_from=0,
        ),
        trend_types=(), completed_trends=(),
        decomposition_mode="general_declared", view_id=view_id,
    )
    parent_level = StrictLevelResult(
        structural_level=parent_structural_level, units=child_units,
        center_result=CenterLevelResult(
            structural_level=parent_structural_level,
            price_basis_revision=values[0].price_basis_revision,
            centers=(parent,), previews=(), events=(),
            locked_unit_count=len(child_units), replay_from=0,
        ),
        trend_types=(), completed_trends=(),
        decomposition_mode="general_declared", view_id=view_id,
    )
    structure = StrictStructureResult(
        schema="chanlun-structure",
        price_basis_revision=values[0].price_basis_revision,
        levels=(
            *(
                StrictLevelResult(
                    structural_level=level,
                    units=(),
                    center_result=CenterLevelResult(
                        structural_level=level,
                        price_basis_revision=values[0].price_basis_revision,
                        centers=(), previews=(), events=(),
                        locked_unit_count=0, replay_from=0,
                    ),
                    trend_types=(), completed_trends=(),
                    decomposition_mode="general_declared", view_id=view_id,
                )
                for level in range(values[0].structural_level)
            ),
            child_level, parent_level,
        ),
    )
    points = (
        () if protected_relation.status in (
            "overlap_requires_repartition", "lineage_unavailable"
        ) or (
            protected_relation.matching_center_id is not None
            and protected_relation.matching_center_id != parent.center_id
        ) or path_audit.parent_path_status in (
            "protected_center_missing", "same_level_connector_missing"
        )
        else tuple(
            point for point in StrictSignalEngine(
                structure=structure, price_quantum=price_quantum,
            ).third_class_points()
            if point.structural_level == parent_structural_level
        )
    )
    unqualified_tail = (
        values[-len(coverage.unqualified_tail_unit_ids):]
        if coverage.unqualified_tail_unit_ids else ()
    )
    tail_geometry = (
        "none" if not unqualified_tail
        else "touches_parent_core"
        if any(
            max(unit.low_tick, parent.zd_tick)
            <= min(unit.high_tick, parent.zg_tick)
            for unit in unqualified_tail
        )
        else "outside_parent_core"
    )
    return DeclaredParentView(
        view_id=view_id,
        source_unit_ids=tuple(unit.unit_id for unit in values),
        child_certificates=certificates,
        partition_coverage=coverage,
        parent_path_audit=path_audit,
        parent_center=parent,
        third_class_points=points,
        unqualified_prefix_unit_ids=coverage.unqualified_prefix_unit_ids,
        unqualified_tail_unit_ids=coverage.unqualified_tail_unit_ids,
        unqualified_tail_geometry=tail_geometry,
        protected_center_relation=protected_relation,
        as_of=as_of,
    )


def declared_parent_view_payload(view: DeclaredParentView) -> dict:
    parent = view.parent_center
    kinds = {certificate.qualification_kind for certificate in view.child_certificates}
    author_only = kinds == {"author_bounded_case"}
    qualification_scope = (
        "author_bounded_declared_view_only" if author_only
        else "strict_locked_declared_view_only"
        if kinds == {"strict_locked_trend_type"}
        else "mixed_declared_view_only"
    )
    certificates_by_id = {
        certificate.unit.unit_id: certificate
        for certificate in view.child_certificates
    }
    return {
        "schema": (
            "chanlun-author-bounded-declared-parent-view-v1" if author_only
            else "chanlun-declared-parent-view-v2"
        ),
        "view_id": view.view_id,
        "source_unit_ids": list(view.source_unit_ids),
        "source_structural_level": parent.structural_level - 1,
        "as_of": int(view.as_of.timestamp()),
        "source_partition": declared_partition_coverage_payload(view.partition_coverage),
        "parent_path_audit": declared_parent_path_audit_payload(view.parent_path_audit),
        "child_certificates": [
            {
                "child_type_id": certificate.unit.unit_id,
                "source_member_unit_ids": list(certificate.source_unit_ids),
                "direction": certificate.unit.direction,
                "completed_at": int(certificate.unit.confirmed_at.timestamp()),
                "available_at": int(certificate.unit.available_at.timestamp()),
                "evidence_ref": certificate.evidence_ref,
                "qualification_kind": certificate.qualification_kind,
            }
            for certificate in view.child_certificates
        ],
        "parent_center": {
            "center_id": parent.center_id,
            "structural_level": parent.structural_level,
            "core": {"zd_tick": parent.zd_tick, "zg_tick": parent.zg_tick},
            "state": parent.state.value,
            "established_at": int(parent.established_at.timestamp()),
            "completed_at": (
                None if parent.completed_at is None
                else int(parent.completed_at.timestamp())
            ),
            "initial_child_type_ids": [unit.unit_id for unit in parent.initial_units],
        },
        "third_class_points": [
            {
                "point_id": point.point_id,
                "point_type": point.point_type,
                "center_id": point.center_id,
                "anchor_child_type_id": point.anchor_unit_id,
                "anchor_source_unit_ids": list(
                    certificates_by_id[point.anchor_unit_id].source_unit_ids
                ),
                "status": point.status.value,
                "available_at": int(point.available_at.timestamp()),
            }
            for point in view.third_class_points
        ],
        "unqualified_prefix_unit_ids": list(view.unqualified_prefix_unit_ids),
        "unqualified_tail_unit_ids": list(view.unqualified_tail_unit_ids),
        "unqualified_tail_geometry": view.unqualified_tail_geometry,
        "protected_center_relation": {
            "status": view.protected_center_relation.status,
            "checked_center_ids": list(view.protected_center_relation.checked_center_ids),
            "matching_center_id": view.protected_center_relation.matching_center_id,
            "overlapping_center_ids": list(view.protected_center_relation.overlapping_center_ids),
            "overlapping_source_unit_ids": list(
                view.protected_center_relation.overlapping_source_unit_ids
            ),
        },
        "canonical_same_level_output": False,
        "tradable": False,
        "qualification_scope": qualification_scope,
    }


def _followup(
    parent: AssociativeParent,
    tail: tuple[ConstituentUnit, ...],
) -> AssociativeFollowup:
    if len(tail) < 3:
        return AssociativeFollowup(
            departure_member_ids=tuple(unit.unit_id for unit in tail),
            departure_status="pending_three_segment_child_structure",
            return_unit_id=None,
            return_geometry="not_available",
        )
    first_three = tail[:3]
    child_core = _overlap(first_three)
    direction = first_three[-1].direction
    outside = (
        first_three[-1].end_tick < parent.zd_tick
        if direction == "down"
        else first_three[-1].end_tick > parent.zg_tick
    )
    status = (
        "segmentized_departure_candidate"
        if child_core is not None and outside
        else "no_qualified_segmentized_departure"
    )
    ret = tail[3] if len(tail) >= 4 else None
    if status != "segmentized_departure_candidate" or ret is None:
        return_geometry = "not_available"
    elif ret.direction == "up" and direction == "down":
        return_geometry = (
            "outside_or_touching_lower_core_boundary"
            if ret.high_tick <= parent.zd_tick else "reentered_parent_core"
        )
    elif ret.direction == "down" and direction == "up":
        return_geometry = (
            "outside_or_touching_upper_core_boundary"
            if ret.low_tick >= parent.zg_tick else "reentered_parent_core"
        )
    else:
        return_geometry = "not_opposite_return"
    return AssociativeFollowup(
        departure_member_ids=tuple(unit.unit_id for unit in first_three),
        departure_status=status,
        return_unit_id=None if ret is None else ret.unit_id,
        return_geometry=return_geometry,
    )


def materialize_associative_view(
    units: tuple[ConstituentUnit, ...],
    *,
    view_id: str,
    component_member_ids: tuple[tuple[str, str, str], ...],
    as_of: datetime,
) -> AssociativeView:
    """Compute one declared view without mixing its members with another.

    A triple's overlap is a lower center geometry.  A parent uses the full
    price ranges of three consecutive components, as in lesson 54.  Formal
    parent and third-point completion remain separate certificates.
    """

    values = tuple(units)
    if not view_id or not view_id.strip():
        raise ValueError("view_id is required")
    _validate_source(values, as_of)
    groups = tuple(tuple(group) for group in component_member_ids)
    if not groups:
        raise ValueError("at least one component group is required")
    positions = {unit.unit_id: index for index, unit in enumerate(values)}
    components = []
    previous_end = None
    for group in groups:
        if len(group) != 3 or len(set(group)) != 3:
            raise ValueError("each component requires three distinct segments")
        try:
            indexes = tuple(positions[item] for item in group)
        except KeyError as exc:
            raise ValueError("component references an unavailable source unit") from exc
        if indexes != tuple(range(indexes[0], indexes[0] + 3)):
            raise ValueError("component units must be consecutive")
        if previous_end is not None and indexes[0] != previous_end + 1:
            raise ValueError("adjacent components may not reuse or skip units")
        members = tuple(values[index] for index in indexes)
        core = _overlap(members)
        if core is None:
            raise ValueError("component has no three-segment common price")
        components.append(AssociativeComponent(
            component_id=stable_structure_id(
                "chanlun-general-component-v1", view_id, group,
                values[0].price_basis_revision,
            ),
            structural_level=values[0].structural_level,
            member_unit_ids=group,
            core_zd_tick=core[0], core_zg_tick=core[1],
            price_low_tick=min(unit.low_tick for unit in members),
            price_high_tick=max(unit.high_tick for unit in members),
            market_start=members[0].market_start,
            market_end=members[-1].market_end,
            available_at=max(unit.available_at for unit in members),
        ))
        previous_end = indexes[-1]
    first_index = positions[groups[0][0]]
    last_index = positions[groups[-1][-1]]
    parent_centers = []
    for offset in range(len(components) - 2):
        members = tuple(components[offset : offset + 3])
        zd = max(component.price_low_tick for component in members)
        zg = min(component.price_high_tick for component in members)
        if zd > zg:
            continue
        parent_centers.append(AssociativeParent(
            center_id=stable_structure_id(
                "chanlun-general-parent-geometry-v1", view_id,
                tuple(component.component_id for component in members), zd, zg,
            ),
            structural_level=values[0].structural_level + 1,
            component_ids=tuple(component.component_id for component in members),
            zd_tick=zd, zg_tick=zg,
            observed_at=max(component.available_at for component in members),
        ))
    suffix = values[last_index + 1 :]
    latest_parent = next(
        (
            parent for parent in reversed(parent_centers)
            if parent.component_ids[-1] == components[-1].component_id
        ),
        None,
    )
    return AssociativeView(
        view_id=view_id,
        source_structural_level=values[0].structural_level,
        price_basis_revision=values[0].price_basis_revision,
        source_unit_ids=tuple(unit.unit_id for unit in values),
        prefix_unit_ids=tuple(unit.unit_id for unit in values[:first_index]),
        components=tuple(components),
        suffix_unit_ids=tuple(unit.unit_id for unit in suffix),
        parent_centers=tuple(parent_centers),
        latest_parent_followup=(
            None if latest_parent is None else _followup(latest_parent, suffix)
        ),
        as_of=as_of,
    )


def associative_view_payload(view: AssociativeView) -> dict:
    """A JSON-ready, non-tradable record of one declared association."""

    return {
        "schema": "chanlun-declared-associative-view-v1",
        "view_id": view.view_id,
        "source_structural_level": view.source_structural_level,
        "price_basis_revision": view.price_basis_revision,
        "as_of": int(view.as_of.timestamp()),
        "source_unit_ids": list(view.source_unit_ids),
        "prefix_unit_ids": list(view.prefix_unit_ids),
        "components": [
            {
                "component_id": component.component_id,
                "structural_level": component.structural_level,
                "member_unit_ids": list(component.member_unit_ids),
                "core": {
                    "zd_tick": component.core_zd_tick,
                    "zg_tick": component.core_zg_tick,
                },
                "price_range": {
                    "low_tick": component.price_low_tick,
                    "high_tick": component.price_high_tick,
                },
                "market_start": int(component.market_start.timestamp()),
                "market_end": int(component.market_end.timestamp()),
                "available_at": int(component.available_at.timestamp()),
            }
            for component in view.components
        ],
        "suffix_unit_ids": list(view.suffix_unit_ids),
        "parent_centers": [
            {
                "center_id": parent.center_id,
                "structural_level": parent.structural_level,
                "member_component_ids": list(parent.component_ids),
                "core": {"zd_tick": parent.zd_tick, "zg_tick": parent.zg_tick},
                "observed_at": int(parent.observed_at.timestamp()),
                "formal_status": parent.formal_status,
            }
            for parent in view.parent_centers
        ],
        "latest_parent_followup": (
            None if view.latest_parent_followup is None else {
                "departure_member_ids": list(
                    view.latest_parent_followup.departure_member_ids
                ),
                "departure_status": view.latest_parent_followup.departure_status,
                "return_unit_id": view.latest_parent_followup.return_unit_id,
                "return_geometry": view.latest_parent_followup.return_geometry,
                "formal_third_point": view.latest_parent_followup.formal_third_point,
            }
        ),
        "tradable": False,
        "evidence_scope": "declared_general_association_geometry_only",
    }


def discover_entry_anchored_associations(
    units: tuple[ConstituentUnit, ...],
    *,
    anchor_unit_id: str,
    as_of: datetime,
) -> tuple[AssociativeView, ...]:
    """Find geometry hypotheses after one declared entry, without certifying them.

    The opposite-direction first member and successive three-segment groups
    reproduce the two associations available in lesson 54.  More general
    associations still need their own view search and completed-type proof.
    """

    values = tuple(units)
    _validate_source(values, as_of)
    indexes = {unit.unit_id: index for index, unit in enumerate(values)}
    if anchor_unit_id not in indexes:
        raise ValueError("entry anchor is absent from the source prefix")
    anchor = indexes[anchor_unit_id]
    output = []
    for start in range(anchor + 1, len(values) - 5):
        if values[start].direction == values[anchor].direction:
            continue
        groups = []
        for offset in (start, start + 3, start + 6):
            if offset + 3 > len(values):
                break
            members = values[offset : offset + 3]
            if _overlap(members) is None:
                break
            groups.append(tuple(unit.unit_id for unit in members))
        if len(groups) < 2:
            continue
        view = materialize_associative_view(
            values,
            view_id=stable_structure_id(
                "chanlun-entry-anchored-general-hypothesis-v1",
                values[anchor].unit_id,
                values[start].unit_id,
                values[0].price_basis_revision,
            ),
            component_member_ids=tuple(groups),
            as_of=as_of,
        )
        if view.parent_centers or len(view.components) == 2:
            output.append(view)
    return tuple(output)
