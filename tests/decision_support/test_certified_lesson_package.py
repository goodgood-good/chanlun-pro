from __future__ import annotations

from collections import Counter
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from chanlun.decision_support import certified_lesson_package as package_module
from chanlun.decision_support.certified_lesson_package import _render_lesson_package_tree
from chanlun.decision_support.corpus_loader import (
    CertifiedCorpusPolicy,
    make_certified_image_loader,
    load_certified_lesson_corpus,
)
from chanlun.decision_support.lesson_corpus import (
    LessonBoundary,
    LessonTextBlock,
    PdfIdentity,
    SourceRole,
)
from chanlun.decision_support.lesson_image_cache import (
    LessonImageInventory,
    _asset_dict,
    _occurrence_dict,
)
from chanlun.decision_support.lesson_images import ImageOccurrence, PdfImageAsset
from chanlun.decision_support.lesson_pdf import LessonTextExtraction
from tests.decision_support.test_corpus_integrity import _VALID_JPEG


def test_rendered_package_links_original_chart_to_caption_and_occurrence(tmp_path: Path) -> None:
    identity = PdfIdentity("source.pdf", 10, 115, "a" * 64)
    boundaries = tuple(
        LessonBoundary(number, f"教你炒股票 {number}", 7 + number, 7 + number)
        for number in range(109)
    )
    blocks = tuple(
        LessonTextBlock(
            lesson_number=number,
            page_number=7 + number,
            bbox=(100.0, 100.0, 500.0, 120.0),
            page_size=(595.3, 841.9),
            page_rotation=0,
            source_sequence_index=0,
            color_rgb=(0, 0, 0),
            source_role=SourceRole.LESSON_BODY,
            text="图1" if number == 0 else f"第 {number} 课正文。",
        )
        for number in range(109)
    ) + (
        LessonTextBlock(
            lesson_number=0,
            page_number=7,
            bbox=(100.0, 270.0, 500.0, 290.0),
            page_size=(595.3, 841.9),
            page_rotation=0,
            source_sequence_index=1,
            color_rgb=(255, 76, 65),
            source_role=SourceRole.EDITOR_NOTE,
            text="不得进入原文检索的编者按。",
        ),
        LessonTextBlock(
            lesson_number=0,
            page_number=7,
            bbox=(100.0, 300.0, 500.0, 320.0),
            page_size=(595.3, 841.9),
            page_rotation=0,
            source_sequence_index=2,
            color_rgb=(0, 0, 0),
            source_role=SourceRole.LESSON_BODY,
            text="第二段正文。",
        ),
        LessonTextBlock(
            lesson_number=1,
            page_number=8,
            bbox=(100.0, 130.0, 500.0, 150.0),
            page_size=(595.3, 841.9),
            page_rotation=0,
            source_sequence_index=1,
            color_rgb=(0, 0, 0),
            source_role=SourceRole.LESSON_BODY,
            text="同页连续正文。",
        ),
    )
    asset = PdfImageAsset.from_raw(
        source_pdf_sha256=identity.sha256,
        xref=875,
        raw_bytes=_VALID_JPEG,
        pixel_size=(2, 3),
        filter_name="/DCTDecode",
        color_space="/DeviceRGB",
        bits_per_component=8,
    ).descriptor()
    occurrence = ImageOccurrence.create(
        source_pdf_sha256=identity.sha256,
        asset_sha256=asset.raw_sha256,
        lesson_number=0,
        page_number=7,
        draw_index=0,
        xref=875,
        xobject_name="IM875",
        bbox_top_left=(100.0, 130.0, 500.0, 250.0),
        page_size=(595.3, 841.9),
        page_rotation=0,
        source_role=SourceRole.LESSON_CHART,
        reason_codes=("verified_black_caption_below",),
        classifier_version="lesson-image/1",
        caption_page_number=7,
        caption_source_sequence_index=0,
    )
    repeated_occurrence = ImageOccurrence.create(
        source_pdf_sha256=identity.sha256,
        asset_sha256=asset.raw_sha256,
        lesson_number=1,
        page_number=8,
        draw_index=1,
        xref=875,
        xobject_name="IM875",
        bbox_top_left=(110.0, 160.0, 490.0, 260.0),
        page_size=(595.3, 841.9),
        page_rotation=0,
        source_role=SourceRole.LESSON_CHART,
        reason_codes=("verified_black_caption_below",),
        classifier_version="lesson-image/1",
        caption_page_number=8,
        caption_source_sequence_index=0,
    )
    source_image = tmp_path / "source.jpg"
    source_image.write_bytes(_VALID_JPEG)
    inventory = LessonImageInventory(
        assets=(asset,),
        occurrences=(occurrence, repeated_occurrence),
        materialized_paths={asset.raw_sha256: source_image},
    )
    extractions = tuple(
        LessonTextExtraction(
            blocks=tuple(block for block in blocks if block.lesson_number == number),
            reply_records=(),
            reply_record_count=0,
            closed_reply_record_count=0,
            ambiguous_reply_record_count=0,
            skipped_running_matter_count=0,
        )
        for number in range(109)
    )
    text_cache_bytes = b"fixture-text-cache\n"
    semantic_policy = package_module.SemanticAuditPolicy(
        source_pdf=identity,
        text_cache_sha256=hashlib.sha256(text_cache_bytes).hexdigest(),
        text_span_count=len(blocks),
        lesson_boundary_sha256=package_module.lesson_boundary_sha256(boundaries),
        lesson_boundary_count=len(boundaries),
        text_role_counts=dict(
            sorted(Counter(block.source_role.value for block in blocks).items())
        ),
        image_role_counts={"lesson_chart": 2},
        reply_resolution_counts={},
        skipped_running_matter_count=0,
        classification_sha256=package_module.semantic_classification_sha256(
            boundaries=boundaries,
            extractions=extractions,
            image_inventory=inventory,
        ),
    )
    semantic = package_module.build_semantic_certification(
        identity=identity,
        boundaries=boundaries,
        extractions=extractions,
        image_inventory=inventory,
        text_cache_sha256=semantic_policy.text_cache_sha256,
        text_span_count=semantic_policy.text_span_count,
        policy=semantic_policy,
    )
    assert semantic.blockers == ()
    root = tmp_path / "rendered"
    inventory_files = {
        "inventory/image_assets.jsonl": (
            json.dumps(_asset_dict(asset), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8"),
        "inventory/image_occurrences.jsonl": (
            "".join(
                json.dumps(
                    _occurrence_dict(item),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
                for item in (occurrence, repeated_occurrence)
            )
            ).encode("utf-8"),
        "inventory/text_spans.jsonl": text_cache_bytes,
    }

    _render_lesson_package_tree(
        root,
        identity=identity,
        boundaries=boundaries,
        text_blocks=blocks,
        image_inventory=inventory,
        extractor_versions={"text": "lesson-pdf/1", "image": "lesson-image/1", "package": "lesson-package/1"},
        certification={
            "byte_identical_double_build": True,
            "determinism_builds": 2,
            "semantic_audit": semantic.payload,
            "source_identity_reverified": True,
        },
        inventory_files=inventory_files,
        expected_first_page=7,
        expected_last_page=115,
    )

    rows = [
        json.loads(line)
        for line in (root / "source_map.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    image_row = next(row for row in rows if row["record_type"] == "image")
    text_ids = {row["record_id"] for row in rows if row["record_type"] == "text"}
    markdown = next(root.glob("L000_*.md")).read_text(encoding="utf-8")
    copied_image = root / image_row["output_path"]
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))

    assert image_row["caption_record_id"] in text_ids
    assert image_row["source_object_id"] == occurrence.occurrence_id
    assert copied_image.read_bytes() == _VALID_JPEG
    assert hashlib.sha256(copied_image.read_bytes()).hexdigest() == asset.raw_sha256
    assert f"images/assets/{asset.raw_sha256}.jpg" in markdown
    assert manifest["status"] == {
        "blockers": [],
        "integrity": "certified",
        "original_evidence": "available",
    }

    policy = CertifiedCorpusPolicy(
        manifest_sha256=hashlib.sha256((root / "manifest.json").read_bytes()).hexdigest(),
        source_pdf_sha256=identity.sha256,
        source_pdf_size_bytes=identity.size_bytes,
        source_pdf_page_count=identity.page_count,
        lesson_count=109,
        image_asset_count=1,
            image_occurrence_count=2,
        total_primary_raw_stream_bytes=len(_VALID_JPEG),
            image_role_counts={"lesson_chart": 2},
        materialized_chart_asset_count=1,
    )
    corpus = load_certified_lesson_corpus(root, policy=policy)
    assert len(corpus.units) == 111
    assert len(corpus.semantic_units) == 110
    assert len(corpus.images) == 2
    assert all("编者按" not in unit.text for unit in corpus.units)
    first_unit = next(unit for unit in corpus.units if unit.lesson == 0)
    assert first_unit.text == "图1"
    assert first_unit.source_role == "lesson_body"
    first_image = next(image for image in corpus.images if image.page_number == 7)
    assert first_unit.image_ids == (first_image.image_id,)
    lesson_zero_units = tuple(unit for unit in corpus.units if unit.lesson == 0)
    assert tuple(unit.text for unit in lesson_zero_units) == ("图1", "第二段正文。")
    assert tuple(unit.source_sequence_index for unit in lesson_zero_units) == (0, 2)
    assert all(unit.source_record_ids == (unit.source_record_id,) for unit in corpus.units)
    lesson_one_chunk = next(unit for unit in corpus.semantic_units if unit.lesson == 1)
    assert lesson_one_chunk.text == "第 1 课正文。\n同页连续正文。"
    assert len(lesson_one_chunk.source_record_ids) == 2
    assert first_image.sha256 == f"sha256:{asset.raw_sha256}"
    assert first_image.image_id != first_image.sha256
    assert first_image.asset_id == first_image.sha256
    assert first_image.occurrence_id == occurrence.occurrence_id
    assert {image.occurrence_id for image in corpus.images} == {
        occurrence.occurrence_id,
        repeated_occurrence.occurrence_id,
    }

    provider_image = make_certified_image_loader(corpus)(first_image)
    assert provider_image.image_id == first_image.image_id
    assert provider_image.media_type == "image/jpeg"
    assert provider_image.data_url.startswith("data:image/jpeg;base64,")

    with pytest.raises(ValueError, match="manifest fingerprint"):
        load_certified_lesson_corpus(
            root,
            policy=replace(policy, manifest_sha256="f" * 64),
        )
