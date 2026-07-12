from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError, dataclass, replace
from datetime import datetime
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from chanlun.decision_support.corpus_types import (
    EvidenceUnit,
    ImageEvidence,
    SourceTier,
)
from chanlun.decision_support.corpus_loader import CertifiedLessonCorpus
from chanlun.decision_support.fingerprints import sha256_json
from chanlun.decision_support.manual_checks import (
    ManualCheckAudit,
    ManualCheckSnapshot,
    RuleEvaluationContext,
)
from chanlun.decision_support.models import StrategyTrack
from chanlun.decision_support.rule_context import RuleRuntimeFacts
from chanlun.decision_support.rule_engine import RuleEngine
from chanlun.decision_support.rule_cards import (
    EvaluationVerdict,
    FieldResolutionStatus,
    PredicateMode,
    RuleSet,
    derive_structural_stop_breached,
    evaluate_rule_card as _evaluate_bound_rule_card,
    load_rule_set as _load_certified_rule_set,
    load_rule_set_file as _load_certified_rule_set_file,
    resolve_project_field,
)


_SOURCE_PDF_SHA256 = "a" * 64
_CORPUS_MANIFEST_SHA256 = "b" * 64


def _original_evidence(
    evidence_id: str,
    *,
    lesson: int,
    page: int,
    image_ids: tuple[str, ...] = (),
) -> EvidenceUnit:
    source_record_id = "source:" + hashlib.sha256(evidence_id.encode()).hexdigest()
    return EvidenceUnit(
        evidence_id=evidence_id,
        source_tier=SourceTier.LESSON_ORIGINAL,
        source_path=f"lessons/{lesson}.jsonl",
        title=f"lesson {lesson}",
        text="fixture original text",
        sha256="sha256:" + "1" * 64,
        lesson=lesson,
        image_ids=image_ids,
        source_role="lesson_body",
        source_record_id=source_record_id,
        source_pdf_sha256=_SOURCE_PDF_SHA256,
        page_number=page,
        source_record_ids=(source_record_id,),
    )


def _chart(image_id: str, *, page: int) -> ImageEvidence:
    source_record_id = "source:" + hashlib.sha256(image_id.encode()).hexdigest()
    return ImageEvidence(
        image_id=image_id,
        source_tier=SourceTier.LESSON_CHART,
        source_path=f"images/{image_id}.jpg",
        sha256="sha256:" + "2" * 64,
        media_type="image/jpeg",
        width=640,
        height=480,
        source_role="lesson_chart",
        source_record_id=source_record_id,
        source_pdf_sha256=_SOURCE_PDF_SHA256,
        page_number=page,
        occurrence_id="occurrence:" + hashlib.sha256(image_id.encode()).hexdigest(),
    )


def _catalog() -> tuple[tuple[EvidenceUnit, ...], tuple[ImageEvidence, ...]]:
    return (
        (
            _original_evidence(
                "lesson-20-main",
                lesson=20,
                page=320,
                image_ids=("lesson-20-chart",),
            ),
            _original_evidence("lesson-20-counter", lesson=20, page=321),
        ),
        (_chart("lesson-20-chart", page=320),),
    )


def _certified_corpus(
    evidence_units: tuple[EvidenceUnit, ...],
    images: tuple[ImageEvidence, ...],
    *,
    manifest_sha256: str = _CORPUS_MANIFEST_SHA256,
    source_pdf_sha256: str = _SOURCE_PDF_SHA256,
) -> CertifiedLessonCorpus:
    return CertifiedLessonCorpus(
        root=Path("certified-fixture"),
        units=evidence_units,
        semantic_units=evidence_units,
        images=images,
        manifest_sha256=manifest_sha256,
        source_pdf_sha256=source_pdf_sha256,
    )


def load_rule_set(
    document: dict[str, object],
    *,
    evidence_units: tuple[EvidenceUnit, ...],
    images: tuple[ImageEvidence, ...],
):
    return _load_certified_rule_set(
        document,
        corpus=_certified_corpus(evidence_units, images),
    )


def load_rule_set_file(
    path: Path,
    *,
    evidence_units: tuple[EvidenceUnit, ...],
    images: tuple[ImageEvidence, ...],
):
    return _load_certified_rule_set_file(
        path,
        corpus=_certified_corpus(evidence_units, images),
    )


def evaluate_rule_card(card, context, **kwargs):
    rule_set = RuleSet(
        schema_version=1,
        cards=(card,),
        corpus_manifest_sha256=_CORPUS_MANIFEST_SHA256,
        source_pdf_sha256=_SOURCE_PDF_SHA256,
    )
    return _evaluate_bound_rule_card(
        card,
        context,
        rule_set=rule_set,
        **kwargs,
    )


def _machine(
    predicate_id: str,
    field: str,
    operator: str,
    evidence_id: str,
    *,
    expected: object = None,
) -> dict[str, object]:
    value: dict[str, object] = {
        "predicate_id": predicate_id,
        "mode": "machine",
        "field": field,
        "operator": operator,
        "evidence_ids": [evidence_id],
    }
    if operator not in {"is_true", "is_false"}:
        value["expected"] = expected
    return value


