from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_FLOOR
from enum import Enum
from typing import Any

from chanlun.recursive_bt.engine.engine import SELLS, recommended_sell_ratio

from .fingerprints import normalize_datetime, sha256_json, to_jsonable
from .models import DecisionEvent
from .risk import HoldingSnapshot, RiskContext, evaluate_exit


_A_SHARE_LOT = 100


class ExitTrigger(str, Enum):
    HARD_RISK = "hard_risk"
    STRUCTURAL_INVALIDATION = "structural_invalidation"
    CONTROL_LEVEL_DOWN = "control_level_down"
    CONTROL_LEVEL_SELL = "control_level_sell"
    OPERATION_LEVEL_SELL = "operation_level_sell"


class ExitStatus(str, Enum):
    EXECUTABLE = "executable"
    PARTIAL = "partial"
    PENDING = "pending"
    REJECTED = "rejected"


_TRIGGER_ORDER = {
    ExitTrigger.HARD_RISK: 0,
    ExitTrigger.STRUCTURAL_INVALIDATION: 1,
    ExitTrigger.CONTROL_LEVEL_DOWN: 1,
    ExitTrigger.CONTROL_LEVEL_SELL: 2,
    ExitTrigger.OPERATION_LEVEL_SELL: 3,
}

_TRIGGER_FIELDS = (
    (ExitTrigger.HARD_RISK, "hard_risk"),
    (ExitTrigger.STRUCTURAL_INVALIDATION, "structural_invalidation"),
    (ExitTrigger.CONTROL_LEVEL_DOWN, "control_level_down"),
    (ExitTrigger.CONTROL_LEVEL_SELL, "control_level_sell"),
    (ExitTrigger.OPERATION_LEVEL_SELL, "operation_level_sell"),
)


def _require_non_empty_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _require_bool(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} must be boolean")
    return value


def _require_non_negative_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return value


def _require_positive_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _require_positive_decimal(value: object, field_name: str) -> Decimal:
    if not isinstance(value, Decimal):
        raise ValueError(f"{field_name} must be Decimal")
    if not value.is_finite():
        raise ValueError(f"{field_name} must be finite")
    if value <= 0:
        raise ValueError(f"{field_name} must be positive")
    return value


def _require_string_tuple(
    value: object,
    field_name: str,
    *,
    allow_empty: bool,
) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise ValueError(f"{field_name} must be a tuple")
    if not allow_empty and not value:
        raise ValueError(f"{field_name} must not be empty")
    for item in value:
        _require_non_empty_string(item, field_name)
    if len(set(value)) != len(value):
        raise ValueError(f"{field_name} must contain unique values")
    return value


def _as_json_mapping(value: object, subject: str) -> dict[str, Any]:
    serialized = to_jsonable(value)
    if not isinstance(serialized, dict):
        raise TypeError(f"{subject} serialization must produce a mapping")
    return serialized


@dataclass(frozen=True, slots=True)
class TriggerEvidence:
    trigger: ExitTrigger
    evidence_ids: tuple[str, ...]
    reason: str

    def __post_init__(self) -> None:
        try:
            trigger = ExitTrigger(self.trigger)
        except (TypeError, ValueError) as exc:
            raise ValueError("trigger must be a known exit trigger") from exc
        object.__setattr__(self, "trigger", trigger)
        _require_string_tuple(
            self.evidence_ids,
            "evidence_ids",
            allow_empty=False,
        )
        _require_non_empty_string(self.reason, "reason")

    def to_dict(self) -> dict[str, Any]:
        return _as_json_mapping(self, "trigger evidence")


