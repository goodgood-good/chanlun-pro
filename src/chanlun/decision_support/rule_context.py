from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from .fingerprints import sha256_json
from .manual_checks import (
    ManualCheckAudit,
    ManualCheckSnapshot,
    RuleEvaluationContext,
)
from .models import DecisionEvent, normalize_finite_float
from .rule_cards import derive_structural_stop_breached


def _optional_bool(value: object, field_name: str) -> bool | None:
    if value is not None and not isinstance(value, bool):
        raise ValueError(f"{field_name} must be boolean or null")
    return value


@dataclass(frozen=True, slots=True)
class LevelEvaluationFacts:
    frequency: str
    level: int
    completed_bar_count: int
    latest_bar_closed: bool

    def __post_init__(self) -> None:
        if not isinstance(self.frequency, str) or not self.frequency:
            raise ValueError("frequency must be a non-empty string")
        if isinstance(self.level, bool) or not isinstance(self.level, int) or self.level < 0:
            raise ValueError("level must be a non-negative integer")
        if (
            isinstance(self.completed_bar_count, bool)
            or not isinstance(self.completed_bar_count, int)
            or self.completed_bar_count < 0
        ):
            raise ValueError("completed_bar_count must be a non-negative integer")
        if not isinstance(self.latest_bar_closed, bool):
            raise ValueError("latest_bar_closed must be boolean")


@dataclass(frozen=True, slots=True)
class RuleRuntimeFacts:
    fundamental_ok: bool | None = None
    comparison_ok: bool | None = None
    market_liquid: bool | None = None
    risk_allowed: bool | None = None
    latest_price: float | None = None
    level_facts: tuple[LevelEvaluationFacts, ...] = ()
    manual_checks: tuple[ManualCheckSnapshot, ...] = ()

    def __post_init__(self) -> None:
        for field_name in (
            "fundamental_ok",
            "comparison_ok",
            "market_liquid",
            "risk_allowed",
        ):
            _optional_bool(getattr(self, field_name), field_name)
        object.__setattr__(
            self,
            "latest_price",
            normalize_finite_float(self.latest_price, "latest_price"),
        )
        if isinstance(self.level_facts, (str, bytes)) or not isinstance(
            self.level_facts, Sequence
        ):
            raise ValueError("level_facts must be a sequence")
        values = tuple(self.level_facts)
        if not all(isinstance(item, LevelEvaluationFacts) for item in values):
            raise ValueError("level_facts must contain LevelEvaluationFacts")
        object.__setattr__(self, "level_facts", values)
        if isinstance(self.manual_checks, (str, bytes)) or not isinstance(
            self.manual_checks, Sequence
        ):
            raise ValueError(
                "manual_checks must contain ManualCheckSnapshot"
            )
        manual_checks = tuple(self.manual_checks)
        if not all(type(item) is ManualCheckSnapshot for item in manual_checks):
            raise ValueError(
                "manual_checks must contain ManualCheckSnapshot"
            )
        object.__setattr__(self, "manual_checks", manual_checks)


def _frozen_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    return MappingProxyType(dict(value))


