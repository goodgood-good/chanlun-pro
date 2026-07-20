from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
import json
import math
from pathlib import Path
import re
from typing import Mapping, Sequence

from .corpus_loader import CertifiedLessonCorpus
from .corpus_types import EvidenceUnit, ImageEvidence, SourceTier
from .fingerprints import sha256_json
from .manual_checks import (
    RuleEvaluationContext,
    validate_manual_check_audit,
)
from .models import StrategyTrack


class PredicateMode(str, Enum):
    MACHINE = "machine"
    MANUAL = "manual"


class PredicateOperator(str, Enum):
    EQ = "eq"
    NE = "ne"
    LT = "lt"
    LTE = "lte"
    GT = "gt"
    GTE = "gte"
    IN = "in"
    NOT_IN = "not_in"
    CONTAINS = "contains"
    IS_TRUE = "is_true"
    IS_FALSE = "is_false"


class ProjectFieldKind(str, Enum):
    BOOLEAN = "boolean"
    INTEGER = "integer"
    NUMBER = "number"
    STRING = "string"
    STRING_SEQUENCE = "string_sequence"


class FieldResolutionStatus(str, Enum):
    VALUE = "value"
    MISSING = "missing"
    NULL = "null"
    INCONSISTENT = "inconsistent"


class EvaluationVerdict(str, Enum):
    CONFIRM = "CONFIRM"
    WATCH = "WATCH"
    REJECT = "REJECT"


@dataclass(frozen=True, slots=True)
class ProjectFieldSpec:
    path: str
    kind: ProjectFieldKind
    nullable: bool = False
    level: int | None = None


@dataclass(frozen=True, slots=True)
class FieldResolution:
    path: str
    status: FieldResolutionStatus
    value: object = None
    detail: str = ""


_EXACT_PROJECT_FIELDS = {
    "comparison.ok": (ProjectFieldKind.BOOLEAN, False),
    "fundamental.ok": (ProjectFieldKind.BOOLEAN, False),
    "market.is_tradeable": (ProjectFieldKind.BOOLEAN, False),
    "market.liquid": (ProjectFieldKind.BOOLEAN, False),
    "risk.allowed": (ProjectFieldKind.BOOLEAN, False),
    "risk.stop_breached": (ProjectFieldKind.BOOLEAN, False),
    "signal.bs_type": (ProjectFieldKind.STRING, False),
    "signal.confirmation_bs_type": (ProjectFieldKind.STRING, True),
    "signal.divergence_kind": (ProjectFieldKind.STRING, True),
    "signal.level": (ProjectFieldKind.INTEGER, False),
    "signal.live_divergence": (ProjectFieldKind.BOOLEAN, False),
    "signal.nest_depth": (ProjectFieldKind.INTEGER, False),
    "signal.nest_operable": (ProjectFieldKind.BOOLEAN, True),
    "signal.price": (ProjectFieldKind.NUMBER, False),
    "signal.structural_stop_above": (ProjectFieldKind.NUMBER, True),
    "signal.structural_stop_below": (ProjectFieldKind.NUMBER, True),
    "signal.zs_zd": (ProjectFieldKind.NUMBER, True),
    "signal.zs_zg": (ProjectFieldKind.NUMBER, True),
}
_LEVEL_FIELD_KINDS = {
    "completed_bar_count": (ProjectFieldKind.INTEGER, False),
    "direction": (ProjectFieldKind.STRING, False),
    "latest_bar_closed": (ProjectFieldKind.BOOLEAN, False),
    "mmds": (ProjectFieldKind.STRING_SEQUENCE, False),
}
_LEVEL_FIELD_RE = re.compile(
    r"levels\.(?P<level>0|[1-9][0-9]*)\.(?P<name>[a-z_]+)"
)
PROJECT_FIELD_WHITELIST = tuple(sorted(_EXACT_PROJECT_FIELDS)) + tuple(
    f"levels.<level>.{name}" for name in sorted(_LEVEL_FIELD_KINDS)
)
_RULE_ID_RE = re.compile(r"[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*")
_RULE_SET_FIELDS = frozenset({"schema_version", "cards"})
_RULE_CARD_FIELDS = frozenset(
    {
        "algorithm_version",
        "applicable_levels",
        "automation_boundary",
        "candidate_predicates",
        "completed_bar_requirements",
        "concepts",
        "conflict_predicates",
        "confirmation_predicates",
        "counterevidence",
        "data_requirements",
        "evidence",
        "invalidation_predicates",
        "project_fields",
        "rule_id",
        "track",
        "version",
    }
)
_MACHINE_PREDICATE_BASE_FIELDS = frozenset(
    {"evidence_ids", "field", "mode", "operator", "predicate_id"}
)
_MANUAL_PREDICATE_FIELDS = frozenset(
    {"evidence_ids", "manual_check_id", "mode", "predicate_id", "prompt"}
)
_AUTOMATION_BOUNDARY_FIELDS = frozenset(
    {"machine_predicate_ids", "manual_check_ids"}
)
_EVIDENCE_REFERENCE_FIELDS = frozenset(
    {"evidence_id", "lesson", "pdf_pages", "lesson_chart_ids"}
)
_DATA_REQUIREMENT_FIELDS = frozenset({"required_fields", "required_levels"})
_COMPLETED_BAR_REQUIREMENT_FIELDS = frozenset(
    {"level", "minimum_count", "require_latest_closed"}
)


def _require_fields(
    value: Mapping[str, object], expected: frozenset[str], label: str
) -> None:
    if set(value) != expected:
        raise ValueError(f"{label} fields mismatch")


def _sequence(value: object, label: str) -> tuple[object, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise ValueError(f"{label} must be a sequence")
    return tuple(value)


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) for key in value
    ):
        raise ValueError(f"{label} must be a mapping with string keys")
    return value


def _unique_strings(value: object, label: str) -> tuple[str, ...]:
    items = _sequence(value, label)
    if not items or not all(isinstance(item, str) and item for item in items):
        raise ValueError(f"{label} must contain non-empty strings")
    if len(set(items)) != len(items):
        raise ValueError(f"{label} contains duplicates")
    return tuple(sorted(items))


def _unique_strings_allow_empty(value: object, label: str) -> tuple[str, ...]:
    items = _sequence(value, label)
    if not all(isinstance(item, str) and item for item in items):
        raise ValueError(f"{label} must contain non-empty strings")
    if len(set(items)) != len(items):
        raise ValueError(f"{label} contains duplicates")
    return tuple(sorted(items))


