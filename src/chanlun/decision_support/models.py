from __future__ import annotations

from dataclasses import dataclass, field, fields
from datetime import datetime
from enum import Enum
import math
from numbers import Integral, Real
import re
from typing import Any, Mapping

from .fingerprints import (
    build_event_id,
    normalize_datetime,
    sha256_json,
    to_jsonable,
)


class StrategyTrack(str, Enum):
    TREND_CONTINUATION = "trend_continuation"
    BOTTOM_REVERSAL = "bottom_reversal"


class EventState(str, Enum):
    DETECTED = "detected"
    RISK_CHECKED = "risk_checked"
    REVIEW_PENDING = "review_pending"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    ABSTAINED = "abstained"
    EXPIRED = "expired"
    INVALIDATED = "invalidated"
    ACTED = "acted"


_FINGERPRINT_RE = re.compile(r"sha256:[0-9a-f]{64}")
_RULE_BINDING_FIELDS = frozenset(
    {
        "rule_id",
        "rule_card_version",
        "rule_card_fingerprint",
        "rule_set_fingerprint",
        "corpus_manifest_fingerprint",
        "algorithm_fingerprint",
    }
)
_STRATEGY_RUN_BINDING_FIELDS = frozenset(
    {
        "strategy_run_id",
        "strategy_run_epoch",
        "strategy_run_fingerprint",
    }
)


def _require_non_empty_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _require_non_negative_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return value


def _require_bool(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} must be boolean")
    return value


def normalize_finite_float(
    value: object, field_name: str, *, optional: bool = True
) -> float | None:
    if value is None:
        if optional:
            return None
        raise ValueError(f"{field_name} must be a finite number")
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{field_name} must be a finite number")
    if isinstance(value, Integral):
        try:
            converted = float(value)
        except OverflowError as exc:
            raise ValueError(
                f"{field_name} must be a finite number"
            ) from exc
        if not math.isfinite(converted) or int(converted) != int(value):
            raise ValueError(f"{field_name} must be a finite number")
        return converted
    try:
        converted = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a finite number") from exc
    if not math.isfinite(converted):
        raise ValueError(f"{field_name} must be a finite number")
    return converted


def _finite(
    value: object, field_name: str, *, optional: bool = True
) -> float | None:
    return normalize_finite_float(value, field_name, optional=optional)

def _string_tuple(value: object, field_name: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (tuple, list)):
        raise ValueError(
            f"{field_name} must be a sequence of non-empty strings"
        )
    result = tuple(value)
    if not all(isinstance(item, str) and item for item in result):
        raise ValueError(
            f"{field_name} must be a sequence of non-empty strings"
        )
    return result


def _require_fingerprint(value: object) -> str:
    if not isinstance(value, str) or _FINGERPRINT_RE.fullmatch(value) is None:
        raise ValueError("fingerprints must use sha256:<64 lowercase hex>")
    return value

def _parse_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be an ISO-8601 string")
    normalized_value = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized_value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 string") from exc
    return normalize_datetime(parsed, field_name)


def _require_mapping_fields(
    value: Mapping[str, object],
    expected: frozenset[str],
    label: str,
) -> None:
    if set(value) != expected:
        raise ValueError(f"{label} fields mismatch")

@dataclass(frozen=True, slots=True)
class SignalSnapshot:
    bs_type: str
    signal_at: datetime
    level: int
    price: float
    first_visible_bar: int
    structural_stop_below: float | None
    structural_stop_above: float | None
    zs_zd: float | None
    zs_zg: float | None
    nest_operable: bool | None = None
    nest_depth: int = 0
    divergence_kind: str | None = None
    live_divergence: bool = False
    confirmation_bs_type: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "signal_at",
            normalize_datetime(self.signal_at, "signal_at"),
        )
        _require_non_empty_string(self.bs_type, "bs_type")
        _require_non_negative_int(self.level, "level")
        _require_non_negative_int(self.first_visible_bar, "first_visible_bar")
        _require_non_negative_int(self.nest_depth, "nest_depth")
        if self.nest_operable is not None:
            _require_bool(self.nest_operable, "nest_operable")
        _require_bool(self.live_divergence, "live_divergence")
        for field_name in ("divergence_kind", "confirmation_bs_type"):
            value = getattr(self, field_name)
            if value is not None:
                _require_non_empty_string(value, field_name)
        if self.live_divergence and self.divergence_kind != "qs":
            raise ValueError("inconsistent signal divergence metadata")
        if self.confirmation_bs_type is not None and not self.live_divergence:
            raise ValueError("inconsistent signal confirmation metadata")
        numeric_fields = (
            "price",
            "structural_stop_below",
            "structural_stop_above",
            "zs_zd",
            "zs_zg",
        )
        for field_name in numeric_fields:
            object.__setattr__(
                self,
                field_name,
                _finite(
                    getattr(self, field_name),
                    field_name,
                    optional=field_name != "price",
                ),
            )