def _valid_card(*, rule_id: str = "chanlun.third_buy") -> dict[str, object]:
    project_fields = [
        "levels.1.completed_bar_count",
        "levels.1.latest_bar_closed",
        "market.is_tradeable",
        "risk.stop_breached",
        "signal.bs_type",
        "signal.level",
        "signal.structural_stop_below",
    ]
    return {
        "rule_id": rule_id,
        "version": 1,
        "track": "trend_continuation",
        "applicable_levels": [1],
        "algorithm_version": "chanlun-core/1",
        "concepts": ["third_buy", "central_zone_retest"],
        "evidence": [
            {
                "evidence_id": "lesson-20-main",
                "lesson": 20,
                "pdf_pages": [320],
                "lesson_chart_ids": ["lesson-20-chart"],
            }
        ],
        "counterevidence": [
            {
                "evidence_id": "lesson-20-counter",
                "lesson": 20,
                "pdf_pages": [321],
                "lesson_chart_ids": [],
            }
        ],
        "project_fields": project_fields,
        "data_requirements": {
            "required_fields": list(project_fields),
            "required_levels": [1],
        },
        "completed_bar_requirements": [
            {"level": 1, "minimum_count": 2, "require_latest_closed": True}
        ],
        "candidate_predicates": [
            _machine(
                "candidate.third_buy",
                "signal.bs_type",
                "in",
                "lesson-20-main",
                expected=["3buy", "3buy_nest"],
            )
        ],
        "confirmation_predicates": [
            {
                "predicate_id": "confirmation.chart_structure",
                "mode": "manual",
                "manual_check_id": "chart.structure_confirmed",
                "prompt": "人工确认课程图示结构仍成立",
                "evidence_ids": ["lesson-20-main"],
            }
        ],
        "invalidation_predicates": [
            _machine(
                "invalidation.stop_reached",
                "risk.stop_breached",
                "is_true",
                "lesson-20-counter",
            )
        ],
        "conflict_predicates": [
            _machine(
                "conflict.not_tradeable",
                "market.is_tradeable",
                "is_false",
                "lesson-20-counter",
            )
        ],
        "automation_boundary": {
            "machine_predicate_ids": [
                "candidate.third_buy",
                "conflict.not_tradeable",
                "invalidation.stop_reached",
            ],
            "manual_check_ids": ["chart.structure_confirmed"],
        },
    }


def _document(*cards: dict[str, object]) -> dict[str, object]:
    return {"schema_version": 1, "cards": list(cards)}


def test_loads_immutable_versioned_rule_card_with_original_evidence() -> None:
    evidence, images = _catalog()

    rules = load_rule_set(
        _document(_valid_card()),
        evidence_units=evidence,
        images=images,
    )

    card = rules.cards[0]
    assert rules.schema_version == 1
    assert rules.corpus_manifest_sha256 == _CORPUS_MANIFEST_SHA256
    assert rules.source_pdf_sha256 == _SOURCE_PDF_SHA256
    assert card.rule_id == "chanlun.third_buy"
    assert card.version == 1
    assert card.track is StrategyTrack.TREND_CONTINUATION
    assert card.applicable_levels == (1,)
    assert card.evidence[0].pdf_pages == (320,)
    assert card.evidence[0].lesson_chart_ids == ("lesson-20-chart",)
    assert card.candidate_predicates[0].mode is PredicateMode.MACHINE
    assert card.confirmation_predicates[0].mode is PredicateMode.MANUAL
    assert card.project_fields == tuple(sorted(card.project_fields))
    assert card.fingerprint.startswith("sha256:")
    assert rules.fingerprint.startswith("sha256:")
    with pytest.raises(FrozenInstanceError):
        card.version = 2  # type: ignore[misc]


def test_rule_set_fingerprint_binds_certified_corpus_identity() -> None:
    evidence, images = _catalog()
    first = _load_certified_rule_set(
        _document(_valid_card()),
        corpus=_certified_corpus(evidence, images),
    )
    changed = _load_certified_rule_set(
        _document(_valid_card()),
        corpus=_certified_corpus(
            evidence,
            images,
            manifest_sha256="f" * 64,
        ),
    )

    assert first.fingerprint != changed.fingerprint


def test_rule_card_fingerprints_are_canonical_and_order_independent() -> None:
    evidence, images = _catalog()
    first_card = _valid_card(rule_id="chanlun.third_buy")
    second_card = _valid_card(rule_id="chanlun.third_buy.nested")
    reordered = deepcopy(first_card)
    for key in (
        "applicable_levels",
        "concepts",
        "evidence",
        "counterevidence",
        "project_fields",
        "candidate_predicates",
        "confirmation_predicates",
        "invalidation_predicates",
        "conflict_predicates",
    ):
        reordered[key] = list(reversed(reordered[key]))
    reordered["data_requirements"]["required_fields"].reverse()
    reordered["data_requirements"]["required_levels"].reverse()
    reordered["automation_boundary"]["machine_predicate_ids"].reverse()
    reordered["automation_boundary"]["manual_check_ids"].reverse()
    reordered["candidate_predicates"][0]["expected"].reverse()

    ordered_rules = load_rule_set(
        _document(first_card, second_card),
        evidence_units=evidence,
        images=images,
    )
    reordered_rules = load_rule_set(
        _document(
            _valid_card(rule_id="chanlun.third_buy.nested"),
            reordered,
        ),
        evidence_units=tuple(reversed(evidence)),
        images=tuple(reversed(images)),
    )

    assert ordered_rules.cards[0].fingerprint == reordered_rules.cards[0].fingerprint
    assert ordered_rules.fingerprint == reordered_rules.fingerprint


