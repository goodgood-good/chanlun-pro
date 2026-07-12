from __future__ import annotations

from collections import Counter
from dataclasses import replace

import pytest

from chanlun.decision_support.certified_lesson_package import (
    SEMANTIC_AUDIT_VERSION,
    SemanticAuditPolicy,
    build_semantic_certification,
    lesson_boundary_sha256,
    semantic_classification_sha256,
)
from chanlun.decision_support.lesson_corpus import (
    LessonBoundary,
    LessonTextBlock,
    PdfIdentity,
    SourceRole,
)
from chanlun.decision_support.lesson_image_cache import LessonImageInventory
from chanlun.decision_support.lesson_images import ImageOccurrence, PdfImageAsset
from chanlun.decision_support.lesson_pdf import LessonTextExtraction, ReplyRecordAudit
from tests.decision_support.test_corpus_integrity import _VALID_JPEG


IDENTITY = PdfIdentity("source.pdf", 1_000, 7, "a" * 64)
BOUNDARIES = (LessonBoundary(0, "lesson zero", 7, 7),)


def _block(sequence: int, role: SourceRole, text: str) -> LessonTextBlock:
    return LessonTextBlock(
        lesson_number=0,
        page_number=7,
        bbox=(10.0, 20.0 + sequence, 30.0, 21.0 + sequence),
        page_size=(100.0, 200.0),
        page_rotation=0,
        source_sequence_index=sequence,
        color_rgb=(0, 0, 0),
        source_role=role,
        text=text,
    )


def _extraction(
    blocks: tuple[LessonTextBlock, ...],
    *,
    resolution: str | None = None,
    ambiguous: int = 0,
    skipped: int = 0,
    reader_positions: tuple[tuple[int, int], ...] = (),
    author_positions: tuple[tuple[int, int], ...] = (),
    quarantined_positions: tuple[tuple[int, int], ...] = (),
    separator_positions: tuple[tuple[int, int], ...] = (),
) -> LessonTextExtraction:
    records = ()
    if resolution is not None:
        records = (
            ReplyRecordAudit(
                record_index=0,
                source_positions=tuple(
                    (block.page_number, block.source_sequence_index) for block in blocks
                ),
                resolution=resolution,
                reason_codes=("test_fixture",),
                reader_source_positions=reader_positions,
                author_source_positions=author_positions,
                quarantined_source_positions=quarantined_positions,
                separator_source_positions=separator_positions,
            ),
        )
    return LessonTextExtraction(
        blocks=blocks,
        reply_records=records,
        reply_record_count=len(records),
        closed_reply_record_count=len(records) - ambiguous,
        ambiguous_reply_record_count=ambiguous,
        skipped_running_matter_count=skipped,
    )


def _policy(
    extraction: LessonTextExtraction,
    inventory: LessonImageInventory | None = None,
) -> SemanticAuditPolicy:
    image_inventory = inventory or LessonImageInventory(
        assets=(), occurrences=(), materialized_paths={}
    )
    return SemanticAuditPolicy(
        source_pdf=IDENTITY,
        text_cache_sha256="b" * 64,
        text_span_count=3,
        lesson_boundary_sha256=lesson_boundary_sha256(BOUNDARIES),
        lesson_boundary_count=1,
        text_role_counts=dict(
            sorted(Counter(block.source_role.value for block in extraction.blocks).items())
        ),
        image_role_counts=dict(
            sorted(
                Counter(item.source_role.value for item in image_inventory.occurrences).items()
            )
        ),
        reply_resolution_counts=dict(
            sorted(Counter(item.resolution for item in extraction.reply_records).items())
        ),
        skipped_running_matter_count=extraction.skipped_running_matter_count,
        classification_sha256=semantic_classification_sha256(
            boundaries=BOUNDARIES,
            extractions=(extraction,),
            image_inventory=image_inventory,
        ),
    )


def test_semantic_gate_blocks_reader_leak_ambiguous_reply_and_unreviewed_unknowns() -> None:
    blocks = (
        _block(0, SourceRole.LESSON_BODY, "reader text leaked as author"),
        _block(1, SourceRole.EDITOR_NOTE, "===="),
        _block(2, SourceRole.UNKNOWN_TEXT, "unreviewed author text"),
    )
    extraction = _extraction(
        blocks,
        resolution="ambiguous",
        ambiguous=1,
        reader_positions=((7, 0),),
        quarantined_positions=((7, 2),),
        separator_positions=((7, 1),),
    )
    unknown_asset = PdfImageAsset.from_raw(
        source_pdf_sha256=IDENTITY.sha256,
        xref=1,
        raw_bytes=_VALID_JPEG,
        pixel_size=(2, 3),
        filter_name="/DCTDecode",
        color_space="/DeviceRGB",
        bits_per_component=8,
    ).descriptor()
    unknown_occurrence = ImageOccurrence.create(
        source_pdf_sha256=IDENTITY.sha256,
        asset_sha256=unknown_asset.raw_sha256,
        lesson_number=0,
        page_number=7,
        draw_index=0,
        xref=1,
        xobject_name="IM1",
        bbox_top_left=(10.0, 30.0, 40.0, 60.0),
        page_size=(100.0, 200.0),
        page_rotation=0,
        source_role=SourceRole.UNKNOWN_IMAGE,
        reason_codes=("unreviewed",),
        classifier_version="test/1",
    )
    inventory = LessonImageInventory(
        assets=(unknown_asset,),
        occurrences=(unknown_occurrence,),
        materialized_paths={},
    )

    result = build_semantic_certification(
        identity=IDENTITY,
        boundaries=BOUNDARIES,
        extractions=(extraction,),
        image_inventory=inventory,
        text_cache_sha256="b" * 64,
        text_span_count=3,
        policy=_policy(extraction, inventory),
    )

    assert result.payload["semantic_audit_version"] == SEMANTIC_AUDIT_VERSION
    assert result.payload["semantic_gate_passed"] is False
    assert result.payload["role_audit"]["reader_authoritative_leak_count"] == 1
    assert result.payload["role_audit"]["ambiguous_reply_record_count"] == 1
    assert result.payload["role_audit"]["quarantined_unknown_text_count"] == 1
    assert result.payload["role_audit"]["quarantined_unknown_image_count"] == 1
    assert result.payload["role_audit"]["unknown_image_authoritative_leak_count"] == 0
    assert result.blockers == ("reader_authoritative_leak:1",)
    assert result.warnings == (
        "ambiguous_reply_records:1",
        "quarantined_unknown_text:1",
        "quarantined_unknown_image:1",
    )