def _unique_levels(value: object, label: str) -> tuple[int, ...]:
    items = _sequence(value, label)
    if not items or not all(
        isinstance(item, int) and not isinstance(item, bool) and item >= 0
        for item in items
    ):
        raise ValueError(f"{label} must contain non-negative integers")
    if len(set(items)) != len(items):
        raise ValueError(f"{label} contains duplicates")
    return tuple(sorted(items))


def _positive_ints(value: object, label: str) -> tuple[int, ...]:
    items = _sequence(value, label)
    if not items or not all(
        isinstance(item, int) and not isinstance(item, bool) and item > 0
        for item in items
    ):
        raise ValueError(f"{label} must contain positive integers")
    if len(set(items)) != len(items):
        raise ValueError(f"{label} contains duplicates")
    return tuple(sorted(items))


def project_field_spec(path: str) -> ProjectFieldSpec:
    if not isinstance(path, str) or not path:
        raise ValueError("unknown project field")
    exact = _EXACT_PROJECT_FIELDS.get(path)
    if exact is not None:
        return ProjectFieldSpec(path, exact[0], exact[1])
    match = _LEVEL_FIELD_RE.fullmatch(path)
    if match is None or match.group("name") not in _LEVEL_FIELD_KINDS:
        raise ValueError(f"unknown project field: {path}")
    kind, nullable = _LEVEL_FIELD_KINDS[match.group("name")]
    return ProjectFieldSpec(
        path,
        kind,
        nullable,
        int(match.group("level")),
    )


def _finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, TypeError, ValueError):
        return False


def _expected_matches(kind: ProjectFieldKind, value: object) -> bool:
    if kind is ProjectFieldKind.BOOLEAN:
        return isinstance(value, bool)
    if kind is ProjectFieldKind.INTEGER:
        return isinstance(value, int) and not isinstance(value, bool)
    if kind is ProjectFieldKind.NUMBER:
        return _finite_number(value)
    if kind is ProjectFieldKind.STRING:
        return isinstance(value, str)
    if kind is ProjectFieldKind.STRING_SEQUENCE:
        return (
            isinstance(value, tuple)
            and bool(value)
            and all(isinstance(item, str) for item in value)
        )
    return False


def _runtime_value_matches(kind: ProjectFieldKind, value: object) -> bool:
    if kind is ProjectFieldKind.STRING_SEQUENCE:
        return (
            isinstance(value, (list, tuple))
            and all(isinstance(item, str) for item in value)
        )
    return _expected_matches(kind, value)


def derive_structural_stop_breached(
    *,
    bs_type: str,
    latest_price: object,
    stop_below: object,
    stop_above: object,
) -> bool | None:
    if not isinstance(bs_type, str) or not _finite_number(latest_price):
        return None
    if "buy" in bs_type:
        if not _finite_number(stop_below):
            return None
        return float(latest_price) <= float(stop_below)
    if "sell" in bs_type:
        if not _finite_number(stop_above):
            return None
        return float(latest_price) >= float(stop_above)
    return None


def resolve_project_field(
    context: object,
    path: str,
) -> FieldResolution:
    spec = project_field_spec(path)
    current = context
    for segment in path.split("."):
        if current is None:
            return FieldResolution(
                path,
                FieldResolutionStatus.NULL,
                detail=f"null before segment {segment}",
            )
        if isinstance(current, Mapping):
            has_text = segment in current
            numeric_key = int(segment) if segment.isdecimal() else None
            has_numeric = numeric_key is not None and numeric_key in current
            if has_text and has_numeric:
                return FieldResolution(
                    path,
                    FieldResolutionStatus.INCONSISTENT,
                    detail=f"ambiguous mapping keys for segment {segment}",
                )
            if has_text:
                current = current[segment]
                continue
            if has_numeric:
                current = current[numeric_key]
                continue
            return FieldResolution(
                path,
                FieldResolutionStatus.MISSING,
                detail=f"missing segment {segment}",
            )
        return FieldResolution(
            path,
            FieldResolutionStatus.INCONSISTENT,
            detail="unsupported context object",
        )
    if current is None:
        return FieldResolution(path, FieldResolutionStatus.NULL)
    if not _runtime_value_matches(spec.kind, current):
        return FieldResolution(
            path,
            FieldResolutionStatus.INCONSISTENT,
            value=current,
            detail=f"runtime type does not match {spec.kind.value}",
        )
    if isinstance(current, list):
        current = tuple(current)
    return FieldResolution(path, FieldResolutionStatus.VALUE, current)


def _validate_predicate_operator(
    predicate: Predicate,
    spec: ProjectFieldSpec,
) -> None:
    operator = predicate.operator
    if operator is None:
        raise ValueError("machine predicate requires an operator")
    allowed = {
        ProjectFieldKind.BOOLEAN: {
            PredicateOperator.EQ,
            PredicateOperator.NE,
            PredicateOperator.IS_TRUE,
            PredicateOperator.IS_FALSE,
        },
        ProjectFieldKind.INTEGER: {
            PredicateOperator.EQ,
            PredicateOperator.NE,
            PredicateOperator.LT,
            PredicateOperator.LTE,
            PredicateOperator.GT,
            PredicateOperator.GTE,
            PredicateOperator.IN,
            PredicateOperator.NOT_IN,
        },
        ProjectFieldKind.NUMBER: {
            PredicateOperator.EQ,
            PredicateOperator.NE,
            PredicateOperator.LT,
            PredicateOperator.LTE,
            PredicateOperator.GT,
            PredicateOperator.GTE,
            PredicateOperator.IN,
            PredicateOperator.NOT_IN,
        },
        ProjectFieldKind.STRING: {
            PredicateOperator.EQ,
            PredicateOperator.NE,
            PredicateOperator.IN,
            PredicateOperator.NOT_IN,
            PredicateOperator.CONTAINS,
        },
        ProjectFieldKind.STRING_SEQUENCE: {
            PredicateOperator.EQ,
            PredicateOperator.NE,
            PredicateOperator.CONTAINS,
        },
    }[spec.kind]
    if operator not in allowed:
        raise ValueError(
            f"operator {operator.value} is incompatible with {spec.path}"
        )
    if operator in {PredicateOperator.IS_TRUE, PredicateOperator.IS_FALSE}:
        return
    if operator in {PredicateOperator.IN, PredicateOperator.NOT_IN}:
        if not isinstance(predicate.expected, tuple) or not predicate.expected:
            raise ValueError(
                f"{operator.value} expected must be a non-empty sequence"
            )
        if len({sha256_json(item) for item in predicate.expected}) != len(
            predicate.expected
        ):
            raise ValueError(f"{operator.value} expected contains duplicates")
        if not all(
            _expected_matches(spec.kind, item) for item in predicate.expected
        ):
            raise ValueError("expected value type mismatch")
        return
    if operator is PredicateOperator.CONTAINS:
        if not isinstance(predicate.expected, str):
            raise ValueError("expected value type mismatch")
        return
    if not _expected_matches(spec.kind, predicate.expected):
        raise ValueError("expected value type mismatch")


