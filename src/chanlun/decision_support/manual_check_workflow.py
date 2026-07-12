"""Durable, fail-closed human chart-check gate for decision events.

This module deliberately stops at ``REVIEW_PENDING``.  It neither invokes an
LLM nor imports an order/trader implementation.  A pending record freezes the
machine-derived facts and every manual-check identity needed to re-evaluate the
same RuleCard input after an operator has inspected the chart.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
import json
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from threading import RLock

from .event_service import DecisionEventService
from .fingerprints import normalize_datetime, sha256_json, to_jsonable
from .manual_check_binding import (
    MANUAL_CHECK_TRANSITION_ACTOR,
    manual_check_transition_reason,
)
from .manual_checks import ManualCheckSnapshot, validate_manual_check_snapshot
from .models import DecisionEvent, EventState
from .mutation_fence import MutationLeaseGuard, mutation_fenced
from .rule_cards import (
    EvaluationVerdict,
    PredicateMode,
    RuleEvaluation,
    RuleCard,
)
from .rule_context import (
    LevelEvaluationFacts,
    RuleRuntimeFacts,
    build_rule_evaluation_context,
)
from .rule_engine import RuleEngine


_FINGERPRINT_RE = re.compile(r"sha256:[0-9a-f]{64}")
_PENDING_ID_RE = re.compile(r"manual-pending:[0-9a-f]{64}")
_ATTEMPT_ID_RE = re.compile(r"manual-attempt:[0-9a-f]{64}")
_OUTCOMES = frozenset({"rejected", "validated", "advanced"})


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _required_fingerprint(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _FINGERPRINT_RE.fullmatch(value) is None:
        raise ValueError(f"{field_name} must use sha256:<64 lowercase hex>")
    return value


def _exact_fields(
    value: Mapping[str, object],
    expected: frozenset[str],
    label: str,
) -> None:
    if set(value) != expected:
        raise ValueError(f"{label} fields mismatch")


def _parse_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be an ISO-8601 string")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 string") from exc
    return normalize_datetime(parsed, field_name)


def _strings(value: object, field_name: str, *, allow_empty: bool) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{field_name} must be a sequence")
    values = tuple(value)
    if (not allow_empty and not values) or any(
        not isinstance(item, str) or not item for item in values
    ):
        raise ValueError(f"{field_name} must contain non-empty strings")
    if len(set(values)) != len(values):
        raise ValueError(f"{field_name} contains duplicates")
    return values


@dataclass(frozen=True, slots=True)
class RequiredManualCheck:
    manual_check_id: str
    evidence_ids: tuple[str, ...]
    prompt: str

    def __post_init__(self) -> None:
        _required_text(self.manual_check_id, "manual_check_id")
        evidence_ids = tuple(
            sorted(_strings(self.evidence_ids, "evidence_ids", allow_empty=False))
        )
        object.__setattr__(self, "evidence_ids", evidence_ids)
        _required_text(self.prompt, "prompt")

    def to_dict(self) -> dict[str, object]:
        return {
            "manual_check_id": self.manual_check_id,
            "evidence_ids": list(self.evidence_ids),
            "prompt": self.prompt,
        }

    @classmethod
    def from_dict(cls, value: object) -> RequiredManualCheck:
        if not isinstance(value, Mapping):
            raise ValueError("required check must be an object")
        _exact_fields(
            value,
            frozenset({"manual_check_id", "evidence_ids", "prompt"}),
            "required check",
        )
        return cls(
            manual_check_id=value["manual_check_id"],
            evidence_ids=tuple(value["evidence_ids"]),
            prompt=value["prompt"],
        )


def _snapshot_payload(snapshot: ManualCheckSnapshot) -> dict[str, object]:
    validate_manual_check_snapshot(snapshot)
    return {
        "manual_check_id": snapshot.manual_check_id,
        "value": snapshot.value,
        "operator_id": snapshot.operator_id,
        "recorded_at": snapshot.recorded_at.isoformat(),
        "event_id": snapshot.event_id,
        "context_fingerprint": snapshot.context_fingerprint,
        "evidence_ids": list(snapshot.evidence_ids),
        "fingerprint": snapshot.fingerprint,
    }


def _snapshot_from_payload(value: object) -> ManualCheckSnapshot:
    if not isinstance(value, Mapping):
        raise ValueError("manual check snapshot must be an object")
    _exact_fields(
        value,
        frozenset(
            {
                "manual_check_id",
                "value",
                "operator_id",
                "recorded_at",
                "event_id",
                "context_fingerprint",
                "evidence_ids",
                "fingerprint",
            }
        ),
        "manual check snapshot",
    )
    snapshot = ManualCheckSnapshot(
        manual_check_id=value["manual_check_id"],
        value=value["value"],
        operator_id=value["operator_id"],
        recorded_at=_parse_datetime(value["recorded_at"], "recorded_at"),
        event_id=value["event_id"],
        context_fingerprint=value["context_fingerprint"],
        evidence_ids=tuple(value["evidence_ids"]),
    )
    if value["fingerprint"] != snapshot.fingerprint:
        raise ValueError("manual check snapshot fingerprint mismatch")
    return snapshot


def manual_check_snapshot_from_dict(value: object) -> ManualCheckSnapshot:
    """Parse the exact untrusted Web/API submission shape."""

    if not isinstance(value, Mapping):
        raise ValueError("manual check snapshot must be an object")
    _exact_fields(
        value,
        frozenset(
            {
                "manual_check_id",
                "value",
                "operator_id",
                "recorded_at",
                "event_id",
                "context_fingerprint",
                "evidence_ids",
            }
        ),
        "manual check snapshot",
    )
    evidence_ids = value["evidence_ids"]
    if isinstance(evidence_ids, (str, bytes)) or not isinstance(
        evidence_ids, Sequence
    ):
        raise ValueError("evidence_ids must be a sequence")
    return ManualCheckSnapshot(
        manual_check_id=value["manual_check_id"],
        value=value["value"],
        operator_id=value["operator_id"],
        recorded_at=_parse_datetime(value["recorded_at"], "recorded_at"),
        event_id=value["event_id"],
        context_fingerprint=value["context_fingerprint"],
        evidence_ids=tuple(evidence_ids),
    )


def _runtime_facts_payload(facts: RuleRuntimeFacts) -> dict[str, object]:
    if type(facts) is not RuleRuntimeFacts:
        raise ValueError("runtime_facts must be RuleRuntimeFacts")
    if facts.manual_checks:
        raise ValueError("initial runtime facts must not contain manual checks")
    return {
        "fundamental_ok": facts.fundamental_ok,
        "comparison_ok": facts.comparison_ok,
        "market_liquid": facts.market_liquid,
        "risk_allowed": facts.risk_allowed,
        "latest_price": facts.latest_price,
        "level_facts": [
            {
                "frequency": item.frequency,
                "level": item.level,
                "completed_bar_count": item.completed_bar_count,
                "latest_bar_closed": item.latest_bar_closed,
            }
            for item in facts.level_facts
        ],
    }


def _runtime_facts_from_payload(value: object) -> RuleRuntimeFacts:
    if not isinstance(value, Mapping):
        raise ValueError("runtime_facts must be an object")
    _exact_fields(
        value,
        frozenset(
            {
                "fundamental_ok",
                "comparison_ok",
                "market_liquid",
                "risk_allowed",
                "latest_price",
                "level_facts",
            }
        ),
        "runtime_facts",
    )
    level_values = value["level_facts"]
    if isinstance(level_values, (str, bytes)) or not isinstance(
        level_values, Sequence
    ):
        raise ValueError("level_facts must be a sequence")
    levels: list[LevelEvaluationFacts] = []
    for item in level_values:
        if not isinstance(item, Mapping):
            raise ValueError("level_facts must contain objects")
        _exact_fields(
            item,
            frozenset(
                {
                    "frequency",
                    "level",
                    "completed_bar_count",
                    "latest_bar_closed",
                }
            ),
            "level facts",
        )
        levels.append(
            LevelEvaluationFacts(
                frequency=item["frequency"],
                level=item["level"],
                completed_bar_count=item["completed_bar_count"],
                latest_bar_closed=item["latest_bar_closed"],
            )
        )
    return RuleRuntimeFacts(
        fundamental_ok=value["fundamental_ok"],
        comparison_ok=value["comparison_ok"],
        market_liquid=value["market_liquid"],
        risk_allowed=value["risk_allowed"],
        latest_price=value["latest_price"],
        level_facts=tuple(levels),
    )


@dataclass(frozen=True, slots=True)
class ManualCheckAttempt:
    pending_id: str
    submitted_at: datetime
    snapshots: tuple[ManualCheckSnapshot, ...]
    outcome: str
    reasons: tuple[str, ...]
    evaluation_verdict: str | None = None
    evaluation_input_fingerprint: str | None = None
    attempt_id: str = field(init=False)

    def __post_init__(self) -> None:
        if _PENDING_ID_RE.fullmatch(self.pending_id) is None:
            raise ValueError("pending_id has invalid format")
        submitted_at = normalize_datetime(self.submitted_at, "submitted_at")
        object.__setattr__(self, "submitted_at", submitted_at)
        if isinstance(self.snapshots, (str, bytes)) or not isinstance(
            self.snapshots, Sequence
        ):
            raise ValueError("snapshots must contain ManualCheckSnapshot")
        snapshots = tuple(self.snapshots)
        if not all(type(item) is ManualCheckSnapshot for item in snapshots):
            raise ValueError("snapshots must contain ManualCheckSnapshot")
        for snapshot in snapshots:
            validate_manual_check_snapshot(snapshot)
        identifiers = tuple(item.manual_check_id for item in snapshots)
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("duplicate manual_check_id")
        snapshots = tuple(sorted(snapshots, key=lambda item: item.manual_check_id))
        object.__setattr__(self, "snapshots", snapshots)
        if self.outcome not in _OUTCOMES:
            raise ValueError("invalid manual check outcome")
        reasons = _strings(self.reasons, "reasons", allow_empty=True)
        object.__setattr__(self, "reasons", reasons)
        if self.outcome == "rejected":
            if not reasons:
                raise ValueError("rejected attempt requires reasons")
            if self.evaluation_verdict is not None:
                raise ValueError("rejected attempt cannot carry evaluation verdict")
            if self.evaluation_input_fingerprint is not None:
                raise ValueError("rejected attempt cannot carry evaluation fingerprint")
        else:
            if reasons:
                raise ValueError("accepted attempt cannot carry reasons")
            if self.evaluation_verdict != EvaluationVerdict.CONFIRM.value:
                raise ValueError("accepted attempt requires CONFIRM evaluation")
            _required_fingerprint(
                self.evaluation_input_fingerprint,
                "evaluation_input_fingerprint",
            )
        identity = sha256_json(
            {
                "pending_id": self.pending_id,
                "submitted_at": self.submitted_at,
                "snapshots": self.snapshots,
            }
        )
        object.__setattr__(self, "attempt_id", "manual-attempt:" + identity[7:])

    def to_dict(self) -> dict[str, object]:
        return {
            "attempt_id": self.attempt_id,
            "pending_id": self.pending_id,
            "submitted_at": self.submitted_at.isoformat(),
            "snapshots": [_snapshot_payload(item) for item in self.snapshots],
            "outcome": self.outcome,
            "reasons": list(self.reasons),
            "evaluation_verdict": self.evaluation_verdict,
            "evaluation_input_fingerprint": self.evaluation_input_fingerprint,
        }

    @classmethod
    def from_dict(cls, value: object) -> ManualCheckAttempt:
        if not isinstance(value, Mapping):
            raise ValueError("manual check attempt must be an object")
        _exact_fields(
            value,
            frozenset(
                {
                    "attempt_id",
                    "pending_id",
                    "submitted_at",
                    "snapshots",
                    "outcome",
                    "reasons",
                    "evaluation_verdict",
                    "evaluation_input_fingerprint",
                }
            ),
            "manual check attempt",
        )
        attempt = cls(
            pending_id=value["pending_id"],
            submitted_at=_parse_datetime(value["submitted_at"], "submitted_at"),
            snapshots=tuple(
                _snapshot_from_payload(item) for item in value["snapshots"]
            ),
            outcome=value["outcome"],
            reasons=tuple(value["reasons"]),
            evaluation_verdict=value["evaluation_verdict"],
            evaluation_input_fingerprint=value["evaluation_input_fingerprint"],
        )
        if value["attempt_id"] != attempt.attempt_id:
            raise ValueError("manual check attempt id mismatch")
        return attempt


@dataclass(frozen=True, slots=True)
class ManualCheckPending:
    event_id: str
    event_data_fingerprint: str
    rule_id: str
    rule_card_version: int
    rule_card_fingerprint: str
    rule_set_fingerprint: str
    corpus_manifest_fingerprint: str
    algorithm_fingerprint: str
    context_fingerprint: str
    risk_snapshot_id: str
    created_at: datetime
    required_checks: tuple[RequiredManualCheck, ...]
    runtime_facts: RuleRuntimeFacts = field(repr=False)
    status: str = "pending"
    attempts: tuple[ManualCheckAttempt, ...] = ()
    pending_id: str = field(init=False)
    payload_fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        _required_text(self.event_id, "event_id")
        _required_fingerprint(
            self.event_data_fingerprint,
            "event_data_fingerprint",
        )
        _required_text(self.rule_id, "rule_id")
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
            "context_fingerprint",
        ):
            _required_fingerprint(getattr(self, field_name), field_name)
        if self.event_data_fingerprint != self.context_fingerprint:
            raise ValueError("manual check context must match event data fingerprint")
        _required_text(self.risk_snapshot_id, "risk_snapshot_id")
        created_at = normalize_datetime(self.created_at, "created_at")
        object.__setattr__(self, "created_at", created_at)
        if isinstance(self.required_checks, (str, bytes)) or not isinstance(
            self.required_checks, Sequence
        ):
            raise ValueError("required_checks must contain RequiredManualCheck")
        checks = tuple(self.required_checks)
        if not checks or not all(
            type(item) is RequiredManualCheck for item in checks
        ):
            raise ValueError("required_checks must contain RequiredManualCheck")
        check_ids = tuple(item.manual_check_id for item in checks)
        if len(set(check_ids)) != len(check_ids):
            raise ValueError("required_checks contains duplicate identifiers")
        checks = tuple(sorted(checks, key=lambda item: item.manual_check_id))
        object.__setattr__(self, "required_checks", checks)
        _runtime_facts_payload(self.runtime_facts)
        if self.status not in {"pending", "approved"}:
            raise ValueError("invalid manual check pending status")
        if isinstance(self.attempts, (str, bytes)) or not isinstance(
            self.attempts, Sequence
        ):
            raise ValueError("attempts must contain ManualCheckAttempt")
        attempts = tuple(self.attempts)
        if not all(type(item) is ManualCheckAttempt for item in attempts):
            raise ValueError("attempts must contain ManualCheckAttempt")
        if len({item.attempt_id for item in attempts}) != len(attempts):
            raise ValueError("attempts contains duplicate identifiers")
        if tuple(item.submitted_at for item in attempts) != tuple(
            sorted(item.submitted_at for item in attempts)
        ):
            raise ValueError("attempts must be ordered by submitted_at")
        identity = sha256_json(self._identity_payload())
        pending_id = "manual-pending:" + identity[7:]
        if any(item.pending_id != pending_id for item in attempts):
            raise ValueError("attempt pending_id mismatch")
        if self.status == "approved" and (
            not attempts or attempts[-1].outcome != "advanced"
        ):
            raise ValueError("approved record requires an advanced attempt")
        if self.status == "pending" and any(
            item.outcome == "advanced" for item in attempts
        ):
            raise ValueError("pending record cannot contain an advanced attempt")
        object.__setattr__(self, "attempts", attempts)
        object.__setattr__(self, "pending_id", pending_id)
        object.__setattr__(
            self,
            "payload_fingerprint",
            sha256_json(self._payload_without_fingerprint()),
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
            "context_fingerprint": self.context_fingerprint,
            "risk_snapshot_id": self.risk_snapshot_id,
            "created_at": self.created_at,
            "required_checks": self.required_checks,
            "runtime_facts": self.runtime_facts,
        }

    def _payload_without_fingerprint(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "pending_id": self.pending_id,
            "event_id": self.event_id,
            "event_data_fingerprint": self.event_data_fingerprint,
            "rule_id": self.rule_id,
            "rule_card_version": self.rule_card_version,
            "rule_card_fingerprint": self.rule_card_fingerprint,
            "rule_set_fingerprint": self.rule_set_fingerprint,
            "corpus_manifest_fingerprint": self.corpus_manifest_fingerprint,
            "algorithm_fingerprint": self.algorithm_fingerprint,
            "context_fingerprint": self.context_fingerprint,
            "risk_snapshot_id": self.risk_snapshot_id,
            "created_at": self.created_at.isoformat(),
            "required_checks": [item.to_dict() for item in self.required_checks],
            "runtime_facts": _runtime_facts_payload(self.runtime_facts),
            "status": self.status,
            "attempts": [item.to_dict() for item in self.attempts],
        }

    def to_dict(self) -> dict[str, object]:
        return {
            **self._payload_without_fingerprint(),
            "payload_fingerprint": self.payload_fingerprint,
        }

    @classmethod
    def from_dict(cls, value: object) -> ManualCheckPending:
        if not isinstance(value, Mapping):
            raise ValueError("manual check pending record must be an object")
        _exact_fields(
            value,
            frozenset(
                {
                    "schema_version",
                    "pending_id",
                    "payload_fingerprint",
                    "event_id",
                    "event_data_fingerprint",
                    "rule_id",
                    "rule_card_version",
                    "rule_card_fingerprint",
                    "rule_set_fingerprint",
                    "corpus_manifest_fingerprint",
                    "algorithm_fingerprint",
                    "context_fingerprint",
                    "risk_snapshot_id",
                    "created_at",
                    "required_checks",
                    "runtime_facts",
                    "status",
                    "attempts",
                }
            ),
            "manual check pending record",
        )
        if value["schema_version"] != 1:
            raise ValueError("unsupported manual check schema version")
        record = cls(
            event_id=value["event_id"],
            event_data_fingerprint=value["event_data_fingerprint"],
            rule_id=value["rule_id"],
            rule_card_version=value["rule_card_version"],
            rule_card_fingerprint=value["rule_card_fingerprint"],
            rule_set_fingerprint=value["rule_set_fingerprint"],
            corpus_manifest_fingerprint=value["corpus_manifest_fingerprint"],
            algorithm_fingerprint=value["algorithm_fingerprint"],
            context_fingerprint=value["context_fingerprint"],
            risk_snapshot_id=value["risk_snapshot_id"],
            created_at=_parse_datetime(value["created_at"], "created_at"),
            required_checks=tuple(
                RequiredManualCheck.from_dict(item)
                for item in value["required_checks"]
            ),
            runtime_facts=_runtime_facts_from_payload(value["runtime_facts"]),
            status=value["status"],
            attempts=tuple(
                ManualCheckAttempt.from_dict(item) for item in value["attempts"]
            ),
        )
        if value["pending_id"] != record.pending_id:
            raise ValueError("manual check pending id mismatch")
        if value["payload_fingerprint"] != record.payload_fingerprint:
            raise ValueError("manual check payload fingerprint mismatch")
        return record


class FileManualCheckStore:
    """Atomic single-process durable store with payload integrity checks."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.root.is_dir():
            raise ValueError("manual check store root must be a directory")
        self._lock = RLock()
        self._mutation_fence = MutationLeaseGuard()

    def bind_strategy_run(self, strategy_run: object) -> None:
        self._mutation_fence.bind(strategy_run)

    @staticmethod
    def _filename(pending_id: str) -> str:
        if _PENDING_ID_RE.fullmatch(pending_id) is None:
            raise ValueError("pending_id has invalid format")
        return pending_id.removeprefix("manual-pending:") + ".json"

    def _path(self, pending_id: str) -> Path:
        return self.root / self._filename(pending_id)

    @staticmethod
    def _read(path: Path) -> ManualCheckPending:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("manual check record is unreadable") from exc
        return ManualCheckPending.from_dict(payload)

    @staticmethod
    def _write(path: Path, record: ManualCheckPending) -> None:
        payload = json.dumps(
            record.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        temporary: Path | None = None
        try:
            with NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                prefix=f".{path.stem}.",
                suffix=".tmp",
                dir=path.parent,
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def get_for_event(self, event_id: str) -> ManualCheckPending | None:
        _required_text(event_id, "event_id")
        with self._lock:
            matches = tuple(
                record
                for path in sorted(self.root.glob("*.json"))
                for record in (self._read(path),)
                if record.event_id == event_id
            )
            if len(matches) > 1:
                raise ValueError("multiple manual check records for event")
            return matches[0] if matches else None

    @mutation_fenced("manual_check_store.put_if_absent")
    def put_if_absent(self, record: ManualCheckPending) -> ManualCheckPending:
        self._mutation_fence.require()
        if type(record) is not ManualCheckPending:
            raise TypeError("record must be ManualCheckPending")
        with self._lock:
            existing = self.get_for_event(record.event_id)
            if existing is not None:
                if existing.pending_id != record.pending_id:
                    raise ValueError("manual check candidate identity conflict")
                return existing
            path = self._path(record.pending_id)
            if path.exists():
                existing = self._read(path)
                if existing != record:
                    raise ValueError("manual check pending id conflict")
                return existing
            self._write(path, record)
            return self._read(path)

    @mutation_fenced("manual_check_store.append_attempt")
    def append_attempt(
        self,
        event_id: str,
        attempt: ManualCheckAttempt,
    ) -> ManualCheckPending:
        self._mutation_fence.require()
        if type(attempt) is not ManualCheckAttempt:
            raise TypeError("attempt must be ManualCheckAttempt")
        with self._lock:
            record = self.get_for_event(event_id)
            if record is None:
                raise KeyError(event_id)
            if record.status != "pending":
                return record
            for existing in record.attempts:
                if existing.attempt_id == attempt.attempt_id:
                    if existing != attempt:
                        raise ValueError("manual check attempt identity conflict")
                    return record
            if record.attempts and attempt.submitted_at < record.attempts[-1].submitted_at:
                raise ValueError("manual check submission time moved backwards")
            updated = replace(record, attempts=record.attempts + (attempt,))
            self._write(self._path(record.pending_id), updated)
            return self._read(self._path(record.pending_id))

    @mutation_fenced("manual_check_store.mark_advanced")
    def mark_advanced(
        self,
        event_id: str,
        attempt_id: str,
    ) -> ManualCheckPending:
        self._mutation_fence.require()
        if _ATTEMPT_ID_RE.fullmatch(attempt_id) is None:
            raise ValueError("attempt_id has invalid format")
        with self._lock:
            record = self.get_for_event(event_id)
            if record is None:
                raise KeyError(event_id)
            if record.status == "approved":
                return record
            if not record.attempts or record.attempts[-1].attempt_id != attempt_id:
                raise ValueError("only latest manual check attempt can advance")
            updated = _advanced_record(record, attempt_id)
            self._write(self._path(record.pending_id), updated)
            return self._read(self._path(record.pending_id))


def _advanced_record(
    record: ManualCheckPending,
    attempt_id: str,
) -> ManualCheckPending:
    if not record.attempts or record.attempts[-1].attempt_id != attempt_id:
        raise ValueError("only latest manual check attempt can advance")
    attempt = record.attempts[-1]
    if attempt.outcome != "validated":
        raise ValueError("only a validated manual check attempt can advance")
    advanced = replace(attempt, outcome="advanced")
    return replace(
        record,
        status="approved",
        attempts=record.attempts[:-1] + (advanced,),
    )


def _has_bound_manual_transition(
    transitions: Sequence[object],
    record: ManualCheckPending,
) -> bool:
    expected_reason = manual_check_transition_reason(
        record.pending_id,
        record.payload_fingerprint,
    )
    matches = tuple(
        transition
        for transition in transitions
        if getattr(transition, "from_state", None) is EventState.RISK_CHECKED
        and getattr(transition, "to_state", None) is EventState.REVIEW_PENDING
    )
    return (
        len(matches) == 1
        and getattr(matches[0], "actor", None) == MANUAL_CHECK_TRANSITION_ACTOR
        and getattr(matches[0], "reason", None) == expected_reason
    )


@dataclass(frozen=True, slots=True)
class ManualCheckSubmissionResult:
    accepted: bool
    reasons: tuple[str, ...]
    record: ManualCheckPending
    evaluation: RuleEvaluation | None = None

    def to_dict(self) -> dict[str, object]:
        evaluation = (
            None if self.evaluation is None else to_jsonable(self.evaluation)
        )
        if evaluation is not None and not isinstance(evaluation, dict):
            raise TypeError("RuleEvaluation serialization must be an object")
        return {
            "accepted": self.accepted,
            "reasons": list(self.reasons),
            "record": self.record.to_dict(),
            "evaluation": evaluation,
        }


def _manual_checks_for_card(card: RuleCard) -> tuple[RequiredManualCheck, ...]:
    predicates = tuple(
        predicate
        for group in (
            card.candidate_predicates,
            card.confirmation_predicates,
            card.invalidation_predicates,
            card.conflict_predicates,
        )
        for predicate in group
        if predicate.mode is PredicateMode.MANUAL
    )
    by_id = {predicate.manual_check_id: predicate for predicate in predicates}
    expected = set(card.automation_boundary.manual_check_ids)
    if not expected or set(by_id) != expected or len(by_id) != len(predicates):
        raise ValueError("RuleCard manual check boundary is inconsistent")
    return tuple(
        RequiredManualCheck(
            manual_check_id=check_id,
            evidence_ids=by_id[check_id].evidence_ids,
            prompt=by_id[check_id].prompt,
        )
        for check_id in sorted(expected)
    )


class ManualCheckWorkflow:
    """Capture WATCH candidates and advance only after strict human checks."""

    def __init__(
        self,
        *,
        event_service: DecisionEventService,
        rule_engine: RuleEngine,
        store: FileManualCheckStore,
        clock: Callable[[], datetime],
    ) -> None:
        if not isinstance(event_service, DecisionEventService):
            raise TypeError("event_service must be DecisionEventService")
        if not isinstance(rule_engine, RuleEngine):
            raise TypeError("rule_engine must be RuleEngine")
        if not isinstance(store, FileManualCheckStore):
            raise TypeError("store must be FileManualCheckStore")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self.event_service = event_service
        self.rule_engine = rule_engine
        self.store = store
        self._clock = clock
        self._lock = RLock()
        self._mutation_fence = MutationLeaseGuard()

    def bind_strategy_run(self, strategy_run: object) -> None:
        self._mutation_fence.bind(strategy_run)

    def _card(self, event: DecisionEvent) -> RuleCard:
        cards = tuple(
            card
            for card in self.rule_engine.rule_set.cards
            if card.track is event.strategy_track
            and event.signal.level in card.applicable_levels
        )
        if len(cards) != 1:
            raise ValueError("exactly one RuleCard must match the event")
        return cards[0]

    @mutation_fenced("manual_check_workflow.capture_candidate")
    def capture_candidate(
        self,
        *,
        event: DecisionEvent,
        runtime_facts: RuleRuntimeFacts,
        evaluation: RuleEvaluation,
    ) -> ManualCheckPending:
        self._mutation_fence.require()
        if not isinstance(event, DecisionEvent):
            raise TypeError("event must be DecisionEvent")
        self.event_service._require_current_strategy_event(event)
        _runtime_facts_payload(runtime_facts)
        if not isinstance(evaluation, RuleEvaluation):
            raise TypeError("evaluation must be RuleEvaluation")
        if (
            evaluation.verdict is not EvaluationVerdict.WATCH
            or evaluation.safe_to_proceed
        ):
            raise ValueError("only fail-closed WATCH candidates require manual checks")
        view = self.event_service.get(event.event_id)
        if view.event != event or view.state is not EventState.RISK_CHECKED:
            raise ValueError("manual check candidate must be persisted at RISK_CHECKED")
        context = build_rule_evaluation_context(event, runtime_facts)
        if context.manual_check_input_fingerprint != event.data_fingerprint:
            raise ValueError("manual check context does not match event input")
        rebound, repeated = self.rule_engine.evaluate(event, runtime_facts)
        if rebound != event or repeated != evaluation:
            raise ValueError("initial RuleCard evaluation is not reproducible")
        card = self._card(event)
        if (
            card.rule_id != event.rule_id
            or card.version != event.rule_card_version
            or card.fingerprint != event.rule_card_fingerprint
            or self.rule_engine.rule_set.fingerprint != event.rule_set_fingerprint
        ):
            raise ValueError("RuleCard identity does not match event binding")
        risk_snapshots = self.event_service.store.list_risk_snapshots(event.event_id)
        if not risk_snapshots:
            raise ValueError("manual check candidate requires a risk snapshot")
        risk_snapshot = risk_snapshots[-1]
        if risk_snapshot.event_binding_reasons(event):
            raise ValueError("risk snapshot does not match event binding")
        record = ManualCheckPending(
            event_id=event.event_id,
            event_data_fingerprint=event.data_fingerprint,
            rule_id=event.rule_id,
            rule_card_version=event.rule_card_version,
            rule_card_fingerprint=event.rule_card_fingerprint,
            rule_set_fingerprint=event.rule_set_fingerprint,
            corpus_manifest_fingerprint=event.corpus_manifest_fingerprint,
            algorithm_fingerprint=event.algorithm_fingerprint,
            context_fingerprint=context.manual_check_input_fingerprint,
            risk_snapshot_id=risk_snapshot.snapshot_id,
            created_at=risk_snapshot.evaluated_at,
            required_checks=_manual_checks_for_card(card),
            runtime_facts=runtime_facts,
        )
        return self.store.put_if_absent(record)

    @staticmethod
    def _submission_reasons(
        record: ManualCheckPending,
        event: DecisionEvent,
        snapshots: tuple[ManualCheckSnapshot, ...],
        submitted_at: datetime,
    ) -> tuple[str, ...]:
        expected = {
            item.manual_check_id: item for item in record.required_checks
        }
        actual_ids = tuple(item.manual_check_id for item in snapshots)
        if len(set(actual_ids)) != len(actual_ids) or set(actual_ids) != set(expected):
            return ("manual_check_set_mismatch",)
        reasons: list[str] = []
        for snapshot in snapshots:
            requirement = expected[snapshot.manual_check_id]
            if snapshot.event_id != event.event_id:
                reasons.append("manual_check_event_id_mismatch")
            if snapshot.context_fingerprint != record.context_fingerprint:
                reasons.append("manual_check_context_fingerprint_mismatch")
            if snapshot.evidence_ids != requirement.evidence_ids:
                reasons.append("manual_check_evidence_ids_mismatch")
            if snapshot.recorded_at < event.observed_at:
                reasons.append("manual_check_predates_event")
            if snapshot.recorded_at > submitted_at:
                reasons.append("manual_check_recorded_in_future")
            if snapshot.value is not True:
                reasons.append("manual_check_failed")
        return tuple(dict.fromkeys(reasons))

    def _reject(
        self,
        record: ManualCheckPending,
        snapshots: tuple[ManualCheckSnapshot, ...],
        submitted_at: datetime,
        reasons: tuple[str, ...],
    ) -> ManualCheckSubmissionResult:
        attempt = ManualCheckAttempt(
            pending_id=record.pending_id,
            submitted_at=submitted_at,
            snapshots=snapshots,
            outcome="rejected",
            reasons=reasons,
        )
        updated = self.store.append_attempt(record.event_id, attempt)
        return ManualCheckSubmissionResult(False, reasons, updated)

    @mutation_fenced("manual_check_workflow.submit")
    def submit(
        self,
        event_id: str,
        snapshots: Sequence[ManualCheckSnapshot],
    ) -> ManualCheckSubmissionResult:
        self._mutation_fence.require()
        _required_text(event_id, "event_id")
        if isinstance(snapshots, (str, bytes)) or not isinstance(
            snapshots, Sequence
        ):
            raise ValueError("snapshots must contain ManualCheckSnapshot")
        values = tuple(snapshots)
        if not all(type(item) is ManualCheckSnapshot for item in values):
            raise ValueError("snapshots must contain ManualCheckSnapshot")
        for snapshot in values:
            validate_manual_check_snapshot(snapshot)
        values = tuple(sorted(values, key=lambda item: item.manual_check_id))
        with self._lock:
            record = self.store.get_for_event(event_id)
            if record is None:
                raise KeyError(event_id)
            view = self.event_service.get(event_id)
            event = view.event
            self.event_service._require_current_strategy_event(event)
            if (
                event.data_fingerprint != record.event_data_fingerprint
                or event.rule_id != record.rule_id
                or event.rule_card_version != record.rule_card_version
                or event.rule_card_fingerprint != record.rule_card_fingerprint
                or event.rule_set_fingerprint != record.rule_set_fingerprint
                or event.corpus_manifest_fingerprint
                != record.corpus_manifest_fingerprint
                or event.algorithm_fingerprint != record.algorithm_fingerprint
            ):
                raise ValueError("persisted event no longer matches manual check record")
            if record.status == "approved":
                if (
                    record.attempts
                    and record.attempts[-1].outcome == "advanced"
                    and record.attempts[-1].snapshots == values
                    and _has_bound_manual_transition(view.transitions, record)
                ):
                    return ManualCheckSubmissionResult(True, (), record)
                return ManualCheckSubmissionResult(
                    False,
                    ("manual_check_record_already_approved",),
                    record,
                )
            if (
                record.attempts
                and record.attempts[-1].outcome == "validated"
                and record.attempts[-1].snapshots == values
            ):
                preview = _advanced_record(
                    record,
                    record.attempts[-1].attempt_id,
                )
                if _has_bound_manual_transition(view.transitions, preview):
                    advanced = self.store.mark_advanced(
                        event_id,
                        record.attempts[-1].attempt_id,
                    )
                    return ManualCheckSubmissionResult(True, (), advanced)
            if view.state is not EventState.RISK_CHECKED:
                raise ValueError("manual check transition binding mismatch")
            submitted_at = normalize_datetime(self._clock(), "clock")
            if submitted_at < record.created_at:
                raise ValueError("trusted clock moved backwards")
            reasons = self._submission_reasons(
                record,
                event,
                values,
                submitted_at,
            )
            if reasons:
                return self._reject(record, values, submitted_at, reasons)

            risk_snapshot = self.event_service.store.get_risk_snapshot(
                record.risk_snapshot_id
            )
            if risk_snapshot is None:
                return self._reject(
                    record,
                    values,
                    submitted_at,
                    ("risk_snapshot_missing",),
                )
            validation = risk_snapshot.validate_for_review(
                event,
                as_of=submitted_at,
            )
            if not validation.usable:
                return self._reject(
                    record,
                    values,
                    submitted_at,
                    validation.reasons,
                )

            runtime_facts = replace(record.runtime_facts, manual_checks=values)
            rebound, evaluation = self.rule_engine.evaluate(event, runtime_facts)
            if (
                rebound != event
                or evaluation.evaluation_input_fingerprint
                != record.context_fingerprint
                or evaluation.verdict is not EvaluationVerdict.CONFIRM
                or evaluation.safe_to_proceed is not True
            ):
                return self._reject(
                    record,
                    values,
                    submitted_at,
                    ("rule_re_evaluation_not_confirmed",),
                )
            latest_attempt = record.attempts[-1] if record.attempts else None
            if (
                latest_attempt is not None
                and latest_attempt.outcome == "validated"
                and latest_attempt.snapshots == values
                and latest_attempt.evaluation_verdict == evaluation.verdict.value
                and latest_attempt.evaluation_input_fingerprint
                == evaluation.evaluation_input_fingerprint
            ):
                attempt = latest_attempt
                validated_record = record
            else:
                attempt = ManualCheckAttempt(
                    pending_id=record.pending_id,
                    submitted_at=submitted_at,
                    snapshots=values,
                    outcome="validated",
                    reasons=(),
                    evaluation_verdict=evaluation.verdict.value,
                    evaluation_input_fingerprint=(
                        evaluation.evaluation_input_fingerprint
                    ),
                )
                validated_record = self.store.append_attempt(event_id, attempt)
            advance_at = normalize_datetime(self._clock(), "clock")
            if advance_at < submitted_at:
                raise ValueError("trusted clock moved backwards")
            final_validation = risk_snapshot.validate_for_review(
                event,
                as_of=advance_at,
            )
            if not final_validation.usable:
                current = self.store.get_for_event(event_id)
                if current is None:
                    raise RuntimeError("manual check record disappeared")
                return self._reject(
                    current,
                    values,
                    advance_at,
                    final_validation.reasons,
                )
            current_state = self.event_service.get(event_id).state
            if current_state is EventState.RISK_CHECKED:
                preview = _advanced_record(validated_record, attempt.attempt_id)
                self.event_service.mark_review_pending(
                    event_id,
                    risk_snapshot.decision,
                    occurred_at=advance_at,
                    manual_check_pending_id=preview.pending_id,
                    manual_check_payload_fingerprint=preview.payload_fingerprint,
                )
            elif current_state is not EventState.REVIEW_PENDING:
                raise ValueError("event is not eligible for manual-check advance")
            advanced = self.store.mark_advanced(event_id, attempt.attempt_id)
            return ManualCheckSubmissionResult(
                True,
                (),
                advanced,
                evaluation,
            )


__all__ = [
    "FileManualCheckStore",
    "ManualCheckAttempt",
    "ManualCheckPending",
    "ManualCheckSubmissionResult",
    "ManualCheckWorkflow",
    "RequiredManualCheck",
    "manual_check_snapshot_from_dict",
]
