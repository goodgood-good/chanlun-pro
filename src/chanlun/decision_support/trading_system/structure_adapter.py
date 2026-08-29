from __future__ import annotations

from datetime import datetime
from typing import cast

from chanlun.core.strict_structure.current_events import (
    TerminalSegmentReference,
    current_strict_point_evidence,
    terminal_segment_reference,
)
from chanlun.core.strict_structure.models import (
    StrictEvidenceResult,
    StrictPointEvidence,
    StrictPointStatus,
    StrictStructureResult,
)
from chanlun.decision_support.fingerprints import normalize_datetime
from chanlun.decision_support.trading_system.models import (
    PointType,
    PointVariant,
    StructuralPoint,
    build_point_id,
)
from chanlun.decision_support.trading_system.operational_point_graph import (
    resolve_current_operational_point_graph,
)


def structural_point_id_map(
    raw_points,
    *,
    code: str,
    source_frequency: str,
    allow_identity_aliases: bool = False,
) -> dict[str, str]:
    """在不破坏证据关系的前提下把严格结构身份转换为交易身份。"""

    values = tuple(raw_points)
    raw_by_id = {point.point_id: point for point in values}
    if len(raw_by_id) != len(values):
        raise ValueError("strict point ids must be unique before conversion")
    converted: dict[str, str] = {}
    pending = list(values)
    while pending:
        deferred = []
        for raw in pending:
            if raw.parent_point_id is not None and raw.parent_point_id not in converted:
                if raw.parent_point_id not in raw_by_id:
                    raise ValueError("strict point parent is missing before conversion")
                deferred.append(raw)
                continue
            point_type = cast(PointType, raw.point_type)
            converted[raw.point_id] = build_point_id(
                code=code,
                price_basis_revision=raw.price_basis_revision,
                point_type=point_type,
                source_frequency=source_frequency,
                tower="formal",
                recursive_level=raw.structural_level,
                anchor_at=raw.anchor_at,
                center_id=raw.center_id,
                parent_point_id=(
                    None
                    if raw.parent_point_id is None
                    else converted[raw.parent_point_id]
                ),
                variant=cast(PointVariant, raw.variant.value),
                structure_anchor_price=float(raw.structure_anchor_price),
                structure_invalidation_price=float(raw.structure_invalidation_price),
                center_zd=(
                    None
                    if raw.center_zd_tick is None
                    else float(raw.center_zd_tick * raw.price_quantum)
                ),
                center_zg=(
                    None
                    if raw.center_zg_tick is None
                    else float(raw.center_zg_tick * raw.price_quantum)
                ),
                center_ordinal=raw.center_ordinal,
                divergence_kind=(
                    None if raw.divergence is None else raw.divergence.kind
                ),
            )
        if len(deferred) == len(pending):
            raise ValueError("strict point parent graph is cyclic")
        pending = deferred
    if not allow_identity_aliases and len(set(converted.values())) != len(converted):
        raise ValueError("converted structural point identity collision")
    return converted


def _historical_terminal_segment_reference(
    structure: StrictStructureResult | None,
    raw: StrictPointEvidence,
) -> TerminalSegmentReference | None:
    """Recover the terminal unit carried by a historical confirmed point.

    ``terminal_segment_reference`` intentionally resolves only the current
    two-unit live tail.  The 1m interval-nesting ledger also needs older points
    whose exact anchor unit is still present in ``evidence.structure``.  Such a
    unit was the point's latest completed segment at the occurrence time, even
    when it is no longer the structure's current tail.
    """

    if structure is None:
        return None
    unit = next(
        (
            unit
            for level in structure.levels
            if level.structural_level == raw.structural_level
            for unit in level.units
            if unit.unit_id == raw.anchor_unit_id
        ),
        None,
    )
    expected_direction = "down" if raw.side == "buy" else "up"
    if (
        unit is None
        or unit.forming
        or not (unit.locked or unit.formed_at is not None)
        or unit.direction != expected_direction
        or unit.market_end != raw.anchor_at
    ):
        return None
    return TerminalSegmentReference(
        role="latest_completed",
        structural_level=unit.structural_level,
        unit_id=unit.unit_id,
        source_kind=unit.source_kind,
        direction=unit.direction,
        state="locked" if unit.locked else "formed",
        market_start=unit.market_start,
        market_end=unit.market_end,
        available_at=unit.available_at,
    )


