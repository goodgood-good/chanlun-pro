from __future__ import annotations

import hashlib

import pytest

from chanlun.decision_support.lesson_corpus import SourceRole
from chanlun.decision_support.lesson_images import ImageOccurrence, PdfImageAsset
from tests.decision_support.test_corpus_integrity import _VALID_JPEG


def test_pdf_image_asset_preserves_original_jpeg_bytes_and_metadata() -> None:
    asset = PdfImageAsset.from_raw(
        source_pdf_sha256="a" * 64,
        xref=43,
        raw_bytes=_VALID_JPEG,
        pixel_size=(2, 3),
        filter_name="/DCTDecode",
        color_space="/DeviceRGB",
        bits_per_component=8,
    )

    digest = hashlib.sha256(_VALID_JPEG).hexdigest()
    assert asset.asset_id == f"asset:{digest}"
    assert asset.raw_sha256 == digest
    assert asset.raw_bytes == _VALID_JPEG
    assert asset.pixel_size == (2, 3)
    descriptor = asset.descriptor()
    assert descriptor.asset_id == asset.asset_id
    assert descriptor.raw_sha256 == digest
    assert descriptor.raw_size_bytes == len(_VALID_JPEG)
    assert not hasattr(descriptor, "raw_bytes")

    with pytest.raises(ValueError, match="JPEG"):
        PdfImageAsset.from_raw(
            source_pdf_sha256="a" * 64,
            xref=43,
            raw_bytes=b"not-a-jpeg",
            pixel_size=(2, 3),
            filter_name="/DCTDecode",
            color_space="/DeviceRGB",
            bits_per_component=8,
        )


def test_image_occurrence_keeps_repeated_draws_of_the_same_asset_distinct() -> None:
    common = dict(
        source_pdf_sha256="a" * 64,
        asset_sha256="b" * 64,
        lesson_number=16,
        page_number=313,
        xref=1034,
        xobject_name="IM1034",
        bbox_top_left=(153.12, 74.88, 441.60, 271.44),
        page_size=(595.3, 841.9),
        page_rotation=0,
        source_role=SourceRole.UNKNOWN_IMAGE,
        reason_codes=("no_verified_caption",),
        classifier_version="lesson-image/1",
    )

    first = ImageOccurrence.create(**common, draw_index=0)
    second = ImageOccurrence.create(
        **{
            **common,
            "draw_index": 1,
            "bbox_top_left": (153.0, 277.8, 441.48, 474.36),
        }
    )
    editor = ImageOccurrence.create(
        **{
            **common,
            "source_role": SourceRole.EDITOR_IMAGE,
            "reason_codes": ("editor_flowchart_hint",),
        },
        draw_index=2,
    )
    chart = ImageOccurrence.create(
        **{
            **common,
            "source_role": SourceRole.LESSON_CHART,
            "reason_codes": ("verified_black_caption_below",),
            "caption_page_number": 313,
            "caption_source_sequence_index": 9,
        },
        draw_index=3,
    )
    front_matter = ImageOccurrence.create(
        **{
            **common,
            "lesson_number": None,
            "page_number": 1,
            "source_role": SourceRole.UNKNOWN_IMAGE,
            "reason_codes": ("outside_lesson_coverage",),
        },
        draw_index=0,
    )

    assert first.asset_sha256 == second.asset_sha256
    assert first.occurrence_id != second.occurrence_id
    assert first.bbox_pdf_bottom_left == pytest.approx(
        (153.12, 841.9 - 271.44, 441.60, 841.9 - 74.88)
    )
    assert second.draw_index == 1
    assert editor.source_role is SourceRole.EDITOR_IMAGE
    assert chart.caption_source_position == (313, 9)
    assert front_matter.lesson_number is None

    with pytest.raises(ValueError, match="caption"):
        ImageOccurrence.create(
            **{
                **common,
                "source_role": SourceRole.LESSON_CHART,
                "reason_codes": ("verified_black_caption_below",),
            },
            draw_index=4,
        )