@dataclass(frozen=True, slots=True)
class EvidenceReference:
    evidence_id: str
    lesson: int
    pdf_pages: tuple[int, ...]
    lesson_chart_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Predicate:
    predicate_id: str
    mode: PredicateMode
    evidence_ids: tuple[str, ...]
    field: str | None = None
    operator: PredicateOperator | None = None
    expected: object = None
    manual_check_id: str | None = None
    prompt: str | None = None


@dataclass(frozen=True, slots=True)
class DataRequirements:
    required_fields: tuple[str, ...]
    required_levels: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class CompletedBarRequirement:
    level: int
    minimum_count: int
    require_latest_closed: bool


@dataclass(frozen=True, slots=True)
class AutomationBoundary:
    machine_predicate_ids: tuple[str, ...]
    manual_check_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RuleCard:
    rule_id: str
    version: int
    track: StrategyTrack
    applicable_levels: tuple[int, ...]
    algorithm_version: str
    concepts: tuple[str, ...]
    evidence: tuple[EvidenceReference, ...]
    counterevidence: tuple[EvidenceReference, ...]
    project_fields: tuple[str, ...]
    data_requirements: DataRequirements
    completed_bar_requirements: tuple[CompletedBarRequirement, ...]
    candidate_predicates: tuple[Predicate, ...]
    confirmation_predicates: tuple[Predicate, ...]
    invalidation_predicates: tuple[Predicate, ...]
    conflict_predicates: tuple[Predicate, ...]
    automation_boundary: AutomationBoundary

    @property
    def fingerprint(self) -> str:
        return sha256_json(self)


@dataclass(frozen=True, slots=True)
class RuleSet:
    schema_version: int
    cards: tuple[RuleCard, ...]
    corpus_manifest_sha256: str
    source_pdf_sha256: str

    @property
    def fingerprint(self) -> str:
        return sha256_json(
            {
                "schema_version": self.schema_version,
                "cards": self.cards,
                "corpus_manifest_sha256": self.corpus_manifest_sha256,
                "source_pdf_sha256": self.source_pdf_sha256,
            }
        )


@dataclass(frozen=True, slots=True)
class EvaluationReason:
    code: str
    phase: str
    verdict: EvaluationVerdict
    evidence_ids: tuple[str, ...]
    predicate_id: str | None = None
    field: str | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class RuleEvaluation:
    rule_id: str
    rule_card_version: int
    rule_card_fingerprint: str
    rule_set_fingerprint: str
    corpus_manifest_fingerprint: str
    algorithm_fingerprint: str
    evaluation_input_fingerprint: str
    strategy_track: StrategyTrack
    level: int
    verdict: EvaluationVerdict
    candidate_satisfied: bool
    confirmation_satisfied: bool
    invalidation_triggered: bool
    conflict_triggered: bool
    critical_indeterminate: bool
    safe_to_proceed: bool
    reasons: tuple[EvaluationReason, ...]
    evidence_ids: tuple[str, ...]
    supporting_evidence_ids: tuple[str, ...]
    counterevidence_ids: tuple[str, ...]


def _reference(value: Mapping[str, object]) -> EvidenceReference:
    _require_fields(value, _EVIDENCE_REFERENCE_FIELDS, "evidence reference")
    evidence_id = value["evidence_id"]
    if not isinstance(evidence_id, str) or not evidence_id:
        raise ValueError("evidence_id must be non-empty")
    lesson = value["lesson"]
    if isinstance(lesson, bool) or not isinstance(lesson, int) or lesson < 0:
        raise ValueError("lesson must be a non-negative integer")
    return EvidenceReference(
        evidence_id=evidence_id,
        lesson=lesson,
        pdf_pages=_positive_ints(value["pdf_pages"], "pdf_pages"),
        lesson_chart_ids=_unique_strings_allow_empty(
            value["lesson_chart_ids"], "lesson_chart_ids"
        ),
    )


def _references(value: object, label: str) -> tuple[EvidenceReference, ...]:
    items = _sequence(value, label)
    if not items:
        raise ValueError(f"{label} cannot be empty")
    if not all(isinstance(item, Mapping) for item in items):
        raise ValueError(f"{label} must contain mappings")
    references = tuple(sorted((_reference(item) for item in items), key=lambda item: item.evidence_id))
    identities = tuple(item.evidence_id for item in references)
    if len(set(identities)) != len(identities):
        raise ValueError(f"{label} contains duplicate evidence_id")
    return references


def _completed_bar_requirements(
    value: object,
) -> tuple[CompletedBarRequirement, ...]:
    items = _sequence(value, "completed_bar_requirements")
    if not items:
        raise ValueError("completed_bar_requirements cannot be empty")
    requirements: list[CompletedBarRequirement] = []
    for raw in items:
        item = _mapping(raw, "completed-bar requirement")
        _require_fields(
            item,
            _COMPLETED_BAR_REQUIREMENT_FIELDS,
            "completed-bar requirement",
        )
        level = item["level"]
        if isinstance(level, bool) or not isinstance(level, int) or level < 0:
            raise ValueError("completed-bar level must be a non-negative integer")
        minimum_count = item["minimum_count"]
        if (
            isinstance(minimum_count, bool)
            or not isinstance(minimum_count, int)
            or minimum_count <= 0
        ):
            raise ValueError("minimum_count must be a positive integer")
        require_latest_closed = item["require_latest_closed"]
        if not isinstance(require_latest_closed, bool):
            raise ValueError("require_latest_closed must be boolean")
        requirements.append(
            CompletedBarRequirement(
                level=level,
                minimum_count=minimum_count,
                require_latest_closed=require_latest_closed,
            )
        )
    result = tuple(sorted(requirements, key=lambda item: item.level))
    if len({item.level for item in result}) != len(result):
        raise ValueError("duplicate completed-bar requirement level")
    return result