def convert_confirmed_point_evidence(
    raw_points,
    *,
    code: str,
    source_frequency: str,
    as_of: datetime,
    structure: StrictStructureResult | None = None,
) -> tuple[StructuralPoint, ...]:
    """Convert an already validated strict point ledger without materializing a snapshot."""

    closed_at = normalize_datetime(as_of, "as_of")
    values = tuple(raw_points)
    converted_ids = structural_point_id_map(
        values,
        code=code,
        source_frequency=source_frequency,
    )
    output: list[StructuralPoint] = []
    for raw in values:
        if raw.status is not StrictPointStatus.CONFIRMED:
            raise ValueError("strict confirmed endpoint returned non-confirmed point")
        if normalize_datetime(raw.available_at, "point.available_at") > closed_at:
            raise ValueError("strict point is available after as_of")
        point_type = cast(PointType, raw.point_type)
        output.append(
            StructuralPoint(
                point_id=converted_ids[raw.point_id],
                code=code,
                point_type=point_type,
                side=raw.side,
                status="confirmed",
                variant=cast(PointVariant, raw.variant.value),
                source_frequency=source_frequency,
                price_basis_revision=raw.price_basis_revision,
                tower="formal",
                recursive_level=raw.structural_level,
                anchor_at=raw.anchor_at,
                confirmed_at=raw.confirmed_at,
                available_at=raw.available_at,
                structure_anchor_price=float(raw.structure_anchor_price),
                structure_invalidation_price=float(raw.structure_invalidation_price),
                center_id=raw.center_id,
                center_zd=(
                    None
                    if raw.center_zd_tick is None
                    else float(raw.center_zd_tick * raw.price_quantum)
                ),
                center_zg=(
                    None
                    if raw.center_zg_tick is None
                    else float(raw.center_zg_tick * raw.price_quantum)
                ),
                center_ordinal=raw.center_ordinal,
                divergence_kind=(
                    None if raw.divergence is None else raw.divergence.kind
                ),
                parent_point_id=(
                    None
                    if raw.parent_point_id is None
                    else converted_ids[raw.parent_point_id]
                ),
                evidence_codes=raw.evidence_codes,
                related_point_ids=tuple(
                    converted_ids[point_id] for point_id in raw.related_point_ids
                ),
                small_to_large_carrier_unit_ids=(raw.small_to_large_carrier_unit_ids),
                terminal_segment=_historical_terminal_segment_reference(
                    structure,
                    raw,
                ),
            )
        )
    return tuple(sorted(output, key=lambda point: (point.available_at, point.point_id)))


def extract_confirmed_points(
    evidence: StrictEvidenceResult,
    *,
    code: str,
    source_frequency: str,
    as_of: datetime,
) -> tuple[StructuralPoint, ...]:
    closed_at = normalize_datetime(as_of, "as_of")
    if evidence.symbol != code or evidence.source_frequency != source_frequency:
        raise ValueError("strict evidence context mismatch")
    if normalize_datetime(evidence.source_closed_at, "source_closed_at") > closed_at:
        raise ValueError("strict evidence snapshot is after as_of")

    return convert_confirmed_point_evidence(
        evidence.confirmed_points,
        code=code,
        source_frequency=source_frequency,
        as_of=closed_at,
        structure=evidence.structure,
    )