def test_loader_rejects_operator_field_and_level_mismatches() -> None:
    evidence, images = _catalog()
    cases: list[tuple[dict[str, object], str]] = []

    unknown_operator = _valid_card()
    unknown_operator["candidate_predicates"][0]["operator"] = "python_eval"
    cases.append((unknown_operator, "unsupported operator"))

    unknown_field = _valid_card()
    unknown_field["candidate_predicates"][0]["field"] = "signal.__dict__"
    unknown_field["project_fields"].append("signal.__dict__")
    unknown_field["data_requirements"]["required_fields"].append(
        "signal.__dict__"
    )
    cases.append((unknown_field, "unknown project field"))

    undeclared_field = _valid_card()
    undeclared_field["candidate_predicates"][0]["field"] = "signal.price"
    cases.append((undeclared_field, "predicate field must be declared"))

    dynamic_level_mismatch = _valid_card()
    dynamic_level_mismatch["candidate_predicates"][0] = _machine(
        "candidate.big_direction",
        "levels.2.direction",
        "eq",
        "lesson-20-main",
        expected="up",
    )
    dynamic_level_mismatch["project_fields"].append("levels.2.direction")
    dynamic_level_mismatch["data_requirements"]["required_fields"].append(
        "levels.2.direction"
    )
    dynamic_level_mismatch["automation_boundary"][
        "machine_predicate_ids"
    ][0] = "candidate.big_direction"
    cases.append((dynamic_level_mismatch, "required level"))

    applicable_level_mismatch = _valid_card()
    applicable_level_mismatch["applicable_levels"] = [2]
    cases.append((applicable_level_mismatch, "applicable levels"))

    missing_signal_level = _valid_card()
    missing_signal_level["project_fields"].remove("signal.level")
    missing_signal_level["data_requirements"]["required_fields"].remove(
        "signal.level"
    )
    cases.append((missing_signal_level, "signal.level must be required"))

    missing_applicable_bar_gate = _valid_card()
    missing_applicable_bar_gate["data_requirements"]["required_levels"].append(2)
    missing_applicable_bar_gate["project_fields"].extend(
        ["levels.2.completed_bar_count", "levels.2.latest_bar_closed"]
    )
    missing_applicable_bar_gate["data_requirements"]["required_fields"].extend(
        ["levels.2.completed_bar_count", "levels.2.latest_bar_closed"]
    )
    missing_applicable_bar_gate["completed_bar_requirements"] = [
        {"level": 2, "minimum_count": 2, "require_latest_closed": True}
    ]
    cases.append(
        (
            missing_applicable_bar_gate,
            "each required level must have a completed-bar requirement",
        )
    )

    for card, pattern in cases:
        with pytest.raises(ValueError, match=pattern):
            load_rule_set(
                _document(card),
                evidence_units=evidence,
                images=images,
            )


def test_evidence_resolver_rejects_missing_or_mismatched_original_sources() -> None:
    evidence, images = _catalog()
    cases: list[
        tuple[dict[str, object], tuple[EvidenceUnit, ...], tuple[ImageEvidence, ...], str]
    ] = []

    missing_evidence = _valid_card()
    missing_reference = deepcopy(missing_evidence["evidence"][0])
    missing_reference["evidence_id"] = "missing-original"
    missing_evidence["evidence"].append(missing_reference)
    missing_evidence["candidate_predicates"][0]["evidence_ids"] = [
        "missing-original"
    ]
    cases.append((missing_evidence, evidence, images, "evidence does not exist"))

    wrong_source_tier = replace(
        evidence[0], source_tier=SourceTier.SECONDARY_ANNOTATION
    )
    cases.append(
        (
            _valid_card(),
            (wrong_source_tier, evidence[1]),
            images,
            "must be lesson_original",
        )
    )

    lesson_mismatch = _valid_card()
    lesson_mismatch["evidence"][0]["lesson"] = 21
    cases.append((lesson_mismatch, evidence, images, "lesson mismatch"))

    page_mismatch = _valid_card()
    page_mismatch["evidence"][0]["pdf_pages"] = [999]
    cases.append((page_mismatch, evidence, images, "PDF page mismatch"))

    unverifiable_extra_page = _valid_card()
    unverifiable_extra_page["evidence"][0]["pdf_pages"] = [320, 999]
    cases.append(
        (
            unverifiable_extra_page,
            evidence,
            images,
            "PDF pages are not fully resolvable",
        )
    )

    missing_chart = _valid_card()
    missing_chart["evidence"][0]["lesson_chart_ids"] = ["missing-chart"]
    cases.append((missing_chart, evidence, images, "lesson chart does not exist"))

    wrong_chart_tier = replace(
        images[0], source_tier=SourceTier.SECONDARY_ANNOTATION
    )
    cases.append(
        (
            _valid_card(),
            evidence,
            (wrong_chart_tier,),
            "must be lesson_chart",
        )
    )

    wrong_chart_page = replace(images[0], page_number=999)
    cases.append(
        (_valid_card(), evidence, (wrong_chart_page,), "chart PDF page mismatch")
    )

    unlinked_evidence = replace(evidence[0], image_ids=())
    cases.append(
        (
            _valid_card(),
            (unlinked_evidence, evidence[1]),
            images,
            "chart is not linked",
        )
    )

    for card, resolved_evidence, resolved_images, pattern in cases:
        with pytest.raises(ValueError, match=pattern):
            load_rule_set(
                _document(card),
                evidence_units=resolved_evidence,
                images=resolved_images,
            )


def test_loader_rejects_editor_reader_unknown_as_original_evidence() -> None:
    evidence, images = _catalog()
    forged = replace(evidence[0], source_role="editor_note")

    with pytest.raises(ValueError, match="authoritative original role"):
        load_rule_set(
            _document(_valid_card()),
            evidence_units=(forged, evidence[1]),
            images=images,
        )


def test_loader_rejects_cross_pdf_chart_reference() -> None:
    evidence, images = _catalog()
    forged = replace(images[0], source_pdf_sha256="f" * 64)

    with pytest.raises(ValueError, match="chart source PDF mismatch"):
        load_rule_set(
            _document(_valid_card()),
            evidence_units=evidence,
            images=(forged,),
        )


