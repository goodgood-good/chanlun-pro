"""Certified original-text evidence bindings for analysis-only exit triggers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from .corpus_loader import CertifiedLessonCorpus
from .corpus_types import EvidenceUnit, SourceTier
from .exits import ExitTrigger


_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_EVIDENCE_ID_RE = re.compile(r"evidence:[0-9a-f]{64}")
_AUTHORITATIVE_ROLES = frozenset(
    {"lesson_body", "chan_reply", "chan_excerpt"}
)
_POLICY_FIELDS = frozenset(
    {
        "schema_version",
        "policy_id",
        "version",
        "corpus_manifest_sha256",
        "source_pdf_sha256",
        "bindings",
    }
)
_BINDING_FIELDS = frozenset(
    {"trigger", "references", "boundary_tags"}
)
_REFERENCE_FIELDS = frozenset(
    {
        "evidence_id",
        "lesson",
        "pdf_page",
        "source_role",
        "evidence_sha256",
    }
)
_TRIGGERS = tuple(ExitTrigger)
_TRIGGER_ORDER = {trigger: index for index, trigger in enumerate(_TRIGGERS)}


def _require_trimmed(value: object, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
    ):
        raise ValueError(f"{field_name} must be a non-empty trimmed string")
    return value


def _require_sha256(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256")
    return value


def _require_positive_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _require_nonnegative_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return value


def _require_fields(
    value: Mapping[str, object],
    expected: frozenset[str],
    label: str,
) -> None:
    if set(value) != expected:
        raise ValueError(f"{label} fields mismatch")


def _sequence(value: object, field_name: str) -> tuple[object, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{field_name} must be a sequence")
    return tuple(value)


def _string_tuple(value: object, field_name: str) -> tuple[str, ...]:
    items = tuple(
        _require_trimmed(item, f"{field_name} item")
        for item in _sequence(value, field_name)
    )
    if len(items) != len(set(items)):
        raise ValueError(f"{field_name} contains duplicates")
    return items


@dataclass(frozen=True, slots=True)
class ExitEvidenceReference:
    evidence_id: str
    lesson: int
    pdf_page: int
    source_role: str
    evidence_sha256: str

    def __post_init__(self) -> None:
        if _EVIDENCE_ID_RE.fullmatch(self.evidence_id) is None:
            raise ValueError(
                "evidence_id must use evidence:<64 lowercase hex>"
            )
        _require_nonnegative_int(self.lesson, "lesson")
        _require_positive_int(self.pdf_page, "pdf_page")
        _require_trimmed(self.source_role, "source_role")
        if self.source_role not in _AUTHORITATIVE_ROLES:
            raise ValueError("source_role must be an authoritative original role")
        _require_sha256(self.evidence_sha256, "evidence_sha256")

    def to_dict(self) -> dict[str, object]:
        return {
            "evidence_id": self.evidence_id,
            "lesson": self.lesson,
            "pdf_page": self.pdf_page,
            "source_role": self.source_role,
            "evidence_sha256": self.evidence_sha256,
        }


@dataclass(frozen=True, slots=True)
class ExitEvidenceBinding:
    trigger: ExitTrigger
    references: tuple[ExitEvidenceReference, ...]
    boundary_tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.trigger, ExitTrigger):
            raise TypeError("trigger must be ExitTrigger")
        references = tuple(self.references)
        if not references or not all(
            isinstance(item, ExitEvidenceReference) for item in references
        ):
            raise ValueError(
                "references must contain original evidence references"
            )
        identities = tuple(item.evidence_id for item in references)
        if len(identities) != len(set(identities)):
            raise ValueError("references contain duplicate evidence_id")
        boundary_tags = tuple(self.boundary_tags)
        if any(
            not isinstance(item, str)
            or not item.strip()
            or item != item.strip()
            for item in boundary_tags
        ):
            raise ValueError("boundary_tags must contain trimmed strings")
        if len(boundary_tags) != len(set(boundary_tags)):
            raise ValueError("boundary_tags contains duplicates")
        object.__setattr__(self, "references", references)
        object.__setattr__(self, "boundary_tags", boundary_tags)

    @property
    def evidence_ids(self) -> tuple[str, ...]:
        return tuple(item.evidence_id for item in self.references)

    def to_dict(self) -> dict[str, object]:
        return {
            "trigger": self.trigger.value,
            "references": [item.to_dict() for item in self.references],
            "boundary_tags": list(self.boundary_tags),
        }


@dataclass(frozen=True, slots=True)
class ExitEvidencePolicy:
    schema_version: int
    policy_id: str
    version: int
    corpus_manifest_sha256: str
    source_pdf_sha256: str
    bindings: tuple[ExitEvidenceBinding, ...]

    def __post_init__(self) -> None:
        if self.schema_version != 1 or isinstance(self.schema_version, bool):
            raise ValueError("unsupported exit evidence policy schema version")
        _require_trimmed(self.policy_id, "policy_id")
        _require_positive_int(self.version, "version")
        _require_sha256(
            self.corpus_manifest_sha256,
            "corpus_manifest_sha256",
        )
        _require_sha256(self.source_pdf_sha256, "source_pdf_sha256")
        bindings = tuple(self.bindings)
        if not all(isinstance(item, ExitEvidenceBinding) for item in bindings):
            raise TypeError("bindings must contain ExitEvidenceBinding values")
        triggers = tuple(item.trigger for item in bindings)
        if len(triggers) != len(set(triggers)):
            raise ValueError("duplicate exit trigger binding")
        if set(triggers) != set(_TRIGGERS):
            raise ValueError("bindings must cover the complete exit trigger set")
        hard_risk = next(
            item for item in bindings if item.trigger is ExitTrigger.HARD_RISK
        )
        if "project_risk_latch" not in hard_risk.boundary_tags:
            raise ValueError(
                "hard_risk binding must declare project_risk_latch"
            )
        object.__setattr__(
            self,
            "bindings",
            tuple(sorted(bindings, key=lambda item: _TRIGGER_ORDER[item.trigger])),
        )

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(payload).hexdigest()

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "policy_id": self.policy_id,
            "version": self.version,
            "corpus_manifest_sha256": self.corpus_manifest_sha256,
            "source_pdf_sha256": self.source_pdf_sha256,
            "bindings": [item.to_dict() for item in self.bindings],
        }

    def binding(self, trigger: ExitTrigger) -> ExitEvidenceBinding:
        if not isinstance(trigger, ExitTrigger):
            raise TypeError("trigger must be ExitTrigger")
        for binding in self.bindings:
            if binding.trigger is trigger:
                return binding
        raise KeyError(f"exit evidence binding is missing: {trigger.value}")

    def resolve(self, trigger: ExitTrigger) -> tuple[str, ...]:
        evidence_ids = self.binding(trigger).evidence_ids
        if not evidence_ids:
            raise ValueError(f"original evidence is missing: {trigger.value}")
        return evidence_ids

    def __call__(self, trigger: ExitTrigger) -> tuple[str, ...]:
        return self.resolve(trigger)


def _reference(value: object) -> ExitEvidenceReference:
    if not isinstance(value, Mapping):
        raise ValueError("reference must be a mapping")
    _require_fields(value, _REFERENCE_FIELDS, "reference")
    return ExitEvidenceReference(
        evidence_id=value["evidence_id"],
        lesson=value["lesson"],
        pdf_page=value["pdf_page"],
        source_role=value["source_role"],
        evidence_sha256=value["evidence_sha256"],
    )


def _binding(value: object) -> ExitEvidenceBinding:
    if not isinstance(value, Mapping):
        raise ValueError("binding must be a mapping")
    _require_fields(value, _BINDING_FIELDS, "binding")
    raw_trigger = value["trigger"]
    try:
        trigger = ExitTrigger(raw_trigger)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"unknown exit trigger: {raw_trigger}") from exc
    references = tuple(
        _reference(item)
        for item in _sequence(value["references"], "references")
    )
    return ExitEvidenceBinding(
        trigger=trigger,
        references=references,
        boundary_tags=_string_tuple(value["boundary_tags"], "boundary_tags"),
    )


def _validate_original_reference(
    reference: ExitEvidenceReference,
    unit: EvidenceUnit,
    *,
    source_pdf_sha256: str,
) -> None:
    if unit.source_tier is not SourceTier.LESSON_ORIGINAL:
        raise ValueError(
            f"evidence {reference.evidence_id} must be lesson_original"
        )
    if unit.source_role not in _AUTHORITATIVE_ROLES:
        raise ValueError(
            f"evidence {reference.evidence_id} must have an authoritative original role"
        )
    if (
        unit.source_pdf_sha256 != source_pdf_sha256
        or not unit.source_record_ids
        or unit.source_record_id not in unit.source_record_ids
        or any(
            not isinstance(record_id, str)
            or not record_id.startswith("source:")
            for record_id in unit.source_record_ids
        )
        or unit.bbox is None
        or unit.page_number is None
    ):
        raise ValueError(
            f"evidence {reference.evidence_id} original provenance is incomplete"
        )
    if unit.lesson != reference.lesson:
        raise ValueError(f"evidence {reference.evidence_id} lesson mismatch")
    if unit.page_number != reference.pdf_page:
        raise ValueError(f"evidence {reference.evidence_id} PDF page mismatch")
    if unit.source_role != reference.source_role:
        raise ValueError(
            f"evidence {reference.evidence_id} source role mismatch"
        )
    if unit.sha256 != reference.evidence_sha256:
        raise ValueError(
            f"evidence {reference.evidence_id} content fingerprint mismatch"
        )


def load_exit_evidence_policy(
    document: Mapping[str, object],
    *,
    corpus: CertifiedLessonCorpus,
) -> ExitEvidencePolicy:
    """Load a complete policy or reject it without producing a resolver."""

    if not isinstance(document, Mapping):
        raise TypeError("exit evidence policy document must be a mapping")
    if not isinstance(corpus, CertifiedLessonCorpus):
        raise TypeError("corpus must be CertifiedLessonCorpus")
    _require_fields(document, _POLICY_FIELDS, "exit evidence policy")
    if (
        _SHA256_RE.fullmatch(corpus.manifest_sha256) is None
        or _SHA256_RE.fullmatch(corpus.source_pdf_sha256) is None
    ):
        raise ValueError("certified corpus identity is invalid")
    if document["corpus_manifest_sha256"] != corpus.manifest_sha256:
        raise ValueError("certified corpus manifest fingerprint mismatch")
    if document["source_pdf_sha256"] != corpus.source_pdf_sha256:
        raise ValueError("certified source PDF fingerprint mismatch")

    policy = ExitEvidencePolicy(
        schema_version=document["schema_version"],
        policy_id=document["policy_id"],
        version=document["version"],
        corpus_manifest_sha256=document["corpus_manifest_sha256"],
        source_pdf_sha256=document["source_pdf_sha256"],
        bindings=tuple(
            _binding(item)
            for item in _sequence(document["bindings"], "bindings")
        ),
    )
    evidence_by_id: dict[str, EvidenceUnit] = {}
    for unit in corpus.semantic_units:
        if unit.evidence_id in evidence_by_id:
            raise ValueError(f"duplicate evidence_id: {unit.evidence_id}")
        evidence_by_id[unit.evidence_id] = unit
    for binding in policy.bindings:
        for reference in binding.references:
            unit = evidence_by_id.get(reference.evidence_id)
            if unit is None:
                raise ValueError(
                    f"exit evidence does not exist: {reference.evidence_id}"
                )
            _validate_original_reference(
                reference,
                unit,
                source_pdf_sha256=corpus.source_pdf_sha256,
            )
    return policy


def _strict_json_object(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_exit_evidence_policy_file(
    path: str | Path,
    *,
    corpus: CertifiedLessonCorpus,
) -> ExitEvidencePolicy:
    policy_path = Path(path)
    if not policy_path.is_file():
        raise ValueError("exit evidence policy file is missing")
    try:
        document = json.loads(
            policy_path.read_text(encoding="utf-8"),
            object_pairs_hook=_strict_json_object,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("exit evidence policy file is unreadable") from exc
    if not isinstance(document, Mapping):
        raise ValueError("exit evidence policy document must be an object")
    return load_exit_evidence_policy(document, corpus=corpus)


__all__ = [
    "ExitEvidenceBinding",
    "ExitEvidencePolicy",
    "ExitEvidenceReference",
    "load_exit_evidence_policy",
    "load_exit_evidence_policy_file",
]