@dataclass(frozen=True, slots=True)
class ExitSignalSnapshot:
    observed_at: datetime
    trigger_price: Decimal
    hard_risk: bool
    structural_invalidation: bool
    control_level_down: bool
    control_level_sell: bool
    operation_level_sell: bool
    control_direction: str
    operation_bs_type: str | None
    operation_level: int
    control_level: int
    swing_level: int
    evidence: tuple[TriggerEvidence, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "observed_at",
            normalize_datetime(self.observed_at, "observed_at"),
        )
        _require_positive_decimal(self.trigger_price, "trigger_price")
        for _, field_name in _TRIGGER_FIELDS:
            _require_bool(getattr(self, field_name), field_name)

        direction = _require_non_empty_string(
            self.control_direction,
            "control_direction",
        )
        if direction not in {"up", "down", "neutral"}:
            raise ValueError("control_direction must be up, down, or neutral")
        if self.control_level_down != (direction == "down"):
            raise ValueError(
                "control_level_down conflicts with control_direction"
            )

        for field_name in (
            "operation_level",
            "control_level",
            "swing_level",
        ):
            _require_non_negative_int(getattr(self, field_name), field_name)

        if self.operation_level_sell:
            if self.operation_bs_type not in SELLS:
                raise ValueError(
                    "operation_bs_type must be a known sell signal"
                )
        elif self.operation_bs_type is not None:
            raise ValueError(
                "operation_bs_type requires operation_level_sell"
            )

        if not isinstance(self.evidence, tuple) or not all(
            isinstance(item, TriggerEvidence) for item in self.evidence
        ):
            raise ValueError("evidence must contain TriggerEvidence values")
        active = self.active_triggers
        evidence_triggers = tuple(item.trigger for item in self.evidence)
        if (
            len(set(evidence_triggers)) != len(evidence_triggers)
            or set(evidence_triggers) != set(active)
        ):
            raise ValueError("evidence must match active triggers")
        canonical_evidence = tuple(
            sorted(
                self.evidence,
                key=lambda item: (
                    _TRIGGER_ORDER[item.trigger],
                    item.trigger.value,
                ),
            )
        )
        object.__setattr__(self, "evidence", canonical_evidence)

    @property
    def active_triggers(self) -> tuple[ExitTrigger, ...]:
        return tuple(
            trigger
            for trigger, field_name in _TRIGGER_FIELDS
            if getattr(self, field_name)
        )

    def to_dict(self) -> dict[str, Any]:
        return _as_json_mapping(self, "exit signal snapshot")


@dataclass(frozen=True, slots=True)
class ExitIntent:
    intent_id: str
    entry_event_id: str
    code: str
    trigger: ExitTrigger
    full_exit: bool
    requested_shares: int
    layered_fraction: Decimal
    trigger_price: Decimal
    observed_at: datetime
    evidence: tuple[TriggerEvidence, ...]
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        intent_id = _require_non_empty_string(self.intent_id, "intent_id")
        if (
            not intent_id.startswith("exit:")
            or len(intent_id) != 69
            or any(character not in "0123456789abcdef" for character in intent_id[5:])
        ):
            raise ValueError("intent_id must use exit:<64 lowercase hex>")
        _require_non_empty_string(self.entry_event_id, "entry_event_id")
        _require_non_empty_string(self.code, "code")
        try:
            object.__setattr__(self, "trigger", ExitTrigger(self.trigger))
        except (TypeError, ValueError) as exc:
            raise ValueError("trigger must be a known exit trigger") from exc
        _require_bool(self.full_exit, "full_exit")
        _require_positive_int(self.requested_shares, "requested_shares")
        fraction = _require_positive_decimal(
            self.layered_fraction,
            "layered_fraction",
        )
        if fraction > 1:
            raise ValueError("layered_fraction must not exceed one")
        if self.full_exit and fraction != Decimal("1"):
            raise ValueError("full exit must use layered_fraction one")
        if not self.full_exit and self.requested_shares % _A_SHARE_LOT:
            raise ValueError("layered exit shares must use 100-share lots")
        _require_positive_decimal(self.trigger_price, "trigger_price")
        object.__setattr__(
            self,
            "observed_at",
            normalize_datetime(self.observed_at, "observed_at"),
        )
        if not isinstance(self.evidence, tuple) or not self.evidence:
            raise ValueError("evidence must contain TriggerEvidence values")
        if not all(isinstance(item, TriggerEvidence) for item in self.evidence):
            raise ValueError("evidence must contain TriggerEvidence values")
        if self.trigger not in {item.trigger for item in self.evidence}:
            raise ValueError("evidence must include the selected trigger")
        _require_string_tuple(self.reasons, "reasons", allow_empty=False)

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": 1, **_as_json_mapping(self, "exit intent")}


@dataclass(frozen=True, slots=True)
class ExitSelection:
    intent: ExitIntent | None
    evidence: tuple[TriggerEvidence, ...]
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.intent is not None and not isinstance(self.intent, ExitIntent):
            raise ValueError("intent must be ExitIntent or None")
        if not isinstance(self.evidence, tuple) or not all(
            isinstance(item, TriggerEvidence) for item in self.evidence
        ):
            raise ValueError("evidence must contain TriggerEvidence values")
        _require_string_tuple(self.reasons, "reasons", allow_empty=False)

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": 1, **_as_json_mapping(self, "exit selection")}


