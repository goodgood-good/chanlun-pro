"""行情消费者共享的规范价格基准元数据。"""

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
    "ohlc_geometry_normalization",
    "ohlc_geometry_repair_count",
    "ohlc_geometry_max_adjustment",
)
_QMT_ADJUSTMENTS = frozenset(
    {"none", "front", "back", "front_ratio", "back_ratio"}
)
# 缠论结构只使用等比前复权。QMT 的普通前复权会按现金分红做减法，长回看区间内
# 可能产生零价或负价，继而破坏振幅、比例和结构止损语义；等比前复权既消除除权跳空，
# 又始终保留正价格。真实成交边界仍须由调用方显式请求 ``none``。
QMT_STRUCTURE_DIVIDEND_TYPE = "front_ratio"
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
    """在不同或未知价格纪元的数据帧混合前抛出。"""


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
        schema="chanlun-price-basis/qmt",
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
    normalization_revision: str | None = None,
) -> PriceBasisMetadata:
    """为已声明价格方法的行情提供器构建稳定元数据。"""

    values = {
        "provider": provider,
        "market": market,
        "code": code,
        "adjustment": adjustment,
    }
    if any(not isinstance(value, str) or not value.strip() for value in values.values()):
        raise ValueError("provider price basis fields must be non-empty strings")
    if normalization_revision is not None and (
        not isinstance(normalization_revision, str)
        or not normalization_revision.strip()
        or normalization_revision != normalization_revision.strip()
    ):
        raise ValueError("行情规范版本必须是非空且无首尾空白的字符串")
    normalization_ledger = (
        ()
        if normalization_revision is None
        else ({"normalization_revision": normalization_revision},)
    )
    revision = _build_price_basis_revision(
        schema="chanlun-price-basis/provider",
        provider=provider,
        market=market,
        code=code,
        adjustment=adjustment,
        structure_price_quantum=structure_price_quantum,
        adjustment_ledger=normalization_ledger,
    )
    return PriceBasisMetadata(
        structure_price_quantum=structure_price_quantum,
        price_basis_revision=revision,
        provider=provider,
        adjustment=adjustment,
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
    "QMT_STRUCTURE_DIVIDEND_TYPE",
    "PriceBasisMetadata",
    "PriceBasisMismatchError",
    "attach_price_basis_metadata",
    "build_price_basis_revision",
    "build_provider_price_basis_metadata",
    "build_qmt_price_basis_metadata",
    "copy_price_basis_metadata",
    "merge_price_basis_metadata",
)