def _predicate(value: Mapping[str, object]) -> Predicate:
    try:
        mode = PredicateMode(value.get("mode"))
    except ValueError as exc:
        raise ValueError(f"unsupported predicate mode: {value.get('mode')}") from exc
    if mode is PredicateMode.MANUAL:
        _require_fields(value, _MANUAL_PREDICATE_FIELDS, "manual predicate")
    else:
        operator_value = value.get("operator")
        try:
            operator = PredicateOperator(operator_value)
        except ValueError as exc:
            raise ValueError(f"unsupported operator: {operator_value}") from exc
        expected_fields = _MACHINE_PREDICATE_BASE_FIELDS
        if operator not in {PredicateOperator.IS_TRUE, PredicateOperator.IS_FALSE}:
            expected_fields = expected_fields | {"expected"}
        _require_fields(value, expected_fields, "machine predicate")

    predicate_id = value["predicate_id"]
    if (
        not isinstance(predicate_id, str)
        or _RULE_ID_RE.fullmatch(predicate_id) is None
    ):
        raise ValueError("predicate_id must be a stable identifier")
    evidence_ids = _unique_strings(value["evidence_ids"], "predicate evidence_ids")
    expected = value.get("expected")
    if isinstance(expected, list):
        expected = tuple(expected)
    if value.get("operator") in {"in", "not_in"} and isinstance(expected, tuple):
        expected = tuple(sorted(expected, key=sha256_json))
    operator: PredicateOperator | None = None
    if mode is PredicateMode.MACHINE:
        operator = PredicateOperator(value["operator"])
    manual_check_id: str | None = None
    prompt: str | None = None
    if mode is PredicateMode.MANUAL:
        manual_check_value = value["manual_check_id"]
        if (
            not isinstance(manual_check_value, str)
            or _RULE_ID_RE.fullmatch(manual_check_value) is None
        ):
            raise ValueError("manual_check_id must be a stable identifier")
        prompt_value = value["prompt"]
        if not isinstance(prompt_value, str) or not prompt_value.strip():
            raise ValueError("manual predicate prompt must be non-empty")
        manual_check_id = manual_check_value
        prompt = prompt_value
    return Predicate(
        predicate_id=predicate_id,
        mode=mode,
        evidence_ids=evidence_ids,
        field=str(value["field"]) if "field" in value else None,
        operator=operator,
        expected=expected,
        manual_check_id=manual_check_id,
        prompt=prompt,
    )


def _sorted_predicates(value: object, label: str) -> tuple[Predicate, ...]:
    items = _sequence(value, label)
    if not items:
        raise ValueError(f"{label} cannot be empty")
    if not all(isinstance(item, Mapping) for item in items):
        raise ValueError(f"{label} must contain mappings")
    return tuple(
        sorted(
            (_predicate(item) for item in items),
            key=lambda item: item.predicate_id,
        )
    )


def _validate_card_fields(card: RuleCard) -> None:
    predicate_groups = (
        card.candidate_predicates,
        card.confirmation_predicates,
        card.invalidation_predicates,
        card.conflict_predicates,
    )
    if any(not group for group in predicate_groups):
        raise ValueError("predicate groups cannot be empty")
    required_levels = set(card.data_requirements.required_levels)
    if not set(card.applicable_levels).issubset(required_levels):
        raise ValueError("applicable levels must be included in required levels")

    project_fields = set(card.project_fields)
    required_fields = set(card.data_requirements.required_fields)
    if not required_fields.issubset(project_fields):
        raise ValueError("required fields must be declared as project fields")
    if "signal.level" not in required_fields:
        raise ValueError("signal.level must be required")
    completed_levels = {
        item.level for item in card.completed_bar_requirements
    }
    if completed_levels != required_levels:
        raise ValueError("each required level must have a completed-bar requirement")
    if any(
        requirement.require_latest_closed is not True
        for requirement in card.completed_bar_requirements
    ):
        raise ValueError("operational rules require latest closed bars")

    for path in card.project_fields:
        spec = project_field_spec(path)
        if spec.level is not None and spec.level not in required_levels:
            raise ValueError(f"project field {path} references an undeclared required level")

    predicates = tuple(item for group in predicate_groups for item in group)
    predicate_ids = tuple(item.predicate_id for item in predicates)
    if len(set(predicate_ids)) != len(predicate_ids):
        raise ValueError("duplicate predicate_id")

    evidence_ids = {item.evidence_id for item in card.evidence}
    counterevidence_ids = {item.evidence_id for item in card.counterevidence}
    if evidence_ids & counterevidence_ids:
        raise ValueError("evidence and counterevidence overlap")
    referenced_evidence_ids = evidence_ids | counterevidence_ids
    for predicate in predicates:
        if not set(predicate.evidence_ids).issubset(referenced_evidence_ids):
            raise ValueError(
                "predicate evidence must be referenced by the rule card"
            )

    machine_ids = {
        item.predicate_id
        for item in predicates
        if item.mode is PredicateMode.MACHINE
    }
    manual_ids = {
        item.manual_check_id
        for item in predicates
        if item.mode is PredicateMode.MANUAL
    }
    manual_id_values = tuple(
        item.manual_check_id
        for item in predicates
        if item.mode is PredicateMode.MANUAL
    )
    if len(set(manual_id_values)) != len(manual_id_values):
        raise ValueError("duplicate manual_check_id")
    if (
        set(card.automation_boundary.machine_predicate_ids) != machine_ids
        or set(card.automation_boundary.manual_check_ids) != manual_ids
    ):
        raise ValueError("automation boundary mismatch")

    for predicate in predicates:
        if predicate.mode is not PredicateMode.MACHINE:
            continue
        if predicate.field not in project_fields:
            raise ValueError(
                f"predicate field must be declared as a project field: {predicate.field}"
            )
        if predicate.field not in required_fields:
            raise ValueError(
                f"predicate field must be declared as a required field: {predicate.field}"
            )
        spec = project_field_spec(predicate.field)
        _validate_predicate_operator(predicate, spec)

    for requirement in card.completed_bar_requirements:
        if requirement.level not in required_levels:
            raise ValueError("completed-bar requirement references an undeclared required level")
        required_paths = {f"levels.{requirement.level}.completed_bar_count"}
        if requirement.require_latest_closed:
            required_paths.add(f"levels.{requirement.level}.latest_bar_closed")
        if not required_paths.issubset(required_fields):
            raise ValueError(
                "completed-bar requirement fields must be declared as required fields"
            )


