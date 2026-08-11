from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pytest

from chanlun.decision_support.corpus_integrity import scan_corpus
from chanlun.decision_support.corpus_sources import (
    CorpusChangedError,
    collect_image_evidence,
    parse_illustrated_archive,
    parse_lesson_index,
    write_trusted_manifest,
)
from chanlun.decision_support.corpus_types import SourceTier
from tests.decision_support.test_corpus_integrity import _VALID_JPEG


def _write_article(root: Path, body: str, *, name: str = "01_1_区间套.md") -> Path:
    article_dir = root / "articles"
    article_dir.mkdir(parents=True, exist_ok=True)
    article = article_dir / name
    article.write_text(
        "# 区间套\n\n"
        "- Author: 整理者\n"
        "- Source: https://example.invalid/1\n\n"
        + body,
        encoding="utf-8",
    )
    return article


def test_parse_lesson_index_keeps_lesson_and_source_path(tmp_path: Path):
    root = tmp_path / "extract"
    root.mkdir()
    lesson = root / "L061_s072_区间套定位标准图解.md"
    lesson.write_text(
        "# 教你炒股票 61：区间套定位标准图解\n\n区间套正文。",
        encoding="utf-8",
    )
    (root / "_index.md").write_text(
        "| 72 | 61 | 4 | 0 | L061_s072_区间套定位标准图解.md | 预览 |\n",
        encoding="utf-8",
    )

    units = parse_lesson_index(root, scan_corpus([root]))

    assert units
    assert all(unit.source_tier is SourceTier.LESSON_ORIGINAL for unit in units)
    assert units[0].lesson == 61
    assert units[0].sha256 == hashlib.sha256(lesson.read_bytes()).hexdigest()
    assert units[0].title == "教你炒股票 61：区间套定位标准图解"
    assert units[0].source_path.endswith("L061_s072_区间套定位标准图解.md")
    assert "区间套正文" in units[0].text


def test_illustrated_archive_binds_only_verified_images(tmp_path: Path):
    root = tmp_path / "archive"
    image_dir = root / "articles" / "images" / "01_1"
    image_dir.mkdir(parents=True)
    _write_article(
        root,
        "图前说明。\n\n![](images/01_1/01.jpg)\n\n图后说明。\n\n"
        "![](images/01_1/02.jpg)",
    )
    (image_dir / "01.jpg").write_bytes(_VALID_JPEG)
    (image_dir / "02.jpg").write_bytes(b"")
    report = scan_corpus([root])

    units = parse_illustrated_archive(root, report)
    images = collect_image_evidence(root, report, SourceTier.SECONDARY_ANNOTATION)

    assert len(images) == 1
    assert images[0].image_id.startswith("sha256:")
    assert images[0].source_tier is SourceTier.SECONDARY_ANNOTATION
    assert images[0].media_type == "image/jpeg"
    assert (images[0].width, images[0].height) == (2, 3)
    assert not Path(images[0].source_path).is_absolute()
    bound = [unit for unit in units if images[0].image_id in unit.image_ids]
    assert len(bound) == 1
    assert "图前说明" in bound[0].text and "图后说明" in bound[0].text
    assert bound[0].author == "整理者"
    assert bound[0].source_url == "https://example.invalid/1"
    assert all("02.jpg" not in unit.image_ids for unit in units)


def test_illustrated_archive_rejects_image_path_traversal(tmp_path: Path):
    root = tmp_path / "archive"
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(_VALID_JPEG)
    _write_article(root, "图前。\n\n![](../../outside.jpg)\n\n图后。")
    report = scan_corpus([tmp_path])

    units = parse_illustrated_archive(root, report)
    images = collect_image_evidence(root, report, SourceTier.SECONDARY_ANNOTATION)

    assert units
    assert all(unit.image_ids == () for unit in units)
    assert images == ()


def test_source_parser_skips_markdown_absent_from_valid_files(tmp_path: Path):
    root = tmp_path / "archive"
    article = root / "articles" / "01_1_损坏.md"
    article.parent.mkdir(parents=True)
    article.write_bytes(b"\xff\xfe\xfa")

    units = parse_illustrated_archive(root, scan_corpus([root]))

    assert units == ()


