from __future__ import annotations

import hashlib
import json
import math
from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return value


def stable_structure_id(namespace: str, *parts: Any) -> str:
    """Return a process-independent SHA-256 identity for structural facts."""

    if not namespace:
        raise ValueError("namespace is required")
    payload = json.dumps(
        [namespace, *(_json_value(part) for part in parts)],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_center_id(
    *,
    price_basis_revision: str,
    structural_level: int,
    source_kind: str,
    entry_unit_id: str,
    initial_unit_ids: tuple[str, ...],
    establishment_unit_id: str | None,
    zd_tick: int,
    zg_tick: int,
) -> str:
    """Return the immutable identity of a formal center seed."""

    return stable_structure_id(
        "chanlun-center",
        price_basis_revision,
        structural_level,
        source_kind,
        entry_unit_id,
        tuple(initial_unit_ids),
        establishment_unit_id,
        zd_tick,
        zg_tick,
    )


def build_trend_id(
    *,
    price_basis_revision: str,
    structural_level: int,
    center_ids: tuple[str, ...],
    constituent_unit_ids: tuple[str, ...],
    direction: str,
    terminal_divergence_id: str | None = None,
) -> str:
    """Return the immutable identity shared by a trend's state snapshots."""

    return stable_structure_id(
        "chanlun-trend",
        price_basis_revision,
        structural_level,
        tuple(center_ids),
        tuple(constituent_unit_ids),
        direction,
        terminal_divergence_id,
    )


def _canonical_revision_value(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("evidence revision datetimes must be timezone-aware")
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds")
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("evidence revision decimals must be finite")
        normalized = value.normalize()
        return format(normalized, "f")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("evidence revision floats must be finite")
        return 0.0 if value == 0 else value
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _canonical_revision_value(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, (tuple, list)):
        items = [_canonical_revision_value(item) for item in value]
        return sorted(
            items,
            key=lambda item: json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
    if isinstance(value, dict):
        return {
            str(key): _canonical_revision_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError(f"unsupported evidence revision value: {type(value).__name__}")


def _require_unique_ids(values, attribute: str, label: str) -> None:
    identifiers = tuple(getattr(value, attribute) for value in values)
    if any(
        not isinstance(identifier, str) or not identifier for identifier in identifiers
    ):
        raise ValueError(f"{label} requires non-empty ids")
    if len(identifiers) != len(set(identifiers)):
        raise ValueError(f"duplicate {label} id")


def _formal_structure_payload(structure) -> dict:
    levels = []
    _require_unique_ids(
        tuple(
            unit for level in structure.levels for unit in level.units if unit.locked
        ),
        "unit_id",
        "formal unit",
    )
    for level in structure.levels:
        centers = tuple(level.center_result.centers)
        current_trends = tuple(level.trend_types)
        completed_trends = tuple(level.completed_trends)
        decomposition_boundaries = tuple(level.decomposition_boundaries)
        _require_unique_ids(centers, "center_id", "formal center")
        _require_unique_ids(current_trends, "trend_id", "current trend")
        _require_unique_ids(completed_trends, "trend_id", "completed trend")
        _require_unique_ids(
            decomposition_boundaries,
            "boundary_id",
            "decomposition boundary",
        )
        levels.append(
            {
                "structural_level": level.structural_level,
                "locked_units": tuple(unit for unit in level.units if unit.locked),
                "centers": centers,
                "current_trends": current_trends,
                "completed_trends": completed_trends,
                "decomposition_boundaries": decomposition_boundaries,
            }
        )
    return {
        "schema": structure.schema,
        "price_basis_revision": structure.price_basis_revision,
        "levels": tuple(levels),
    }


def build_strict_evidence_revision(
    *,
    symbol: str,
    source_frequency: str,
    price_basis_revision: str,
    strict_config_revision: str,
    structure,
    confirmed_points,
    divergences=(),
) -> str:
    """Hash only formal, executable evidence; projections stay outside."""

    for value, label in (
        (symbol, "symbol"),
        (source_frequency, "source_frequency"),
        (price_basis_revision, "price_basis_revision"),
        (strict_config_revision, "strict_config_revision"),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} is required")
    if structure.price_basis_revision != price_basis_revision:
        raise ValueError("strict evidence structure price basis mismatch")
    points = tuple(confirmed_points)
    _require_unique_ids(points, "point_id", "confirmed point")
    if any(
        getattr(point.status, "value", point.status) != "confirmed" for point in points
    ):
        raise ValueError("formal evidence accepts confirmed points only")
    if any(point.price_basis_revision != price_basis_revision for point in points):
        raise ValueError("strict evidence point price basis mismatch")
    divergence_items = tuple(divergences)
    _require_unique_ids(divergence_items, "divergence_id", "divergence")
    if any(
        item.price_basis_revision != price_basis_revision for item in divergence_items
    ):
        raise ValueError("strict evidence divergence price basis mismatch")
    structure_levels = {level.structural_level for level in structure.levels}
    if any(item.structural_level not in structure_levels for item in divergence_items):
        raise ValueError("strict evidence divergence level is unavailable")
    payload = _canonical_revision_value(
        {
            "schema": "chanlun-strict-evidence",
            "symbol": symbol,
            "source_frequency": source_frequency,
            "price_basis_revision": price_basis_revision,
            "strict_config_revision": strict_config_revision,
            "structure": _formal_structure_payload(structure),
            "confirmed_points": points,
            "divergences": divergence_items,
        }
    )
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()
