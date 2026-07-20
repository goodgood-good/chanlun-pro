from __future__ import annotations

from datetime import datetime
from typing import Literal, cast

from chanlun.core.beichi_calculator import is_qs
from chanlun.core.types import Config
from chanlun.decision_support.fingerprints import normalize_datetime, sha256_json
from chanlun.decision_support.trading_system.models import (
    PointType,
    PointVariant,
    StructuralPoint,
    StructureTower,
    build_point_id,
)


_POINT_TYPES = {"1buy", "2buy", "3buy", "1sell", "2sell", "3sell"}
_VARIANTS = {"standard", "strict", "weak_divergence", "boundary_touch"}


def _center_boundary_lines(raw_zone: object) -> tuple[object, object]:
    body = tuple(getattr(raw_zone, "lines", None) or ())
    start_line = getattr(raw_zone, "start", None) or (body[0] if body else None)
    end_line = getattr(raw_zone, "end", None) or (body[-1] if body else None)
    if (
        start_line is None
        or end_line is None
        or getattr(start_line, "start", None) is None
        or getattr(end_line, "end", None) is None
    ):
        raise ValueError("confirmed center requires stable boundary lines")
    return start_line, end_line


def center_id(raw_zone: object, *, tower: StructureTower, level: int) -> str:
    start_line, end_line = _center_boundary_lines(raw_zone)
    start = start_line.start.k.date
    end = end_line.end.k.date
    return sha256_json(
        {
            "schema": "chanlun-center/v1",
            "tower": tower,
            "level": level,
            "start": normalize_datetime(start, "center.start").isoformat(),
            "end": normalize_datetime(end, "center.end").isoformat(),
            "zd": float(raw_zone.zd),
            "zg": float(raw_zone.zg),
        }
    )


def center_ordinal(
    *,
    zones: tuple[object, ...],
    target: object,
    direction: Literal["up", "down"],
    wzgx: str,
) -> int | None:
    completed = tuple(
        zone
        for zone in zones
        if getattr(zone, "done", False) is True
        and getattr(zone, "real", True) is not False
    )
    position = next(
        (index for index, zone in enumerate(completed) if zone is target),
        None,
    )
    if position is None:
        return None
    ordinal = 1
    for index in range(position, 0, -1):
        if (
            is_qs(
                completed[index - 1],
                completed[index],
                wzgx,
                use_core_envelope=True,
            )
            != direction
        ):
            break
        ordinal += 1
    return ordinal


def _confirmation_time(raw: object) -> datetime:
    _start_line, end_line = _center_boundary_lines(raw.zs)
    dependencies = (
        raw.anchor_fx.k.date,
        raw.signal_seg.end.k.date,
        end_line.end.k.date,
    )
    return max(
        normalize_datetime(value, "point dependency")
        for value in dependencies
    )


def _structural_stop(raw: object) -> float:
    point_type = raw.bs_type
    stop = (
        raw.structural_stop_below
        if point_type.endswith("buy")
        else raw.structural_stop_above
    )
    if stop is not None:
        return float(stop)
    if point_type == "3buy":
        return float(raw.zs.zg)
    if point_type == "3sell":
        return float(raw.zs.zd)
    if point_type in {"1buy", "1sell"}:
        return float(raw.anchor_fx.val)
    raise ValueError("second point requires an explicit structural stop")


def _wzgx(cd: object) -> str:
    provider = getattr(cd, "_recursive_wzgx", None)
    if callable(provider):
        return str(provider())
    return Config.ZS_WZGX_GD.value


def extract_confirmed_points(
    cd: object,
    *,
    code: str,
    source_frequency: str,
    as_of: datetime,
) -> tuple[StructuralPoint, ...]:
    closed_at = normalize_datetime(as_of, "as_of")
    output: list[StructuralPoint] = []
    tower_specs: tuple[tuple[StructureTower, bool], ...] = (
        ("bi", False),
        ("xd", True),
    )
    for tower, use_xd in tower_specs:
        levels = tuple(cd.get_recursive_branch_levels_for_tower(use_xd=use_xd))
        levels_by_number = {
            int(level_result.level): level_result for level_result in levels
        }
        raw_points = tuple(cd.get_branch_bspoints(use_xd=use_xd))
        for raw in raw_points:
            if raw.bs_type not in _POINT_TYPES:
                raise ValueError(f"unsupported structural point type: {raw.bs_type}")
            point_type = cast(PointType, raw.bs_type)
            anchor_at = normalize_datetime(raw.anchor_fx.k.date, "anchor_at")
            confirmed_at = _confirmation_time(raw)
            if anchor_at > closed_at or confirmed_at > closed_at:
                raise ValueError("confirmed point dependency cannot be after as_of")
            level = 0 if raw.level is None else int(raw.level)
            zone_key = center_id(raw.zs, tower=tower, level=level)
            raw_variant = getattr(raw, "definition_variant", "standard")
            if raw_variant not in _VARIANTS:
                raise ValueError(f"unsupported point variant: {raw_variant}")
            level_result = levels_by_number.get(level)
            ordinal = None
            if point_type in {"3buy", "3sell"} and level_result is not None:
                ordinal = center_ordinal(
                    zones=tuple(level_result.zss),
                    target=raw.zs,
                    direction="up" if point_type == "3buy" else "down",
                    wzgx=_wzgx(cd),
                )
            point_id = build_point_id(
                code=code,
                point_type=point_type,
                source_frequency=source_frequency,
                tower=tower,
                recursive_level=level,
                anchor_at=anchor_at,
                center_id=zone_key,
                parent_point_id=None,
            )
            divergence = getattr(raw, "divergence", None)
            output.append(
                StructuralPoint(
                    point_id=point_id,
                    code=code,
                    point_type=point_type,
                    side="buy" if point_type.endswith("buy") else "sell",
                    status="confirmed",
                    variant=cast(PointVariant, raw_variant),
                    source_frequency=source_frequency,
                    tower=tower,
                    recursive_level=level,
                    anchor_at=anchor_at,
                    confirmed_at=confirmed_at,
                    anchor_price=float(raw.anchor_fx.val),
                    invalidation_price=_structural_stop(raw),
                    center_id=zone_key,
                    center_zd=float(raw.zs.zd),
                    center_zg=float(raw.zs.zg),
                    center_ordinal=ordinal,
                    divergence_kind=(
                        None if divergence is None else divergence.kind
                    ),
                    parent_point_id=None,
                    evidence_codes=("core_confirmed_point",),
                )
            )
    return tuple(
        sorted(
            output,
            key=lambda point: (
                point.anchor_at,
                point.tower,
                point.recursive_level,
                point.point_type,
            ),
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
            point.variant,
            point.center_id,
            point.invalidation_price,
        )
        for point in points
    )