def test_parser_rejects_markdown_changed_after_integrity_scan(tmp_path: Path):
    root = tmp_path / "archive"
    article = _write_article(root, "扫描时正文。")
    report = scan_corpus([root])
    article.write_text(article.read_text("utf-8") + "\n\n扫描后篡改。", encoding="utf-8")

    with pytest.raises(CorpusChangedError, match="changed after integrity scan"):
        parse_illustrated_archive(root, report)


def test_image_collection_rejects_bytes_changed_after_integrity_scan(tmp_path: Path):
    root = tmp_path / "archive"
    image_dir = root / "articles" / "images" / "01_1"
    image_dir.mkdir(parents=True)
    image = image_dir / "01.jpg"
    image.write_bytes(_VALID_JPEG)
    report = scan_corpus([root])
    image.write_bytes(b"tampered")

    with pytest.raises(CorpusChangedError, match="changed after integrity scan"):
        collect_image_evidence(root, report, SourceTier.SECONDARY_ANNOTATION)


def test_image_collection_rejects_model_inference_tier(tmp_path: Path):
    root = tmp_path / "archive"
    image_dir = root / "articles" / "images" / "01_1"
    image_dir.mkdir(parents=True)
    (image_dir / "01.jpg").write_bytes(_VALID_JPEG)
    report = scan_corpus([root])

    with pytest.raises(ValueError, match="untrusted image source tier"):
        collect_image_evidence(root, report, SourceTier.MODEL_INFERENCE)


def test_evidence_ids_are_stable_and_change_with_text(tmp_path: Path):
    root = tmp_path / "archive"
    article = _write_article(root, "第三类买点回试不进入中枢。")
    report = scan_corpus([root])
    first = parse_illustrated_archive(root, report)
    second = parse_illustrated_archive(root, report)
    article.write_text(article.read_text("utf-8") + "\n\n新增段落。", encoding="utf-8")
    changed = parse_illustrated_archive(root, scan_corpus([root]))

    assert [unit.evidence_id for unit in first] == [unit.evidence_id for unit in second]
    assert {unit.evidence_id for unit in first} != {unit.evidence_id for unit in changed}


def test_manifest_is_deterministic_and_marks_missing_original(tmp_path: Path):
    root = tmp_path / "archive"
    image_dir = root / "articles" / "images" / "01_1"
    image_dir.mkdir(parents=True)
    _write_article(root, "区间套。\n\n![](images/01_1/01.jpg)")
    (image_dir / "01.jpg").write_bytes(_VALID_JPEG)
    report = scan_corpus([root])
    units = parse_illustrated_archive(root, report)
    images = collect_image_evidence(root, report, SourceTier.SECONDARY_ANNOTATION)
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"

    write_trusted_manifest(
        first,
        tuple(reversed(units)),
        tuple(reversed(images)),
        integrity_status="incomplete",
    )
    write_trusted_manifest(
        second,
        units,
        images,
        integrity_status="incomplete",
    )

    assert first.read_bytes() == second.read_bytes()
    body = json.loads(first.read_text("utf-8"))
    assert body["schema"] == "current"
    assert body["corpus_status"]["original_evidence"] == "missing_original"
    assert body["corpus_status"]["integrity"] == "incomplete"
    assert [item["evidence_id"] for item in body["units"]] == sorted(
        item["evidence_id"] for item in body["units"]
    )
    assert body["images"][0]["media_type"] == "image/jpeg"
    assert not first.with_suffix(first.suffix + ".tmp").exists()


def test_manifest_rejects_parent_traversal_source_path(tmp_path: Path):
    root = tmp_path / "archive"
    _write_article(root, "区间套。")
    units = parse_illustrated_archive(root, scan_corpus([root]))
    unsafe = replace(units[0], source_path="../escape.md")

    with pytest.raises(ValueError, match="unsafe source_path"):
        write_trusted_manifest(tmp_path / "manifest.json", (unsafe,), ())


