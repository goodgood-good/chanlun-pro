from __future__ import annotations

from dataclasses import fields, is_dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
import hashlib
import json
import math
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo


_MARKET_TIMEZONE = ZoneInfo("Asia/Shanghai")
_OMIT_IF_NONE_METADATA_KEY = "canonical_omit_if_none"


def require_aware_datetime(value: datetime, field_name: str) -> None:
    if not isinstance(value, datetime):
        raise ValueError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def normalize_datetime(value: datetime, field_name: str) -> datetime:
    require_aware_datetime(value, field_name)
    return value.astimezone(_MARKET_TIMEZONE)


def _decimal_components(value: Decimal) -> list[object]:
    if not value.is_finite():
        raise ValueError("decimal values must be finite")
    decimal_tuple = value.as_tuple()
    digits = list(decimal_tuple.digits)
    exponent = decimal_tuple.exponent
    if not isinstance(exponent, int):
        raise ValueError("decimal values must be finite")
    while digits and digits[-1] == 0:
        digits.pop()
        exponent += 1
    if not digits:
        return [0, "0", 0]
    return [decimal_tuple.sign, "".join(str(digit) for digit in digits), exponent]


def to_jsonable(value: object) -> object:
    if isinstance(value, Enum):
        return to_jsonable(value.value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("floating-point values must be finite")
        return int(value) if value.is_integer() else value
    if isinstance(value, Decimal):
        return {"$decimal": _decimal_components(value)}
    if isinstance(value, datetime):
        return normalize_datetime(value, "datetime").isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: to_jsonable(getattr(value, field.name))
            for field in fields(value)
            if not (
                getattr(value, field.name) is None
                and field.metadata.get(_OMIT_IF_NONE_METADATA_KEY) is True
            )
        }
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("canonical mappings require string keys")
        return {key: to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [to_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"unsupported canonical value: {type(value).__name__}")


def _canonical_value(value: object) -> object:
    if isinstance(value, Enum):
        return _canonical_value(value.value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("floating-point values must be finite")
        return int(value) if value.is_integer() else value
    if isinstance(value, Decimal):
        return {"$decimal": _decimal_components(value)}
    if isinstance(value, datetime):
        return {
            "$datetime": normalize_datetime(value, "datetime").isoformat()
        }
    if is_dataclass(value) and not isinstance(value, type):
        value = {
            field.name: getattr(value, field.name)
            for field in fields(value)
            if not (
                getattr(value, field.name) is None
                and field.metadata.get(_OMIT_IF_NONE_METADATA_KEY) is True
            )
        }
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("canonical mappings require string keys")
        return {
            "$map": [
                [key, _canonical_value(value[key])]
                for key in sorted(value)
            ]
        }
    if isinstance(value, (tuple, list)):
        return {"$sequence": [_canonical_value(item) for item in value]}
    if isinstance(value, Path):
        return {"$path": str(value)}
    raise TypeError(f"unsupported canonical value: {type(value).__name__}")


def canonical_json(value: object) -> str:
    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_json(value: object) -> str:
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"