def test_loader_enforces_strict_schema_and_unique_versioned_identity() -> None:
    evidence, images = _catalog()

    unsupported_schema = _document(_valid_card())
    unsupported_schema["schema_version"] = 2

    unknown_document_field = _document(_valid_card())
    unknown_document_field["allow_eval"] = True

    unknown_card_field = _valid_card()
    unknown_card_field["notes"] = "must not affect execution"

    unstable_rule_id = _valid_card()
    unstable_rule_id["rule_id"] = "Chan Lun:Third Buy"

    invalid_version = _valid_card()
    invalid_version["version"] = True

    duplicate_level = _valid_card()
    duplicate_level["applicable_levels"] = [1, 1]

    duplicate_project_field = _valid_card()
    duplicate_project_field["project_fields"].append("signal.bs_type")

    cases = (
        (unsupported_schema, "unsupported rule-set schema version"),
        (unknown_document_field, "rule-set fields mismatch"),
        (_document(unknown_card_field), "rule-card fields mismatch"),
        (_document(unstable_rule_id), "stable rule_id"),
        (_document(invalid_version), "version must be a positive integer"),
        (_document(duplicate_level), "applicable_levels contains duplicates"),
        (_document(duplicate_project_field), "project_fields contains duplicates"),
        (
            _document(_valid_card(), _valid_card()),
            "duplicate rule card identity",
        ),
    )

    for document, pattern in cases:
        with pytest.raises(ValueError, match=pattern):
            load_rule_set(
                document,
                evidence_units=evidence,
                images=images,
            )


def test_loader_enforces_machine_manual_boundary_and_predicate_evidence() -> None:
    evidence, images = _catalog()
    cases: list[tuple[dict[str, object], str]] = []

    machine_with_manual_fields = _valid_card()
    machine_with_manual_fields["candidate_predicates"][0].update(
        {
            "manual_check_id": "not.allowed",
            "prompt": "not allowed on machine predicate",
        }
    )
    cases.append((machine_with_manual_fields, "machine predicate fields mismatch"))

    manual_with_machine_fields = _valid_card()
    manual_with_machine_fields["confirmation_predicates"][0].update(
        {"field": "risk.allowed", "operator": "eq", "expected": True}
    )
    cases.append((manual_with_machine_fields, "manual predicate fields mismatch"))

    missing_machine_boundary = _valid_card()
    missing_machine_boundary["automation_boundary"][
        "machine_predicate_ids"
    ].remove("candidate.third_buy")
    cases.append((missing_machine_boundary, "automation boundary mismatch"))

    missing_manual_boundary = _valid_card()
    missing_manual_boundary["automation_boundary"]["manual_check_ids"] = []
    cases.append((missing_manual_boundary, "automation boundary mismatch"))

    unreferenced_predicate_evidence = _valid_card()
    unreferenced_predicate_evidence["candidate_predicates"][0][
        "evidence_ids"
    ] = ["not-referenced-by-card"]
    cases.append(
        (
            unreferenced_predicate_evidence,
            "predicate evidence must be referenced by the rule card",
        )
    )

    duplicate_predicate_id = _valid_card()
    duplicate_predicate_id["conflict_predicates"][0][
        "predicate_id"
    ] = "candidate.third_buy"
    duplicate_predicate_id["automation_boundary"][
        "machine_predicate_ids"
    ] = ["candidate.third_buy", "invalidation.stop_reached"]
    cases.append((duplicate_predicate_id, "duplicate predicate_id"))

    empty_group = _valid_card()
    empty_group["conflict_predicates"] = []
    empty_group["automation_boundary"]["machine_predicate_ids"].remove(
        "conflict.not_tradeable"
    )
    cases.append((empty_group, "conflict_predicates cannot be empty"))

    evidence_counterevidence_overlap = _valid_card()
    evidence_counterevidence_overlap["counterevidence"] = deepcopy(
        evidence_counterevidence_overlap["evidence"]
    )
    cases.append(
        (evidence_counterevidence_overlap, "evidence and counterevidence overlap")
    )

    duplicate_manual_check = _valid_card()
    duplicate_manual_check["confirmation_predicates"].append(
        {
            "predicate_id": "confirmation.chart_structure.secondary",
            "mode": "manual",
            "manual_check_id": "chart.structure_confirmed",
            "prompt": "第二个 predicate 不得复用同一人工检查 ID",
            "evidence_ids": ["lesson-20-main"],
        }
    )
    cases.append((duplicate_manual_check, "duplicate manual_check_id"))

    for card, pattern in cases:
        with pytest.raises(ValueError, match=pattern):
            load_rule_set(
                _document(card),
                evidence_units=evidence,
                images=images,
            )


def test_loader_rejects_incompatible_operator_and_expected_types() -> None:
    evidence, images = _catalog()
    cases: list[tuple[dict[str, object], str]] = []

    ordered_string = _valid_card()
    ordered_string["candidate_predicates"][0] = _machine(
        "candidate.third_buy",
        "signal.bs_type",
        "gte",
        "lesson-20-main",
        expected="3buy",
    )
    cases.append((ordered_string, "operator gte is incompatible"))

    scalar_membership = _valid_card()
    scalar_membership["candidate_predicates"][0]["expected"] = "3buy"
    cases.append((scalar_membership, "in expected must be a non-empty sequence"))

    boolean_number = _valid_card()
    boolean_number["invalidation_predicates"][0] = _machine(
        "invalidation.stop_reached",
        "signal.structural_stop_below",
        "gte",
        "lesson-20-counter",
        expected=True,
    )
    cases.append((boolean_number, "expected value type mismatch"))

    truthy_string = _valid_card()
    truthy_string["candidate_predicates"][0] = _machine(
        "candidate.third_buy",
        "signal.bs_type",
        "is_true",
        "lesson-20-main",
    )
    cases.append((truthy_string, "operator is_true is incompatible"))

    contains_boolean = _valid_card()
    contains_boolean["conflict_predicates"][0] = _machine(
        "conflict.not_tradeable",
        "market.is_tradeable",
        "contains",
        "lesson-20-counter",
        expected=True,
    )
    cases.append((contains_boolean, "operator contains is incompatible"))

    for card, pattern in cases:
        with pytest.raises(ValueError, match=pattern):
            load_rule_set(
                _document(card),
                evidence_units=evidence,
                images=images,
            )


