from __future__ import annotations

from datetime import datetime
from typing import cast

from chanlun.core.strict_structure.models import (
    StrictEvidenceResult,
    StrictPointStatus,
)
from chanlun.decision_support.fingerprints import normalize_datetime
from chanlun.decision_support.trading_system.models import (
    PointType,
    PointVariant,
    StructuralPoint,
    build_point_id,
)


_SMALL_TO_LARGE_SECOND_CODES = frozenset(
    {
        "confirmed_lower_level_first_class_parent",
        "small_to_large_reversal",
        "last_lower_level_center_reverse_third_class",
        "complete_adjacent_rebound",
        "complete_first_pullback",
    }
)


def has_explicit_small_to_large_second_proof(
    point: StructuralPoint,
    *,
    points_by_id: dict[str, StructuralPoint],
) -> bool:
    """Verify the closed point graph behind a small-to-large second point."""

    if point.point_type not in {"2buy", "2sell"}:
        return False
    if not _SMALL_TO_LARGE_SECOND_CODES.issubset(point.evidence_codes):
        return False
    if point.recursive_level <= 0 or point.parent_point_id is None:
        return False
    if point.parent_point_id not in point.related_point_ids:
        return False
    if (
        len(point.small_to_large_carrier_unit_ids) != 3
        or not point.small_to_large_last_center_id
    ):
        return False
    parent = points_by_id.get(point.parent_point_id)
    expected_parent = "1buy" if point.side == "buy" else "1sell"
    if (
        parent is None
        or parent.point_type != expected_parent
        or parent.side != point.side
        or parent.recursive_level >= point.recursive_level
        or parent.code != point.code
        or parent.source_frequency != point.source_frequency
        or parent.price_basis_revision != point.price_basis_revision
        or parent.available_at > point.available_at
    ):
        return False
    reverse_type = "3buy" if point.side == "buy" else "3sell"
    reverse = tuple(
        candidate
        for related_id in point.related_point_ids
        if related_id != point.parent_point_id
        and (candidate := points_by_id.get(related_id)) is not None
        and candidate.point_type == reverse_type
        and candidate.side == point.side
        and candidate.recursive_level == point.recursive_level - 1
        and candidate.code == point.code
        and candidate.source_frequency == point.source_frequency
        and candidate.price_basis_revision == point.price_basis_revision
        and candidate.available_at <= point.available_at
        and candidate.center_id == point.small_to_large_last_center_id
    )
    return len(reverse) == 1


def structural_point_id_map(
    raw_points,
    *,
    code: str,
    source_frequency: str,
) -> dict[str, str]:
    """Translate strict ids to trading ids without breaking evidence links."""

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
            )
        if len(deferred) == len(pending):
            raise ValueError("strict point parent graph is cyclic")
        pending = deferred
    if len(set(converted.values())) != len(converted):
        raise ValueError("converted structural point identity collision")
    return converted


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

    raw_points = tuple(evidence.confirmed_points)
    converted_ids = structural_point_id_map(
        raw_points,
        code=code,
        source_frequency=source_frequency,
    )
    output: list[StructuralPoint] = []
    for raw in raw_points:
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
                structure_invalidation_price=float(
                    raw.structure_invalidation_price
                ),
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
                    converted_ids[point_id]
                    for point_id in raw.related_point_ids
                ),
                small_to_large_carrier_unit_ids=(
                    raw.small_to_large_carrier_unit_ids
                ),
                small_to_large_last_center_id=(
                    raw.small_to_large_last_center_id
                ),
            )
        )
    return tuple(sorted(output, key=lambda point: (point.available_at, point.point_id)))


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
            point.small_to_large_last_center_id,
        )
        for point in points
    )
