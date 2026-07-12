from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
import re

from .fingerprints import normalize_datetime, sha256_json


_FINGERPRINT_RE = re.compile(r"sha256:[0-9a-f]{64}")


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _fingerprint(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _FINGERPRINT_RE.fullmatch(value) is None:
        raise ValueError(f"{field_name} must use sha256:<64 lowercase hex>")
    return value


def _evidence_ids(value: object) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError("evidence_ids must contain non-empty strings")
    result = tuple(value)
    if not result or not all(isinstance(item, str) and bool(item) for item in result):
        raise ValueError("evidence_ids must contain non-empty strings")
    if len(set(result)) != len(result):
        raise ValueError("evidence_ids contains duplicates")
    return tuple(sorted(result))


@dataclass(frozen=True, slots=True)
class ManualCheckSnapshot:
    manual_check_id: str
    value: bool
    operator_id: str
    recorded_at: datetime
    event_id: str
    context_fingerprint: str
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _required_text(self.manual_check_id, "manual_check_id")
        if not isinstance(self.value, bool):
            raise ValueError("value must be boolean")
        _required_text(self.operator_id, "operator_id")
        object.__setattr__(
            self,
            "recorded_at",
            normalize_datetime(self.recorded_at, "recorded_at"),
        )
        _required_text(self.event_id, "event_id")
        _fingerprint(self.context_fingerprint, "context_fingerprint")
        object.__setattr__(self, "evidence_ids", _evidence_ids(self.evidence_ids))

    @property
    def fingerprint(self) -> str:
        return sha256_json(self)


def validate_manual_check_snapshot(value: object) -> ManualCheckSnapshot:
    if type(value) is not ManualCheckSnapshot:
        raise ValueError("manual_checks must contain ManualCheckSnapshot")
    _required_text(value.manual_check_id, "manual_check_id")
    if not isinstance(value.value, bool):
        raise ValueError("value must be boolean")
    _required_text(value.operator_id, "operator_id")
    normalized_at = normalize_datetime(value.recorded_at, "recorded_at")
    if normalized_at != value.recorded_at:
        raise ValueError("recorded_at must be normalized")
    _required_text(value.event_id, "event_id")
    _fingerprint(value.context_fingerprint, "context_fingerprint")
    normalized_evidence = _evidence_ids(value.evidence_ids)
    if not isinstance(value.evidence_ids, tuple) or (
        normalized_evidence != value.evidence_ids
    ):
        raise ValueError("evidence_ids must be a canonical tuple")
    return value


@dataclass(frozen=True, slots=True)
class ManualCheckAudit:
    event_id: str
    context_fingerprint: str
    snapshots: tuple[ManualCheckSnapshot, ...] = ()

    def __post_init__(self) -> None:
        _required_text(self.event_id, "event_id")
        _fingerprint(self.context_fingerprint, "context_fingerprint")
        if isinstance(self.snapshots, (str, bytes)) or not isinstance(
            self.snapshots, Sequence
        ):
            raise ValueError("manual_checks must contain ManualCheckSnapshot")
        snapshots = tuple(self.snapshots)
        if not all(type(item) is ManualCheckSnapshot for item in snapshots):
            raise ValueError("manual_checks must contain ManualCheckSnapshot")
        for snapshot in snapshots:
            validate_manual_check_snapshot(snapshot)
        identifiers = tuple(item.manual_check_id for item in snapshots)
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("duplicate manual_check_id")
        if any(item.event_id != self.event_id for item in snapshots):
            raise ValueError("manual check event_id mismatch")
        if any(
            item.context_fingerprint != self.context_fingerprint for item in snapshots
        ):
            raise ValueError("manual check context fingerprint mismatch")
        object.__setattr__(
            self,
            "snapshots",
            tuple(sorted(snapshots, key=lambda item: item.manual_check_id)),
        )

    @property
    def fingerprint(self) -> str:
        return sha256_json(self)


def validate_manual_check_audit(value: object) -> ManualCheckAudit:
    if type(value) is not ManualCheckAudit:
        raise ValueError("manual_check_audit must be a ManualCheckAudit")
    rebuilt = ManualCheckAudit(
        event_id=value.event_id,
        context_fingerprint=value.context_fingerprint,
        snapshots=value.snapshots,
    )
    if rebuilt != value:
        raise ValueError("manual_check_audit is not canonical")
    return value


@dataclass(frozen=True, slots=True)
class RuleEvaluationContext(Mapping[str, object]):
    data_fingerprint: str
    manual_check_audit: ManualCheckAudit
    _values: Mapping[str, object] = field(repr=False)

    def __post_init__(self) -> None:
        _fingerprint(self.data_fingerprint, "data_fingerprint")
        if type(self.manual_check_audit) is not ManualCheckAudit:
            raise ValueError("manual_check_audit must be a ManualCheckAudit")
        if not isinstance(self._values, Mapping):
            raise ValueError("context values must be a mapping")

    @property
    def manual_check_input_fingerprint(self) -> str:
        return self.manual_check_audit.context_fingerprint

    def __getitem__(self, key: str) -> object:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)
