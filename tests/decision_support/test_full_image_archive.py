from __future__ import annotations

from collections import Counter
from dataclasses import replace
import hashlib
from io import BytesIO
import json
from pathlib import Path

from PIL import Image
import pytest

from chanlun.decision_support import certified_lesson_package as package_module
from chanlun.decision_support.certified_lesson_package import (
    _render_lesson_package_tree,
)
from chanlun.decision_support.corpus_loader import (
    CertifiedCorpusPolicy,
    load_certified_lesson_corpus,
    make_certified_image_loader,
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
from tests.decision_support.test_lesson_image_cache import _symlink_or_skip


def test_inventory_path_gate_rejects_windows_escape_syntax() -> None:
    assert package_module._safe_inventory_path("inventory/image_assets.jsonl") is True
    assert package_module._safe_inventory_path(r"inventory/..\..\outside") is False
    assert package_module._safe_inventory_path(r"inventory\outside") is False


def _jpeg(color: tuple[int, int, int]) -> bytes:
    stream = BytesIO()
    Image.new("RGB", (1, 1), color).save(stream, format="JPEG")
    return stream.getvalue()


def _render_full_image_fixture(
    tmp_path: Path,
    *,
    reverse_inventory: bool = False,
    root_name: str = "rendered",
) -> tuple[Path, CertifiedCorpusPolicy, dict[str, object]]:
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
    )
    quarantine_jpeg = _jpeg((12, 34, 56))
    front_jpeg = _jpeg((78, 90, 12))
    smask = b"raw-alpha-mask"
    chart = PdfImageAsset.from_raw(
        source_pdf_sha256=identity.sha256,
        xref=10,
        raw_bytes=_VALID_JPEG,
        pixel_size=(2, 3),
        filter_name="/DCTDecode",
        color_space="/DeviceRGB",
        bits_per_component=8,
    ).descriptor()
    quarantine = PdfImageAsset.from_raw(
        source_pdf_sha256=identity.sha256,
        xref=11,
        raw_bytes=quarantine_jpeg,
        pixel_size=(1, 1),
        filter_name="/DCTDecode",
        color_space="/DeviceRGB",
        bits_per_component=8,
        smask_xref=21,
        smask_sha256=hashlib.sha256(smask).hexdigest(),
        smask_size_bytes=len(smask),
    ).descriptor()
    front = PdfImageAsset.from_raw(
        source_pdf_sha256=identity.sha256,
        xref=12,
        raw_bytes=front_jpeg,
        pixel_size=(1, 1),
        filter_name="/DCTDecode",
        color_space="/DeviceRGB",
        bits_per_component=8,
    ).descriptor()

    common = {
        "source_pdf_sha256": identity.sha256,
        "page_size": (595.3, 841.9),
        "page_rotation": 0,
        "classifier_version": "lesson-image/2",
    }
    chart_occurrence = ImageOccurrence.create(
        **common,
        asset_sha256=chart.raw_sha256,
        lesson_number=0,
        page_number=7,
        draw_index=0,
        xref=10,
        xobject_name="IM10",
        bbox_top_left=(100.0, 130.0, 500.0, 250.0),
        source_role=SourceRole.LESSON_CHART,
        reason_codes=("verified_black_caption_below",),
        caption_page_number=7,
        caption_source_sequence_index=0,
    )
    unknown_occurrence = ImageOccurrence.create(
        **common,
        asset_sha256=quarantine.raw_sha256,
        lesson_number=0,
        page_number=7,
        draw_index=1,
        xref=11,
        xobject_name="IM11",
        bbox_top_left=(100.0, 300.0, 500.0, 400.0),
        source_role=SourceRole.UNKNOWN_IMAGE,
        reason_codes=("no_verified_caption",),
    )
    editor_occurrence = ImageOccurrence.create(
        **common,
        asset_sha256=quarantine.raw_sha256,
        lesson_number=1,
        page_number=8,
        draw_index=0,
        xref=11,
        xobject_name="IM11",
        bbox_top_left=(100.0, 300.0, 500.0, 400.0),
        source_role=SourceRole.EDITOR_IMAGE,
        reason_codes=("nearby_editor_flowchart_text",),
    )
    front_occurrence = ImageOccurrence.create(
        **common,
        asset_sha256=front.raw_sha256,
        lesson_number=None,
        page_number=1,
        draw_index=0,
        xref=12,
        xobject_name="IM12",
        bbox_top_left=(0.0, 0.0, 595.3, 841.9),
        source_role=SourceRole.UNKNOWN_IMAGE,
        reason_codes=("outside_lesson_coverage",),
    )
    assets = (chart, quarantine, front)
    occurrences = (
        chart_occurrence,
        unknown_occurrence,
        editor_occurrence,
        front_occurrence,
    )
    source_paths = {}
    for descriptor, payload in (
        (chart, _VALID_JPEG),
        (quarantine, quarantine_jpeg),
        (front, front_jpeg),
    ):
        path = tmp_path / f"{descriptor.raw_sha256}.jpg"
        path.write_bytes(payload)
        source_paths[descriptor.raw_sha256] = path
    smask_path = tmp_path / f"{quarantine.smask_sha256}.bin"
    smask_path.write_bytes(smask)
    inventory = LessonImageInventory(
        assets=tuple(reversed(assets)) if reverse_inventory else assets,
        occurrences=(
            tuple(reversed(occurrences)) if reverse_inventory else occurrences
        ),
        materialized_paths={chart.raw_sha256: source_paths[chart.raw_sha256]},
        archived_primary_paths=source_paths,
        archived_smask_paths={quarantine.smask_sha256: smask_path},
    )
    extractions = tuple(
        LessonTextExtraction(
            blocks=(block,),
            reply_records=(),
            reply_record_count=0,
            closed_reply_record_count=0,
            ambiguous_reply_record_count=0,
            skipped_running_matter_count=0,
        )
        for block in blocks
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
        image_role_counts=dict(
            sorted(Counter(item.source_role.value for item in occurrences).items())
        ),
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
    inventory_files = {
        "inventory/image_assets.jsonl": b"".join(
            (
                json.dumps(_asset_dict(asset), sort_keys=True, separators=(",", ":"))
                + "\n"
            ).encode("utf-8")
            for asset in assets
        ),
        "inventory/image_occurrences.jsonl": b"".join(
            (
                json.dumps(_occurrence_dict(item), sort_keys=True, separators=(",", ":"))
                + "\n"
            ).encode("utf-8")
            for item in occurrences
        ),
        "inventory/text_spans.jsonl": text_cache_bytes,
    }
    root = tmp_path / root_name
    _render_lesson_package_tree(
        root,
        identity=identity,
        boundaries=boundaries,
        text_blocks=blocks,
        image_inventory=inventory,
        extractor_versions={
            "text": "lesson-pdf/2",
            "image": "lesson-image/2",
            "package": "lesson-package/2",
        },
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
    manifest_sha256 = hashlib.sha256((root / "manifest.json").read_bytes()).hexdigest()
    policy = CertifiedCorpusPolicy(
        manifest_sha256=manifest_sha256,
        source_pdf_sha256=identity.sha256,
        source_pdf_size_bytes=identity.size_bytes,
        source_pdf_page_count=identity.page_count,
        lesson_count=109,
        image_asset_count=3,
        image_occurrence_count=4,
        total_primary_raw_stream_bytes=sum(asset.raw_size_bytes for asset in assets),
        image_role_counts={"editor_image": 1, "lesson_chart": 1, "unknown_image": 2},
        materialized_chart_asset_count=1,
    )
    return root, policy, {
        "chart": chart,
        "chart_occurrence": chart_occurrence,
        "editor_occurrence": editor_occurrence,
        "front": front,
        "quarantine": quarantine,
        "smask": smask,
        "unknown_occurrence": unknown_occurrence,
    }


def test_certified_package_archives_every_course_image_but_exposes_only_verified_charts(
    tmp_path: Path,
) -> None:
    root, policy, evidence = _render_full_image_fixture(tmp_path)
    rows = tuple(
        json.loads(line)
        for line in (root / "source_map.jsonl").read_text(encoding="utf-8").splitlines()
    )
    image_rows = tuple(row for row in rows if row["record_type"] == "image")
    image_role_counts = Counter(row["source_role"] for row in image_rows)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    markdown = "\n".join(path.read_text(encoding="utf-8") for path in root.glob("L*.md"))

    assert image_role_counts == {"lesson_chart": 1, "unknown_image": 1, "editor_image": 1}
    assert len(image_rows) == 3
    for row in image_rows:
        assert row["asset_id"] == f"asset:{row['content_sha256']}"
        assert row["occurrence_id"] == row["source_object_id"]
        assert row["classification_id"].startswith("classification:")
        assert row["reason_codes"]
        assert (root / row["output_path"]).is_file()
    quarantine_row = next(row for row in image_rows if row["source_role"] == "unknown_image")
    assert quarantine_row["smask_sha256"] == evidence["quarantine"].smask_sha256
    assert (root / quarantine_row["smask_output_path"]).read_bytes() == evidence["smask"]
    assert not (root / "images" / "assets" / f"{evidence['front'].raw_sha256}.jpg").exists()
    assert markdown.count("![lesson_chart") == 1
    assert "unknown_image" not in markdown
    assert "editor_image" not in markdown
    assert manifest["inventory"]["course_image_occurrence_count"] == 3
    assert manifest["inventory"]["front_matter_occurrence_count"] == 1
    assert manifest["inventory"]["archived_course_primary_asset_count"] == 2
    assert manifest["inventory"]["archived_course_smask_asset_count"] == 1

    corpus = load_certified_lesson_corpus(root, policy=policy)
    assert len(corpus.images) == 1
    assert corpus.images[0].source_role == "lesson_chart"
    assert corpus.images[0].occurrence_id == evidence["chart_occurrence"].occurrence_id


def test_corpus_loader_verifies_quarantine_files_without_buffering_their_payloads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, policy, evidence = _render_full_image_fixture(tmp_path)
    quarantine_name = f"{evidence['quarantine'].raw_sha256}.jpg"
    original_read_bytes = Path.read_bytes

    def guarded_read_bytes(path: Path) -> bytes:
        if path.name == quarantine_name:
            raise AssertionError("quarantined payload must not be buffered by the runtime loader")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)

    corpus = load_certified_lesson_corpus(root, policy=policy)
    assert len(corpus.images) == 1


def test_corpus_loader_rejects_missing_semantic_certificate_even_when_manifest_is_pinned(
    tmp_path: Path,
) -> None:
    root, policy, _ = _render_full_image_fixture(tmp_path)
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    del manifest["certification"]["semantic_audit"]
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    missing_semantic_policy = replace(
        policy,
        manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    )

    with pytest.raises(ValueError, match="semantic certification"):
        load_certified_lesson_corpus(root, policy=missing_semantic_policy)


def test_corpus_loader_rejects_asset_pdf_identity_forgery_even_when_manifest_is_pinned(
    tmp_path: Path,
) -> None:
    root, policy, _ = _render_full_image_fixture(tmp_path)
    inventory_path = root / "inventory" / "image_assets.jsonl"
    rows = [json.loads(line) for line in inventory_path.read_text(encoding="utf-8").splitlines()]
    rows[0]["source_pdf_sha256"] = "f" * 64
    inventory_path.write_text(
        "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = next(
        item for item in manifest["files"] if item["path"] == "inventory/image_assets.jsonl"
    )
    entry["sha256"] = hashlib.sha256(inventory_path.read_bytes()).hexdigest()
    entry["size_bytes"] = inventory_path.stat().st_size
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    forged_policy = replace(
        policy,
        manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    )

    with pytest.raises(ValueError, match="asset PDF hash"):
        load_certified_lesson_corpus(root, policy=forged_policy)


def test_corpus_loader_rejects_lesson_boundary_forgery_even_when_manifest_is_pinned(
    tmp_path: Path,
) -> None:
    root, policy, _ = _render_full_image_fixture(tmp_path)
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["lessons"][0]["title"] = "forged lesson boundary"
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    forged_policy = replace(
        policy,
        manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    )

    with pytest.raises(ValueError, match="lesson boundary"):
        load_certified_lesson_corpus(root, policy=forged_policy)


def test_corpus_loader_rejects_root_symlink_or_junction(tmp_path: Path) -> None:
    root, policy, _ = _render_full_image_fixture(tmp_path)
    alias = tmp_path / "corpus-alias"
    _symlink_or_skip(alias, root, target_is_directory=True)

    with pytest.raises(ValueError, match="symbolic link or reparse point"):
        load_certified_lesson_corpus(alias, policy=policy)


def test_provider_loader_rejects_oversized_replacement_before_read_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, policy, _ = _render_full_image_fixture(tmp_path)
    corpus = load_certified_lesson_corpus(root, policy=policy)
    image = corpus.images[0]
    image_path = root / image.source_path
    image_path.write_bytes(b"x" * 32)
    original_read_bytes = Path.read_bytes

    def guarded_read_bytes(path: Path) -> bytes:
        if path == image_path:
            raise AssertionError("oversized provider image must be rejected before read_bytes")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)
    loader = make_certified_image_loader(corpus, max_bytes=16)

    with pytest.raises(ValueError, match="payload size"):
        loader(image)