def build_rule_evaluation_context(
    event: DecisionEvent,
    runtime_facts: RuleRuntimeFacts,
) -> RuleEvaluationContext:
    if not isinstance(event, DecisionEvent):
        raise TypeError("event must be a DecisionEvent")
    if type(runtime_facts) is not RuleRuntimeFacts:
        raise TypeError("runtime_facts must be RuleRuntimeFacts")

    relevant_levels = event.levels
    if event.levels and all(
        snapshot.source_frequency is not None for snapshot in event.levels
    ):
        signal_snapshot = next(
            snapshot
            for snapshot in event.levels
            if snapshot.frequency == event.signal_frequency
            and snapshot.level == event.signal.level
        )
        relevant_levels = tuple(
            snapshot
            for snapshot in event.levels
            if snapshot.source_frequency == signal_snapshot.source_frequency
        )

    event_levels: dict[int, object] = {}
    event_keys: set[tuple[str, int]] = set()
    for snapshot in relevant_levels:
        if snapshot.level in event_levels:
            raise ValueError(f"ambiguous numeric level: {snapshot.level}")
        event_levels[snapshot.level] = snapshot
        event_keys.add((snapshot.frequency, snapshot.level))

    facts_by_key: dict[tuple[str, int], LevelEvaluationFacts] = {}
    for facts in runtime_facts.level_facts:
        key = (facts.frequency, facts.level)
        if key in facts_by_key:
            raise ValueError("duplicate level runtime facts")
        if key not in event_keys:
            raise ValueError("level runtime facts do not match event levels")
        facts_by_key[key] = facts

    level_values: dict[str, object] = {}
    for level, snapshot in sorted(event_levels.items()):
        facts = facts_by_key.get((snapshot.frequency, snapshot.level))
        level_values[str(level)] = _frozen_mapping(
            {
                "completed_bar_count": (
                    facts.completed_bar_count if facts is not None else None
                ),
                "direction": snapshot.direction,
                "latest_bar_closed": (
                    facts.latest_bar_closed if facts is not None else None
                ),
                "mmds": snapshot.mmds,
            }
        )

    if "buy" in event.signal.bs_type:
        is_tradeable: bool | None = event.market_constraints.entry_tradable
    elif "sell" in event.signal.bs_type:
        is_tradeable = event.market_constraints.exit_tradable
    else:
        is_tradeable = None
    stop_breached = derive_structural_stop_breached(
        bs_type=event.signal.bs_type,
        latest_price=runtime_facts.latest_price,
        stop_below=event.signal.structural_stop_below,
        stop_above=event.signal.structural_stop_above,
    )
    values = _frozen_mapping(
        {
            "comparison": _frozen_mapping({"ok": runtime_facts.comparison_ok}),
            "fundamental": _frozen_mapping({"ok": runtime_facts.fundamental_ok}),
            "levels": _frozen_mapping(level_values),
            "market": _frozen_mapping(
                {
                    "is_tradeable": is_tradeable,
                    "liquid": runtime_facts.market_liquid,
                }
            ),
            "risk": _frozen_mapping(
                {
                    "allowed": runtime_facts.risk_allowed,
                    "stop_breached": stop_breached,
                }
            ),
            "signal": _frozen_mapping(
                {
                    "bs_type": event.signal.bs_type,
                    "confirmation_bs_type": event.signal.confirmation_bs_type,
                    "divergence_kind": event.signal.divergence_kind,
                    "level": event.signal.level,
                    "live_divergence": event.signal.live_divergence,
                    "nest_depth": event.signal.nest_depth,
                    "nest_operable": event.signal.nest_operable,
                    "price": event.signal.price,
                    "structural_stop_above": event.signal.structural_stop_above,
                    "structural_stop_below": event.signal.structural_stop_below,
                    "zs_zd": event.signal.zs_zd,
                    "zs_zg": event.signal.zs_zg,
                }
            ),
        }
    )
    manual_check_input_fingerprint = sha256_json(
        {
            "derived_context": values,
            "event_inputs": {
                "bar_closed_at": event.bar_closed_at,
                "code": event.code,
                "config_fingerprint": event.config_fingerprint,
                "levels": event.levels,
                "market": event.market,
                "market_constraints": event.market_constraints,
                "name": event.name,
                "observed_at": event.observed_at,
                "signal": event.signal,
                "strategy_track": event.strategy_track,
            },
            "runtime_facts": {
                "comparison_ok": runtime_facts.comparison_ok,
                "fundamental_ok": runtime_facts.fundamental_ok,
                "latest_price": runtime_facts.latest_price,
                "level_facts": runtime_facts.level_facts,
                "market_liquid": runtime_facts.market_liquid,
                "risk_allowed": runtime_facts.risk_allowed,
            },
        }
    )
    manual_check_audit = ManualCheckAudit(
        event_id=event.event_id,
        context_fingerprint=manual_check_input_fingerprint,
        snapshots=runtime_facts.manual_checks,
    )
    if any(
        snapshot.recorded_at < event.observed_at
        for snapshot in manual_check_audit.snapshots
    ):
        raise ValueError("manual check predates event")
    data_fingerprint = sha256_json(
        {
            "manual_check_audit": manual_check_audit,
            "manual_check_input_fingerprint": manual_check_input_fingerprint,
        }
    )
    return RuleEvaluationContext(
        data_fingerprint=data_fingerprint,
        manual_check_audit=manual_check_audit,
        _values=values,
    )
