"""Strategy-neutral immutable structure snapshots.

These DTOs are retained because paper-account and exit-audit infrastructure
consume frozen Chanlun facts.  They contain no candidate-selection policy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import re
from types import MappingProxyType
from typing import Mapping

from chanlun.recursive_bt.engine.engine import Signal

from .fingerprints import normalize_datetime


_FINGERPRINT_RE = re.compile(r"sha256:[0-9a-f]{64}")
SIGNAL_OBSERVATION_STATES = frozenset(
    {
        "trusted_first_seen",
        "baseline_not_fresh",
        "quarantined_unknown",
    }
)


@dataclass(frozen=True, slots=True)
class InvalidationNotice:
    event_id: str
    reason: str

    def __post_init__(self) -> None:
        for field_name in ("event_id", "reason"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{field_name} must be a non-empty string")


@dataclass(frozen=True, slots=True)
class SymbolStructureSnapshot:
    frequency: str
    cd: object
    signals: tuple[Signal, ...]
    first_visible_bar: int
    completed_bars: tuple[Mapping[str, object], ...]
    config: Mapping[str, object]
    operation_bar_closed: bool
    fund_ok: bool
    comparison_ok: bool
    invalidations: tuple[InvalidationNotice, ...] = ()
    current_cycle_id: str | None = None
    signals_first_observed_at: Mapping[str, datetime] = field(
        default_factory=dict
    )
    signal_observation_states: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.frequency, str) or not self.frequency:
            raise ValueError("frequency must be a non-empty string")
        object.__setattr__(self, "signals", tuple(self.signals))
        object.__setattr__(self, "completed_bars", tuple(self.completed_bars))
        object.__setattr__(self, "invalidations", tuple(self.invalidations))
        if not all(isinstance(item, Signal) for item in self.signals):
            raise TypeError("signals must contain Signal values")
        if not all(
            isinstance(item, InvalidationNotice)
            for item in self.invalidations
        ):
            raise TypeError(
                "invalidations must contain InvalidationNotice values"
            )
        if (
            isinstance(self.first_visible_bar, bool)
            or not isinstance(self.first_visible_bar, int)
            or self.first_visible_bar < 0
        ):
            raise ValueError("first_visible_bar must be non-negative")
        if not isinstance(self.config, Mapping):
            raise TypeError("config must be a mapping")
        if self.current_cycle_id is not None and (
            not isinstance(self.current_cycle_id, str)
            or _FINGERPRINT_RE.fullmatch(self.current_cycle_id) is None
        ):
            raise ValueError(
                "current_cycle_id must use sha256:<64 lowercase hex>"
            )

        if not isinstance(self.signals_first_observed_at, Mapping):
            raise TypeError("signals_first_observed_at must be a mapping")
        observations: dict[str, datetime] = {}
        for fingerprint, observed_at in self.signals_first_observed_at.items():
            if (
                not isinstance(fingerprint, str)
                or _FINGERPRINT_RE.fullmatch(fingerprint) is None
            ):
                raise ValueError(
                    "signals_first_observed_at keys must be fingerprints"
                )
            observations[fingerprint] = normalize_datetime(
                observed_at,
                "signals_first_observed_at value",
            )
        object.__setattr__(
            self,
            "signals_first_observed_at",
            MappingProxyType(observations),
        )

        if not isinstance(self.signal_observation_states, Mapping):
            raise TypeError("signal_observation_states must be a mapping")
        states = dict(self.signal_observation_states)
        if not states and observations:
            states = {
                fingerprint: "trusted_first_seen"
                for fingerprint in observations
            }
        if (
            any(
                not isinstance(fingerprint, str)
                or _FINGERPRINT_RE.fullmatch(fingerprint) is None
                for fingerprint in states
            )
            or any(
                state not in SIGNAL_OBSERVATION_STATES
                for state in states.values()
            )
            or not set(observations).issubset(states)
            or any(
                (
                    state == "quarantined_unknown"
                    and fingerprint in observations
                )
                or (
                    state != "quarantined_unknown"
                    and fingerprint not in observations
                )
                for fingerprint, state in states.items()
            )
        ):
            raise ValueError("signal observation state bindings are invalid")
        object.__setattr__(
            self,
            "signal_observation_states",
            MappingProxyType(states),
        )

        for field_name in (
            "operation_bar_closed",
            "fund_ok",
            "comparison_ok",
        ):
            if type(getattr(self, field_name)) is not bool:
                raise ValueError(f"{field_name} must be boolean")


__all__ = (
    "InvalidationNotice",
    "SIGNAL_OBSERVATION_STATES",
    "SymbolStructureSnapshot",
)