def extract_current_confirmed_points(
    evidence: StrictEvidenceResult,
    *,
    code: str,
    source_frequency: str,
    as_of: datetime,
) -> tuple[StructuralPoint, ...]:
    """把末端已完成线段的操作确认点转换为交易层买卖点。

    核心 ``confirmed_points`` 仍只表示晚到的防重绘审计锁。实时交易以当前
    线段的首个因果几何形成时刻确认：严格 ``approaching`` 证据若锚定
    ``latest_completed`` 且该段保留 ``formed_at``，会在这里提升为操作确认点。
    ``latest_unfinished`` 仍保持不可操作候选。两种状态共享同一个交易身份，
    因而后续审计锁不会重复发出信号。
    """
    closed_at = normalize_datetime(as_of, "as_of")
    if evidence.symbol != code or evidence.source_frequency != source_frequency:
        raise ValueError("strict evidence context mismatch")
    if normalize_datetime(evidence.source_closed_at, "source_closed_at") > closed_at:
        raise ValueError("strict evidence snapshot is after as_of")

    return convert_current_confirmed_point_evidence(
        evidence.structure,
        confirmed_points=evidence.confirmed_points,
        approaching_points=evidence.approaching_points,
        code=code,
        source_frequency=source_frequency,
        as_of=closed_at,
    )


def convert_current_confirmed_point_evidence(
    structure: StrictStructureResult,
    *,
    confirmed_points: tuple[StrictPointEvidence, ...],
    approaching_points: tuple[StrictPointEvidence, ...],
    code: str,
    source_frequency: str,
    as_of: datetime,
) -> tuple[StructuralPoint, ...]:
    """Convert the production live-tail projection without serializing it.

    Historical replay already owns the exact causal structure snapshot and only
    needs the same operational conversion as screening.  Keeping this component
    adapter beside the evidence adapter avoids constructing divergences and a
    full evidence revision merely to read current setup points.
    """

    normalize_datetime(as_of, "as_of")
    if not isinstance(structure, StrictStructureResult):
        raise TypeError("current point conversion requires strict structure")
    confirmed_values = tuple(confirmed_points)
    approaching_values = tuple(approaching_points)
    raw_points = (*confirmed_values, *approaching_values)
    raw_current = current_strict_point_evidence(structure, raw_points)
    operational_graph = resolve_current_operational_point_graph(
        structure,
        confirmed_points=confirmed_values,
        approaching_points=approaching_values,
        source_frequency=source_frequency,
        include_one_minute_segment_level=True,
    )
    converted_ids = structural_point_id_map(
        raw_points,
        code=code,
        source_frequency=source_frequency,
        allow_identity_aliases=True,
    )
    references = {
        point.point_id: terminal_segment_reference(
            structure,
            structural_level=point.structural_level,
            unit_id=point.anchor_unit_id,
        )
        for point in raw_current
    }
    if any(reference is None for reference in references.values()):
        raise ValueError("current point lost terminal segment lineage")

    units = {
        (level.structural_level, unit.unit_id): unit
        for level in structure.levels
        for unit in level.units
    }
    output: dict[str, tuple[int, StructuralPoint]] = {}
    for raw in raw_current:
        reference = references[raw.point_id]
        if reference is None:
            raise AssertionError("terminal reference was validated above")
        unit = units[(raw.structural_level, raw.anchor_unit_id)]
        if raw.status is StrictPointStatus.CONFIRMED:
            # The trading event starts when the terminal segment first becomes
            # geometrically complete.  A later audit lock must not move the
            # same operation point's clock forward after a process restart.
            geometry_confirmed_at = (
                unit.formed_at
                if reference.role == "latest_completed" and unit.formed_at is not None
                else None
            )
            confirmed_at = geometry_confirmed_at or raw.confirmed_at
            available_at = geometry_confirmed_at or raw.available_at
            priority = 1
            evidence_codes = (
                raw.evidence_codes
                if geometry_confirmed_at is None
                else tuple(
                    dict.fromkeys(
                        (
                            *raw.evidence_codes,
                            "geometry_confirmed_before_audit_lock",
                        )
                    )
                )
            )
        else:
            projection = operational_graph.get(raw.point_id)
            if (
                projection is None
                or projection.confirmation_basis != "latest_completed_geometry"
            ):
                continue
            if projection.terminal_segment != reference:
                raise ValueError("operational point terminal lineage diverged")
            confirmed_at = projection.confirmed_at
            available_at = confirmed_at
            priority = 0
            evidence_codes = tuple(
                dict.fromkeys(
                    (
                        *raw.evidence_codes,
                        "geometry_confirmed_before_audit_lock",
                        *(
                            ("operational_parent_geometry_confirmed",)
                            if projection.projected_parent_point_ids
                            else ()
                        ),
                    )
                )
            )
        if confirmed_at is None:
            raise ValueError("operational point confirmation time is unavailable")
        point_type = cast(PointType, raw.point_type)
        point = StructuralPoint(
            point_id=converted_ids[raw.point_id],
            code=code,
            point_type=point_type,
            side=raw.side,
            status="confirmed",
            variant=cast(PointVariant, raw.variant.value),
            source_frequency=source_frequency,
            price_basis_revision=raw.price_basis_revision,
            tower="formal",
            recursive_level=raw.structural_level,
            anchor_at=raw.anchor_at,
            confirmed_at=confirmed_at,
            available_at=available_at,
            structure_anchor_price=float(raw.structure_anchor_price),
            structure_invalidation_price=float(raw.structure_invalidation_price),
            center_id=raw.center_id,
            center_zd=(
                None
                if raw.center_zd_tick is None
                else float(raw.center_zd_tick * raw.price_quantum)
            ),
            center_zg=(
                None
                if raw.center_zg_tick is None
                else float(raw.center_zg_tick * raw.price_quantum)
            ),
            center_ordinal=raw.center_ordinal,
            divergence_kind=(None if raw.divergence is None else raw.divergence.kind),
            parent_point_id=(
                None
                if raw.parent_point_id is None
                else converted_ids[raw.parent_point_id]
            ),
            evidence_codes=evidence_codes,
            related_point_ids=tuple(
                converted_ids[point_id] for point_id in raw.related_point_ids
            ),
            small_to_large_carrier_unit_ids=(raw.small_to_large_carrier_unit_ids),
            terminal_segment=reference,
        )
        previous = output.get(point.point_id)
        if previous is None or priority > previous[0]:
            output[point.point_id] = (priority, point)
        elif previous[1] != point:
            raise ValueError("operational point identity maps to conflicting evidence")
    return tuple(
        sorted(
            (value[1] for value in output.values()),
            key=lambda point: (point.available_at, point.point_id),
        )
    )