def test_loader_validates_evidence_data_and_completed_bar_requirements() -> None:
    evidence, images = _catalog()
    cases: list[tuple[dict[str, object], str]] = []

    unknown_reference_field = _valid_card()
    unknown_reference_field["evidence"][0]["quote"] = "unverified"
    cases.append((unknown_reference_field, "evidence reference fields mismatch"))

    duplicate_page = _valid_card()
    duplicate_page["evidence"][0]["pdf_pages"] = [320, 320]
    cases.append((duplicate_page, "pdf_pages contains duplicates"))

    invalid_page = _valid_card()
    invalid_page["evidence"][0]["pdf_pages"] = [0]
    cases.append((invalid_page, "pdf_pages must contain positive integers"))

    duplicate_chart = _valid_card()
    duplicate_chart["evidence"][0]["lesson_chart_ids"] = [
        "lesson-20-chart",
        "lesson-20-chart",
    ]
    cases.append((duplicate_chart, "lesson_chart_ids contains duplicates"))

    empty_counterevidence = _valid_card()
    empty_counterevidence["counterevidence"] = []
    cases.append((empty_counterevidence, "counterevidence cannot be empty"))

    empty_algorithm_version = _valid_card()
    empty_algorithm_version["algorithm_version"] = ""
    cases.append((empty_algorithm_version, "algorithm_version must be non-empty"))

    duplicate_concept = _valid_card()
    duplicate_concept["concepts"] = ["third_buy", "third_buy"]
    cases.append((duplicate_concept, "concepts contains duplicates"))

    unknown_data_field = _valid_card()
    unknown_data_field["data_requirements"]["allow_partial"] = True
    cases.append((unknown_data_field, "data_requirements fields mismatch"))

    duplicate_required_level = _valid_card()
    duplicate_required_level["data_requirements"]["required_levels"] = [1, 1]
    cases.append((duplicate_required_level, "required_levels contains duplicates"))

    unknown_completed_field = _valid_card()
    unknown_completed_field["completed_bar_requirements"][0]["future_ok"] = True
    cases.append(
        (unknown_completed_field, "completed-bar requirement fields mismatch")
    )

    zero_completed_count = _valid_card()
    zero_completed_count["completed_bar_requirements"][0]["minimum_count"] = 0
    cases.append((zero_completed_count, "minimum_count must be a positive integer"))

    coerced_closed_flag = _valid_card()
    coerced_closed_flag["completed_bar_requirements"][0][
        "require_latest_closed"
    ] = "false"
    cases.append((coerced_closed_flag, "require_latest_closed must be boolean"))

    duplicate_completed_level = _valid_card()
    duplicate_completed_level["completed_bar_requirements"].append(
        deepcopy(duplicate_completed_level["completed_bar_requirements"][0])
    )
    cases.append(
        (duplicate_completed_level, "duplicate completed-bar requirement level")
    )

    for card, pattern in cases:
        with pytest.raises(ValueError, match=pattern):
            load_rule_set(
                _document(card),
                evidence_units=evidence,
                images=images,
            )


def test_loader_rejects_required_level_without_completed_bar_gate() -> None:
    evidence, images = _catalog()
    card = _valid_card()
    card["data_requirements"]["required_levels"].append(2)
    card["project_fields"].append("levels.2.direction")
    card["data_requirements"]["required_fields"].append("levels.2.direction")

    with pytest.raises(ValueError, match="each required level"):
        load_rule_set(
            _document(card),
            evidence_units=evidence,
            images=images,
        )


def test_loader_rejects_disabled_latest_closed_for_operational_rule() -> None:
    evidence, images = _catalog()
    card = _valid_card()
    card["completed_bar_requirements"][0]["require_latest_closed"] = False
    card["project_fields"].remove("levels.1.latest_bar_closed")
    card["data_requirements"]["required_fields"].remove(
        "levels.1.latest_bar_closed"
    )

    with pytest.raises(ValueError, match="require latest closed"):
        load_rule_set(
            _document(card),
            evidence_units=evidence,
            images=images,
        )


def _loaded_card():
    evidence, images = _catalog()
    return load_rule_set(
        _document(_valid_card()),
        evidence_units=evidence,
        images=images,
    ).cards[0]


def _evaluation_context() -> dict[str, object]:
    return {
        "risk": {"stop_breached": False},
        "signal": {
            "bs_type": "3buy",
            "level": 1,
            "structural_stop_below": 90.0,
        },
        "market": {"is_tradeable": True},
        "levels": {
            1: {
                "completed_bar_count": 2,
                "latest_bar_closed": True,
            }
        },
    }