def _card(value: Mapping[str, object]) -> RuleCard:
    _require_fields(value, _RULE_CARD_FIELDS, "rule-card")
    rule_id = value["rule_id"]
    if not isinstance(rule_id, str) or _RULE_ID_RE.fullmatch(rule_id) is None:
        raise ValueError("rule_id must be a stable rule_id")
    version = value["version"]
    if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
        raise ValueError("version must be a positive integer")
    applicable_levels = _unique_levels(
        value["applicable_levels"], "applicable_levels"
    )
    project_fields = _unique_strings(value["project_fields"], "project_fields")
    algorithm_version = value["algorithm_version"]
    if not isinstance(algorithm_version, str) or not algorithm_version:
        raise ValueError("algorithm_version must be non-empty")
    concepts = _unique_strings(value["concepts"], "concepts")
    data = _mapping(value["data_requirements"], "data_requirements")
    _require_fields(data, _DATA_REQUIREMENT_FIELDS, "data_requirements")
    boundary = _mapping(value["automation_boundary"], "automation_boundary")
    _require_fields(
        boundary,
        _AUTOMATION_BOUNDARY_FIELDS,
        "automation_boundary",
    )
    card = RuleCard(
        rule_id=rule_id,
        version=version,
        track=StrategyTrack(value["track"]),
        applicable_levels=applicable_levels,
        algorithm_version=algorithm_version,
        concepts=concepts,
        evidence=_references(value["evidence"], "evidence"),
        counterevidence=_references(value["counterevidence"], "counterevidence"),
        project_fields=project_fields,
        data_requirements=DataRequirements(
            required_fields=_unique_strings(
                data["required_fields"], "required_fields"
            ),
            required_levels=_unique_levels(
                data["required_levels"], "required_levels"
            ),
        ),
        completed_bar_requirements=_completed_bar_requirements(
            value["completed_bar_requirements"]
        ),
        candidate_predicates=_sorted_predicates(
            value["candidate_predicates"], "candidate_predicates"
        ),
        confirmation_predicates=_sorted_predicates(
            value["confirmation_predicates"], "confirmation_predicates"
        ),
        invalidation_predicates=_sorted_predicates(
            value["invalidation_predicates"], "invalidation_predicates"
        ),
        conflict_predicates=_sorted_predicates(
            value["conflict_predicates"], "conflict_predicates"
        ),
        automation_boundary=AutomationBoundary(
            machine_predicate_ids=_unique_strings_allow_empty(
                boundary["machine_predicate_ids"],
                "machine_predicate_ids",
            ),
            manual_check_ids=_unique_strings_allow_empty(
                boundary["manual_check_ids"],
                "manual_check_ids",
            ),
        ),
    )
    _validate_card_fields(card)
    return card


def _catalog_by_id(
    evidence_units: Sequence[EvidenceUnit],
    images: Sequence[ImageEvidence],
) -> tuple[dict[str, EvidenceUnit], dict[str, ImageEvidence]]:
    evidence_by_id: dict[str, EvidenceUnit] = {}
    for unit in evidence_units:
        if unit.evidence_id in evidence_by_id:
            raise ValueError(f"duplicate evidence_id: {unit.evidence_id}")
        evidence_by_id[unit.evidence_id] = unit
    images_by_id: dict[str, ImageEvidence] = {}
    for image in images:
        if image.image_id in images_by_id:
            raise ValueError(f"duplicate image_id: {image.image_id}")
        if image.image_id in evidence_by_id:
            raise ValueError(f"duplicate evidence identifier: {image.image_id}")
        images_by_id[image.image_id] = image
    return evidence_by_id, images_by_id


def _validate_evidence_references(
    card: RuleCard,
    evidence_by_id: Mapping[str, EvidenceUnit],
    images_by_id: Mapping[str, ImageEvidence],
    *,
    source_pdf_sha256: str,
) -> None:
    for reference in (*card.evidence, *card.counterevidence):
        unit = evidence_by_id.get(reference.evidence_id)
        if unit is None:
            raise ValueError(
                f"evidence does not exist: {reference.evidence_id}"
            )
        if unit.source_tier is not SourceTier.LESSON_ORIGINAL:
            raise ValueError(
                f"evidence {reference.evidence_id} must be lesson_original"
            )
        if unit.source_role not in {"lesson_body", "chan_reply", "chan_excerpt"}:
            raise ValueError(
                f"evidence {reference.evidence_id} must have an authoritative original role"
            )
        if (
            not isinstance(unit.source_pdf_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", unit.source_pdf_sha256) is None
            or unit.source_pdf_sha256 != source_pdf_sha256
            or not unit.source_record_ids
            or not all(
                isinstance(record_id, str) and record_id.startswith("source:")
                for record_id in unit.source_record_ids
            )
            or unit.source_record_id not in unit.source_record_ids
        ):
            raise ValueError(
                f"evidence {reference.evidence_id} original provenance is incomplete"
            )
        if unit.lesson != reference.lesson:
            raise ValueError(
                f"evidence {reference.evidence_id} lesson mismatch"
            )
        if unit.page_number not in reference.pdf_pages:
            raise ValueError(
                f"evidence {reference.evidence_id} PDF page mismatch"
            )
        resolved_pages = {unit.page_number}
        for chart_id in reference.lesson_chart_ids:
            image = images_by_id.get(chart_id)
            if image is None:
                raise ValueError(f"lesson chart does not exist: {chart_id}")
            if image.source_tier is not SourceTier.LESSON_CHART:
                raise ValueError(f"chart {chart_id} must be lesson_chart")
            if image.source_role != "lesson_chart":
                raise ValueError(f"chart source role mismatch: {chart_id}")
            if (
                image.source_pdf_sha256 != source_pdf_sha256
                or image.source_pdf_sha256 != unit.source_pdf_sha256
            ):
                raise ValueError(f"chart source PDF mismatch: {chart_id}")
            if image.page_number not in reference.pdf_pages:
                raise ValueError(f"chart PDF page mismatch: {chart_id}")
            resolved_pages.add(image.page_number)
            if chart_id not in unit.image_ids:
                raise ValueError(
                    f"chart is not linked to evidence {reference.evidence_id}: {chart_id}"
                )
        if set(reference.pdf_pages) != resolved_pages:
            raise ValueError(
                f"evidence {reference.evidence_id} PDF pages are not fully resolvable"
            )


def load_rule_set(
    document: Mapping[str, object],
    *,
    corpus: CertifiedLessonCorpus,
) -> RuleSet:
    if not isinstance(document, Mapping):
        raise TypeError("rule-set document must be a mapping")
    if not isinstance(corpus, CertifiedLessonCorpus):
        raise TypeError("corpus must be a CertifiedLessonCorpus")
    if (
        re.fullmatch(r"[0-9a-f]{64}", corpus.manifest_sha256) is None
        or re.fullmatch(r"[0-9a-f]{64}", corpus.source_pdf_sha256) is None
    ):
        raise ValueError("certified corpus identity is invalid")
    _require_fields(document, _RULE_SET_FIELDS, "rule-set")
    schema_version = document["schema_version"]
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != 1
    ):
        raise ValueError("unsupported rule-set schema version")
    card_values = _sequence(document["cards"], "cards")
    if not card_values:
        raise ValueError("cards cannot be empty")
    if not all(isinstance(item, Mapping) for item in card_values):
        raise ValueError("cards must contain mappings")
    evidence_by_id, images_by_id = _catalog_by_id(
        corpus.semantic_units,
        corpus.images,
    )
    cards = tuple(
        sorted(
            (_card(item) for item in card_values),
            key=lambda item: (item.rule_id, item.version),
        )
    )
    identities = tuple((card.rule_id, card.version) for card in cards)
    if len(set(identities)) != len(identities):
        raise ValueError("duplicate rule card identity")
    for card in cards:
        _validate_evidence_references(
            card,
            evidence_by_id,
            images_by_id,
            source_pdf_sha256=corpus.source_pdf_sha256,
        )
    return RuleSet(
        schema_version=schema_version,
        cards=cards,
        corpus_manifest_sha256=corpus.manifest_sha256,
        source_pdf_sha256=corpus.source_pdf_sha256,
    )


