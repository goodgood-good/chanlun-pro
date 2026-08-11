from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from chanlun.decision_support.lesson_corpus import (
    ImagePlacement,
    ImageRoleContext,
    LessonBoundary,
    LessonTextBlock,
    PageSpan,
    PdfIdentity,
    PdfIdentityMismatch,
    SourceRole,
    SourceRecord,
    TextRoleContext,
    classify_text_role,
    classify_image_role,
    build_lesson_package,
    validate_lesson_boundaries,
    is_running_header_or_footer,
    normalize_pdf_color,
    order_lesson_text_blocks,
    verify_pdf_identity,
)


def _identity_for(payload: bytes, *, pages: int = 3) -> PdfIdentity:
    return PdfIdentity(
        filename="source.pdf",
        size_bytes=len(payload),
        page_count=pages,
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def test_verify_pdf_identity_requires_hash_size_and_page_count(tmp_path: Path) -> None:
    payload = b"synthetic-pdf-source"
    source = tmp_path / "source.pdf"
    source.write_bytes(payload)
    expected = _identity_for(payload)

    actual = verify_pdf_identity(
        source,
        expected,
        page_counter=lambda path: 3,
        chunk_size=4,
    )

    assert actual == expected

    for forged in (
        PdfIdentity("source.pdf", len(payload), 3, "0" * 64),
        PdfIdentity("source.pdf", len(payload) + 1, 3, expected.sha256),
        PdfIdentity("source.pdf", len(payload), 4, expected.sha256),
    ):
        with pytest.raises(PdfIdentityMismatch):
            verify_pdf_identity(
                source,
                forged,
                page_counter=lambda path: 3,
                chunk_size=4,
            )


def test_pdf_identity_requires_integral_size_and_page_count() -> None:
    with pytest.raises(ValueError, match="size_bytes"):
        PdfIdentity("source.pdf", 1.5, 3, "a" * 64)
    with pytest.raises(ValueError, match="page_count"):
        PdfIdentity("source.pdf", 1, 3.5, "a" * 64)


def test_hash_mismatch_is_rejected_before_pdf_parser_is_called(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"untrusted-pdf")
    parser_calls: list[Path] = []

    with pytest.raises(PdfIdentityMismatch):
        verify_pdf_identity(
            source,
            PdfIdentity("source.pdf", len(b"untrusted-pdf"), 3, "0" * 64),
            page_counter=lambda path: parser_calls.append(path) or 3,
            chunk_size=4,
        )

    assert parser_calls == []


def test_zero_byte_size_mismatch_is_reported_without_constructing_an_invalid_identity(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"")
    parser_calls: list[Path] = []

    with pytest.raises(PdfIdentityMismatch) as captured:
        verify_pdf_identity(
            source,
            _identity_for(b"expected"),
            page_counter=lambda path: parser_calls.append(path) or 3,
        )

    assert captured.value.actual.size_bytes == 0
    assert captured.value.differing_fields == ("size_bytes",)
    assert parser_calls == []


def test_pdf_extraction_dependencies_are_declared() -> None:
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib

    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    corpus_dependencies = project["project"]["optional-dependencies"]["corpus"]
    normalized = {dependency.split("[", 1)[0].split(">", 1)[0].casefold() for dependency in corpus_dependencies}

    assert {"pdfplumber", "pypdf", "pillow"}.issubset(normalized)


def test_page_span_validates_geometry_and_filters_only_running_matter() -> None:
    header = PageSpan(
        page_number=7,
        bbox=(126.0, 44.8, 440.0, 55.0),
        page_size=(595.3, 841.9),
        text="天使版---教你炒股票 108 课",
        color_rgb=(0, 0, 0),
    )
    footer = PageSpan(
        page_number=7,
        bbox=(260.0, 799.0, 350.0, 812.0),
        page_size=(595.3, 841.9),
        text="第 7 页 共 2533 页",
        color_rgb=(0, 0, 0),
    )
    same_words_in_body = PageSpan(
        page_number=7,
        bbox=(100.0, 300.0, 400.0, 320.0),
        page_size=(595.3, 841.9),
        text="第 7 页 共 2533 页",
        color_rgb=(0, 0, 0),
    )

    assert is_running_header_or_footer(header) is True
    assert is_running_header_or_footer(footer) is True
    assert is_running_header_or_footer(same_words_in_body) is False

    with pytest.raises(ValueError, match="bbox"):
        PageSpan(
            page_number=7,
            bbox=(10.0, 20.0, 9.0, 21.0),
            page_size=(595.3, 841.9),
            text="broken",
            color_rgb=(0, 0, 0),
        )


def test_raw_text_hash_is_distinct_from_normalized_search_text() -> None:
    common = dict(
        page_number=7,
        bbox=(100.0, 100.0, 500.0, 120.0),
        page_size=(595.3, 841.9),
        color_rgb=(0, 0, 0),
    )
    circled = PageSpan(**common, text=" ① ")
    plain = PageSpan(**common, text="1")

    assert circled.raw_text == " ① "
    assert plain.raw_text == "1"
    assert circled.normalized_text == plain.normalized_text == "1"
    assert circled.raw_sha256 != plain.raw_sha256
    assert circled.normalized_sha256 == plain.normalized_sha256

    circled_block = LessonTextBlock(
        lesson_number=0,
        page_number=7,
        bbox=common["bbox"],
        page_size=common["page_size"],
        page_rotation=0,
        source_sequence_index=0,
        color_rgb=(0, 0, 0),
        source_role=SourceRole.LESSON_BODY,
        text=" ① ",
    )
    plain_block = LessonTextBlock(
        lesson_number=0,
        page_number=7,
        bbox=common["bbox"],
        page_size=common["page_size"],
        page_rotation=0,
        source_sequence_index=1,
        color_rgb=(0, 0, 0),
        source_role=SourceRole.LESSON_BODY,
        text="1",
    )

    assert circled_block.raw_text == " ① "
    assert circled_block.normalized_text == plain_block.normalized_text == "1"
    assert circled_block.raw_sha256 != plain_block.raw_sha256
    assert circled_block.normalized_sha256 == plain_block.normalized_sha256
    assert circled_block.content_sha256 == circled_block.raw_sha256


def test_normalize_pdf_color_handles_gray_rgb_cmyk_and_unknown() -> None:
    assert normalize_pdf_color(0.0) == (0, 0, 0)
    assert normalize_pdf_color((0.5,)) == (128, 128, 128)
    assert normalize_pdf_color((1.0, 0.298, 0.255)) == (255, 76, 65)
    assert normalize_pdf_color((0.0, 0.82, 0.0)) == (0, 209, 0)
    assert normalize_pdf_color((0.0, 1.0, 1.0, 0.0)) == (255, 0, 0)
    assert normalize_pdf_color(None) is None

    with pytest.raises(ValueError, match="PDF color"):
        normalize_pdf_color((1.2, 0.0, 0.0))


def test_text_role_classifier_fails_closed_for_ambiguous_authorship() -> None:
    def span(color: tuple[int, int, int] | None) -> PageSpan:
        return PageSpan(
            page_number=263,
            bbox=(100.0, 200.0, 500.0, 220.0),
            page_size=(595.3, 841.9),
            text="来源待分类的文字",
            color_rgb=color,
        )

    assert classify_text_role(span((0, 0, 0)), TextRoleContext()) is SourceRole.UNKNOWN_TEXT
    assert classify_text_role(
        span((0, 0, 0)),
        TextRoleContext(verified_lesson_body=True),
    ) is SourceRole.LESSON_BODY
    assert classify_text_role(
        span((48, 0, 0)),
        TextRoleContext(verified_lesson_body=True),
    ) is SourceRole.UNKNOWN_TEXT
    assert classify_text_role(
        span((0, 0, 0)),
        TextRoleContext(in_reply_section=True, reply_author="缠中说禅"),
    ) is SourceRole.CHAN_REPLY
    assert classify_text_role(
        span((0, 0, 0)),
        TextRoleContext(in_reply_section=True, reply_author="新浪网友"),
    ) is SourceRole.READER_COMMENT
    assert classify_text_role(
        span((0, 0, 0)),
        TextRoleContext(in_reply_section=True),
    ) is SourceRole.UNKNOWN_TEXT
    assert classify_text_role(span((255, 76, 65)), TextRoleContext()) is SourceRole.EDITOR_NOTE
    assert classify_text_role(
        span((0, 209, 0)),
        TextRoleContext(verified_chan_excerpt=True),
    ) is SourceRole.CHAN_EXCERPT
    assert classify_text_role(
        span((0, 209, 0)),
        TextRoleContext(verified_chan_excerpt=False),
    ) is SourceRole.EDITOR_NOTE
    assert classify_text_role(span(None), TextRoleContext()) is SourceRole.UNKNOWN_TEXT


def test_image_role_requires_positive_verification_and_original_context() -> None:
    placement = ImagePlacement(
        page_number=263,
        bbox=(170.0, 426.0, 962.0, 649.0),
        page_size=(1190.6, 1683.8),
        xobject_name="Im42",
        pixel_size=(1584, 446),
        sha256="a" * 64,
    )

    assert classify_image_role(
        placement,
        ImageRoleContext(
            lesson_number=16,
            adjacent_text_roles=(SourceRole.EDITOR_NOTE,),
        ),
    ) is SourceRole.UNKNOWN_IMAGE
    assert classify_image_role(
        placement,
        ImageRoleContext(
            lesson_number=16,
            adjacent_text_roles=(SourceRole.LESSON_BODY,),
            verification_reason="只有自由文本理由不足以晋级",
        ),
    ) is SourceRole.UNKNOWN_IMAGE
    assert classify_image_role(
        placement,
        ImageRoleContext(
            lesson_number=16,
            adjacent_text_roles=(SourceRole.LESSON_BODY,),
            caption_record_id="source:" + "b" * 64,
            position_verified=True,
            verification_reason="相邻原文图 1 标题与位置一致",
        ),
    ) is SourceRole.LESSON_CHART
    assert classify_image_role(
        placement,
        ImageRoleContext(
            lesson_number=16,
            adjacent_text_roles=(SourceRole.LESSON_BODY,),
            caption_record_id="source:" + "b" * 64,
            position_verified=True,
            verification_reason="流程图标题",
            editor_flowchart_hint=True,
        ),
    ) is SourceRole.EDITOR_IMAGE


def test_source_record_id_is_stable_and_binds_pdf_location_and_role() -> None:
    arguments = dict(
        record_type="text",
        lesson_number=16,
        page_number=263,
        bbox=(100.0, 700.0, 500.0, 720.0),
        page_size=(595.3, 841.9),
        coordinate_system="pdf_top_left_pt",
        page_rotation=0,
        color_rgb=(0, 0, 0),
        source_role=SourceRole.LESSON_BODY,
        content_sha256="b" * 64,
        normalized_text_sha256="d" * 64,
        source_pdf_sha256="c" * 64,
        output_path="L016_中小资金的高效买卖法.md",
        source_sequence_index=27,
        block_index=12,
        extractor_id="lesson-corpus",
    )

    first = SourceRecord.create(**arguments)
    second = SourceRecord.create(**arguments)
    changed_role = SourceRecord.create(
        **{**arguments, "source_role": SourceRole.EDITOR_NOTE}
    )

    assert first == second
    assert first.record_id.startswith("source:")
    assert len(first.record_id) == len("source:") + 64
    assert changed_role.record_id != first.record_id

    with pytest.raises(ValueError, match="output_path"):
        SourceRecord.create(**{**arguments, "output_path": "../escape.md"})

    for unsafe in (
        "images/file.txt:ads",
        "images/CON.jpg",
        "images/name.jpg.",
    ):
        with pytest.raises(ValueError, match="output_path"):
            SourceRecord.create(**{**arguments, "output_path": unsafe})


def test_lesson_chart_source_record_binds_caption_and_pdf_occurrence() -> None:
    arguments = dict(
        record_type="image",
        lesson_number=16,
        page_number=263,
        bbox=(90.0, 445.8, 505.2, 553.32),
        page_size=(595.3, 841.9),
        coordinate_system="pdf_top_left_pt",
        page_rotation=0,
        color_rgb=None,
        source_role=SourceRole.LESSON_CHART,
        content_sha256="a" * 64,
        source_pdf_sha256="b" * 64,
        output_path="images/a.jpg",
        source_sequence_index=1,
        block_index=0,
        extractor_id="lesson-corpus",
        caption_record_id="source:" + "c" * 64,
        source_object_id="occurrence:" + "d" * 64,
    )

    record = SourceRecord.create(**arguments)
    changed_caption = SourceRecord.create(
        **{**arguments, "caption_record_id": "source:" + "e" * 64}
    )

    assert record.caption_record_id == "source:" + "c" * 64
    assert record.source_object_id == "occurrence:" + "d" * 64
    assert changed_caption.record_id != record.record_id

    with pytest.raises(ValueError, match="caption"):
        SourceRecord.create(**{**arguments, "caption_record_id": None})


def test_lesson_boundaries_require_exact_numbers_and_contiguous_pages() -> None:
    boundaries = tuple(
        LessonBoundary(
            lesson_number=number,
            title=f"教你炒股票 {number}",
            page_start=7 + number,
            page_end=7 + number,
        )
        for number in range(109)
    )

    validated = validate_lesson_boundaries(
        boundaries,
        expected_first_page=7,
        expected_last_page=115,
    )

    assert validated == boundaries

    with pytest.raises(ValueError, match="lesson numbers"):
        validate_lesson_boundaries(
            boundaries[:-1],
            expected_first_page=7,
            expected_last_page=114,
        )
    with pytest.raises(ValueError, match="continuous"):
        validate_lesson_boundaries(
            (
                boundaries[0],
                LessonBoundary(1, "教你炒股票 1", 9, 9),
                *boundaries[2:],
            ),
            expected_first_page=7,
            expected_last_page=115,
        )


def test_text_block_order_uses_source_sequence_for_identical_geometry() -> None:
    common = dict(
        lesson_number=1,
        page_number=7,
        bbox=(100.0, 100.0, 500.0, 120.0),
        page_size=(595.3, 841.9),
        page_rotation=0,
        color_rgb=(0, 0, 0),
        source_role=SourceRole.LESSON_BODY,
    )
    later = LessonTextBlock(**common, source_sequence_index=1, text="后出现")
    earlier = LessonTextBlock(**common, source_sequence_index=0, text="先出现")

    ordered = order_lesson_text_blocks((later, earlier))

    assert tuple(block.text for block in ordered) == ("先出现", "后出现")


def test_lesson_package_writes_109_lessons_and_is_byte_deterministic(tmp_path: Path) -> None:
    payload = b"synthetic-pdf-source"
    identity = _identity_for(payload, pages=115)
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
            text=f"第 {number} 课可审计正文。",
        )
        for number in range(109)
    )

    first = build_lesson_package(
        tmp_path / "first",
        identity=identity,
        boundaries=boundaries,
        text_blocks=blocks,
        extractor_id="lesson-corpus",
        expected_first_page=7,
        expected_last_page=115,
    )
    second = build_lesson_package(
        tmp_path / "second",
        identity=identity,
        boundaries=boundaries,
        text_blocks=tuple(reversed(blocks)),
        extractor_id="lesson-corpus",
        expected_first_page=7,
        expected_last_page=115,
    )

    assert first == (tmp_path / "first").resolve()
    assert len(tuple(first.glob("L*.md"))) == 109
    assert (first / "_index.md").is_file()
    assert (first / "source_map.jsonl").is_file()
    assert (first / "manifest.json").is_file()

    manifest = json.loads((first / "manifest.json").read_text(encoding="utf-8"))
    source_rows = tuple(
        json.loads(line)
        for line in (first / "source_map.jsonl").read_text(encoding="utf-8").splitlines()
    )
    assert manifest["source_pdf"]["sha256"] == identity.sha256
    assert manifest["coverage"] == {
        "first_page": 7,
        "last_page": 115,
        "lesson_count": 109,
    }
    assert manifest["role_counts"] == {"lesson_body": 109}
    assert manifest["status"] == {
        "blockers": [
            "image_inventory_unverified",
            "source_identity_unattested",
            "determinism_uncertified",
        ],
        "integrity": "pending",
        "original_evidence": "unavailable",
    }
    assert len(source_rows) == 109
    assert {row["source_role"] for row in source_rows} == {"lesson_body"}

    def bytes_by_relative_path(root: Path) -> dict[str, bytes]:
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    assert bytes_by_relative_path(first) == bytes_by_relative_path(second)


