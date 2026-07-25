"""Canonical price-basis metadata shared by market-data consumers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
import math

import pandas as pd


_FORMAL_ATTRS = ("structure_price_quantum", "price_basis_revision")
_DIAGNOSTIC_ATTRS = (
    "price_basis_provider",
    "price_basis_adjustment",
    "price_basis_error_code",
)
_QMT_ADJUSTMENTS = frozenset(
    {"none", "front", "back", "front_ratio", "back_ratio"}
)
_QMT_FACTOR_FIELDS = (
    "time",
    "interest",
    "stockBonus",
    "stockGift",
    "allotNum",
    "allotPrice",
    "gugai",
    "dr",
)


class PriceBasisMismatchError(ValueError):
    """Raised before frames from different or unknown price epochs are mixed."""


@dataclass(frozen=True, slots=True)
class PriceBasisMetadata:
    structure_price_quantum: Decimal
    price_basis_revision: str
    provider: str
    adjustment: str


def _canonical_quantum(value: Decimal) -> str:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise ValueError("structure_price_quantum must be a positive finite Decimal")
    return format(value.normalize(), "f")


def _canonical_factor_time(value: object) -> int:
    number = float(value)
    if not math.isfinite(number) or not number.is_integer():
        raise ValueError("QMT factor time must be a finite integer millisecond")
    return int(number)


def _canonical_factor_number(value: object) -> str:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("QMT factor values must be finite")
    return number.hex()


def _canonical_qmt_factors(
    factors: pd.DataFrame | None,
) -> list[dict[str, object]]:
    if factors is None or len(factors) == 0:
        return []
    missing = [name for name in _QMT_FACTOR_FIELDS if name not in factors.columns]
    if missing:
        raise ValueError(f"missing QMT factor fields: {','.join(missing)}")
    rows: list[dict[str, object]] = []
    for row in factors.loc[:, list(_QMT_FACTOR_FIELDS)].to_dict("records"):
        rows.append(
            {
                "time": _canonical_factor_time(row["time"]),
                **{
                    field: _canonical_factor_number(row[field])
                    for field in _QMT_FACTOR_FIELDS
                    if field != "time"
                },
            }
        )
    return sorted(
        rows,
        key=lambda row: json.dumps(
            row,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


def _build_price_basis_revision(
    *,
    schema: str,
    provider: str,
    market: str,
    code: str,
    adjustment: str,
    structure_price_quantum: Decimal,
    adjustment_ledger: Sequence[Mapping[str, object]],
) -> str:
    payload = {
        "schema": schema,
        "provider": provider,
        "market": market,
        "code": code,
        "adjustment": adjustment,
        "structure_price_quantum": _canonical_quantum(
            structure_price_quantum
        ),
        "adjustment_ledger": list(adjustment_ledger),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def build_price_basis_revision(
    *,
    provider: str,
    market: str,
    code: str,
    adjustment: str,
    structure_price_quantum: Decimal,
    adjustment_ledger: Sequence[Mapping[str, object]],
) -> str:
    return _build_price_basis_revision(
        schema="chanlun-price-basis/qmt-v1",
        provider=provider,
        market=market,
        code=code,
        adjustment=adjustment,
        structure_price_quantum=structure_price_quantum,
        adjustment_ledger=adjustment_ledger,
    )


def build_qmt_price_basis_metadata(
    *,
    code: str,
    adjustment: str,
    structure_price_quantum: Decimal,
    factors: pd.DataFrame | None,
) -> PriceBasisMetadata:
    if adjustment not in _QMT_ADJUSTMENTS:
        raise ValueError("unsupported QMT dividend_type for price basis")
    ledger = [] if adjustment == "none" else _canonical_qmt_factors(factors)
    revision = build_price_basis_revision(
        provider="qmt",
        market="a",
        code=code,
        adjustment=adjustment,
        structure_price_quantum=structure_price_quantum,
        adjustment_ledger=ledger,
    )
    return PriceBasisMetadata(
        structure_price_quantum=structure_price_quantum,
        price_basis_revision=revision,
        provider="qmt",
        adjustment=adjustment,
    )


def build_provider_price_basis_metadata(
    *,
    provider: str,
    market: str,
    code: str,
    adjustment: str,
    structure_price_quantum: Decimal,
) -> PriceBasisMetadata:
    """Build stable metadata for a provider with a declared price method."""

    values = {
        "provider": provider,
        "market": market,
        "code": code,
        "adjustment": adjustment,
    }
    if any(not isinstance(value, str) or not value.strip() for value in values.values()):
        raise ValueError("provider price basis fields must be non-empty strings")
    revision = _build_price_basis_revision(
        schema="chanlun-price-basis/provider-v1",
        provider=provider,
        market=market,
        code=code,
        adjustment=adjustment,
        structure_price_quantum=structure_price_quantum,
        adjustment_ledger=(),
    )
    return PriceBasisMetadata(
        structure_price_quantum=structure_price_quantum,
        price_basis_revision=revision,
        provider=provider,
        adjustment=adjustment,
    )


def build_tdx_industry_price_basis_metadata(
    code: str,
    structure_price_quantum: Decimal,
) -> PriceBasisMetadata:
    revision = _build_price_basis_revision(
        schema="chanlun-price-basis/tdx-industry-v1",
        provider="tdx-industry-index",
        market="a",
        code=code,
        adjustment="none",
        structure_price_quantum=structure_price_quantum,
        adjustment_ledger=(),
    )
    return PriceBasisMetadata(
        structure_price_quantum=structure_price_quantum,
        price_basis_revision=revision,
        provider="tdx-industry-index",
        adjustment="none",
    )


def _clear_price_basis_attrs(frame: pd.DataFrame) -> None:
    for name in (*_FORMAL_ATTRS, *_DIAGNOSTIC_ATTRS):
        frame.attrs.pop(name, None)


def attach_price_basis_metadata(
    frame: pd.DataFrame, metadata: PriceBasisMetadata
) -> pd.DataFrame:
    _clear_price_basis_attrs(frame)
    frame.attrs.update(
        structure_price_quantum=_canonical_quantum(
            metadata.structure_price_quantum
        ),
        price_basis_revision=metadata.price_basis_revision,
        price_basis_provider=metadata.provider,
        price_basis_adjustment=metadata.adjustment,
    )
    return frame


def copy_price_basis_metadata(
    source: pd.DataFrame, target: pd.DataFrame
) -> pd.DataFrame:
    _clear_price_basis_attrs(target)
    for name in (*_FORMAL_ATTRS, *_DIAGNOSTIC_ATTRS):
        if name in source.attrs:
            target.attrs[name] = source.attrs[name]
    return target


def merge_price_basis_metadata(
    cached: pd.DataFrame,
    new: pd.DataFrame,
    target: pd.DataFrame,
) -> pd.DataFrame:
    cached_revision = cached.attrs.get("price_basis_revision")
    new_revision = new.attrs.get("price_basis_revision")
    if new_revision is None:
        return copy_price_basis_metadata(new, target)
    if cached_revision is None:
        raise PriceBasisMismatchError(
            "unknown cached price basis cannot be mixed with known new basis"
        )
    if cached_revision != new_revision:
        raise PriceBasisMismatchError(
            f"price basis changed: {cached_revision} -> {new_revision}"
        )
    return copy_price_basis_metadata(new, target)


__all__ = (
    "PriceBasisMetadata",
    "PriceBasisMismatchError",
    "attach_price_basis_metadata",
    "build_price_basis_revision",
    "build_provider_price_basis_metadata",
    "build_qmt_price_basis_metadata",
    "build_tdx_industry_price_basis_metadata",
    "copy_price_basis_metadata",
    "merge_price_basis_metadata",
)
