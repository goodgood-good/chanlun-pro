"""Fail-closed, analysis-only runtime adapter for tracked position exits.

This module freezes the current structural facts, derives exit triggers, and
delegates sizing/tradability decisions to :mod:`chanlun.decision_support.exits`.
It deliberately has no execution, broker, exchange, trader, or LLM dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import re
from types import MappingProxyType
from typing import Callable, Iterable, Mapping

from chanlun.recursive_bt.engine.engine import SELLS, Signal

from .event_factory import snapshot_levels
from .exit_evidence_policy import ExitEvidencePolicy
from .exits import (
    ExitOutcome,
    ExitSelection,
    ExitSignalSnapshot,
    ExitTrigger,
    TriggerEvidence,
    evaluate_exit_intent,
    select_exit_intent,
)
from .fingerprints import normalize_datetime, sha256_json, to_jsonable
from .models import DecisionEvent, LevelSnapshot, StrategyTrack
from .risk import HoldingSnapshot, RiskContext
from .scanner import SIGNAL_OBSERVATION_STATES, SymbolStructureSnapshot


_FINGERPRINT_RE = re.compile(r"sha256:[0-9a-f]{64}")
_FREQUENCY_RE = re.compile(r"([1-9][0-9]*)(m|h|d)")
_TIME_FIELDS = ("closed_at", "time", "date", "datetime", "timestamp")
_TRIGGER_ORDER = (
    ExitTrigger.HARD_RISK,
    ExitTrigger.STRUCTURAL_INVALIDATION,
    ExitTrigger.CONTROL_LEVEL_DOWN,
    ExitTrigger.CONTROL_LEVEL_SELL,
    ExitTrigger.OPERATION_LEVEL_SELL,
)
_SELL_STRENGTH = {"1sell": 3, "2sell": 2, "3sell": 1}
EXIT_ALGORITHM_VERSION = "chanlun-exit-runtime-v3"
EXIT_EVALUATION_VERSION = 3


EvidenceResolver = ExitEvidencePolicy


def _require_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field_name} must be a non-empty trimmed string")
    return value


def _require_fingerprint(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _FINGERPRINT_RE.fullmatch(value) is None:
        raise ValueError(
            f"{field_name} must use sha256:<64 lowercase hex>"
        )
    return value


def _require_positive_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _require_string_tuple(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, tuple) or not value:
        raise ValueError(f"{field_name} must be a non-empty tuple")
    for item in value:
        _require_string(item, f"{field_name} item")
    if len(value) != len(set(value)):
        raise ValueError(f"{field_name} must contain unique values")
    return value


def _json_mapping(value: object, subject: str) -> dict[str, object]:
    result = to_jsonable(value)
    if not isinstance(result, dict):
        raise TypeError(f"{subject} serialization must produce a mapping")
    return result


def build_exit_evaluation_cycle_id(
    *,
    code: str,
    frequency: str,
    bar_closed_at: datetime,
    structure_source_fingerprint: str,
) -> str:
    """Build the immutable identity of one right-side evaluation cycle."""

    _require_string(code, "code")
    _require_string(frequency, "frequency")
    _require_fingerprint(
        structure_source_fingerprint,
        "structure_source_fingerprint",
    )
    normalized_bar = normalize_datetime(bar_closed_at, "bar_closed_at")
    return sha256_json(
        {
            "code": code,
            "frequency": frequency,
            "bar_closed_at": normalized_bar,
            "structure_source_fingerprint": structure_source_fingerprint,
        }
    )


class ExitRuntimeError(ValueError):
    """Stable fail-closed error used by the per-position isolation boundary."""

    def __init__(self, reason: str, detail: str) -> None:
        self.reason = _require_string(reason, "reason")
        self.detail = _require_string(detail, "detail")
        super().__init__(f"{self.reason}: {self.detail}")


def _reject(reason: str, detail: str) -> None:
    raise ExitRuntimeError(reason, detail)


@dataclass(frozen=True, slots=True)
class TrackedPosition:
    """A paper position cryptographically linked to its entry decision."""

    entry_event_id: str
    entry_data_fingerprint: str
    entry_review_id: str
    entry_risk_snapshot_id: str
    entry_paper_admission_id: str
    paper_fill_ids: tuple[str, ...]
    paper_ledger_revision: int
    lot_provenance_fingerprint: str
    strategy_track: StrategyTrack
    holding: HoldingSnapshot

    def __post_init__(self) -> None:
        _require_string(self.entry_event_id, "entry_event_id")
        _require_fingerprint(
            self.entry_data_fingerprint,
            "entry_data_fingerprint",
        )
        _require_string(self.entry_review_id, "entry_review_id")
        _require_string(self.entry_risk_snapshot_id, "entry_risk_snapshot_id")
        _require_fingerprint(
            self.entry_paper_admission_id,
            "entry_paper_admission_id",
        )
        object.__setattr__(
            self,
            "paper_fill_ids",
            _require_string_tuple(self.paper_fill_ids, "paper_fill_ids"),
        )
        _require_positive_int(self.paper_ledger_revision, "paper_ledger_revision")
        _require_fingerprint(
            self.lot_provenance_fingerprint,
            "lot_provenance_fingerprint",
        )
        try:
            track = StrategyTrack(self.strategy_track)
        except (TypeError, ValueError) as exc:
            raise ValueError("strategy_track must be a known strategy track") from exc
        object.__setattr__(self, "strategy_track", track)
        if not isinstance(self.holding, HoldingSnapshot):
            raise TypeError("holding must be HoldingSnapshot")

    @property
    def entry_provenance_fingerprint(self) -> str:
        return sha256_json(
            {
                "entry_event_id": self.entry_event_id,
                "entry_data_fingerprint": self.entry_data_fingerprint,
                "entry_review_id": self.entry_review_id,
                "entry_risk_snapshot_id": self.entry_risk_snapshot_id,
                "entry_paper_admission_id": self.entry_paper_admission_id,
                "paper_fill_ids": self.paper_fill_ids,
                "paper_ledger_revision": self.paper_ledger_revision,
                "lot_provenance_fingerprint": self.lot_provenance_fingerprint,
                "strategy_track": self.strategy_track,
                "holding": self.holding,
            }
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 2,
            **_json_mapping(self, "tracked position"),
            "entry_provenance_fingerprint": self.entry_provenance_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class AuthoritativeEntryLink:
    """Canonical paper-ledger record linking a holding to its entry event."""

    position: TrackedPosition
    entry_event: DecisionEvent
    entry_review_id: str
    entry_risk_snapshot_id: str
    entry_paper_admission_id: str
    paper_fill_ids: tuple[str, ...]
    paper_ledger_revision: int
    lot_provenance_fingerprint: str

    def __post_init__(self) -> None:
        if not isinstance(self.position, TrackedPosition):
            raise TypeError("position must be TrackedPosition")
        if not isinstance(self.entry_event, DecisionEvent):
            raise TypeError("entry_event must be DecisionEvent")
        if self.position.entry_event_id != self.entry_event.event_id:
            raise ValueError("authoritative entry_event_id mismatch")
        if (
            self.position.entry_data_fingerprint
            != self.entry_event.data_fingerprint
        ):
            raise ValueError("authoritative entry data fingerprint mismatch")
        if self.position.strategy_track is not self.entry_event.strategy_track:
            raise ValueError("authoritative strategy track mismatch")
        if self.position.holding.code != self.entry_event.code:
            raise ValueError("authoritative holding code mismatch")
        for field_name in (
            "entry_review_id",
            "entry_risk_snapshot_id",
            "entry_paper_admission_id",
            "paper_fill_ids",
            "paper_ledger_revision",
            "lot_provenance_fingerprint",
        ):
            if getattr(self, field_name) != getattr(self.position, field_name):
                raise ValueError(f"authoritative {field_name} mismatch")


EntryLedgerResolver = Callable[
    [TrackedPosition], AuthoritativeEntryLink | None
]


@dataclass(frozen=True, slots=True)
class ExitEvaluationRequest:
    position: TrackedPosition
    entry_event: DecisionEvent
    structure: SymbolStructureSnapshot
    risk_context: RiskContext
    bar_closed_at: datetime
    evaluation_cycle_id: str
    max_signal_confirmation_lag_seconds: int = 600

    def __post_init__(self) -> None:
        if not isinstance(self.position, TrackedPosition):
            raise TypeError("position must be TrackedPosition")
        if not isinstance(self.entry_event, DecisionEvent):
            raise TypeError("entry_event must be DecisionEvent")
        if not isinstance(self.structure, SymbolStructureSnapshot):
            raise TypeError("structure must be SymbolStructureSnapshot")
        if not isinstance(self.risk_context, RiskContext):
            raise TypeError("risk_context must be RiskContext")
        object.__setattr__(
            self,
            "bar_closed_at",
            normalize_datetime(self.bar_closed_at, "bar_closed_at"),
        )
        _require_fingerprint(self.evaluation_cycle_id, "evaluation_cycle_id")
        _require_positive_int(
            self.max_signal_confirmation_lag_seconds,
            "max_signal_confirmation_lag_seconds",
        )


@dataclass(frozen=True, slots=True)
class ExitRecommendation:
    entry_event_id: str
    code: str
    strategy_track: StrategyTrack
    evaluation_cycle_id: str
    entry_provenance_fingerprint: str
    exit_evidence_policy_fingerprint: str
    certified_corpus_manifest_fingerprint: str
    source_pdf_fingerprint: str
    bar_structure_payload_fingerprint: str
    risk_context_payload_fingerprint: str
    quote_payload_fingerprint: str
    algorithm_version: str
    evaluation_version: int
    max_signal_confirmation_lag_seconds: int
    signal_observation_states: Mapping[str, str]
    signal_observation_suppressions: Mapping[str, str]
    signal_snapshot: ExitSignalSnapshot
    selection: ExitSelection
    outcome: ExitOutcome | None

    def __post_init__(self) -> None:
        _require_string(self.entry_event_id, "entry_event_id")
        _require_string(self.code, "code")
        try:
            track = StrategyTrack(self.strategy_track)
        except (TypeError, ValueError) as exc:
            raise ValueError("strategy_track must be a known strategy track") from exc
        object.__setattr__(self, "strategy_track", track)
        _require_fingerprint(self.evaluation_cycle_id, "evaluation_cycle_id")
        for field_name in (
            "entry_provenance_fingerprint",
            "exit_evidence_policy_fingerprint",
            "certified_corpus_manifest_fingerprint",
            "source_pdf_fingerprint",
            "bar_structure_payload_fingerprint",
            "risk_context_payload_fingerprint",
            "quote_payload_fingerprint",
        ):
            _require_fingerprint(getattr(self, field_name), field_name)
        _require_string(self.algorithm_version, "algorithm_version")
        _require_positive_int(self.evaluation_version, "evaluation_version")
        _require_positive_int(
            self.max_signal_confirmation_lag_seconds,
            "max_signal_confirmation_lag_seconds",
        )
        if not isinstance(self.signal_observation_states, Mapping):
            raise TypeError("signal_observation_states must be a mapping")
        observation_states = dict(self.signal_observation_states)
        if any(
            not isinstance(signal_fingerprint, str)
            or _FINGERPRINT_RE.fullmatch(signal_fingerprint) is None
            or state not in SIGNAL_OBSERVATION_STATES
            for signal_fingerprint, state in observation_states.items()
        ):
            raise ValueError("signal_observation_states is invalid")
        object.__setattr__(
            self,
            "signal_observation_states",
            MappingProxyType(observation_states),
        )
        if not isinstance(self.signal_observation_suppressions, Mapping):
            raise TypeError("signal_observation_suppressions must be a mapping")
        suppressions = dict(self.signal_observation_suppressions)
        for signal_fingerprint, reason in suppressions.items():
            state = observation_states.get(signal_fingerprint)
            if (
                not isinstance(signal_fingerprint, str)
                or _FINGERPRINT_RE.fullmatch(signal_fingerprint) is None
                or not isinstance(reason, str)
                or reason
                not in {
                    "baseline_not_fresh",
                    "quarantined_unknown",
                    "stale_signal_observation",
                }
                or (
                    reason == "stale_signal_observation"
                    and state != "trusted_first_seen"
                )
                or (
                    reason != "stale_signal_observation"
                    and state != reason
                )
            ):
                raise ValueError("signal_observation_suppressions is invalid")
        object.__setattr__(
            self,
            "signal_observation_suppressions",
            MappingProxyType(dict(sorted(suppressions.items()))),
        )
        if not isinstance(self.signal_snapshot, ExitSignalSnapshot):
            raise TypeError("signal_snapshot must be ExitSignalSnapshot")
        if not isinstance(self.selection, ExitSelection):
            raise TypeError("selection must be ExitSelection")
        if self.outcome is not None and not isinstance(self.outcome, ExitOutcome):
            raise TypeError("outcome must be ExitOutcome or None")
        intent = self.selection.intent
        if intent is None:
            if self.outcome is not None:
                raise ValueError("no-op recommendation cannot contain outcome")
        else:
            if intent.entry_event_id != self.entry_event_id:
                raise ValueError("selection entry link mismatch")
            if intent.code != self.code:
                raise ValueError("selection code mismatch")
            if self.outcome is None or self.outcome.intent_id != intent.intent_id:
                raise ValueError("selection and outcome mismatch")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 3,
            **_json_mapping(self, "exit recommendation"),
        }


@dataclass(frozen=True, slots=True)
class ExitRuntimeFailure:
    entry_event_id: str
    code: str
    reason: str
    detail: str

    def __post_init__(self) -> None:
        for field_name in ("entry_event_id", "code", "reason", "detail"):
            _require_string(getattr(self, field_name), field_name)

    def to_dict(self) -> dict[str, object]:
        return _json_mapping(self, "exit runtime failure")


@dataclass(frozen=True, slots=True)
class ExitBatchResult:
    recommendations: tuple[ExitRecommendation, ...]
    failures: tuple[ExitRuntimeFailure, ...]

    def __post_init__(self) -> None:
        recommendations = tuple(self.recommendations)
        failures = tuple(self.failures)
        if not all(
            isinstance(item, ExitRecommendation) for item in recommendations
        ):
            raise TypeError("recommendations must contain ExitRecommendation")
        if not all(isinstance(item, ExitRuntimeFailure) for item in failures):
            raise TypeError("failures must contain ExitRuntimeFailure")
        object.__setattr__(self, "recommendations", recommendations)
        object.__setattr__(self, "failures", failures)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            **_json_mapping(self, "exit batch result"),
        }


def _validate_entry_link(
    tracked: TrackedPosition,
    event: DecisionEvent,
    observed_at: datetime,
) -> None:
    if tracked.entry_event_id != event.event_id:
        _reject("entry_event_id_mismatch", "tracked entry event is not supplied event")
    if tracked.entry_data_fingerprint != event.data_fingerprint:
        _reject(
            "entry_data_fingerprint_mismatch",
            "tracked data fingerprint is not supplied event fingerprint",
        )
    if tracked.strategy_track is not event.strategy_track:
        _reject("strategy_track_mismatch", "tracked strategy track changed")
    if tracked.holding.code != event.code:
        _reject("holding_code_mismatch", "holding code differs from entry event")
    if event.market != "a":
        _reject("unsupported_market", "exit runtime supports A shares only")
    if event.observed_at > observed_at or event.bar_closed_at > observed_at:
        _reject("future_entry_event", "entry event is after current bar")
    if tracked.holding.opened_at < event.observed_at:
        _reject("holding_predates_entry", "holding opened before its entry event")
    if tracked.holding.opened_at > observed_at:
        _reject("future_holding", "holding opened after current bar")


def _resolve_authoritative_entry(
    request: ExitEvaluationRequest,
    resolver: EntryLedgerResolver,
) -> AuthoritativeEntryLink:
    try:
        link = resolver(request.position)
    except Exception as exc:
        _reject("authoritative_entry_resolution_failed", str(exc))
    if link is None:
        _reject(
            "authoritative_entry_link_missing",
            "paper ledger has no entry link for the holding",
        )
    if not isinstance(link, AuthoritativeEntryLink):
        _reject(
            "invalid_authoritative_entry_link",
            "ledger resolver must return AuthoritativeEntryLink or None",
        )
    identity_fields = (
        "entry_event_id",
        "entry_data_fingerprint",
        "entry_review_id",
        "entry_risk_snapshot_id",
        "entry_paper_admission_id",
        "paper_fill_ids",
        "paper_ledger_revision",
        "lot_provenance_fingerprint",
        "strategy_track",
        "holding",
    )
    if (
        link.entry_event != request.entry_event
        or any(
            getattr(link.position, field_name)
            != getattr(request.position, field_name)
            for field_name in identity_fields
        )
    ):
        _reject(
            "authoritative_entry_link_mismatch",
            "request differs from the canonical paper-ledger entry link",
        )
    return link


def _bar_time(
    bar: object,
    index: int,
    observed_at: datetime,
) -> datetime:
    if not isinstance(bar, Mapping):
        _reject("invalid_completed_bar", "completed bars must be mappings")
    keys = tuple(key for key in _TIME_FIELDS if key in bar)
    if len(keys) != 1:
        _reject(
            "invalid_completed_bar",
            "completed bar must contain exactly one time field",
        )
    try:
        bar_time = normalize_datetime(
            bar[keys[0]],
            f"completed_bars[{index}].time",
        )
    except (TypeError, ValueError) as exc:
        _reject("invalid_completed_bar", str(exc))
    if bar_time > observed_at:
        _reject("future_completed_bar", "completed bar is after current bar")
    return bar_time


def _validate_completed_bars(
    bars: tuple[Mapping[str, object], ...],
    observed_at: datetime,
) -> None:
    previous: datetime | None = None
    for index, bar in enumerate(bars):
        current = _bar_time(bar, index, observed_at)
        if previous is not None and current <= previous:
            _reject(
                "invalid_completed_bar_order",
                "completed bars must be strictly chronological",
            )
        previous = current
    if previous != observed_at:
        _reject("non_current_bar", "latest completed bar is not current bar")


def _freeze_current_levels(
    structure: SymbolStructureSnapshot,
    event: DecisionEvent,
    observed_at: datetime,
) -> tuple[LevelSnapshot, ...]:
    if not structure.operation_bar_closed:
        _reject("operation_bar_open", "operation bar is not closed")
    if structure.frequency != event.signal_frequency:
        _reject("structure_frequency_mismatch", "structure frequency changed")
    if not structure.completed_bars:
        _reject("missing_completed_bar", "completed bar history is empty")
    _validate_completed_bars(structure.completed_bars, observed_at)

    try:
        levels = snapshot_levels(structure.cd)
    except (AttributeError, TypeError, ValueError) as exc:
        _reject("invalid_frozen_levels", str(exc))
    if not levels:
        _reject("incomplete_levels", "current frozen levels are empty")
    if any(not level.completed for level in levels):
        _reject("incomplete_levels", "current frozen level is incomplete")
    numeric_levels = tuple(level.level for level in levels)
    if len(set(numeric_levels)) != len(numeric_levels):
        _reject("ambiguous_levels", "current levels contain duplicate level numbers")
    current_keys = {(level.frequency, level.level) for level in levels}
    entry_keys = {(level.frequency, level.level) for level in event.levels}
    missing = sorted(entry_keys - current_keys)
    if missing:
        _reject("incomplete_levels", f"entry levels missing from current snapshot: {missing}")
    return levels


def _validate_current_context(
    tracked: TrackedPosition,
    context: RiskContext,
    observed_at: datetime,
) -> None:
    if context.asof != observed_at:
        _reject("non_current_risk_context", "risk context is not current bar")
    if context.quote.quote_time != observed_at:
        _reject("non_current_quote", "quote is not current bar")
    if context.quote.code != tracked.holding.code:
        _reject("quote_code_mismatch", "quote code differs from tracked holding")
    matches = tuple(
        holding
        for holding in context.holdings
        if holding.code == tracked.holding.code
    )
    if len(matches) != 1:
        _reject("position_not_in_context", "risk context lacks unique holding")
    if matches[0] != tracked.holding:
        _reject("position_snapshot_mismatch", "risk holding is not tracked holding")


def _decimal_signal_price(signal: Signal) -> Decimal:
    try:
        value = Decimal(str(signal.price))
    except (InvalidOperation, TypeError, ValueError) as exc:
        _reject("invalid_sell_signal", f"sell signal price is invalid: {exc}")
    if not value.is_finite() or value <= 0:
        _reject("invalid_sell_signal", "sell signal price must be finite and positive")
    return value


def _cycle_started_at(frequency: str, observed_at: datetime) -> datetime:
    match = _FREQUENCY_RE.fullmatch(frequency)
    if match is None:
        _reject(
            "invalid_structure_frequency",
            "structure frequency cannot define a bounded evaluation cycle",
        )
    amount = int(match.group(1))
    unit = match.group(2)
    if unit == "m":
        duration = timedelta(minutes=amount)
    elif unit == "h":
        duration = timedelta(hours=amount)
    else:
        duration = timedelta(days=amount)
    return observed_at - duration


def _fresh_sell_signals(
    structure: SymbolStructureSnapshot,
    observed_at: datetime,
    known_levels: frozenset[int],
    evaluation_cycle_id: str,
    max_confirmation_lag_seconds: int,
) -> tuple[tuple[Signal, ...], Mapping[str, str]]:
    if structure.current_cycle_id != evaluation_cycle_id:
        _reject(
            "evaluation_cycle_mismatch",
            "structure is not bound to the current evaluation cycle",
        )
    cycle_started_at = _cycle_started_at(structure.frequency, observed_at)
    signal_fingerprints = {
        sha256_json(signal): signal for signal in structure.signals
    }
    unknown_observations = set(structure.signals_first_observed_at).difference(
        signal_fingerprints
    )
    unknown_states = set(structure.signal_observation_states).difference(
        signal_fingerprints
    )
    if unknown_observations or unknown_states:
        _reject(
            "invalid_signal_observation_binding",
            "signal observation binding contains a signal absent from the snapshot",
        )
    sells: list[Signal] = []
    suppressions: dict[str, str] = {}
    for signal in structure.signals:
        try:
            signal_time = normalize_datetime(signal.date, "signal.date")
        except (TypeError, ValueError) as exc:
            _reject("invalid_signal_time", str(exc))
        if signal_time > observed_at:
            _reject("future_signal", "structure contains a future signal")
        if signal.bs_type not in SELLS:
            continue
        signal_fingerprint = sha256_json(signal)
        observation_state = structure.signal_observation_states.get(
            signal_fingerprint
        )
        if observation_state in {
            "baseline_not_fresh",
            "quarantined_unknown",
        }:
            suppressions[signal_fingerprint] = observation_state
            continue
        if observation_state != "trusted_first_seen":
            _reject(
                "unbound_sell_signal",
                "sell signal has no trusted observation state",
            )
        first_observed_at = structure.signals_first_observed_at.get(
            signal_fingerprint
        )
        if first_observed_at is None:
            _reject(
                "unbound_sell_signal",
                "sell signal has no first-observed binding",
            )
        if first_observed_at > observed_at:
            _reject(
                "future_signal_observation",
                "sell signal was first observed after the current bar",
            )
        if signal_time > first_observed_at:
            _reject("future_signal", "sell signal formed after first observation")
        if first_observed_at <= cycle_started_at:
            suppressions[signal_fingerprint] = "stale_signal_observation"
            continue
        lag_seconds = (first_observed_at - signal_time).total_seconds()
        if lag_seconds > max_confirmation_lag_seconds:
            _reject(
                "sell_signal_confirmation_window_exceeded",
                "sell signal confirmation exceeded the bounded lag window",
            )
        if (
            isinstance(signal.level, bool)
            or not isinstance(signal.level, int)
            or signal.level < 0
        ):
            _reject("invalid_sell_signal", "sell signal level is invalid")
        if signal.level not in known_levels:
            _reject("incomplete_levels", "sell signal level is absent from frozen levels")
        _decimal_signal_price(signal)
        sells.append(signal)
    return (
        tuple(
            sorted(
                sells,
                key=lambda signal: (
                    -signal.level,
                    -_SELL_STRENGTH[signal.bs_type],
                    signal.bs_type,
                    str(signal.price),
                ),
            )
        ),
        MappingProxyType(dict(sorted(suppressions.items()))),
    )


def _entry_structural_stop(event: DecisionEvent) -> Decimal:
    raw = event.signal.structural_stop_below
    if raw is None:
        _reject("missing_entry_structural_stop", "entry event has no structural stop")
    try:
        stop = Decimal(str(raw))
        entry_price = Decimal(str(event.signal.price))
    except (InvalidOperation, TypeError, ValueError) as exc:
        _reject("invalid_entry_structural_stop", str(exc))
    if (
        not stop.is_finite()
        or not entry_price.is_finite()
        or stop <= 0
        or stop >= entry_price
    ):
        _reject(
            "invalid_entry_structural_stop",
            "entry structural stop must be positive and below entry signal price",
        )
    return stop


def _resolve_evidence(
    resolver: EvidenceResolver,
    trigger: ExitTrigger,
    reason: str,
) -> TriggerEvidence:
    try:
        binding = resolver.binding(trigger)
        evidence_ids = binding.evidence_ids
    except Exception as exc:
        _reject("evidence_resolution_failed", f"{trigger.value}: {exc}")
    if not isinstance(evidence_ids, tuple):
        _reject(
            "invalid_original_evidence",
            f"{trigger.value}: policy binding evidence ids must be a tuple",
        )
    if not evidence_ids:
        _reject(
            "missing_original_evidence",
            f"{trigger.value}: no original evidence id",
        )
    if len(set(evidence_ids)) != len(evidence_ids) or any(
        not isinstance(item, str)
        or not item.strip()
        or item != item.strip()
        for item in evidence_ids
    ):
        _reject(
            "invalid_original_evidence",
            f"{trigger.value}: evidence ids must be unique trimmed strings",
        )
    return TriggerEvidence(
        trigger=trigger,
        evidence_ids=evidence_ids,
        reason=reason,
    )


def _structure_payload_fingerprint(
    structure: SymbolStructureSnapshot,
    levels: tuple[LevelSnapshot, ...],
) -> str:
    return sha256_json(
        {
            "frequency": structure.frequency,
            "levels": levels,
            "signals": structure.signals,
            "first_visible_bar": structure.first_visible_bar,
            "completed_bars": structure.completed_bars,
            "config": structure.config,
            "operation_bar_closed": structure.operation_bar_closed,
            "fund_ok": structure.fund_ok,
            "comparison_ok": structure.comparison_ok,
            "invalidations": structure.invalidations,
            "current_cycle_id": structure.current_cycle_id,
            "signals_first_observed_at": structure.signals_first_observed_at,
            "signal_observation_states": structure.signal_observation_states,
        }
    )


def _build_signal_snapshot(
    request: ExitEvaluationRequest,
    evidence_resolver: EvidenceResolver,
    entry_ledger_resolver: EntryLedgerResolver,
) -> tuple[
    ExitSignalSnapshot,
    AuthoritativeEntryLink,
    tuple[LevelSnapshot, ...],
    Mapping[str, str],
]:
    authoritative = _resolve_authoritative_entry(
        request,
        entry_ledger_resolver,
    )
    event = authoritative.entry_event
    structure = request.structure
    context = request.risk_context
    observed_at = request.bar_closed_at

    _validate_entry_link(request.position, event, observed_at)
    _validate_current_context(request.position, context, observed_at)
    levels = _freeze_current_levels(structure, event, observed_at)
    levels_by_number = {level.level: level for level in levels}
    control_level = max(level.level for level in event.levels)
    control = levels_by_number.get(control_level)
    if control is None:
        _reject("incomplete_levels", "control level is missing")
    known_levels = frozenset(levels_by_number)
    sells, signal_observation_suppressions = _fresh_sell_signals(
        structure,
        observed_at,
        known_levels,
        request.evaluation_cycle_id,
        request.max_signal_confirmation_lag_seconds,
    )

    current_price = context.quote.price
    stop = _entry_structural_stop(event)
    hard_risk = context.daily_loss_locked or context.drawdown_locked
    structural_invalidation = current_price <= stop or any(
        notice.event_id == event.event_id for notice in structure.invalidations
    )
    control_level_down = control.direction == "down"
    control_sells = tuple(
        signal for signal in sells if signal.level >= control_level
    )
    operation_sells = tuple(
        signal for signal in sells if signal.level < control_level
    )
    control_level_sell = bool(control_sells)
    operation_level_sell = bool(operation_sells)
    selected_operation = operation_sells[0] if operation_sells else None
    operation_level = (
        selected_operation.level
        if selected_operation is not None
        else event.signal.level
    )

    reasons = {
        ExitTrigger.HARD_RISK: "risk latch is active",
        ExitTrigger.STRUCTURAL_INVALIDATION: (
            "current price reached entry structural stop or entry structure was invalidated"
        ),
        ExitTrigger.CONTROL_LEVEL_DOWN: "current control level direction is down",
        ExitTrigger.CONTROL_LEVEL_SELL: "fresh control-level sell signal is visible",
        ExitTrigger.OPERATION_LEVEL_SELL: "fresh operation-level sell signal is visible",
    }
    active = {
        ExitTrigger.HARD_RISK: hard_risk,
        ExitTrigger.STRUCTURAL_INVALIDATION: structural_invalidation,
        ExitTrigger.CONTROL_LEVEL_DOWN: control_level_down,
        ExitTrigger.CONTROL_LEVEL_SELL: control_level_sell,
        ExitTrigger.OPERATION_LEVEL_SELL: operation_level_sell,
    }
    evidence = tuple(
        _resolve_evidence(evidence_resolver, trigger, reasons[trigger])
        for trigger in _TRIGGER_ORDER
        if active[trigger]
    )
    snapshot = ExitSignalSnapshot(
        observed_at=observed_at,
        trigger_price=current_price,
        hard_risk=hard_risk,
        structural_invalidation=structural_invalidation,
        control_level_down=control_level_down,
        control_level_sell=control_level_sell,
        operation_level_sell=operation_level_sell,
        control_direction=control.direction,
        operation_bs_type=(
            selected_operation.bs_type
            if selected_operation is not None
            else None
        ),
        operation_level=operation_level,
        control_level=control_level,
        swing_level=0,
        evidence=evidence,
    )
    return snapshot, authoritative, levels, signal_observation_suppressions


def evaluate_tracked_position(
    request: ExitEvaluationRequest,
    *,
    evidence_resolver: EvidenceResolver,
    entry_ledger_resolver: EntryLedgerResolver,
) -> ExitRecommendation:
    """Return one deterministic recommendation; never perform an order action."""

    if not isinstance(request, ExitEvaluationRequest):
        raise TypeError("request must be ExitEvaluationRequest")
    if not isinstance(evidence_resolver, ExitEvidencePolicy):
        raise TypeError("evidence_resolver must be ExitEvidencePolicy")
    if not callable(entry_ledger_resolver):
        raise TypeError("entry_ledger_resolver must be callable")
    signals, authoritative, levels, signal_observation_suppressions = (
        _build_signal_snapshot(
            request,
            evidence_resolver,
            entry_ledger_resolver,
        )
    )
    tracked = request.position
    selection = select_exit_intent(
        entry_event_id=tracked.entry_event_id,
        position=tracked.holding,
        signals=signals,
    )
    outcome = None
    if selection.intent is not None:
        outcome = evaluate_exit_intent(
            selection.intent,
            request.entry_event,
            tracked.holding,
            request.risk_context,
        )
    return ExitRecommendation(
        entry_event_id=tracked.entry_event_id,
        code=tracked.holding.code,
        strategy_track=tracked.strategy_track,
        evaluation_cycle_id=request.evaluation_cycle_id,
        entry_provenance_fingerprint=(
            authoritative.position.entry_provenance_fingerprint
        ),
        exit_evidence_policy_fingerprint=evidence_resolver.fingerprint,
        certified_corpus_manifest_fingerprint=(
            "sha256:" + evidence_resolver.corpus_manifest_sha256
        ),
        source_pdf_fingerprint=(
            "sha256:" + evidence_resolver.source_pdf_sha256
        ),
        bar_structure_payload_fingerprint=_structure_payload_fingerprint(
            request.structure,
            levels,
        ),
        risk_context_payload_fingerprint=sha256_json(request.risk_context),
        quote_payload_fingerprint=sha256_json(request.risk_context.quote),
        algorithm_version=EXIT_ALGORITHM_VERSION,
        evaluation_version=EXIT_EVALUATION_VERSION,
        max_signal_confirmation_lag_seconds=(
            request.max_signal_confirmation_lag_seconds
        ),
        signal_observation_states=request.structure.signal_observation_states,
        signal_observation_suppressions=signal_observation_suppressions,
        signal_snapshot=signals,
        selection=selection,
        outcome=outcome,
    )


def evaluate_tracked_positions(
    requests: Iterable[ExitEvaluationRequest],
    *,
    evidence_resolver: EvidenceResolver,
    entry_ledger_resolver: EntryLedgerResolver,
) -> ExitBatchResult:
    """Evaluate positions independently and retain a failure for every reject."""

    if isinstance(requests, (str, bytes)):
        raise TypeError("requests must be an iterable of ExitEvaluationRequest")
    if not isinstance(evidence_resolver, ExitEvidencePolicy):
        raise TypeError("evidence_resolver must be ExitEvidencePolicy")
    if not callable(entry_ledger_resolver):
        raise TypeError("entry_ledger_resolver must be callable")
    try:
        frozen_requests = tuple(requests)
    except TypeError as exc:
        raise TypeError(
            "requests must be an iterable of ExitEvaluationRequest"
        ) from exc

    recommendations: list[ExitRecommendation] = []
    failures: list[ExitRuntimeFailure] = []
    seen_entries: set[str] = set()
    for index, request in enumerate(frozen_requests):
        if isinstance(request, ExitEvaluationRequest):
            entry_event_id = request.position.entry_event_id
            code = request.position.holding.code
        else:
            entry_event_id = f"invalid-request-{index}"
            code = "unknown"
        if entry_event_id in seen_entries:
            failures.append(
                ExitRuntimeFailure(
                    entry_event_id=entry_event_id,
                    code=code,
                    reason="duplicate_entry_event_id",
                    detail="batch contains the entry event more than once",
                )
            )
            continue
        seen_entries.add(entry_event_id)
        try:
            recommendation = evaluate_tracked_position(
                request,
                evidence_resolver=evidence_resolver,
                entry_ledger_resolver=entry_ledger_resolver,
            )
        except Exception as exc:
            reason = (
                exc.reason
                if isinstance(exc, ExitRuntimeError)
                else type(exc).__name__
            )
            detail = str(exc) or reason
            failures.append(
                ExitRuntimeFailure(
                    entry_event_id=entry_event_id,
                    code=code,
                    reason=reason,
                    detail=detail,
                )
            )
        else:
            recommendations.append(recommendation)
    return ExitBatchResult(tuple(recommendations), tuple(failures))


__all__ = (
    "AuthoritativeEntryLink",
    "EntryLedgerResolver",
    "EvidenceResolver",
    "EXIT_ALGORITHM_VERSION",
    "EXIT_EVALUATION_VERSION",
    "ExitBatchResult",
    "ExitEvaluationRequest",
    "ExitRecommendation",
    "ExitRuntimeError",
    "ExitRuntimeFailure",
    "TrackedPosition",
    "build_exit_evaluation_cycle_id",
    "evaluate_tracked_position",
    "evaluate_tracked_positions",
)
