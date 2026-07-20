from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from numbers import Real
from types import MappingProxyType
from typing import Mapping

from .fingerprints import normalize_datetime


@dataclass(frozen=True, slots=True)
class ReplayBar:
    frequency: str
    closed_at: datetime
    available_at: datetime
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        if not isinstance(self.frequency, str) or not self.frequency:
            raise ValueError("frequency must be a non-empty string")
        object.__setattr__(
            self,
            "closed_at",
            normalize_datetime(self.closed_at, "closed_at"),
        )
        object.__setattr__(
            self,
            "available_at",
            normalize_datetime(self.available_at, "available_at"),
        )
        if self.available_at < self.closed_at:
            raise ValueError("bar cannot be available before it closes")
        if not isinstance(self.payload, Mapping):
            raise TypeError("payload must be a mapping")
        payload = dict(self.payload)
        timestamp_fields = tuple(
            field_name for field_name in ("time", "date") if field_name in payload
        )
        if not timestamp_fields:
            raise ValueError("payload must include time or date")
        for field_name in timestamp_fields:
            payload[field_name] = normalize_datetime(
                payload[field_name],
                f"payload {field_name}",
            )
        if (
            "time" in payload
            and "date" in payload
            and payload["time"] != payload["date"]
        ):
            raise ValueError("payload time and date must describe the same instant")
        for field_name in timestamp_fields:
            if payload[field_name] > self.closed_at:
                raise ValueError(f"payload {field_name} cannot be later than closed_at")
            if payload[field_name] < self.closed_at:
                raise ValueError(
                    f"payload {field_name} must match the closed_at endpoint label"
                )
        if "closed" in payload and payload["closed"] is not True:
            raise ValueError("payload closed must be boolean True")
        for field_name in ("open", "high", "low", "close", "volume"):
            if field_name not in payload:
                continue
            value = payload[field_name]
            if isinstance(value, bool) or not isinstance(value, Real):
                raise ValueError(f"payload {field_name} must be a real number")
            try:
                normalized_value = float(value)
            except (TypeError, ValueError, OverflowError):
                raise ValueError(
                    f"payload {field_name} must be a finite real number"
                ) from None
            if not isfinite(normalized_value):
                raise ValueError(f"payload {field_name} must be finite")
            payload[field_name] = normalized_value
        object.__setattr__(self, "payload", MappingProxyType(payload))