def test_lesson_package_never_replaces_an_unowned_existing_directory(tmp_path: Path) -> None:
    target = tmp_path / "existing"
    target.mkdir()
    sentinel = target / "user-owned.txt"
    sentinel.write_text("preserve", encoding="utf-8")
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
            text=f"第 {number} 课正文。",
        )
        for number in range(109)
    )

    with pytest.raises(FileExistsError, match="already exists"):
        build_lesson_package(
            target,
            identity=_identity_for(b"source", pages=115),
            boundaries=boundaries,
            text_blocks=blocks,
            extractor_id="lesson-corpus",
            expected_first_page=7,
            expected_last_page=115,
        )

    assert sentinel.read_text(encoding="utf-8") == "preserve"
    assert tuple(target.iterdir()) == (sentinel,)


def test_lesson_package_rejects_missing_extractor_id_before_writing(tmp_path: Path) -> None:
    boundaries = tuple(
        LessonBoundary(number, f"教你炒股票 {number}", 7 + number, 7 + number)
        for number in range(109)
    )

    with pytest.raises(TypeError, match="extractor_id"):
        build_lesson_package(
            tmp_path / "never-created",
            identity=_identity_for(b"source", pages=115),
            boundaries=boundaries,
            text_blocks=(),
            extractor_id=None,
            expected_first_page=7,
            expected_last_page=115,
        )

    assert not (tmp_path / "never-created").exists()


