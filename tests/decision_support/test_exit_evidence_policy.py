from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from chanlun.decision_support.corpus_loader import CertifiedLessonCorpus
from chanlun.decision_support.corpus_types import EvidenceUnit, SourceTier
from chanlun.decision_support.exit_evidence_policy import (
    load_exit_evidence_policy,
    load_exit_evidence_policy_file,
)
from chanlun.decision_support.exits import ExitTrigger


_MANIFEST_SHA256 = "a" * 64
_SOURCE_PDF_SHA256 = "b" * 64
_TRIGGERS = tuple(ExitTrigger)


def _unit(index: int) -> EvidenceUnit:
    record_id = f"source:{index:064x}"
    return EvidenceUnit(
        evidence_id=f"evidence:{index:064x}",
        source_tier=SourceTier.LESSON_ORIGINAL,
        source_path=f"L{index:03d}_lesson.md",
        title=f"lesson {index}",
        text=f"certified original exit evidence {index}",
        sha256=f"{index + 100:064x}",
        lesson=index,
        source_role="lesson_body",
        source_record_id=record_id,
        source_pdf_sha256=_SOURCE_PDF_SHA256,
        page_number=100 + index,
        bbox=(1.0, 2.0, 3.0, 4.0),
        source_sequence_index=index,
        block_index=index,
        source_record_ids=(record_id,),
    )


def _corpus(
    units: tuple[EvidenceUnit, ...],
    *,
    manifest_sha256: str = _MANIFEST_SHA256,
    source_pdf_sha256: str = _SOURCE_PDF_SHA256,
) -> CertifiedLessonCorpus:
    return CertifiedLessonCorpus(
        root=Path("certified-corpus"),
        units=units,
        semantic_units=units,
        images=(),
        manifest_sha256=manifest_sha256,
        source_pdf_sha256=source_pdf_sha256,
    )


def _reference(unit: EvidenceUnit) -> dict[str, object]:
    return {
        "evidence_id": unit.evidence_id,
        "lesson": unit.lesson,
        "pdf_page": unit.page_number,
        "source_role": unit.source_role,
        "evidence_sha256": unit.sha256,
    }


def _document(units: tuple[EvidenceUnit, ...]) -> dict[str, object]:
    bindings = []
    for trigger, unit in zip(_TRIGGERS, units, strict=True):
        bindings.append(
            {
                "trigger": trigger.value,
                "references": [_reference(unit)],
                "boundary_tags": (
                    ["project_risk_latch"]
                    if trigger is ExitTrigger.HARD_RISK
                    else []
                ),
            }
        )
    return {
        "schema_version": 1,
        "policy_id": "chanlun.original_exit_evidence",
        "version": 1,
        "corpus_manifest_sha256": _MANIFEST_SHA256,
        "source_pdf_sha256": _SOURCE_PDF_SHA256,
        "bindings": bindings,
    }


def test_policy_is_a_complete_deterministic_exit_evidence_resolver() -> None:
    units = tuple(_unit(index) for index in range(1, 6))

    policy = load_exit_evidence_policy(_document(units), corpus=_corpus(units))

    assert policy.policy_id == "chanlun.original_exit_evidence"
    assert policy.version == 1
    assert policy.fingerprint.startswith("sha256:")
    assert tuple(binding.trigger for binding in policy.bindings) == _TRIGGERS
    for trigger, unit in zip(_TRIGGERS, units, strict=True):
        assert policy.resolve(trigger) == (unit.evidence_id,)
        assert policy(trigger) == (unit.evidence_id,)
    assert policy.binding(ExitTrigger.HARD_RISK).boundary_tags == (
        "project_risk_latch",
    )