def extract_one_minute_segment_difference_points(
    evidence: StrictEvidenceResult,
    *,
    code: str,
    source_frequency: str,
    as_of: datetime,
) -> tuple[StructuralPoint, ...]:
    """Build the causal 1m segment-difference ledger used by all decision paths.

    The current-tail projection contains geometry-confirmed operational points,
    while the audit-confirmed ledger retains points that have already left that
    tail.  A current 5m setup needs both.  When both ledgers describe the same
    trading identity, prefer the current projection because it also carries the
    latest terminal-segment lineage.
    """

    if source_frequency != "1m":
        raise ValueError("segment difference points require 1m evidence")
    historical_confirmed = extract_confirmed_points(
        evidence,
        code=code,
        source_frequency=source_frequency,
        as_of=as_of,
    )
    current_confirmed = extract_current_confirmed_points(
        evidence,
        code=code,
        source_frequency=source_frequency,
        as_of=as_of,
    )
    points_by_id = {point.point_id: point for point in historical_confirmed}
    points_by_id.update({point.point_id: point for point in current_confirmed})
    return tuple(
        sorted(
            points_by_id.values(),
            key=lambda point: (point.available_at, point.point_id),
        )
    )


def point_signature(
    points: tuple[StructuralPoint, ...],
) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (
            point.point_id,
            point.point_type,
            point.tower,
            point.recursive_level,
            point.anchor_at,
            point.confirmed_at,
            point.available_at,
            point.price_basis_revision,
            point.variant,
            point.center_id,
            point.structure_invalidation_price,
            point.parent_point_id,
            point.related_point_ids,
            point.small_to_large_carrier_unit_ids,
            point.terminal_segment,
        )
        for point in points
    )