def _strict_json_object(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON value: {value}")


def load_rule_set_file(
    path: str | Path,
    *,
    corpus: CertifiedLessonCorpus,
) -> RuleSet:
    source = Path(path)
    try:
        document = json.loads(
            source.read_text(encoding="utf-8"),
            object_pairs_hook=_strict_json_object,
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load rule-set JSON: {source}") from exc
    if not isinstance(document, Mapping):
        raise ValueError("rule-set JSON root must be a mapping")
    return load_rule_set(
        document,
        corpus=corpus,
    )


_VERDICT_SEVERITY = {
    EvaluationVerdict.CONFIRM: 0,
    EvaluationVerdict.WATCH: 1,
    EvaluationVerdict.REJECT: 2,
}


def _operator_result(predicate: Predicate, actual: object) -> bool:
    operator = predicate.operator
    expected = predicate.expected
    if operator is PredicateOperator.EQ:
        return actual == expected
    if operator is PredicateOperator.NE:
        return actual != expected
    if operator is PredicateOperator.LT:
        return actual < expected
    if operator is PredicateOperator.LTE:
        return actual <= expected
    if operator is PredicateOperator.GT:
        return actual > expected
    if operator is PredicateOperator.GTE:
        return actual >= expected
    if operator is PredicateOperator.IN:
        return actual in expected
    if operator is PredicateOperator.NOT_IN:
        return actual not in expected
    if operator is PredicateOperator.CONTAINS:
        return expected in actual
    if operator is PredicateOperator.IS_TRUE:
        return actual is True
    if operator is PredicateOperator.IS_FALSE:
        return actual is False
    raise ValueError("unsupported operator at evaluation")


def _card_evidence_ids(card: RuleCard) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                *(item.evidence_id for item in card.evidence),
                *(item.evidence_id for item in card.counterevidence),
            }
        )
    )


def _evaluate_predicate_groups(
    card: RuleCard,
    resolved: Mapping[str, FieldResolution],
    manual_snapshots: Mapping[str, object],
    invalid_manual_check_ids: set[str],
    *,
    include_manual: bool,
) -> tuple[
    dict[str, bool],
    tuple[EvaluationReason, ...],
    dict[str, tuple[tuple[str, bool | None], ...]],
]:
    """Evaluate one canonical predicate loop for full and machine-only callers."""
    if type(include_manual) is not bool:
        raise TypeError("include_manual must be boolean")
    group_states: dict[str, bool] = {
        "candidate": True,
        "confirmation": True,
        "invalidation": False,
        "conflict": False,
    }
    group_results: dict[str, list[tuple[str, bool | None]]] = {
        "candidate": [],
        "confirmation": [],
        "invalidation": [],
        "conflict": [],
    }
    reasons: list[EvaluationReason] = []
    predicate_groups = (
        ("candidate", card.candidate_predicates, False),
        ("confirmation", card.confirmation_predicates, False),
        ("invalidation", card.invalidation_predicates, True),
        ("conflict", card.conflict_predicates, True),
    )
    for phase, predicates, critical in predicate_groups:
        for predicate in predicates:
            if predicate.mode is PredicateMode.MANUAL and not include_manual:
                continue
            predicate_value: bool | None = None
            detail = ""
            if predicate.mode is PredicateMode.MANUAL:
                snapshot = manual_snapshots.get(predicate.manual_check_id)
                if predicate.manual_check_id in invalid_manual_check_ids:
                    detail = "manual check evidence binding is invalid"
                elif snapshot is None:
                    detail = "audited manual check is missing"
                else:
                    predicate_value = snapshot.value
            else:
                resolution = resolved[predicate.field]
                if resolution.status is FieldResolutionStatus.VALUE:
                    try:
                        predicate_value = _operator_result(
                            predicate, resolution.value
                        )
                    except (TypeError, ValueError, ArithmeticError) as exc:
                        detail = (
                            "operator evaluation failed: "
                            f"{type(exc).__name__}"
                        )
                else:
                    detail = resolution.detail or resolution.status.value

            group_results[phase].append(
                (predicate.predicate_id, predicate_value)
            )
            if predicate_value is None:
                if not critical:
                    group_states[phase] = False
                reasons.append(
                    EvaluationReason(
                        code=f"{phase}_indeterminate",
                        phase=phase,
                        verdict=(
                            EvaluationVerdict.REJECT
                            if critical
                            else EvaluationVerdict.WATCH
                        ),
                        evidence_ids=predicate.evidence_ids,
                        predicate_id=predicate.predicate_id,
                        field=predicate.field,
                        detail=detail,
                    )
                )
                continue
            if critical:
                if predicate_value:
                    group_states[phase] = True
                    reasons.append(
                        EvaluationReason(
                            code=f"{phase}_triggered",
                            phase=phase,
                            verdict=EvaluationVerdict.REJECT,
                            evidence_ids=predicate.evidence_ids,
                            predicate_id=predicate.predicate_id,
                            field=predicate.field,
                        )
                    )
            elif not predicate_value:
                group_states[phase] = False
                reasons.append(
                    EvaluationReason(
                        code=f"{phase}_not_satisfied",
                        phase=phase,
                        verdict=EvaluationVerdict.WATCH,
                        evidence_ids=predicate.evidence_ids,
                        predicate_id=predicate.predicate_id,
                        field=predicate.field,
                    )
                )
    return (
        group_states,
        tuple(reasons),
        {phase: tuple(values) for phase, values in group_results.items()},
    )