@dataclass(frozen=True, slots=True)
class ExitOutcome:
    intent_id: str
    entry_event_id: str
    code: str
    trigger: ExitTrigger
    status: ExitStatus
    requested_shares: int
    executable_shares: int
    pending_shares: int
    evidence: tuple[TriggerEvidence, ...]
    reasons: tuple[str, ...]
    evaluated_at: datetime

    def __post_init__(self) -> None:
        _require_non_empty_string(self.intent_id, "intent_id")
        _require_non_empty_string(self.entry_event_id, "entry_event_id")
        _require_non_empty_string(self.code, "code")
        try:
            object.__setattr__(self, "trigger", ExitTrigger(self.trigger))
            object.__setattr__(self, "status", ExitStatus(self.status))
        except (TypeError, ValueError) as exc:
            raise ValueError("outcome enum value is unknown") from exc
        _require_positive_int(self.requested_shares, "requested_shares")
        _require_non_negative_int(
            self.executable_shares,
            "executable_shares",
        )
        _require_non_negative_int(self.pending_shares, "pending_shares")
        if self.status is ExitStatus.REJECTED:
            if self.executable_shares or self.pending_shares:
                raise ValueError("rejected exit cannot contain exit shares")
        elif (
            self.executable_shares + self.pending_shares
            != self.requested_shares
        ):
            raise ValueError("exit shares must reconcile with the intent")
        if self.status is ExitStatus.EXECUTABLE and (
            self.executable_shares <= 0 or self.pending_shares != 0
        ):
            raise ValueError("executable exit status conflicts with shares")
        if self.status is ExitStatus.PARTIAL and (
            self.executable_shares <= 0 or self.pending_shares <= 0
        ):
            raise ValueError("partial exit status conflicts with shares")
        if self.status is ExitStatus.PENDING and (
            self.executable_shares != 0 or self.pending_shares <= 0
        ):
            raise ValueError("pending exit status conflicts with shares")
        if not isinstance(self.evidence, tuple) or not self.evidence:
            raise ValueError("evidence must contain TriggerEvidence values")
        if not all(isinstance(item, TriggerEvidence) for item in self.evidence):
            raise ValueError("evidence must contain TriggerEvidence values")
        _require_string_tuple(self.reasons, "reasons", allow_empty=True)
        object.__setattr__(
            self,
            "evaluated_at",
            normalize_datetime(self.evaluated_at, "evaluated_at"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": 1, **_as_json_mapping(self, "exit outcome")}


def _lot_floor(shares: Decimal) -> int:
    lots = (shares / Decimal(_A_SHARE_LOT)).to_integral_value(
        rounding=ROUND_FLOOR
    )
    return int(lots) * _A_SHARE_LOT


def _selected_trigger(active: tuple[ExitTrigger, ...]) -> ExitTrigger:
    if ExitTrigger.HARD_RISK in active:
        return ExitTrigger.HARD_RISK
    if ExitTrigger.STRUCTURAL_INVALIDATION in active:
        return ExitTrigger.STRUCTURAL_INVALIDATION
    if ExitTrigger.CONTROL_LEVEL_DOWN in active:
        return ExitTrigger.CONTROL_LEVEL_DOWN
    if ExitTrigger.CONTROL_LEVEL_SELL in active:
        return ExitTrigger.CONTROL_LEVEL_SELL
    return ExitTrigger.OPERATION_LEVEL_SELL


def _selection_reasons(
    selected: ExitTrigger,
    active: tuple[ExitTrigger, ...],
) -> tuple[str, ...]:
    reasons = [f"selected:{selected.value}"]
    for trigger in active:
        if trigger is selected:
            continue
        if (
            selected is ExitTrigger.STRUCTURAL_INVALIDATION
            and trigger is ExitTrigger.CONTROL_LEVEL_DOWN
        ):
            reasons.append("co_priority:control_level_down")
        else:
            reasons.append(f"suppressed:{trigger.value}")
    return tuple(reasons)


def _build_intent_id(
    *,
    entry_event_id: str,
    position: HoldingSnapshot,
    signals: ExitSignalSnapshot,
    trigger: ExitTrigger,
    requested_shares: int,
    layered_fraction: Decimal,
    reasons: tuple[str, ...],
) -> str:
    fingerprint = sha256_json(
        {
            "entry_event_id": entry_event_id,
            "code": position.code,
            "trigger": trigger,
            "requested_shares": requested_shares,
            "layered_fraction": layered_fraction,
            "trigger_price": signals.trigger_price,
            "observed_at": signals.observed_at,
            "evidence": signals.evidence,
            "reasons": reasons,
        }
    )
    return f"exit:{fingerprint[7:]}"


def select_exit_intent(
    *,
    entry_event_id: str,
    position: HoldingSnapshot,
    signals: ExitSignalSnapshot,
) -> ExitSelection:
    _require_non_empty_string(entry_event_id, "entry_event_id")
    if not isinstance(position, HoldingSnapshot):
        raise TypeError("position must be HoldingSnapshot")
    if not isinstance(signals, ExitSignalSnapshot):
        raise TypeError("signals must be ExitSignalSnapshot")

    active = signals.active_triggers
    if not active:
        return ExitSelection(
            intent=None,
            evidence=(),
            reasons=("no_exit_trigger",),
        )

    selected = _selected_trigger(active)
    full_exit = selected is not ExitTrigger.OPERATION_LEVEL_SELL
    if full_exit:
        layered_fraction = Decimal("1")
        requested_shares = position.shares
    else:
        ratio = recommended_sell_ratio(
            signals.operation_bs_type or "",
            big_dir=signals.control_direction,
            policy="original_layered",
            exit_level=signals.operation_level,
            core_signal_level=signals.control_level,
            swing_signal_level=signals.swing_level,
        )
        layered_fraction = Decimal(str(ratio))
        if not layered_fraction.is_finite() or not 0 < layered_fraction <= 1:
            raise ValueError("layered exit fraction must be finite and valid")
        requested_shares = _lot_floor(
            Decimal(position.shares) * layered_fraction
        )
        if requested_shares == 0:
            return ExitSelection(
                intent=None,
                evidence=signals.evidence,
                reasons=("layered_exit_below_lot",),
            )

    reasons = _selection_reasons(selected, active)
    intent_id = _build_intent_id(
        entry_event_id=entry_event_id,
        position=position,
        signals=signals,
        trigger=selected,
        requested_shares=requested_shares,
        layered_fraction=layered_fraction,
        reasons=reasons,
    )
    intent = ExitIntent(
        intent_id=intent_id,
        entry_event_id=entry_event_id,
        code=position.code,
        trigger=selected,
        full_exit=full_exit,
        requested_shares=requested_shares,
        layered_fraction=layered_fraction,
        trigger_price=signals.trigger_price,
        observed_at=signals.observed_at,
        evidence=signals.evidence,
        reasons=reasons,
    )
    return ExitSelection(
        intent=intent,
        evidence=signals.evidence,
        reasons=reasons,
    )


def _unique_reasons(*groups: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item for group in groups for item in group))


def evaluate_exit_intent(
    intent: ExitIntent,
    event: DecisionEvent,
    position: HoldingSnapshot,
    context: RiskContext,
) -> ExitOutcome:
    if not isinstance(intent, ExitIntent):
        raise TypeError("intent must be ExitIntent")
    if not isinstance(event, DecisionEvent):
        raise TypeError("event must be DecisionEvent")
    if not isinstance(position, HoldingSnapshot):
        raise TypeError("position must be HoldingSnapshot")
    if not isinstance(context, RiskContext):
        raise TypeError("context must be RiskContext")
    if intent.code != position.code:
        raise ValueError("intent and position code mismatch")
    if intent.code != event.code:
        raise ValueError("intent and event code mismatch")
    if position.shares < intent.requested_shares:
        raise ValueError("position shares conflict with frozen exit intent")

    risk_decision = evaluate_exit(
        event,
        position,
        context,
        reason=intent.trigger.value,
    )
    reasons = _unique_reasons(intent.reasons, risk_decision.reasons)
    fatal_identity_reasons = {
        "position_not_in_context",
        "position_snapshot_mismatch",
    }
    if fatal_identity_reasons.intersection(risk_decision.reasons):
        return ExitOutcome(
            intent_id=intent.intent_id,
            entry_event_id=intent.entry_event_id,
            code=intent.code,
            trigger=intent.trigger,
            status=ExitStatus.REJECTED,
            requested_shares=intent.requested_shares,
            executable_shares=0,
            pending_shares=0,
            evidence=intent.evidence,
            reasons=reasons,
            evaluated_at=risk_decision.evaluated_at,
        )

    executable_shares = min(
        intent.requested_shares,
        risk_decision.executable_shares,
    )
    if not intent.full_exit:
        executable_shares = _lot_floor(Decimal(executable_shares))
    pending_shares = intent.requested_shares - executable_shares
    if executable_shares == 0:
        status = ExitStatus.PENDING
    elif pending_shares:
        status = ExitStatus.PARTIAL
    else:
        status = ExitStatus.EXECUTABLE

    return ExitOutcome(
        intent_id=intent.intent_id,
        entry_event_id=intent.entry_event_id,
        code=intent.code,
        trigger=intent.trigger,
        status=status,
        requested_shares=intent.requested_shares,
        executable_shares=executable_shares,
        pending_shares=pending_shares,
        evidence=intent.evidence,
        reasons=reasons,
        evaluated_at=risk_decision.evaluated_at,
    )


__all__ = [
    "ExitIntent",
    "ExitOutcome",
    "ExitSelection",
    "ExitSignalSnapshot",
    "ExitStatus",
    "ExitTrigger",
    "TriggerEvidence",
    "evaluate_exit_intent",
    "select_exit_intent",
]