@pytest.mark.parametrize(
    ("field_name", "replacement", "match"),
    (
        ("corpus_manifest_sha256", "c" * 64, "manifest fingerprint mismatch"),
        ("source_pdf_sha256", "d" * 64, "source PDF fingerprint mismatch"),
    ),
)
def test_policy_rejects_document_corpus_identity_mismatch(
    field_name: str,
    replacement: str,
    match: str,
) -> None:
    units = tuple(_unit(index) for index in range(1, 6))
    document = _document(units)
    document[field_name] = replacement

    with pytest.raises(ValueError, match=match):
        load_exit_evidence_policy(document, corpus=_corpus(units))


@pytest.mark.parametrize(
    ("field_name", "replacement", "match"),
    (
        ("evidence_id", "evidence:" + "f" * 64, "does not exist"),
        ("lesson", 99, "lesson mismatch"),
        ("pdf_page", 999, "PDF page mismatch"),
        ("source_role", "chan_reply", "source role mismatch"),
        ("evidence_sha256", "f" * 64, "content fingerprint mismatch"),
    ),
)
def test_policy_rejects_forged_reference_coordinates(
    field_name: str,
    replacement: object,
    match: str,
) -> None:
    units = tuple(_unit(index) for index in range(1, 6))
    document = _document(units)
    reference = document["bindings"][0]["references"][0]
    reference[field_name] = replacement

    with pytest.raises(ValueError, match=match):
        load_exit_evidence_policy(document, corpus=_corpus(units))


@pytest.mark.parametrize(
    ("changed", "match"),
    (
        ({"source_tier": SourceTier.SECONDARY_ANNOTATION}, "lesson_original"),
        ({"source_role": "editor_note"}, "authoritative original role"),
        ({"source_pdf_sha256": "c" * 64}, "original provenance"),
        ({"source_record_ids": ()}, "original provenance"),
    ),
)
def test_policy_rejects_non_original_or_incomplete_corpus_evidence(
    changed: dict[str, object],
    match: str,
) -> None:
    units = tuple(_unit(index) for index in range(1, 6))
    compromised = (replace(units[0], **changed), *units[1:])

    with pytest.raises(ValueError, match=match):
        load_exit_evidence_policy(
            _document(units),
            corpus=_corpus(compromised),
        )


def test_policy_rejects_missing_unknown_or_duplicate_trigger_bindings() -> None:
    units = tuple(_unit(index) for index in range(1, 6))
    corpus = _corpus(units)

    missing = _document(units)
    missing["bindings"] = missing["bindings"][:-1]
    with pytest.raises(ValueError, match="complete exit trigger set"):
        load_exit_evidence_policy(missing, corpus=corpus)

    unknown = _document(units)
    unknown["bindings"][0]["trigger"] = "invented_exit"
    with pytest.raises(ValueError, match="unknown exit trigger"):
        load_exit_evidence_policy(unknown, corpus=corpus)

    duplicate = _document(units)
    duplicate["bindings"][1]["trigger"] = ExitTrigger.HARD_RISK.value
    with pytest.raises(ValueError, match="duplicate exit trigger"):
        load_exit_evidence_policy(duplicate, corpus=corpus)


def test_policy_rejects_hard_risk_without_explicit_project_boundary() -> None:
    units = tuple(_unit(index) for index in range(1, 6))
    document = _document(units)
    document["bindings"][0]["boundary_tags"] = []

    with pytest.raises(ValueError, match="project_risk_latch"):
        load_exit_evidence_policy(document, corpus=_corpus(units))


def test_file_loader_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    units = tuple(_unit(index) for index in range(1, 6))
    path = tmp_path / "duplicate.json"
    path.write_text(
        '{"schema_version":1,"schema_version":1}',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate JSON key"):
        load_exit_evidence_policy_file(path, corpus=_corpus(units))


def test_policy_rejects_unknown_fields_and_never_uses_a_default_binding() -> None:
    units = tuple(_unit(index) for index in range(1, 6))
    document = deepcopy(_document(units))
    document["unexpected"] = True

    with pytest.raises(ValueError, match="policy fields"):
        load_exit_evidence_policy(document, corpus=_corpus(units))