def test_lesson_package_coverage_cannot_exceed_verified_pdf_pages(tmp_path: Path) -> None:
    boundaries = tuple(
        LessonBoundary(number, f"教你炒股票 {number}", 7 + number, 7 + number)
        for number in range(109)
    )

    with pytest.raises(ValueError, match="page_count"):
        build_lesson_package(
            tmp_path / "never-created",
            identity=_identity_for(b"source", pages=114),
            boundaries=boundaries,
            text_blocks=(),
            extractor_id="lesson-corpus",
            expected_first_page=7,
            expected_last_page=115,
        )


def test_required_provenance_strings_reject_none_instead_of_stringifying() -> None:
    with pytest.raises(TypeError, match="text"):
        PageSpan(1, (1.0, 1.0, 2.0, 2.0), (10.0, 10.0), None, (0, 0, 0))
    with pytest.raises(TypeError, match="xobject_name"):
        ImagePlacement(
            1,
            (1.0, 1.0, 2.0, 2.0),
            (10.0, 10.0),
            None,
            (1, 1),
            "a" * 64,
        )
    with pytest.raises(TypeError, match="verification_reason"):
        ImageRoleContext(
            lesson_number=1,
            adjacent_text_roles=(SourceRole.LESSON_BODY,),
            verification_reason=None,
        )
    with pytest.raises(TypeError, match="filename"):
        PdfIdentity(None, 1, 1, "a" * 64)
    with pytest.raises(TypeError, match="title"):
        LessonBoundary(1, None, 7, 7)
    with pytest.raises(TypeError, match="text"):
        LessonTextBlock(
            lesson_number=1,
            page_number=7,
            bbox=(1.0, 1.0, 2.0, 2.0),
            page_size=(10.0, 10.0),
            page_rotation=0,
            source_sequence_index=0,
            color_rgb=(0, 0, 0),
            source_role=SourceRole.LESSON_BODY,
            text=None,
        )

    record_arguments = dict(
        record_type="text",
        lesson_number=1,
        page_number=7,
        bbox=(1.0, 1.0, 2.0, 2.0),
        page_size=(10.0, 10.0),
        coordinate_system="pdf_top_left_pt",
        page_rotation=0,
        color_rgb=(0, 0, 0),
        source_role=SourceRole.LESSON_BODY,
        content_sha256="a" * 64,
        normalized_text_sha256="c" * 64,
        source_pdf_sha256="b" * 64,
        output_path="L001.md",
        source_sequence_index=0,
        block_index=0,
        extractor_id="lesson-corpus",
    )
    with pytest.raises(TypeError, match="output_path"):
        SourceRecord.create(**{**record_arguments, "output_path": None})
    with pytest.raises(TypeError, match="extractor_id"):
        SourceRecord.create(**{**record_arguments, "extractor_id": None})