def test_semantic_gate_passes_only_when_structural_reader_side_is_quarantined() -> None:
    blocks = (
        _block(0, SourceRole.READER_COMMENT, "reader text"),
        _block(1, SourceRole.EDITOR_NOTE, "===="),
        _block(2, SourceRole.CHAN_REPLY, "author text"),
    )
    extraction = _extraction(
        blocks,
        resolution="reader_then_author",
        reader_positions=((7, 0),),
        author_positions=((7, 2),),
        separator_positions=((7, 1),),
    )

    result = build_semantic_certification(
        identity=IDENTITY,
        boundaries=BOUNDARIES,
        extractions=(extraction,),
        image_inventory=LessonImageInventory(assets=(), occurrences=(), materialized_paths={}),
        text_cache_sha256="b" * 64,
        text_span_count=3,
        policy=_policy(extraction),
    )

    assert result.blockers == ()
    assert result.payload["semantic_gate_passed"] is True
    assert result.payload["thresholds"] == {
        "anonymous_chan_reply_count_max": 0,
        "image_provenance_incomplete_count_max": 0,
        "quarantined_text_authoritative_leak_count_max": 0,
        "reader_authoritative_leak_count_max": 0,
        "reply_provenance_incomplete_count_max": 0,
        "unknown_image_authoritative_leak_count_max": 0,
    }


def test_semantic_classification_hash_binds_image_classification_id() -> None:
    extraction = _extraction((_block(0, SourceRole.LESSON_BODY, "body"),))
    asset = PdfImageAsset.from_raw(
        source_pdf_sha256=IDENTITY.sha256,
        xref=1,
        raw_bytes=_VALID_JPEG,
        pixel_size=(2, 3),
        filter_name="/DCTDecode",
        color_space="/DeviceRGB",
        bits_per_component=8,
    ).descriptor()
    values = {
        "source_pdf_sha256": IDENTITY.sha256,
        "asset_sha256": asset.raw_sha256,
        "lesson_number": 0,
        "page_number": 7,
        "draw_index": 0,
        "xref": 1,
        "xobject_name": "IM1",
        "bbox_top_left": (10.0, 30.0, 40.0, 60.0),
        "page_size": (100.0, 200.0),
        "page_rotation": 0,
        "source_role": SourceRole.UNKNOWN_IMAGE,
        "reason_codes": ("unreviewed",),
    }
    first = ImageOccurrence.create(**values, classifier_version="test/1")
    second = ImageOccurrence.create(**values, classifier_version="test/2")
    assert first.occurrence_id == second.occurrence_id
    assert first.classification_id != second.classification_id

    first_hash = semantic_classification_sha256(
        boundaries=BOUNDARIES,
        extractions=(extraction,),
        image_inventory=LessonImageInventory(
            assets=(asset,), occurrences=(first,), materialized_paths={}
        ),
    )
    second_hash = semantic_classification_sha256(
        boundaries=BOUNDARIES,
        extractions=(extraction,),
        image_inventory=LessonImageInventory(
            assets=(asset,), occurrences=(second,), materialized_paths={}
        ),
    )

    assert first_hash != second_hash


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("identity", replace(IDENTITY, sha256="f" * 64), "source PDF identity"),
        ("text_cache_sha256", "f" * 64, "text cache fingerprint"),
        ("text_span_count", 4, "text span count"),
        ("boundaries", (LessonBoundary(0, "changed", 7, 7),), "lesson boundaries"),
    ),
)
def test_semantic_gate_rejects_unpinned_source_cache_or_boundaries(
    field: str,
    value: object,
    message: str,
) -> None:
    extraction = _extraction((_block(0, SourceRole.LESSON_BODY, "body"),))
    kwargs = {
        "identity": IDENTITY,
        "boundaries": BOUNDARIES,
        "extractions": (extraction,),
        "image_inventory": LessonImageInventory(
            assets=(), occurrences=(), materialized_paths={}
        ),
        "text_cache_sha256": "b" * 64,
        "text_span_count": 3,
        "policy": _policy(extraction),
    }
    kwargs[field] = value

    with pytest.raises(ValueError, match=message):
        build_semantic_certification(**kwargs)
