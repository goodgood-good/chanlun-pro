"""Immutable, event-bound risk snapshots and latch audit records."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
import re
from typing import Mapping

from .fingerprints import normalize_datetime, sha256_json
from .models import DecisionEvent
from .risk import RiskDecision


_FINGERPRINT_RE = re.compile(r"sha256:[0-9a-f]{64}")
_SNAPSHOT_ID_RE = re.compile(r"risk-snapshot:[0-9a-f]{64}")
_AUDIT_ID_RE = re.compile(r"risk-latch-audit:[0-9a-f]{64}")
_RULE_BINDING_FIELDS = (
    "rule_id",
    "rule_card_version",
    "rule_card_fingerprint",
    "rule_set_fingerprint",
    "corpus_manifest_fingerprint",
    "algorithm_fingerprint",
)


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _required_fingerprint(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _FINGERPRINT_RE.fullmatch(value) is None:
        raise ValueError(f"{field_name} must use sha256:<64 lowercase hex>")
    return value


def _required_non_negative_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return value


def _required_positive_int(value: object, field_name: str) -> int:
    result = _required_non_negative_int(value, field_name)
    if result == 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return result


def _required_bool(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} must be boolean")
    return value


def _required_decimal(value: object, field_name: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"RiskDecision {field_name} must be a finite Decimal")
    return value


def _parse_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be an ISO-8601 string")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 string") from exc
    return normalize_datetime(parsed, field_name)


def _parse_decimal(value: object, field_name: str) -> Decimal:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a decimal string")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{field_name} must be a decimal string") from exc
    if not result.is_finite():
        raise ValueError(f"{field_name} must be a finite decimal string")
    return result


def _require_exact_fields(
    payload: Mapping[str, object],
    expected: frozenset[str],
    label: str,
) -> None:
    if set(payload) != expected:
        raise ValueError(f"{label} fields mismatch")


def _normalized_risk_decision(decision: object) -> RiskDecision:
    if not isinstance(decision, RiskDecision):
        raise ValueError("decision must be RiskDecision")
    allowed = _required_bool(decision.allowed, "RiskDecision allowed")
    shares = _required_non_negative_int(decision.shares, "RiskDecision shares")
    planned_risk_cash = _required_decimal(
        decision.planned_risk_cash,
        "planned_risk_cash",
    )
    target_weight = _required_decimal(decision.target_weight, "target_weight")
    entry_reference = _required_decimal(
        decision.entry_reference,
        "entry_reference",
    )
    if planned_risk_cash < 0:
        raise ValueError("RiskDecision planned_risk_cash must be non-negative")
    if target_weight < 0 or target_weight > 1:
        raise ValueError("RiskDecision target_weight must be between zero and one")
    if entry_reference <= 0:
        raise ValueError("RiskDecision entry_reference must be positive")
    if isinstance(decision.reasons, (str, bytes)) or not isinstance(
        decision.reasons,
        (tuple, list),
    ):
        raise ValueError("RiskDecision reasons must be a sequence")
    reasons = tuple(decision.reasons)
    if any(not isinstance(reason, str) or not reason for reason in reasons):
        raise ValueError("RiskDecision reasons must contain non-empty strings")
    if len(reasons) != len(set(reasons)):
        raise ValueError("RiskDecision reasons must not contain duplicates")
    daily_loss_locked = _required_bool(
        decision.daily_loss_locked,
        "RiskDecision daily_loss_locked",
    )
    drawdown_locked = _required_bool(
        decision.drawdown_locked,
        "RiskDecision drawdown_locked",
    )
    evaluated_at = normalize_datetime(
        decision.evaluated_at,
        "RiskDecision evaluated_at",
    )

    expected_allowed = shares > 0 and not reasons
    if allowed is not expected_allowed:
        raise ValueError("RiskDecision allowed state is inconsistent")
    if shares == 0 and planned_risk_cash != 0:
        raise ValueError("RiskDecision zero shares must have zero planned risk")
    if shares > 0 and planned_risk_cash <= 0:
        raise ValueError("RiskDecision positive shares require positive planned risk")
    if daily_loss_locked != ("daily_loss_lock" in reasons):
        raise ValueError("RiskDecision daily loss latch is inconsistent")
    if drawdown_locked != ("strategy_drawdown_lock" in reasons):
        raise ValueError("RiskDecision drawdown latch is inconsistent")

    return RiskDecision(
        allowed=allowed,
        shares=shares,
        planned_risk_cash=planned_risk_cash,
        target_weight=target_weight,
        entry_reference=entry_reference,
        reasons=reasons,
        daily_loss_locked=daily_loss_locked,
        drawdown_locked=drawdown_locked,
        evaluated_at=evaluated_at,
    )


def _decision_payload(decision: RiskDecision) -> dict[str, object]:
    return {
        "allowed": decision.allowed,
        "shares": decision.shares,
        "planned_risk_cash": str(decision.planned_risk_cash),
        "target_weight": str(decision.target_weight),
        "entry_reference": str(decision.entry_reference),
        "reasons": list(decision.reasons),
        "daily_loss_locked": decision.daily_loss_locked,
        "drawdown_locked": decision.drawdown_locked,
        "evaluated_at": decision.evaluated_at.isoformat(),
    }


def _decision_from_payload(payload: object) -> RiskDecision:
    if not isinstance(payload, Mapping):
        raise ValueError("risk decision payload must be an object")
    _require_exact_fields(
        payload,
        frozenset(
            {
                "allowed",
                "shares",
                "planned_risk_cash",
                "target_weight",
                "entry_reference",
                "reasons",
                "daily_loss_locked",
                "drawdown_locked",
                "evaluated_at",
            }
        ),
        "risk decision",
    )
    reasons = payload["reasons"]
    if isinstance(reasons, (str, bytes)) or not isinstance(reasons, list):
        raise ValueError("risk decision reasons must be a list")
    return RiskDecision(
        allowed=payload["allowed"],
        shares=payload["shares"],
        planned_risk_cash=_parse_decimal(
            payload["planned_risk_cash"],
            "planned_risk_cash",
        ),
        target_weight=_parse_decimal(payload["target_weight"], "target_weight"),
        entry_reference=_parse_decimal(
            payload["entry_reference"],
            "entry_reference",
        ),
        reasons=tuple(reasons),
        daily_loss_locked=payload["daily_loss_locked"],
        drawdown_locked=payload["drawdown_locked"],
        evaluated_at=_parse_datetime(payload["evaluated_at"], "evaluated_at"),
    )


@dataclass(frozen=True, slots=True)
class RiskSnapshotValidation:
    snapshot_id: str
    usable: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RiskSnapshot:
    event_id: str
    event_data_fingerprint: str
    rule_id: str
    rule_card_version: int
    rule_card_fingerprint: str
    rule_set_fingerprint: str
    corpus_manifest_fingerprint: str
    algorithm_fingerprint: str
    evaluation_input_fingerprint: str
    observed_at: datetime
    evaluated_at: datetime
    expires_at: datetime
    decision: RiskDecision
    snapshot_id: str = field(init=False)
    identity_fingerprint: str = field(init=False)
    payload_fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        _required_text(self.event_id, "event_id")
        _required_fingerprint(
            self.event_data_fingerprint,
            "event_data_fingerprint",
        )
        _required_text(self.rule_id, "rule_id")
        _required_positive_int(self.rule_card_version, "rule_card_version")
        for field_name in (
            "rule_card_fingerprint",
            "rule_set_fingerprint",
            "corpus_manifest_fingerprint",
            "algorithm_fingerprint",
            "evaluation_input_fingerprint",
        ):
            _required_fingerprint(getattr(self, field_name), field_name)
        if self.evaluation_input_fingerprint != self.event_data_fingerprint:
            raise ValueError("evaluation input fingerprint mismatch")
        observed_at = normalize_datetime(self.observed_at, "observed_at")
        evaluated_at = normalize_datetime(self.evaluated_at, "evaluated_at")
        expires_at = normalize_datetime(self.expires_at, "expires_at")
        decision = _normalized_risk_decision(self.decision)
        if observed_at > evaluated_at:
            raise ValueError("observed_at cannot be after evaluated_at")
        if decision.evaluated_at != evaluated_at:
            raise ValueError("RiskDecision evaluated_at mismatch")
        if expires_at <= evaluated_at:
            raise ValueError("expires_at must be after evaluated_at")
        object.__setattr__(self, "observed_at", observed_at)
        object.__setattr__(self, "evaluated_at", evaluated_at)
        object.__setattr__(self, "expires_at", expires_at)
        object.__setattr__(self, "decision", decision)

        identity_fingerprint = sha256_json(self._identity_payload())
        snapshot_id = "risk-snapshot:" + identity_fingerprint[7:]
        object.__setattr__(self, "identity_fingerprint", identity_fingerprint)
        object.__setattr__(self, "snapshot_id", snapshot_id)
        object.__setattr__(
            self,
            "payload_fingerprint",
            sha256_json(self._payload_without_fingerprint()),
        )

    @classmethod
    def capture(
        cls,
        *,
        event: DecisionEvent,
        evaluation_input_fingerprint: str,
        decision: RiskDecision,
        observed_at: datetime,
        expires_at: datetime,
    ) -> RiskSnapshot:
        if not isinstance(event, DecisionEvent):
            raise TypeError("event must be DecisionEvent")
        if event.rule_binding_status != "bound":
            raise ValueError("risk snapshots require a rule-bound event")
        if evaluation_input_fingerprint != event.data_fingerprint:
            raise ValueError("evaluation input fingerprint mismatch")
        observed_at = normalize_datetime(observed_at, "observed_at")
        if observed_at < event.observed_at:
            raise ValueError("observed_at cannot predate the event")
        bindings = {field_name: getattr(event, field_name) for field_name in _RULE_BINDING_FIELDS}
        if any(value is None for value in bindings.values()):
            raise ValueError("risk snapshots require a complete rule-bound event")
        return cls(
            event_id=event.event_id,
            event_data_fingerprint=event.data_fingerprint,
            rule_id=bindings["rule_id"],
            rule_card_version=bindings["rule_card_version"],
            rule_card_fingerprint=bindings["rule_card_fingerprint"],
            rule_set_fingerprint=bindings["rule_set_fingerprint"],
            corpus_manifest_fingerprint=bindings[
                "corpus_manifest_fingerprint"
            ],
            algorithm_fingerprint=bindings["algorithm_fingerprint"],
            evaluation_input_fingerprint=evaluation_input_fingerprint,
            observed_at=observed_at,
            evaluated_at=decision.evaluated_at,
            expires_at=expires_at,
            decision=decision,
        )

    def _identity_payload(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "event_data_fingerprint": self.event_data_fingerprint,
            "rule_id": self.rule_id,
            "rule_card_version": self.rule_card_version,
            "rule_card_fingerprint": self.rule_card_fingerprint,
            "rule_set_fingerprint": self.rule_set_fingerprint,
            "corpus_manifest_fingerprint": self.corpus_manifest_fingerprint,
            "algorithm_fingerprint": self.algorithm_fingerprint,
            "evaluation_input_fingerprint": self.evaluation_input_fingerprint,
            "observed_at": self.observed_at,
            "evaluated_at": self.evaluated_at,
        }

    def _payload_without_fingerprint(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "snapshot_id": self.snapshot_id,
            "identity_fingerprint": self.identity_fingerprint,
            "event_id": self.event_id,
            "event_data_fingerprint": self.event_data_fingerprint,
            "rule_id": self.rule_id,
            "rule_card_version": self.rule_card_version,
            "rule_card_fingerprint": self.rule_card_fingerprint,
            "rule_set_fingerprint": self.rule_set_fingerprint,
            "corpus_manifest_fingerprint": self.corpus_manifest_fingerprint,
            "algorithm_fingerprint": self.algorithm_fingerprint,
            "evaluation_input_fingerprint": self.evaluation_input_fingerprint,
            "observed_at": self.observed_at.isoformat(),
            "evaluated_at": self.evaluated_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "decision": _decision_payload(self.decision),
        }

    def to_dict(self) -> dict[str, object]:
        return {
            **self._payload_without_fingerprint(),
            "payload_fingerprint": self.payload_fingerprint,
        }

    @classmethod
    def from_dict(cls, payload: object) -> RiskSnapshot:
        if not isinstance(payload, Mapping):
            raise ValueError("risk snapshot payload must be an object")
        _require_exact_fields(
            payload,
            frozenset(
                {
                    "schema_version",
                    "snapshot_id",
                    "identity_fingerprint",
                    "payload_fingerprint",
                    "event_id",
                    "event_data_fingerprint",
                    "rule_id",
                    "rule_card_version",
                    "rule_card_fingerprint",
                    "rule_set_fingerprint",
                    "corpus_manifest_fingerprint",
                    "algorithm_fingerprint",
                    "evaluation_input_fingerprint",
                    "observed_at",
                    "evaluated_at",
                    "expires_at",
                    "decision",
                }
            ),
            "risk snapshot",
        )
        if payload["schema_version"] != 1:
            raise ValueError("unsupported risk snapshot schema version")
        snapshot = cls(
            event_id=payload["event_id"],
            event_data_fingerprint=payload["event_data_fingerprint"],
            rule_id=payload["rule_id"],
            rule_card_version=payload["rule_card_version"],
            rule_card_fingerprint=payload["rule_card_fingerprint"],
            rule_set_fingerprint=payload["rule_set_fingerprint"],
            corpus_manifest_fingerprint=payload[
                "corpus_manifest_fingerprint"
            ],
            algorithm_fingerprint=payload["algorithm_fingerprint"],
            evaluation_input_fingerprint=payload[
                "evaluation_input_fingerprint"
            ],
            observed_at=_parse_datetime(payload["observed_at"], "observed_at"),
            evaluated_at=_parse_datetime(
                payload["evaluated_at"],
                "evaluated_at",
            ),
            expires_at=_parse_datetime(payload["expires_at"], "expires_at"),
            decision=_decision_from_payload(payload["decision"]),
        )
        if payload["snapshot_id"] != snapshot.snapshot_id:
            raise ValueError("risk snapshot id mismatch")
        if payload["identity_fingerprint"] != snapshot.identity_fingerprint:
            raise ValueError("risk snapshot identity fingerprint mismatch")
        if payload["payload_fingerprint"] != snapshot.payload_fingerprint:
            raise ValueError("risk snapshot payload fingerprint mismatch")
        return snapshot

    def validate_for_review(
        self,
        event: DecisionEvent,
        *,
        as_of: datetime,
    ) -> RiskSnapshotValidation:
        if not isinstance(event, DecisionEvent):
            raise TypeError("event must be DecisionEvent")
        as_of = normalize_datetime(as_of, "as_of")
        reasons = list(self.event_binding_reasons(event))
        if as_of < self.evaluated_at:
            reasons.append("risk_snapshot_not_yet_effective")
        if as_of >= self.expires_at:
            reasons.append("risk_snapshot_expired")
        if not self.decision.allowed:
            reasons.append("risk_decision_not_allowed")
        return RiskSnapshotValidation(
            snapshot_id=self.snapshot_id,
            usable=not reasons,
            reasons=tuple(reasons),
        )

    def event_binding_reasons(self, event: DecisionEvent) -> tuple[str, ...]:
        if not isinstance(event, DecisionEvent):
            raise TypeError("event must be DecisionEvent")
        reasons: list[str] = []
        if event.rule_binding_status != "bound":
            reasons.append("event_rule_binding_missing")
        if self.event_id != event.event_id:
            reasons.append("event_id_mismatch")
        if self.event_data_fingerprint != event.data_fingerprint:
            reasons.append("event_data_fingerprint_mismatch")
        if self.evaluation_input_fingerprint != event.data_fingerprint:
            reasons.append("evaluation_input_fingerprint_mismatch")
        for field_name in _RULE_BINDING_FIELDS:
            if getattr(self, field_name) != getattr(event, field_name):
                reasons.append(f"event_{field_name}_mismatch")
        return tuple(reasons)


class RiskLatchKind(str, Enum):
    DAILY_LOSS = "daily_loss"
    STRATEGY_DRAWDOWN = "strategy_drawdown"


class RiskLatchAction(str, Enum):
    LATCHED = "latched"
    MANUAL_RESET = "manual_reset"


@dataclass(frozen=True, slots=True)
class RiskLatchAudit:
    event_id: str
    snapshot_id: str
    latch_kind: RiskLatchKind
    action: RiskLatchAction
    previous_locked: bool
    current_locked: bool
    actor: str
    reason: str
    occurred_at: datetime
    audit_id: str = field(init=False)
    identity_fingerprint: str = field(init=False)
    payload_fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        _required_text(self.event_id, "event_id")
        if (
            not isinstance(self.snapshot_id, str)
            or _SNAPSHOT_ID_RE.fullmatch(self.snapshot_id) is None
        ):
            raise ValueError("snapshot_id has invalid format")
        try:
            latch_kind = RiskLatchKind(self.latch_kind)
            action = RiskLatchAction(self.action)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid risk latch audit enum") from exc
        previous_locked = _required_bool(self.previous_locked, "previous_locked")
        current_locked = _required_bool(self.current_locked, "current_locked")
        if action is RiskLatchAction.LATCHED and (
            previous_locked or not current_locked
        ):
            raise ValueError("latched audit must transition false to true")
        if action is RiskLatchAction.MANUAL_RESET and (
            not previous_locked or current_locked
        ):
            raise ValueError("manual reset audit must transition true to false")
        _required_text(self.actor, "actor")
        _required_text(self.reason, "reason")
        occurred_at = normalize_datetime(self.occurred_at, "occurred_at")
        object.__setattr__(self, "latch_kind", latch_kind)
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "occurred_at", occurred_at)
        identity_fingerprint = sha256_json(self._identity_payload())
        audit_id = "risk-latch-audit:" + identity_fingerprint[7:]
        object.__setattr__(self, "identity_fingerprint", identity_fingerprint)
        object.__setattr__(self, "audit_id", audit_id)
        object.__setattr__(
            self,
            "payload_fingerprint",
            sha256_json(self._payload_without_fingerprint()),
        )

    @classmethod
    def record(
        cls,
        *,
        snapshot: RiskSnapshot,
        latch_kind: RiskLatchKind | str,
        action: RiskLatchAction | str,
        actor: str,
        reason: str,
        occurred_at: datetime,
    ) -> RiskLatchAudit:
        if not isinstance(snapshot, RiskSnapshot):
            raise TypeError("snapshot must be RiskSnapshot")
        kind = RiskLatchKind(latch_kind)
        normalized_action = RiskLatchAction(action)
        source_locked = (
            snapshot.decision.daily_loss_locked
            if kind is RiskLatchKind.DAILY_LOSS
            else snapshot.decision.drawdown_locked
        )
        if not source_locked:
            raise ValueError("source snapshot is not locked")
        if occurred_at < snapshot.evaluated_at:
            raise ValueError("latch audit cannot predate its source snapshot")
        previous_locked, current_locked = (
            (False, True)
            if normalized_action is RiskLatchAction.LATCHED
            else (True, False)
        )
        return cls(
            event_id=snapshot.event_id,
            snapshot_id=snapshot.snapshot_id,
            latch_kind=kind,
            action=normalized_action,
            previous_locked=previous_locked,
            current_locked=current_locked,
            actor=actor,
            reason=reason,
            occurred_at=occurred_at,
        )

    def _identity_payload(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "snapshot_id": self.snapshot_id,
            "latch_kind": self.latch_kind.value,
            "action": self.action.value,
            "actor": self.actor,
            "occurred_at": self.occurred_at,
        }

    def _payload_without_fingerprint(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "audit_id": self.audit_id,
            "identity_fingerprint": self.identity_fingerprint,
            "event_id": self.event_id,
            "snapshot_id": self.snapshot_id,
            "latch_kind": self.latch_kind.value,
            "action": self.action.value,
            "previous_locked": self.previous_locked,
            "current_locked": self.current_locked,
            "actor": self.actor,
            "reason": self.reason,
            "occurred_at": self.occurred_at.isoformat(),
        }

    def to_dict(self) -> dict[str, object]:
        return {
            **self._payload_without_fingerprint(),
            "payload_fingerprint": self.payload_fingerprint,
        }

    @classmethod
    def from_dict(cls, payload: object) -> RiskLatchAudit:
        if not isinstance(payload, Mapping):
            raise ValueError("risk latch audit payload must be an object")
        _require_exact_fields(
            payload,
            frozenset(
                {
                    "schema_version",
                    "audit_id",
                    "identity_fingerprint",
                    "payload_fingerprint",
                    "event_id",
                    "snapshot_id",
                    "latch_kind",
                    "action",
                    "previous_locked",
                    "current_locked",
                    "actor",
                    "reason",
                    "occurred_at",
                }
            ),
            "risk latch audit",
        )
        if payload["schema_version"] != 1:
            raise ValueError("unsupported risk latch audit schema version")
        audit = cls(
            event_id=payload["event_id"],
            snapshot_id=payload["snapshot_id"],
            latch_kind=payload["latch_kind"],
            action=payload["action"],
            previous_locked=payload["previous_locked"],
            current_locked=payload["current_locked"],
            actor=payload["actor"],
            reason=payload["reason"],
            occurred_at=_parse_datetime(payload["occurred_at"], "occurred_at"),
        )
        if payload["audit_id"] != audit.audit_id:
            raise ValueError("risk latch audit id mismatch")
        if payload["identity_fingerprint"] != audit.identity_fingerprint:
            raise ValueError("risk latch audit identity fingerprint mismatch")
        if payload["payload_fingerprint"] != audit.payload_fingerprint:
            raise ValueError("risk latch audit payload fingerprint mismatch")
        return audit


__all__ = [
    "RiskLatchAction",
    "RiskLatchAudit",
    "RiskLatchKind",
    "RiskSnapshot",
    "RiskSnapshotValidation",
]