def _audited_context(
    values: dict[str, object],
    *,
    manual_check_id: str = "chart.structure_confirmed",
    value: bool = True,
    evidence_ids: tuple[str, ...] = ("lesson-20-main",),
) -> RuleEvaluationContext:
    event_id = "manual-check-fixture-event"
    context_fingerprint = "sha256:" + "c" * 64
    snapshot = ManualCheckSnapshot(
        manual_check_id=manual_check_id,
        value=value,
        operator_id="operator.fixture",
        recorded_at=datetime(2026, 7, 14, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        event_id=event_id,
        context_fingerprint=context_fingerprint,
        evidence_ids=evidence_ids,
    )
    audit = ManualCheckAudit(
        event_id=event_id,
        context_fingerprint=context_fingerprint,
        snapshots=(snapshot,),
    )
    return RuleEvaluationContext(
        data_fingerprint="sha256:" + "d" * 64,
        manual_check_audit=audit,
        _values=values,
    )


def test_evaluator_rejects_legacy_bare_boolean_manual_check() -> None:
    card = _loaded_card()

    result = evaluate_rule_card(
        card,
        _evaluation_context(),
        track=StrategyTrack.TREND_CONTINUATION,
        level=1,
        manual_checks={"chart.structure_confirmed": True},
    )

    assert result.verdict is EvaluationVerdict.REJECT
    assert result.safe_to_proceed is False
    assert "manual_checks_inconsistent" in {
        reason.code for reason in result.reasons
    }


def test_rule_engine_has_no_legacy_manual_check_side_channel(
    make_decision_event,
) -> None:
    card = _loaded_card()
    rules = RuleSet(
        schema_version=1,
        cards=(card,),
        corpus_manifest_sha256=_CORPUS_MANIFEST_SHA256,
        source_pdf_sha256=_SOURCE_PDF_SHA256,
    )
    engine = RuleEngine(rules)

    with pytest.raises(TypeError, match="unexpected keyword argument"):
        engine.evaluate(
            make_decision_event(),
            RuleRuntimeFacts(),
            manual_checks={"chart.structure_confirmed": True},
        )


def test_evaluator_confirms_only_matching_audited_manual_check() -> None:
    card = _loaded_card()

    result = evaluate_rule_card(
        card,
        _audited_context(_evaluation_context()),
        track=StrategyTrack.TREND_CONTINUATION,
        level=1,
    )

    assert result.verdict is EvaluationVerdict.CONFIRM
    assert result.safe_to_proceed is True


def test_evaluator_rejects_manual_check_with_unbound_evidence() -> None:
    card = _loaded_card()

    result = evaluate_rule_card(
        card,
        _audited_context(
            _evaluation_context(),
            evidence_ids=("lesson-20-counter",),
        ),
        track=StrategyTrack.TREND_CONTINUATION,
        level=1,
    )

    assert result.verdict is EvaluationVerdict.REJECT
    assert result.safe_to_proceed is False
    assert "manual_check_evidence_mismatch" in {
        reason.code for reason in result.reasons
    }


def test_evaluator_revalidates_mutated_manual_check_snapshot() -> None:
    card = _loaded_card()
    context = _audited_context(_evaluation_context())
    snapshot = context.manual_check_audit.snapshots[0]
    object.__setattr__(snapshot, "value", "yes")

    result = evaluate_rule_card(
        card,
        context,
        track=StrategyTrack.TREND_CONTINUATION,
        level=1,
    )

    assert result.verdict is EvaluationVerdict.REJECT
    assert result.safe_to_proceed is False
    assert "manual_checks_inconsistent" in {
        reason.code for reason in result.reasons
    }


def test_evaluator_confirms_complete_machine_and_manual_rule() -> None:
    card = _loaded_card()
    context = _evaluation_context()

    count = resolve_project_field(context, "levels.1.completed_bar_count")
    result = evaluate_rule_card(
        card,
        _audited_context(context),
        track=StrategyTrack.TREND_CONTINUATION,
        level=1,
    )

    assert count.status is FieldResolutionStatus.VALUE
    assert count.value == 2
    assert result.verdict is EvaluationVerdict.CONFIRM
    assert result.rule_id == card.rule_id
    assert result.rule_card_version == card.version
    assert result.rule_card_fingerprint == card.fingerprint
    assert result.candidate_satisfied is True
    assert result.confirmation_satisfied is True
    assert result.invalidation_triggered is False
    assert result.conflict_triggered is False
    assert result.safe_to_proceed is True
    assert result.critical_indeterminate is False
    assert result.supporting_evidence_ids == ("lesson-20-main",)
    assert result.counterevidence_ids == ("lesson-20-counter",)
    assert [reason.code for reason in result.reasons] == ["rule_confirmed"]
    assert set(result.evidence_ids) == {
        "lesson-20-main",
        "lesson-20-counter",
    }


def test_evaluation_carries_ruleset_corpus_and_algorithm_fingerprints() -> None:
    card = _loaded_card()
    rule_set = RuleSet(
        schema_version=1,
        cards=(card,),
        corpus_manifest_sha256=_CORPUS_MANIFEST_SHA256,
        source_pdf_sha256=_SOURCE_PDF_SHA256,
    )

    result = _evaluate_bound_rule_card(
        card,
        _audited_context(_evaluation_context()),
        rule_set=rule_set,
        track=StrategyTrack.TREND_CONTINUATION,
        level=1,
    )

    assert result.rule_set_fingerprint == rule_set.fingerprint
    assert result.corpus_manifest_fingerprint == (
        "sha256:" + _CORPUS_MANIFEST_SHA256
    )
    assert result.algorithm_fingerprint == sha256_json(
        {"algorithm_version": card.algorithm_version}
    )


def test_evaluator_rejects_rule_card_mutated_after_loader_validation() -> None:
    card = _loaded_card()
    mutated = replace(
        card,
        candidate_predicates=(),
        confirmation_predicates=(),
        invalidation_predicates=(),
        conflict_predicates=(),
    )

    result = evaluate_rule_card(
        mutated,
        _audited_context(_evaluation_context()),
        track=StrategyTrack.TREND_CONTINUATION,
        level=1,
    )

    assert result.verdict is EvaluationVerdict.REJECT
    assert result.safe_to_proceed is False
    assert result.reasons[0].code == "invalid_rule_card"


def test_structural_stop_derivation_uses_price_crossing_not_stop_mutation() -> None:
    assert (
        derive_structural_stop_breached(
            bs_type="3buy",
            latest_price=89.0,
            stop_below=90.0,
            stop_above=None,
        )
        is True
    )
    assert (
        derive_structural_stop_breached(
            bs_type="3buy",
            latest_price=91.0,
            stop_below=90.0,
            stop_above=None,
        )
        is False
    )


def test_open_bar_can_never_confirm() -> None:
    card = _loaded_card()
    context = _evaluation_context()
    context["levels"][1]["latest_bar_closed"] = False

    result = evaluate_rule_card(
        card,
        _audited_context(context),
        track=StrategyTrack.TREND_CONTINUATION,
        level=1,
    )

    assert result.verdict is not EvaluationVerdict.CONFIRM
    assert result.safe_to_proceed is False
    assert "latest_bar_not_closed" in {reason.code for reason in result.reasons}


def test_nonconfirming_result_keeps_supporting_and_counterevidence() -> None:
    card = _loaded_card()
    context = _evaluation_context()
    context["signal"]["bs_type"] = "1buy"

    result = evaluate_rule_card(
        card,
        _audited_context(context),
        track=StrategyTrack.TREND_CONTINUATION,
        level=1,
    )

    assert result.verdict is EvaluationVerdict.WATCH
    assert result.supporting_evidence_ids == ("lesson-20-main",)
    assert result.counterevidence_ids == ("lesson-20-counter",)
    assert set(result.evidence_ids) == {"lesson-20-main", "lesson-20-counter"}


def test_critical_indeterminate_is_authoritatively_blocked() -> None:
    card = _loaded_card()
    context = _evaluation_context()
    del context["risk"]["stop_breached"]

    result = evaluate_rule_card(
        card,
        _audited_context(context),
        track=StrategyTrack.TREND_CONTINUATION,
        level=1,
    )

    assert result.verdict is EvaluationVerdict.REJECT
    assert result.critical_indeterminate is True
    assert result.safe_to_proceed is False
    assert result.invalidation_triggered is False


def test_evaluator_fail_closes_missing_null_and_inconsistent_values() -> None:
    card = _loaded_card()
    cases: list[
        tuple[dict[str, object], EvaluationVerdict, str]
    ] = []

    missing_candidate = _evaluation_context()
    del missing_candidate["signal"]["bs_type"]
    cases.append((missing_candidate, EvaluationVerdict.WATCH, "candidate_indeterminate"))

    null_candidate = _evaluation_context()
    null_candidate["signal"]["bs_type"] = None
    cases.append((null_candidate, EvaluationVerdict.WATCH, "null_project_field"))

    missing_signal_level = _evaluation_context()
    del missing_signal_level["signal"]["level"]
    cases.append(
        (
            missing_signal_level,
            EvaluationVerdict.REJECT,
            "signal_level_indeterminate",
        )
    )

    mismatched_signal_level = _evaluation_context()
    mismatched_signal_level["signal"]["level"] = 2
    cases.append(
        (
            mismatched_signal_level,
            EvaluationVerdict.REJECT,
            "signal_level_mismatch",
        )
    )

    missing_conflict_guard = _evaluation_context()
    del missing_conflict_guard["market"]["is_tradeable"]
    cases.append(
        (
            missing_conflict_guard,
            EvaluationVerdict.REJECT,
            "conflict_indeterminate",
        )
    )

    null_invalidation_guard = _evaluation_context()
    null_invalidation_guard["risk"]["stop_breached"] = None
    cases.append(
        (
            null_invalidation_guard,
            EvaluationVerdict.REJECT,
            "invalidation_indeterminate",
        )
    )

    wrong_completed_count_type = _evaluation_context()
    wrong_completed_count_type["levels"][1]["completed_bar_count"] = "2"
    cases.append(
        (
            wrong_completed_count_type,
            EvaluationVerdict.REJECT,
            "inconsistent_project_field",
        )
    )

    ambiguous_level_keys = _evaluation_context()
    ambiguous_level_keys["levels"]["1"] = {
        "completed_bar_count": 2,
        "latest_bar_closed": True,
    }
    cases.append(
        (
            ambiguous_level_keys,
            EvaluationVerdict.REJECT,
            "inconsistent_project_field",
        )
    )

    for context, expected_verdict, expected_code in cases:
        result = evaluate_rule_card(
            card,
            _audited_context(context),
            track=StrategyTrack.TREND_CONTINUATION,
            level=1,
        )

        assert result.verdict is expected_verdict
        assert expected_code in {reason.code for reason in result.reasons}
        assert result.evidence_ids
        assert all(reason.evidence_ids for reason in result.reasons)


def test_evaluator_applies_watch_reject_precedence_and_manual_boundary() -> None:
    card = _loaded_card()
    cases: list[
        tuple[
            dict[str, object],
            dict[str, object],
            StrategyTrack | str,
            int,
            EvaluationVerdict,
            str,
        ]
    ] = []

    unsupported_candidate = _evaluation_context()
    unsupported_candidate["signal"]["bs_type"] = "1buy"
    cases.append(
        (
            unsupported_candidate,
            {"chart.structure_confirmed": True},
            StrategyTrack.TREND_CONTINUATION,
            1,
            EvaluationVerdict.WATCH,
            "candidate_not_satisfied",
        )
    )

    cases.append(
        (
            _evaluation_context(),
            {"chart.structure_confirmed": False},
            StrategyTrack.TREND_CONTINUATION,
            1,
            EvaluationVerdict.WATCH,
            "confirmation_not_satisfied",
        )
    )

    insufficient_bars = _evaluation_context()
    insufficient_bars["levels"][1]["completed_bar_count"] = 1
    cases.append(
        (
            insufficient_bars,
            {"chart.structure_confirmed": True},
            StrategyTrack.TREND_CONTINUATION,
            1,
            EvaluationVerdict.WATCH,
            "insufficient_completed_bars",
        )
    )

    open_latest_bar = _evaluation_context()
    open_latest_bar["levels"][1]["latest_bar_closed"] = False
    cases.append(
        (
            open_latest_bar,
            {"chart.structure_confirmed": True},
            StrategyTrack.TREND_CONTINUATION,
            1,
            EvaluationVerdict.WATCH,
            "latest_bar_not_closed",
        )
    )

    invalidated = _evaluation_context()
    invalidated["risk"]["stop_breached"] = True
    cases.append(
        (
            invalidated,
            {"chart.structure_confirmed": True},
            StrategyTrack.TREND_CONTINUATION,
            1,
            EvaluationVerdict.REJECT,
            "invalidation_triggered",
        )
    )

    conflict = _evaluation_context()
    conflict["market"]["is_tradeable"] = False
    cases.append(
        (
            conflict,
            {"chart.structure_confirmed": True},
            StrategyTrack.TREND_CONTINUATION,
            1,
            EvaluationVerdict.REJECT,
            "conflict_triggered",
        )
    )

    cases.append(
        (
            _evaluation_context(),
            {"chart.structure_confirmed": True, "unknown.check": True},
            StrategyTrack.TREND_CONTINUATION,
            1,
            EvaluationVerdict.REJECT,
            "manual_check_set_mismatch",
        )
    )

    negative_bar_count = _evaluation_context()
    negative_bar_count["levels"][1]["completed_bar_count"] = -1
    cases.append(
        (
            negative_bar_count,
            {"chart.structure_confirmed": True},
            StrategyTrack.TREND_CONTINUATION,
            1,
            EvaluationVerdict.REJECT,
            "inconsistent_completed_bar_count",
        )
    )

    cases.append(
        (
            _evaluation_context(),
            {"chart.structure_confirmed": "yes"},
            StrategyTrack.TREND_CONTINUATION,
            1,
            EvaluationVerdict.REJECT,
            "manual_checks_inconsistent",
        )
    )

    cases.append(
        (
            _evaluation_context(),
            {"chart.structure_confirmed": True},
            StrategyTrack.BOTTOM_REVERSAL,
            1,
            EvaluationVerdict.REJECT,
            "strategy_track_mismatch",
        )
    )

    for context, checks, track, level, expected_verdict, expected_code in cases:
        legacy_checks = None
        if checks == {"chart.structure_confirmed": True}:
            context = _audited_context(context)
        elif checks == {"chart.structure_confirmed": False}:
            context = _audited_context(context, value=False)
        elif "unknown.check" in checks:
            context = _audited_context(
                context,
                manual_check_id="unknown.check",
            )
        else:
            legacy_checks = checks
        result = evaluate_rule_card(
            card,
            context,
            track=track,
            level=level,
            manual_checks=legacy_checks,
        )

        assert result.verdict is expected_verdict
        assert expected_code in {reason.code for reason in result.reasons}


def test_file_loader_rejects_duplicate_json_keys_and_matches_mapping_load(
    tmp_path,
) -> None:
    evidence, images = _catalog()
    document = _document(_valid_card())
    path = tmp_path / "rule_cards.json"
    path.write_text(
        json.dumps(document, ensure_ascii=False),
        encoding="utf-8",
    )

    from_file = load_rule_set_file(
        path,
        evidence_units=evidence,
        images=images,
    )
    from_mapping = load_rule_set(
        document,
        evidence_units=evidence,
        images=images,
    )

    assert from_file == from_mapping
    assert from_file.fingerprint == from_mapping.fingerprint

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(
        '{"schema_version":1,"schema_version":1,"cards":[]}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate JSON key"):
        load_rule_set_file(
            duplicate,
            evidence_units=evidence,
            images=images,
        )


def test_field_resolver_never_executes_arbitrary_object_attributes() -> None:
    class AttributeTrap:
        def __init__(self) -> None:
            self.access_count = 0

        @property
        def signal(self):
            self.access_count += 1
            raise RuntimeError("arbitrary property executed")

    trap = AttributeTrap()

    resolution = resolve_project_field(trap, "signal.level")

    assert resolution.status is FieldResolutionStatus.INCONSISTENT
    assert resolution.detail == "unsupported context object"
    assert trap.access_count == 0


def test_field_resolver_never_executes_dataclass_descriptors() -> None:
    accesses = {"count": 0}

    class DescriptorTrap:
        def __get__(self, instance, owner):
            accesses["count"] += 1
            raise RuntimeError("dataclass descriptor executed")

        def __set__(self, instance, value):
            instance.__dict__["signal"] = value

    @dataclass
    class DataclassTrap:
        signal: object

    trap = DataclassTrap(signal={"level": 1})
    DataclassTrap.signal = DescriptorTrap()

    resolution = resolve_project_field(trap, "signal.level")

    assert resolution.status is FieldResolutionStatus.INCONSISTENT
    assert resolution.detail == "unsupported context object"
    assert accesses["count"] == 0