def _evaluation_input_fingerprint(context: object) -> str:
    if type(context) is RuleEvaluationContext:
        try:
            audit = validate_manual_check_audit(context.manual_check_audit)
        except (AttributeError, TypeError, ValueError):
            pass
        else:
            return audit.context_fingerprint
    return sha256_json({"context_status": "untrusted_or_invalid"})


def _build_evaluation(
    card: RuleCard,
    rule_set: RuleSet,
    reasons: list[EvaluationReason],
    *,
    evaluation_input_fingerprint: str,
    level: int,
    candidate_satisfied: bool,
    confirmation_satisfied: bool,
    invalidation_triggered: bool,
    conflict_triggered: bool,
) -> RuleEvaluation:
    if reasons:
        verdict = max(
            (reason.verdict for reason in reasons),
            key=_VERDICT_SEVERITY.__getitem__,
        )
    else:
        verdict = EvaluationVerdict.CONFIRM
        reasons.append(
            EvaluationReason(
                code="rule_confirmed",
                phase="rule",
                verdict=verdict,
                evidence_ids=_card_evidence_ids(card),
            )
        )
    supporting_evidence_ids = tuple(
        sorted(item.evidence_id for item in card.evidence)
    )
    counterevidence_ids = tuple(
        sorted(item.evidence_id for item in card.counterevidence)
    )
    evidence_ids = tuple(
        sorted({*supporting_evidence_ids, *counterevidence_ids})
    )
    critical_indeterminate = any(
        reason.code in {"invalidation_indeterminate", "conflict_indeterminate"}
        for reason in reasons
    )
    safe_to_proceed = (
        verdict is EvaluationVerdict.CONFIRM
        and candidate_satisfied
        and confirmation_satisfied
        and not invalidation_triggered
        and not conflict_triggered
        and not critical_indeterminate
    )
    return RuleEvaluation(
        rule_id=card.rule_id,
        rule_card_version=card.version,
        rule_card_fingerprint=card.fingerprint,
        rule_set_fingerprint=rule_set.fingerprint,
        corpus_manifest_fingerprint=f"sha256:{rule_set.corpus_manifest_sha256}",
        algorithm_fingerprint=sha256_json(
            {"algorithm_version": card.algorithm_version}
        ),
        evaluation_input_fingerprint=evaluation_input_fingerprint,
        strategy_track=card.track,
        level=level,
        verdict=verdict,
        candidate_satisfied=candidate_satisfied,
        confirmation_satisfied=confirmation_satisfied,
        invalidation_triggered=invalidation_triggered,
        conflict_triggered=conflict_triggered,
        critical_indeterminate=critical_indeterminate,
        safe_to_proceed=safe_to_proceed,
        reasons=tuple(reasons),
        evidence_ids=evidence_ids,
        supporting_evidence_ids=supporting_evidence_ids,
        counterevidence_ids=counterevidence_ids,
    )