@dataclass(frozen=True, slots=True)
class LevelSnapshot:
    frequency: str
    level: int
    direction: str
    completed: bool
    segment_start: float | None
    segment_end: float | None
    zs_zd: float | None
    zs_zg: float | None
    mmds: tuple[str, ...] = ()
    divergences: tuple[str, ...] = ()
    source_frequency: str | None = field(
        default=None,
        metadata={"canonical_omit_if_none": True},
    )
    source_bar_closed_at: datetime | None = field(
        default=None,
        metadata={"canonical_omit_if_none": True},
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "mmds", _string_tuple(self.mmds, "mmds"))
        object.__setattr__(
            self, "divergences", _string_tuple(self.divergences, "divergences")
        )
        _require_non_empty_string(self.frequency, "frequency")
        _require_non_empty_string(self.direction, "direction")
        if self.direction not in {"up", "down", "neutral"}:
            raise ValueError("direction must be up, down, or neutral")
        _require_non_negative_int(self.level, "level")
        _require_bool(self.completed, "completed")
        source_values = (self.source_frequency, self.source_bar_closed_at)
        if any(value is not None for value in source_values) and not all(
            value is not None for value in source_values
        ):
            raise ValueError("level source binding must be complete or absent")
        if self.source_frequency is not None:
            _require_non_empty_string(
                self.source_frequency,
                "source_frequency",
            )
            object.__setattr__(
                self,
                "source_bar_closed_at",
                normalize_datetime(
                    self.source_bar_closed_at,
                    "source_bar_closed_at",
                ),
            )
        for field_name in ("segment_start", "segment_end", "zs_zd", "zs_zg"):
            object.__setattr__(
                self,
                field_name,
                _finite(getattr(self, field_name), field_name),
            )

@dataclass(frozen=True, slots=True)
class MarketConstraints:
    board: str
    lot: int
    t_plus: int
    limit_pct: float | None
    entry_tradable: bool
    exit_tradable: bool
    quote_time: datetime
    limit_up_locked: bool = False
    limit_down_locked: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "quote_time",
            normalize_datetime(self.quote_time, "quote_time"),
        )
        _require_non_empty_string(self.board, "board")
        if (
            isinstance(self.lot, bool)
            or not isinstance(self.lot, int)
            or self.lot <= 0
        ):
            raise ValueError("lot must be a positive integer")
        _require_non_negative_int(self.t_plus, "t_plus")
        for field_name in (
            "entry_tradable",
            "exit_tradable",
            "limit_up_locked",
            "limit_down_locked",
        ):
            _require_bool(getattr(self, field_name), field_name)
        object.__setattr__(
            self,
            "limit_pct",
            _finite(self.limit_pct, "limit_pct"),
        )
        if self.limit_pct is not None and not 0 < self.limit_pct < 1:
            raise ValueError("limit_pct must be between zero and one")