def test_corpus_loader_rejects_unlisted_nested_manifest_named_file(tmp_path: Path) -> None:
    root, policy, _ = _render_full_image_fixture(tmp_path)
    orphan = root / "images" / "assets" / "nested" / "manifest.json"
    orphan.parent.mkdir()
    orphan.write_text("not part of the certified package", encoding="utf-8")

    with pytest.raises(ValueError, match="close over"):
        load_certified_lesson_corpus(root, policy=policy)


def test_corpus_loader_rejects_smask_whose_manifest_hash_no_longer_matches_provenance(
    tmp_path: Path,
) -> None:
    root, policy, evidence = _render_full_image_fixture(tmp_path)
    relative = f"images/smasks/{evidence['quarantine'].smask_sha256}.bin"
    path = root / relative
    forged = b"forged-mask-with-self-consistent-file-entry"
    path.write_bytes(forged)
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = next(item for item in manifest["files"] if item["path"] == relative)
    entry["sha256"] = hashlib.sha256(forged).hexdigest()
    entry["size_bytes"] = len(forged)
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    forged_policy = replace(
        policy,
        manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    )

    with pytest.raises(ValueError, match="SMask.*hash"):
        load_certified_lesson_corpus(root, policy=forged_policy)


def test_corpus_loader_rejects_manifest_image_roles_that_disagree_with_inventory_policy(
    tmp_path: Path,
) -> None:
    root, policy, _ = _render_full_image_fixture(tmp_path)
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["inventory"]["image_role_counts"] = {"lesson_chart": 4}
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    forged_policy = replace(
        policy,
        manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    )

    with pytest.raises(ValueError, match="image inventory policy"):
        load_certified_lesson_corpus(root, policy=forged_policy)


def test_double_build_fingerprint_streams_archived_images_without_buffering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _, evidence = _render_full_image_fixture(tmp_path)
    quarantine_name = f"{evidence['quarantine'].raw_sha256}.jpg"
    original_read_bytes = Path.read_bytes

    def guarded_read_bytes(path: Path) -> bytes:
        if path.name == quarantine_name:
            raise AssertionError("double-build comparison must stream archived images")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)

    fingerprints = package_module._tree_fingerprints(root)
    assert fingerprints[f"images/assets/{quarantine_name}"][0] > 0


def test_full_image_source_map_is_deterministic_when_inventory_order_changes(
    tmp_path: Path,
) -> None:
    first, _, _ = _render_full_image_fixture(tmp_path, root_name="first")
    second, _, _ = _render_full_image_fixture(
        tmp_path,
        reverse_inventory=True,
        root_name="second",
    )

    assert package_module._tree_fingerprints(first) == package_module._tree_fingerprints(second)