def test_manifest_rejects_drive_absolute_image_path(tmp_path: Path):
    root = tmp_path / "archive"
    image_dir = root / "articles" / "images" / "01_1"
    image_dir.mkdir(parents=True)
    _write_article(root, "区间套。\n\n![](images/01_1/01.jpg)")
    (image_dir / "01.jpg").write_bytes(_VALID_JPEG)
    report = scan_corpus([root])
    units = parse_illustrated_archive(root, report)
    images = collect_image_evidence(root, report, SourceTier.SECONDARY_ANNOTATION)
    unsafe = replace(images[0], source_path="D:/outside.jpg")

    with pytest.raises(ValueError, match="unsafe source_path"):
        write_trusted_manifest(tmp_path / "manifest.json", units, (unsafe,))


def test_manifest_rejects_forged_model_inference_image(tmp_path: Path):
    root = tmp_path / "archive"
    image_dir = root / "articles" / "images" / "01_1"
    image_dir.mkdir(parents=True)
    _write_article(root, "区间套。\n\n![](images/01_1/01.jpg)")
    (image_dir / "01.jpg").write_bytes(_VALID_JPEG)
    report = scan_corpus([root])
    units = parse_illustrated_archive(root, report)
    images = collect_image_evidence(root, report, SourceTier.SECONDARY_ANNOTATION)
    forged = replace(images[0], source_tier=SourceTier.MODEL_INFERENCE)

    with pytest.raises(ValueError, match="model inference cannot enter trusted corpus"):
        write_trusted_manifest(tmp_path / "manifest.json", units, (forged,))


def test_manifest_rejects_forged_evidence_id(tmp_path: Path):
    root = tmp_path / "archive"
    _write_article(root, "区间套。")
    units = parse_illustrated_archive(root, scan_corpus([root]))
    forged = replace(units[0], evidence_id="user-claim")

    with pytest.raises(ValueError, match="invalid evidence_id"):
        write_trusted_manifest(tmp_path / "manifest.json", (forged,), ())

def test_manifest_rejects_unit_without_source_hash(tmp_path: Path):
    root = tmp_path / "archive"
    _write_article(root, "区间套。")
    units = parse_illustrated_archive(root, scan_corpus([root]))
    forged = replace(units[0], sha256="")

    with pytest.raises(ValueError, match="invalid unit sha256"):
        write_trusted_manifest(tmp_path / "manifest.json", (forged,), ())


def test_manifest_rejects_image_id_hash_mismatch(tmp_path: Path):
    root = tmp_path / "archive"
    image_dir = root / "articles" / "images" / "01_1"
    image_dir.mkdir(parents=True)
    _write_article(root, "区间套。\n\n![](images/01_1/01.jpg)")
    (image_dir / "01.jpg").write_bytes(_VALID_JPEG)
    report = scan_corpus([root])
    units = parse_illustrated_archive(root, report)
    images = collect_image_evidence(root, report, SourceTier.SECONDARY_ANNOTATION)
    forged = replace(images[0], sha256="0" * 64)

    with pytest.raises(ValueError, match="image id and sha256 mismatch"):
        write_trusted_manifest(tmp_path / "manifest.json", units, (forged,))

def test_manifest_marks_original_available_when_lesson_exists(tmp_path: Path):
    root = tmp_path / "extract"
    root.mkdir()
    lesson = root / "L001_s001_原文.md"
    lesson.write_text("# 教你炒股票 1\n\n原文段落。", encoding="utf-8")
    (root / "_index.md").write_text(
        "| 1 | 1 | 1 | 0 | L001_s001_原文.md | 预览 |\n",
        encoding="utf-8",
    )
    units = parse_lesson_index(root, scan_corpus([root]))
    manifest = tmp_path / "manifest.json"

    write_trusted_manifest(manifest, units, ())

    body = json.loads(manifest.read_text("utf-8"))
    assert body["corpus_status"]["original_evidence"] == "available"