def _event_frequency(
    event_id: str,
    market: str,
    code: str,
    observed_at: datetime,
    level: int,
    bs_type: str,
    signal_fingerprint: str,
    provenance_fingerprint: str | None,
) -> str:
    stamp = observed_at.isoformat(timespec="seconds")
    prefix = f"{market}:{code}:"
    suffix = f":{stamp}:L{level}:{bs_type}:S{signal_fingerprint[7:]}"
    if provenance_fingerprint is not None:
        suffix += f":P{provenance_fingerprint[7:]}"
    if not event_id.startswith(prefix) or not event_id.endswith(suffix):
        raise ValueError("event_id does not match event facts")
    frequency = event_id[len(prefix) : len(event_id) - len(suffix)]
    try:
        expected = build_event_id(
            market,
            code,
            frequency,
            observed_at,
            level,
            bs_type,
            signal_fingerprint,
            provenance_fingerprint,
        )
    except ValueError as exc:
        raise ValueError("event_id does not match event facts") from exc
    if event_id != expected:
        raise ValueError("event_id does not match event facts")
    return frequency


@dataclass(frozen=True, slots=True)
class DecisionEvent:
    event_id: str
    market: str
    code: str
    name: str
    observed_at: datetime
    bar_closed_at: datetime
    strategy_track: StrategyTrack
    signal: SignalSnapshot
    levels: tuple[LevelSnapshot, ...]
    market_constraints: MarketConstraints
    data_fingerprint: str
    config_fingerprint: str
    rule_id: str | None = None
    rule_card_version: int | None = None
    rule_card_fingerprint: str | None = None
    rule_set_fingerprint: str | None = None
    corpus_manifest_fingerprint: str | None = None
    algorithm_fingerprint: str | None = None
    strategy_run_id: str | None = field(
        default=None,
        metadata={"canonical_omit_if_none": True},
    )
    strategy_run_epoch: int | None = field(
        default=None,
        metadata={"canonical_omit_if_none": True},
    )
    strategy_run_fingerprint: str | None = field(
        default=None,
        metadata={"canonical_omit_if_none": True},
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "strategy_track", StrategyTrack(self.strategy_track))
        object.__setattr__(self, "levels", tuple(self.levels))
        object.__setattr__(
            self,
            "observed_at",
            normalize_datetime(self.observed_at, "observed_at"),
        )
        object.__setattr__(
            self,
            "bar_closed_at",
            normalize_datetime(self.bar_closed_at, "bar_closed_at"),
        )
        if self.bar_closed_at > self.observed_at:
            raise ValueError("bar_closed_at cannot be after observed_at")
        for field_name in ("event_id", "market", "code", "name"):
            _require_non_empty_string(getattr(self, field_name), field_name)
        _require_fingerprint(self.data_fingerprint)
        _require_fingerprint(self.config_fingerprint)
        binding_values = tuple(
            getattr(self, field_name) for field_name in _RULE_BINDING_FIELDS
        )
        if any(value is not None for value in binding_values) and not all(
            value is not None for value in binding_values
        ):
            raise ValueError("rule binding must be complete or absent")
        if self.rule_id is not None:
            _require_non_empty_string(self.rule_id, "rule_id")
            if (
                isinstance(self.rule_card_version, bool)
                or not isinstance(self.rule_card_version, int)
                or self.rule_card_version <= 0
            ):
                raise ValueError("rule_card_version must be a positive integer")
            for field_name in (
                "rule_card_fingerprint",
                "rule_set_fingerprint",
                "corpus_manifest_fingerprint",
                "algorithm_fingerprint",
            ):
                _require_fingerprint(getattr(self, field_name))
        strategy_run_binding_values = tuple(
            getattr(self, field_name)
            for field_name in _STRATEGY_RUN_BINDING_FIELDS
        )
        if any(
            value is not None for value in strategy_run_binding_values
        ) and not all(
            value is not None for value in strategy_run_binding_values
        ):
            raise ValueError(
                "strategy-run binding must be complete or absent"
            )
        if self.strategy_run_id is not None:
            if self.rule_binding_status != "bound":
                raise ValueError("strategy-bound event must be rule-bound")
            _require_non_empty_string(
                self.strategy_run_id,
                "strategy_run_id",
            )
            if (
                isinstance(self.strategy_run_epoch, bool)
                or not isinstance(self.strategy_run_epoch, int)
                or self.strategy_run_epoch <= 0
            ):
                raise ValueError(
                    "strategy_run_epoch must be a positive integer"
                )
            _require_fingerprint(self.strategy_run_fingerprint)
        if not isinstance(self.signal, SignalSnapshot):
            raise TypeError("signal must be SignalSnapshot")
        if self.signal.signal_at > self.bar_closed_at:
            raise ValueError("signal_at cannot be after bar_closed_at")
        if not isinstance(self.market_constraints, MarketConstraints):
            raise TypeError("market_constraints must be MarketConstraints")
        if not all(isinstance(level, LevelSnapshot) for level in self.levels):
            raise TypeError("levels must contain LevelSnapshot values")
        source_bound = tuple(
            level.source_frequency is not None for level in self.levels
        )
        if any(source_bound) and not all(source_bound):
            raise ValueError(
                "event level source bindings must be complete or absent"
            )
        if all(source_bound) and self.levels:
            if any(
                level.source_bar_closed_at > self.bar_closed_at
                for level in self.levels
            ):
                raise ValueError("source bar cannot be after event bar")
            source_watermarks: dict[str, set[datetime]] = {}
            for level in self.levels:
                source_watermarks.setdefault(
                    level.source_frequency,
                    set(),
                ).add(level.source_bar_closed_at)
            if any(len(values) != 1 for values in source_watermarks.values()):
                raise ValueError(
                    "one physical source must use one bar watermark"
                )
        level_keys = [
            (
                level.source_frequency or level.frequency,
                level.level,
            )
            for level in self.levels
        ]
        if len(set(level_keys)) != len(level_keys):
            raise ValueError(
                "level snapshots must have unique frequency and level"
            )
        if any(not level.completed for level in self.levels):
            raise ValueError("event levels must use completed bars")
        frequency = _event_frequency(
            self.event_id,
            self.market,
            self.code,
            self.observed_at,
            self.signal.level,
            self.signal.bs_type,
            sha256_json(self.signal),
            self.provenance_fingerprint,
        )
        if sum(
            level.frequency == frequency and level.level == self.signal.level
            for level in self.levels
        ) != 1:
            raise ValueError(
                "event_id frequency and level must match event levels"
            )
        if self.market_constraints.quote_time > self.observed_at:
            raise ValueError("quote_time cannot be after observed_at")


    def to_dict(self) -> dict[str, Any]:
        value = to_jsonable(self)
        if not isinstance(value, dict):
            raise TypeError("event serialization must produce a mapping")
        if self.strategy_run_binding_status == "legacy_unbound":
            for field_name in _STRATEGY_RUN_BINDING_FIELDS:
                value.pop(field_name, None)
        if self.rule_binding_status == "legacy_unbound":
            for field_name in _RULE_BINDING_FIELDS:
                value.pop(field_name)
            return {"schema_version": 2, **value}
        if self.strategy_run_binding_status == "legacy_unbound":
            return {"schema_version": 3, **value}
        return {"schema_version": 4, **value}

    @property
    def rule_binding_status(self) -> str:
        return "legacy_unbound" if self.rule_id is None else "bound"

    @property
    def strategy_run_binding_status(self) -> str:
        return (
            "legacy_unbound"
            if self.strategy_run_id is None
            else "bound"
        )

    @property
    def rule_provenance_fingerprint(self) -> str | None:
        if self.rule_id is None:
            return None
        return sha256_json(
            {
                "algorithm_fingerprint": self.algorithm_fingerprint,
                "config_fingerprint": self.config_fingerprint,
                "corpus_manifest_fingerprint": self.corpus_manifest_fingerprint,
                "rule_card_fingerprint": self.rule_card_fingerprint,
                "rule_card_version": self.rule_card_version,
                "rule_id": self.rule_id,
                "rule_set_fingerprint": self.rule_set_fingerprint,
            }
        )

    @property
    def provenance_fingerprint(self) -> str | None:
        rule_provenance = self.rule_provenance_fingerprint
        if (
            rule_provenance is None
            or self.strategy_run_binding_status == "legacy_unbound"
        ):
            return rule_provenance
        return sha256_json(
            {
                "schema_version": 1,
                "rule_provenance_fingerprint": rule_provenance,
                "strategy_run": {
                    "strategy_run_id": self.strategy_run_id,
                    "strategy_run_epoch": self.strategy_run_epoch,
                    "strategy_run_fingerprint": (
                        self.strategy_run_fingerprint
                    ),
                },
            }
        )

    @property
    def signal_frequency(self) -> str:
        return _event_frequency(
            self.event_id,
            self.market,
            self.code,
            self.observed_at,
            self.signal.level,
            self.signal.bs_type,
            sha256_json(self.signal),
            self.provenance_fingerprint,
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> DecisionEvent:
        if not isinstance(value, Mapping):
            raise ValueError("event must be a mapping")
        schema_version = value.get("schema_version")
        if type(schema_version) is not int or schema_version not in {2, 3, 4}:
            raise ValueError("unsupported event schema_version")
        model_fields = frozenset(field.name for field in fields(cls))
        event_fields = model_fields | {"schema_version"}
        if schema_version == 2:
            event_fields -= _RULE_BINDING_FIELDS
        if schema_version in {2, 3}:
            event_fields -= _STRATEGY_RUN_BINDING_FIELDS
        _require_mapping_fields(value, event_fields, "event")

        signal_value = value["signal"]
        levels_value = value["levels"]
        constraints_value = value["market_constraints"]
        if not isinstance(signal_value, Mapping):
            raise ValueError("signal must be a mapping")
        _require_mapping_fields(
            signal_value,
            frozenset(field.name for field in fields(SignalSnapshot)),
            "signal",
        )
        parsed_signal = dict(signal_value)
        parsed_signal["signal_at"] = _parse_datetime(
            parsed_signal["signal_at"],
            "signal_at",
        )
        if not isinstance(levels_value, list):
            raise ValueError("levels must be a list")
        level_fields = frozenset(field.name for field in fields(LevelSnapshot))
        source_level_fields = frozenset(
            {"source_frequency", "source_bar_closed_at"}
        )
        legacy_level_fields = level_fields - source_level_fields
        level_mappings: list[Mapping[str, object]] = []
        for item in levels_value:
            if not isinstance(item, Mapping):
                raise ValueError("levels must contain mappings")
            item_fields = frozenset(item)
            if item_fields not in {legacy_level_fields, level_fields}:
                raise ValueError("level fields mismatch")
            parsed_level = dict(item)
            if item_fields == level_fields:
                parsed_level["source_bar_closed_at"] = _parse_datetime(
                    parsed_level["source_bar_closed_at"],
                    "source_bar_closed_at",
                )
            level_mappings.append(parsed_level)
        if not isinstance(constraints_value, Mapping):
            raise ValueError("market_constraints must be a mapping")
        _require_mapping_fields(
            constraints_value,
            frozenset(field.name for field in fields(MarketConstraints)),
            "market_constraints",
        )

        constraints = dict(constraints_value)
        constraints["quote_time"] = _parse_datetime(
            constraints["quote_time"], "quote_time"
        )
        binding: dict[str, object] = {}
        if schema_version in {3, 4}:
            binding = {
                field_name: value[field_name]
                for field_name in _RULE_BINDING_FIELDS
            }
        if schema_version == 4:
            binding.update(
                {
                    field_name: value[field_name]
                    for field_name in _STRATEGY_RUN_BINDING_FIELDS
                }
            )
        return cls(
            event_id=_require_non_empty_string(value["event_id"], "event_id"),
            market=_require_non_empty_string(value["market"], "market"),
            code=_require_non_empty_string(value["code"], "code"),
            name=_require_non_empty_string(value["name"], "name"),
            observed_at=_parse_datetime(value["observed_at"], "observed_at"),
            bar_closed_at=_parse_datetime(value["bar_closed_at"], "bar_closed_at"),
            strategy_track=StrategyTrack(
                _require_non_empty_string(value["strategy_track"], "strategy_track")
            ),
            signal=SignalSnapshot(**parsed_signal),
            levels=tuple(
                LevelSnapshot(**dict(item)) for item in level_mappings
            ),
            market_constraints=MarketConstraints(**constraints),
            data_fingerprint=_require_fingerprint(value["data_fingerprint"]),
            config_fingerprint=_require_fingerprint(value["config_fingerprint"]),
            **binding,
        )
