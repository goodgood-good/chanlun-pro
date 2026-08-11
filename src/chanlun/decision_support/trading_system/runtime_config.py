from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, DecimalException
import hashlib
import json

from chanlun.core.strict_structure.base_profile import (
    strict_base_config,
    strict_base_config_revision,
)


STRICT_STRATEGY_ID = "chanlun_source_faithful"
SCREENING_STRUCTURE_PROFILE_ID = "chanlun-screening-strict-l0"
RECURSIVE_STRUCTURE_PROFILE_ID = "chanlun-strict-recursive"


@dataclass(frozen=True, slots=True)
class StrictSnapshotPriceMetadata:
    structure_price_quantum: Decimal
    price_basis_revision: str


def _validated_quantum(value: object) -> Decimal:
    if not isinstance(value, Decimal):
        raise ValueError("structure_price_quantum must be a positive finite Decimal")
    if not value.is_finite() or value <= 0:
        raise ValueError("structure_price_quantum must be a positive finite Decimal")
    return value


def _validated_basis(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("price_basis_revision is required")
    return value


def _canonical_decimal(value: Decimal) -> str:
    return format(value.normalize(), "f")


def strict_runtime_config_revision(
    *,
    structure_price_quantum: Decimal,
    price_basis_revision: str,
) -> str:
    quantum = _validated_quantum(structure_price_quantum)
    basis = _validated_basis(price_basis_revision)
    payload = {
        "base_revision": strict_base_config_revision(),
        "structure_price_quantum": _canonical_decimal(quantum),
        "price_basis_revision": basis,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _screening_base_config() -> dict[str, object]:
    """Return the fixed early-screening structure profile.

    Every runtime shares the strict base algorithm.  This profile changes only
    the evidence scope: every physical K-line frequency consumes its own
    segment-sourced level zero and recursive trend-type levels are contextual,
    not tradable, evidence.
    """

    result: dict[str, object] = dict(strict_base_config())
    result.update(
        {
            "strict_runtime_scope_profile_id": SCREENING_STRUCTURE_PROFILE_ID,
            "screening_structure_scope": "physical-timeframe-level-zero",
            "screening_center_source": "segment",
            "screening_recursive_structure": False,
            "screening_unfinished_segment_participates": True,
        }
    )
    return result


def _recursive_base_config() -> dict[str, object]:
    """Return the sole recursive profile admissible to the strict strategy.

    strict strategy changes only the visibility scope.  Its stroke, segment, center,
    divergence and point definitions are inherited unchanged from the same
    base profile used by physical-timeframe screening.
    """

    result: dict[str, object] = dict(strict_base_config())
    result.update(
        {
            "strict_runtime_scope_profile_id": RECURSIVE_STRUCTURE_PROFILE_ID,
            "strategy_id": STRICT_STRATEGY_ID,
            "recursive_structure_scope": "same-source-direct-recursion",
        }
    )
    return result


def screening_base_config_revision() -> str:
    encoded = json.dumps(
        _screening_base_config(),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def recursive_base_config_revision() -> str:
    encoded = json.dumps(
        _recursive_base_config(),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def screening_runtime_config_revision(
    *,
    structure_price_quantum: Decimal,
    price_basis_revision: str,
) -> str:
    quantum = _validated_quantum(structure_price_quantum)
    basis = _validated_basis(price_basis_revision)
    payload = {
        "base_revision": screening_base_config_revision(),
        "structure_price_quantum": _canonical_decimal(quantum),
        "price_basis_revision": basis,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def recursive_runtime_config_revision(
    *,
    structure_price_quantum: Decimal,
    price_basis_revision: str,
) -> str:
    quantum = _validated_quantum(structure_price_quantum)
    basis = _validated_basis(price_basis_revision)
    payload = {
        "base_revision": recursive_base_config_revision(),
        "structure_price_quantum": _canonical_decimal(quantum),
        "price_basis_revision": basis,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def strict_cl_config(
    *,
    structure_price_quantum: Decimal,
    price_basis_revision: str,
) -> dict[str, object]:
    quantum = _validated_quantum(structure_price_quantum)
    basis = _validated_basis(price_basis_revision)
    result: dict[str, object] = dict(strict_base_config())
    result["structure_price_quantum"] = _canonical_decimal(quantum)
    result["price_basis_revision"] = basis
    result["strict_base_profile_revision"] = strict_base_config_revision()
    result["strict_config_revision"] = strict_runtime_config_revision(
        structure_price_quantum=quantum,
        price_basis_revision=basis,
    )
    return result


def screening_cl_config(
    *,
    structure_price_quantum: Decimal,
    price_basis_revision: str,
) -> dict[str, object]:
    """Build the immutable CL configuration used only by early screening."""

    quantum = _validated_quantum(structure_price_quantum)
    basis = _validated_basis(price_basis_revision)
    result = _screening_base_config()
    result["structure_price_quantum"] = _canonical_decimal(quantum)
    result["price_basis_revision"] = basis
    result["strict_base_profile_revision"] = strict_base_config_revision()
    result["strict_runtime_scope_profile_revision"] = (
        screening_base_config_revision()
    )
    result["strict_config_revision"] = screening_runtime_config_revision(
        structure_price_quantum=quantum,
        price_basis_revision=basis,
    )
    return result


def recursive_cl_config(
    *,
    structure_price_quantum: Decimal,
    price_basis_revision: str,
) -> dict[str, object]:
    """Build strict strategy's immutable old-pen direct-recursion configuration."""

    quantum = _validated_quantum(structure_price_quantum)
    basis = _validated_basis(price_basis_revision)
    result = _recursive_base_config()
    result["structure_price_quantum"] = _canonical_decimal(quantum)
    result["price_basis_revision"] = basis
    result["strict_base_profile_revision"] = strict_base_config_revision()
    result["strict_runtime_scope_profile_revision"] = (
        recursive_base_config_revision()
    )
    result["strict_config_revision"] = recursive_runtime_config_revision(
        structure_price_quantum=quantum,
        price_basis_revision=basis,
    )
    return result


def strict_snapshot_price_metadata(snapshot: object) -> StrictSnapshotPriceMetadata:
    attrs = getattr(snapshot, "attrs", None)
    if not isinstance(attrs, Mapping):
        raise ValueError("data snapshot attrs metadata is required")
    raw_quantum = attrs.get("structure_price_quantum")
    if raw_quantum is None:
        raise ValueError("structure_price_quantum metadata is required")
    try:
        quantum = Decimal(str(raw_quantum))
    except (DecimalException, ValueError) as exc:
        raise ValueError(
            "structure_price_quantum metadata must be a positive finite decimal"
        ) from exc
    try:
        quantum = _validated_quantum(quantum)
    except ValueError as exc:
        raise ValueError(
            "structure_price_quantum metadata must be a positive finite decimal"
        ) from exc
    if "price_basis_revision" not in attrs:
        raise ValueError("price_basis_revision metadata is required")
    try:
        basis = _validated_basis(attrs["price_basis_revision"])
    except ValueError as exc:
        raise ValueError("price_basis_revision metadata is required") from exc
    return StrictSnapshotPriceMetadata(
        structure_price_quantum=quantum,
        price_basis_revision=basis,
    )


__all__ = (
    "SCREENING_STRUCTURE_PROFILE_ID",
    "STRICT_STRATEGY_ID",
    "RECURSIVE_STRUCTURE_PROFILE_ID",
    "StrictSnapshotPriceMetadata",
    "screening_base_config_revision",
    "screening_cl_config",
    "screening_runtime_config_revision",
    "strict_cl_config",
    "strict_runtime_config_revision",
    "strict_snapshot_price_metadata",
    "recursive_base_config_revision",
    "recursive_cl_config",
    "recursive_runtime_config_revision",
)