def test_image_occurrence_physical_id_does_not_change_when_classification_changes() -> None:
    physical = dict(
        source_pdf_sha256="a" * 64,
        asset_sha256="b" * 64,
        lesson_number=16,
        page_number=313,
        draw_index=0,
        xref=1034,
        xobject_name="IM1034",
        bbox_top_left=(153.12, 74.88, 441.60, 271.44),
        draw_bbox_top_left=(153.0, 74.5, 442.0, 272.0),
        page_size=(595.3, 841.9),
        page_rotation=0,
        cropbox_pdf=(0.0, 0.0, 595.3, 841.9),
        mediabox_pdf=(0.0, 0.0, 595.3, 841.9),
    )
    unknown = ImageOccurrence.create(
        **physical,
        source_role=SourceRole.UNKNOWN_IMAGE,
        reason_codes=("no_verified_caption",),
        classifier_version="lesson-image/1",
    )
    editor = ImageOccurrence.create(
        **{
            **physical,
            "lesson_number": 17,
            "bbox_top_left": (153.0, 74.5, 442.0, 272.0),
        },
        source_role=SourceRole.EDITOR_IMAGE,
        reason_codes=("editor_flowchart_hint",),
        classifier_version="lesson-image/2",
    )
    chart = ImageOccurrence.create(
        **physical,
        source_role=SourceRole.LESSON_CHART,
        reason_codes=("verified_black_caption_below",),
        classifier_version="lesson-image/3",
        caption_page_number=313,
        caption_source_sequence_index=9,
    )

    assert unknown.occurrence_id == editor.occurrence_id == chart.occurrence_id


def test_image_occurrence_classification_id_changes_with_classification_evidence() -> None:
    common = dict(
        source_pdf_sha256="a" * 64,
        asset_sha256="b" * 64,
        lesson_number=16,
        page_number=313,
        draw_index=0,
        xref=1034,
        xobject_name="IM1034",
        bbox_top_left=(153.12, 74.88, 441.60, 271.44),
        page_size=(595.3, 841.9),
        page_rotation=0,
        source_role=SourceRole.UNKNOWN_IMAGE,
        reason_codes=("no_verified_caption",),
    )
    first = ImageOccurrence.create(**common, classifier_version="lesson-image/1")
    reclassified = ImageOccurrence.create(
        **{
            **common,
            "source_role": SourceRole.EDITOR_IMAGE,
            "reason_codes": ("editor_flowchart_hint",),
        },
        classifier_version="lesson-image/2",
    )

    assert first.occurrence_id == reclassified.occurrence_id
    assert first.classification_id != reclassified.classification_id
    assert first.classification_id.startswith("classification:")
    assert len(first.classification_id) == len("classification:") + 64


def test_image_occurrence_physical_id_tracks_raw_draw_geometry_not_clipped_bbox() -> None:
    common = dict(
        source_pdf_sha256="a" * 64,
        asset_sha256="b" * 64,
        lesson_number=16,
        page_number=313,
        draw_index=0,
        xref=1034,
        xobject_name="IM1034",
        bbox_top_left=(0.0, 0.0, 100.0, 100.0),
        draw_bbox_top_left=(-10.0, -20.0, 100.0, 100.0),
        page_size=(595.3, 841.9),
        page_rotation=0,
        source_role=SourceRole.UNKNOWN_IMAGE,
        reason_codes=("no_verified_caption",),
        classifier_version="lesson-image/1",
    )
    baseline = ImageOccurrence.create(**common)
    differently_clipped = ImageOccurrence.create(
        **{**common, "bbox_top_left": (1.0, 2.0, 100.0, 100.0)}
    )
    different_raw_draw = ImageOccurrence.create(
        **{**common, "draw_bbox_top_left": (-11.0, -20.0, 100.0, 100.0)}
    )

    assert baseline.occurrence_id == differently_clipped.occurrence_id
    assert baseline.occurrence_id != different_raw_draw.occurrence_id