def evaluate_rule_card(
    card: RuleCard,
    context: object,
    *,
    rule_set: RuleSet,
    track: StrategyTrack | str,
    level: int,
    manual_checks: object = None,
) -> RuleEvaluation:
    if not isinstance(card, RuleCard):
        raise TypeError("card must be a RuleCard")
    if not isinstance(rule_set, RuleSet):
        raise TypeError("rule_set must be a RuleSet")
    all_evidence = _card_evidence_ids(card)
    reasons: list[EvaluationReason] = []
    evaluation_input_fingerprint = _evaluation_input_fingerprint(context)
    try:
        _validate_card_fields(card)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        reasons.append(
            EvaluationReason(
                code="invalid_rule_card",
                phase="identity",
                verdict=EvaluationVerdict.REJECT,
                evidence_ids=all_evidence,
                detail=str(exc),
            )
        )
        return _build_evaluation(
            card,
            rule_set,
            reasons,
            evaluation_input_fingerprint=evaluation_input_fingerprint,
            level=level,
            candidate_satisfied=False,
            confirmation_satisfied=False,
            invalidation_triggered=False,
            conflict_triggered=False,
        )
    if card not in rule_set.cards:
        reasons.append(
            EvaluationReason(
                code="rule_card_not_in_rule_set",
                phase="identity",
                verdict=EvaluationVerdict.REJECT,
                evidence_ids=all_evidence,
            )
        )
        return _build_evaluation(
            card,
            rule_set,
            reasons,
            evaluation_input_fingerprint=evaluation_input_fingerprint,
            level=level,
            candidate_satisfied=False,
            confirmation_satisfied=False,
            invalidation_triggered=False,
            conflict_triggered=False,
        )

    try:
        runtime_track = StrategyTrack(track)
    except (TypeError, ValueError):
        runtime_track = None
    if runtime_track is not card.track:
        reasons.append(
            EvaluationReason(
                code="strategy_track_mismatch",
                phase="identity",
                verdict=EvaluationVerdict.REJECT,
                evidence_ids=all_evidence,
            )
        )
    if (
        isinstance(level, bool)
        or not isinstance(level, int)
        or level not in card.applicable_levels
    ):
        reasons.append(
            EvaluationReason(
                code="applicable_level_mismatch",
                phase="identity",
                verdict=EvaluationVerdict.REJECT,
                evidence_ids=all_evidence,
            )
        )
    if reasons:
        return _build_evaluation(
            card,
            rule_set,
            reasons,
            evaluation_input_fingerprint=evaluation_input_fingerprint,
            level=level,
            candidate_satisfied=False,
            confirmation_satisfied=False,
            invalidation_triggered=False,
            conflict_triggered=False,
        )

    manual_snapshots = {}
    invalid_manual_check_ids: set[str] = set()
    if manual_checks is not None:
        reasons.append(
            EvaluationReason(
                code="manual_checks_inconsistent",
                phase="data",
                verdict=EvaluationVerdict.REJECT,
                evidence_ids=all_evidence,
                detail=(
                    "manual checks must be carried by an audited "
                    "RuleEvaluationContext"
                ),
            )
        )
    if type(context) is RuleEvaluationContext:
        try:
            audit = validate_manual_check_audit(context.manual_check_audit)
        except (AttributeError, TypeError, ValueError) as exc:
            reasons.append(
                EvaluationReason(
                    code="manual_checks_inconsistent",
                    phase="data",
                    verdict=EvaluationVerdict.REJECT,
                    evidence_ids=all_evidence,
                    detail=str(exc),
                )
            )
        else:
            manual_snapshots = {
                snapshot.manual_check_id: snapshot
                for snapshot in audit.snapshots
            }
    expected_manual_checks = set(card.automation_boundary.manual_check_ids)
    unknown_manual_checks = set(manual_snapshots) - expected_manual_checks
    if unknown_manual_checks:
        reasons.append(
            EvaluationReason(
                code="manual_check_set_mismatch",
                phase="data",
                verdict=EvaluationVerdict.REJECT,
                evidence_ids=all_evidence,
                detail="unknown manual check identifiers",
            )
        )
    manual_predicates = {
        predicate.manual_check_id: predicate
        for group in (
            card.candidate_predicates,
            card.confirmation_predicates,
            card.invalidation_predicates,
            card.conflict_predicates,
        )
        for predicate in group
        if predicate.mode is PredicateMode.MANUAL
    }
    for check_id, snapshot in manual_snapshots.items():
        predicate = manual_predicates.get(check_id)
        if predicate is None:
            continue
        if snapshot.evidence_ids != predicate.evidence_ids:
            invalid_manual_check_ids.add(check_id)
            reasons.append(
                EvaluationReason(
                    code="manual_check_evidence_mismatch",
                    phase="data",
                    verdict=EvaluationVerdict.REJECT,
                    evidence_ids=predicate.evidence_ids,
                    predicate_id=predicate.predicate_id,
                    detail=(
                        "manual check evidence must exactly match its "
                        "rule predicate"
                    ),
                )
            )

    resolved = {
        path: resolve_project_field(context, path)
        for path in card.data_requirements.required_fields
    }
    signal_level = resolved.get("signal.level")
    if signal_level is not None:
        if signal_level.status is not FieldResolutionStatus.VALUE:
            reasons.append(
                EvaluationReason(
                    code="signal_level_indeterminate",
                    phase="data",
                    verdict=EvaluationVerdict.REJECT,
                    evidence_ids=all_evidence,
                    field="signal.level",
                    detail=signal_level.detail,
                )
            )
        elif signal_level.value != level:
            reasons.append(
                EvaluationReason(
                    code="signal_level_mismatch",
                    phase="data",
                    verdict=EvaluationVerdict.REJECT,
                    evidence_ids=all_evidence,
                    field="signal.level",
                )
            )

    for path, resolution in resolved.items():
        if resolution.status is FieldResolutionStatus.VALUE:
            continue
        if resolution.status is FieldResolutionStatus.MISSING:
            code = "missing_project_field"
            verdict = EvaluationVerdict.WATCH
        elif resolution.status is FieldResolutionStatus.NULL:
            code = "null_project_field"
            verdict = EvaluationVerdict.WATCH
        else:
            code = "inconsistent_project_field"
            verdict = EvaluationVerdict.REJECT
        reasons.append(
            EvaluationReason(
                code=code,
                phase="data",
                verdict=verdict,
                evidence_ids=all_evidence,
                field=path,
                detail=resolution.detail,
            )
        )

    for requirement in card.completed_bar_requirements:
        count_path = f"levels.{requirement.level}.completed_bar_count"
        count = resolved[count_path]
        if (
            count.status is FieldResolutionStatus.VALUE
            and count.value < 0
        ):
            reasons.append(
                EvaluationReason(
                    code="inconsistent_completed_bar_count",
                    phase="completed_bar",
                    verdict=EvaluationVerdict.REJECT,
                    evidence_ids=all_evidence,
                    field=count_path,
                )
            )
        if (
            count.status is FieldResolutionStatus.VALUE
            and count.value < requirement.minimum_count
        ):
            reasons.append(
                EvaluationReason(
                    code="insufficient_completed_bars",
                    phase="completed_bar",
                    verdict=EvaluationVerdict.WATCH,
                    evidence_ids=all_evidence,
                    field=count_path,
                )
            )
        if requirement.require_latest_closed:
            closed_path = f"levels.{requirement.level}.latest_bar_closed"
            closed = resolved[closed_path]
            if (
                closed.status is FieldResolutionStatus.VALUE
                and closed.value is not True
            ):
                reasons.append(
                    EvaluationReason(
                        code="latest_bar_not_closed",
                        phase="completed_bar",
                        verdict=EvaluationVerdict.WATCH,
                        evidence_ids=all_evidence,
                        field=closed_path,
                    )
                )

    group_states, predicate_reasons, _ = _evaluate_predicate_groups(
        card,
        resolved,
        manual_snapshots,
        invalid_manual_check_ids,
        include_manual=True,
    )
    reasons.extend(predicate_reasons)

    return _build_evaluation(
        card,
        rule_set,
        reasons,
        evaluation_input_fingerprint=evaluation_input_fingerprint,
        level=level,
        candidate_satisfied=group_states["candidate"],
        confirmation_satisfied=group_states["confirmation"],
        invalidation_triggered=group_states["invalidation"],
        conflict_triggered=group_states["conflict"],
    )
