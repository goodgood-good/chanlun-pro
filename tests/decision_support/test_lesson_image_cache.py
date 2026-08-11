from __future__ import annotations

from dataclasses import replace
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from chanlun.decision_support import lesson_image_cache as image_cache_module
from chanlun.decision_support.lesson_corpus import PdfIdentity, SourceRole
from chanlun.decision_support.lesson_image_cache import (
    IncrementalLessonImageCacheBuilder,
    load_lesson_image_cache,
)
from chanlun.decision_support.lesson_images import ImageOccurrence, PdfImageAsset
from tests.decision_support.test_corpus_integrity import _VALID_JPEG


def _jpeg(color: tuple[int, int, int]) -> bytes:
    from PIL import Image

    stream = BytesIO()
    Image.new("RGB", (1, 1), color).save(stream, format="JPEG")
    return stream.getvalue()


def _identity() -> PdfIdentity:
    return PdfIdentity("source.pdf", 10, 263, "a" * 64)


def _asset():
    return PdfImageAsset.from_raw(
        source_pdf_sha256="a" * 64,
        xref=875,
        raw_bytes=_VALID_JPEG,
        pixel_size=(2, 3),
        filter_name="/DCTDecode",
        color_space="/DeviceRGB",
        bits_per_component=8,
    ).descriptor()


def _occurrences():
    digest = hashlib.sha256(_VALID_JPEG).hexdigest()
    common = dict(
        source_pdf_sha256="a" * 64,
        asset_sha256=digest,
        lesson_number=16,
        page_number=263,
        xref=875,
        xobject_name="IM875",
        page_size=(595.3, 841.9),
        page_rotation=0,
        classifier_id="lesson-image",
        cropbox_pdf=(0.0, 0.0, 595.3, 841.9),
        mediabox_pdf=(0.0, 0.0, 595.3, 841.9),
    )
    return (
        ImageOccurrence.create(
            **common,
            draw_index=0,
            bbox_top_left=(90.0, 445.8, 505.2, 553.32),
            source_role=SourceRole.LESSON_CHART,
            reason_codes=("verified_black_caption_below",),
            caption_page_number=263,
            caption_source_sequence_index=13,
        ),
        ImageOccurrence.create(
            **common,
            draw_index=1,
            bbox_top_left=(90.0, 600.0, 505.2, 700.0),
            source_role=SourceRole.UNKNOWN_IMAGE,
            reason_codes=("no_verified_caption",),
        ),
    )


def build_lesson_image_cache(
    target: Path,
    *,
    identity: PdfIdentity,
    assets,
    occurrences,
    materialized_raw_by_sha256: dict[str, bytes],
    materialized_smask_raw_by_sha256: dict[str, bytes] | None = None,
    extractor_id: str,
) -> Path:
    with IncrementalLessonImageCacheBuilder(
        target,
        identity=identity,
        extractor_id=extractor_id,
    ) as builder:
        builder.add_batch(
            assets=assets,
            occurrences=occurrences,
            primary_raw_by_sha256=materialized_raw_by_sha256,
            smask_raw_by_sha256=dict(materialized_smask_raw_by_sha256 or {}),
        )
        return builder.publish()


def _rewrite_jsonl_and_descriptor(
    target: Path,
    *,
    filename: str,
    descriptor_name: str,
    rows: list[dict[str, object]],
) -> None:
    payload = b"".join(image_cache_module._json_bytes(row) for row in rows)
    (target / filename).write_bytes(payload)
    manifest_path = target / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][descriptor_name] = {
        "path": filename,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def _symlink_or_skip(
    link: Path,
    target: Path,
    *,
    target_is_directory: bool = False,
) -> None:
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except (NotImplementedError, OSError) as exc:
        if target_is_directory and os.name == "nt":
            environment = os.environ.copy()
            environment["CHANLUN_TEST_LINK"] = str(link)
            environment["CHANLUN_TEST_TARGET"] = str(target)
            completed = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "New-Item -ItemType Junction -Path $env:CHANLUN_TEST_LINK "
                    "-Target $env:CHANLUN_TEST_TARGET -ErrorAction Stop | Out-Null",
                ],
                capture_output=True,
                check=False,
                env=environment,
                text=True,
            )
            if completed.returncode != 0:
                pytest.skip(f"symbolic links and junctions are unavailable: {exc}")
        else:
            pytest.skip(f"symbolic links are unavailable: {exc}")
    info = link.lstat()
    assert link.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400


def test_image_cache_is_deterministic_and_materializes_only_verified_charts(tmp_path: Path) -> None:
    digest = hashlib.sha256(_VALID_JPEG).hexdigest()
    first = build_lesson_image_cache(
        tmp_path / "first",
        identity=_identity(),
        assets=(_asset(),),
        occurrences=_occurrences(),
        materialized_raw_by_sha256={digest: _VALID_JPEG},
        extractor_id="lesson-image",
    )
    second = build_lesson_image_cache(
        tmp_path / "second",
        identity=_identity(),
        assets=(_asset(),),
        occurrences=tuple(reversed(_occurrences())),
        materialized_raw_by_sha256={digest: _VALID_JPEG},
        extractor_id="lesson-image",
    )

    loaded = load_lesson_image_cache(first, expected_identity=_identity())

    assert len(loaded.assets) == 1
    assert len(loaded.occurrences) == 2
    assert loaded.materialized_paths == {digest: first / "primary_assets" / f"{digest}.jpg"}
    assert loaded.materialized_paths[digest].read_bytes() == _VALID_JPEG
    for relative in ("image_assets.jsonl", "image_occurrences.jsonl", "manifest.json"):
        assert (first / relative).read_bytes() == (second / relative).read_bytes()


def test_image_cache_rejects_inventory_tampering(tmp_path: Path) -> None:
    digest = hashlib.sha256(_VALID_JPEG).hexdigest()
    target = build_lesson_image_cache(
        tmp_path / "cache",
        identity=_identity(),
        assets=(_asset(),),
        occurrences=_occurrences(),
        materialized_raw_by_sha256={digest: _VALID_JPEG},
        extractor_id="lesson-image",
    )
    path = target / "image_assets.jsonl"
    path.write_bytes(path.read_bytes().replace(b"875", b"876", 1))

    with pytest.raises(ValueError, match="hash"):
        load_lesson_image_cache(target, expected_identity=_identity())


def test_image_cache_rejects_unlisted_nested_archive_payload(tmp_path: Path) -> None:
    digest = hashlib.sha256(_VALID_JPEG).hexdigest()
    target = build_lesson_image_cache(
        tmp_path / "cache-with-orphan",
        identity=_identity(),
        assets=(_asset(),),
        occurrences=_occurrences(),
        materialized_raw_by_sha256={digest: _VALID_JPEG},
        extractor_id="lesson-image-changed",
    )
    orphan = target / "primary_assets" / "nested" / "orphan.jpg"
    orphan.parent.mkdir()
    orphan.write_bytes(_VALID_JPEG)

    with pytest.raises(ValueError, match="close over"):
        load_lesson_image_cache(target, expected_identity=_identity())


def test_image_cache_rejects_forged_chart_view_path(tmp_path: Path) -> None:
    digest = hashlib.sha256(_VALID_JPEG).hexdigest()
    target = build_lesson_image_cache(
        tmp_path / "cache-with-forged-chart-view",
        identity=_identity(),
        assets=(_asset(),),
        occurrences=_occurrences(),
        materialized_raw_by_sha256={digest: _VALID_JPEG},
        extractor_id="lesson-image-changed",
    )
    manifest_path = target / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["materialized_lesson_charts"][0]["path"] = "primary_assets/not-the-asset.jpg"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="chart.*path"):
        load_lesson_image_cache(target, expected_identity=_identity())


def test_image_cache_archives_every_primary_asset_and_smask_without_promoting_unknown(
    tmp_path: Path,
) -> None:
    unknown_jpeg = _jpeg((12, 34, 56))
    smask = b"uncompressed-alpha-mask"
    chart = _asset()
    unknown = PdfImageAsset.from_raw(
        source_pdf_sha256="a" * 64,
        xref=876,
        raw_bytes=unknown_jpeg,
        pixel_size=(1, 1),
        filter_name="/DCTDecode",
        color_space="/DeviceRGB",
        bits_per_component=8,
        smask_xref=877,
        smask_sha256=hashlib.sha256(smask).hexdigest(),
        smask_size_bytes=len(smask),
    ).descriptor()
    unknown_occurrence = ImageOccurrence.create(
        source_pdf_sha256="a" * 64,
        asset_sha256=unknown.raw_sha256,
        lesson_number=16,
        page_number=263,
        draw_index=2,
        xref=876,
        xobject_name="IM876",
        bbox_top_left=(100.0, 710.0, 200.0, 800.0),
        page_size=(595.3, 841.9),
        page_rotation=0,
        source_role=SourceRole.UNKNOWN_IMAGE,
        reason_codes=("no_verified_caption",),
        classifier_id="lesson-image",
    )
    occurrences = (_occurrences()[0], unknown_occurrence)
    primary = {
        chart.raw_sha256: _VALID_JPEG,
        unknown.raw_sha256: unknown_jpeg,
    }
    target = build_lesson_image_cache(
        tmp_path / "full-cache",
        identity=_identity(),
        assets=(unknown, chart),
        occurrences=tuple(reversed(occurrences)),
        materialized_raw_by_sha256=dict(reversed(tuple(primary.items()))),
        materialized_smask_raw_by_sha256={unknown.smask_sha256: smask},
        extractor_id="lesson-image-changed",
    )

    loaded = load_lesson_image_cache(target, expected_identity=_identity())
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))

    assert set(loaded.archived_primary_paths) == set(primary)
    assert loaded.archived_primary_paths[unknown.raw_sha256].read_bytes() == unknown_jpeg
    assert loaded.archived_smask_paths[unknown.smask_sha256].read_bytes() == smask
    assert set(loaded.materialized_paths) == {chart.raw_sha256}
    assert manifest["archived_primary_asset_count"] == 2
    assert manifest["archived_smask_asset_count"] == 1
    assert {entry["sha256"] for entry in manifest["materialized_primary_assets"]} == set(primary)
    assert manifest["materialized_smasks"] == [
        {
            "path": f"smasks/{unknown.smask_sha256}.bin",
            "sha256": unknown.smask_sha256,
            "size_bytes": len(smask),
        }
    ]


def test_incremental_image_cache_builder_spools_batches_and_deduplicates_payloads(
    tmp_path: Path,
) -> None:
    second_jpeg = _jpeg((101, 102, 103))
    chart = _asset()
    second = PdfImageAsset.from_raw(
        source_pdf_sha256="a" * 64,
        xref=876,
        raw_bytes=second_jpeg,
        pixel_size=(1, 1),
        filter_name="/DCTDecode",
        color_space="/DeviceRGB",
        bits_per_component=8,
    ).descriptor()
    unknown = ImageOccurrence.create(
        source_pdf_sha256="a" * 64,
        asset_sha256=second.raw_sha256,
        lesson_number=16,
        page_number=263,
        draw_index=2,
        xref=876,
        xobject_name="IM876",
        bbox_top_left=(100.0, 710.0, 200.0, 800.0),
        page_size=(595.3, 841.9),
        page_rotation=0,
        source_role=SourceRole.UNKNOWN_IMAGE,
        reason_codes=("no_verified_caption",),
        classifier_id="lesson-image-changed",
    )
    repeated = ImageOccurrence.create(
        source_pdf_sha256="a" * 64,
        asset_sha256=second.raw_sha256,
        lesson_number=16,
        page_number=263,
        draw_index=3,
        xref=876,
        xobject_name="IM876",
        bbox_top_left=(100.0, 100.0, 200.0, 200.0),
        page_size=(595.3, 841.9),
        page_rotation=0,
        source_role=SourceRole.EDITOR_IMAGE,
        reason_codes=("nearby_editor_flowchart_text",),
        classifier_id="lesson-image-changed",
    )
    target = tmp_path / "incremental"
    with image_cache_module.IncrementalLessonImageCacheBuilder(
        target,
        identity=_identity(),
        extractor_id="lesson-image-changed",
    ) as builder:
        first_progress = builder.add_batch(
            assets=(chart,),
            occurrences=(_occurrences()[0],),
            primary_raw_by_sha256={chart.raw_sha256: _VALID_JPEG},
            smask_raw_by_sha256={},
        )
        second_progress = builder.add_batch(
            assets=(second,),
            occurrences=(unknown,),
            primary_raw_by_sha256={second.raw_sha256: second_jpeg},
            smask_raw_by_sha256={},
        )
        repeated_progress = builder.add_batch(
            assets=(second,),
            occurrences=(repeated,),
            primary_raw_by_sha256={second.raw_sha256: second_jpeg},
            smask_raw_by_sha256={},
        )
        assert not target.exists()
        assert first_progress.primary_asset_count == 1
        assert second_progress.primary_asset_count == 2
        assert repeated_progress.primary_asset_count == 2
        assert repeated_progress.occurrence_count == 3
        assert builder.retained_raw_bytes == 0
        published = builder.publish()

    loaded = load_lesson_image_cache(published, expected_identity=_identity())
    assert len(loaded.occurrences) == 3
    assert len(loaded.archived_primary_paths) == 2
    assert sum(1 for _ in (published / "primary_assets").glob("*.jpg")) == 2


def test_image_cache_rejects_asset_bound_to_a_different_source_pdf(tmp_path: Path) -> None:
    digest = hashlib.sha256(_VALID_JPEG).hexdigest()
    target = build_lesson_image_cache(
        tmp_path / "asset-source-tamper",
        identity=_identity(),
        assets=(_asset(),),
        occurrences=_occurrences(),
        materialized_raw_by_sha256={digest: _VALID_JPEG},
        extractor_id="lesson-image-changed",
    )
    rows = [
        json.loads(line)
        for line in (target / "image_assets.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    rows[0]["source_pdf_sha256"] = "b" * 64
    _rewrite_jsonl_and_descriptor(
        target,
        filename="image_assets.jsonl",
        descriptor_name="assets",
        rows=rows,
    )

    with pytest.raises(ValueError, match="asset source PDF identity mismatch"):
        load_lesson_image_cache(target, expected_identity=_identity())


def test_image_cache_rejects_occurrence_bound_to_a_different_source_pdf(
    tmp_path: Path,
) -> None:
    digest = hashlib.sha256(_VALID_JPEG).hexdigest()
    target = build_lesson_image_cache(
        tmp_path / "occurrence-source-tamper",
        identity=_identity(),
        assets=(_asset(),),
        occurrences=_occurrences(),
        materialized_raw_by_sha256={digest: _VALID_JPEG},
        extractor_id="lesson-image-changed",
    )
    rows = [
        image_cache_module._occurrence_dict(
            replace(_occurrences()[0], source_pdf_sha256="b" * 64)
        ),
        image_cache_module._occurrence_dict(_occurrences()[1]),
    ]
    _rewrite_jsonl_and_descriptor(
        target,
        filename="image_occurrences.jsonl",
        descriptor_name="occurrences",
        rows=rows,
    )

    with pytest.raises(ValueError, match="occurrence source PDF identity mismatch"):
        load_lesson_image_cache(target, expected_identity=_identity())


def test_image_cache_rejects_root_symlink_before_resolving_it(tmp_path: Path) -> None:
    digest = hashlib.sha256(_VALID_JPEG).hexdigest()
    target = build_lesson_image_cache(
        tmp_path / "real-cache-root",
        identity=_identity(),
        assets=(_asset(),),
        occurrences=_occurrences(),
        materialized_raw_by_sha256={digest: _VALID_JPEG},
        extractor_id="lesson-image-changed",
    )
    alias = tmp_path / "cache-root-alias"
    _symlink_or_skip(alias, target, target_is_directory=True)

    with pytest.raises(ValueError, match="symbolic link or reparse point"):
        load_lesson_image_cache(alias, expected_identity=_identity())


def test_image_cache_rejects_cross_root_archived_leaf_symlink(tmp_path: Path) -> None:
    digest = hashlib.sha256(_VALID_JPEG).hexdigest()
    target = build_lesson_image_cache(
        tmp_path / "leaf-link-cache",
        identity=_identity(),
        assets=(_asset(),),
        occurrences=_occurrences(),
        materialized_raw_by_sha256={digest: _VALID_JPEG},
        extractor_id="lesson-image-changed",
    )
    external = tmp_path / "outside-cache.jpg"
    external.write_bytes(_VALID_JPEG)
    archived_leaf = target / "primary_assets" / f"{digest}.jpg"
    archived_leaf.unlink()
    _symlink_or_skip(archived_leaf, external)

    with pytest.raises(ValueError, match="symbolic link or reparse point"):
        load_lesson_image_cache(target, expected_identity=_identity())


def test_image_cache_rejects_archived_leaf_with_reparse_attribute(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    digest = hashlib.sha256(_VALID_JPEG).hexdigest()
    target = build_lesson_image_cache(
        tmp_path / "leaf-reparse-cache",
        identity=_identity(),
        assets=(_asset(),),
        occurrences=_occurrences(),
        materialized_raw_by_sha256={digest: _VALID_JPEG},
        extractor_id="lesson-image-changed",
    )
    archived_leaf = target / "primary_assets" / f"{digest}.jpg"
    original_lstat = Path.lstat

    def lstat_with_leaf_reparse(path: Path):
        info = original_lstat(path)
        if path == archived_leaf:
            return SimpleNamespace(
                st_file_attributes=0x400,
                st_mode=info.st_mode,
            )
        return info

    monkeypatch.setattr(Path, "lstat", lstat_with_leaf_reparse)

    with pytest.raises(ValueError, match="symbolic link or reparse point"):
        load_lesson_image_cache(target, expected_identity=_identity())


def test_image_cache_rejects_archived_directory_symlink(tmp_path: Path) -> None:
    digest = hashlib.sha256(_VALID_JPEG).hexdigest()
    target = build_lesson_image_cache(
        tmp_path / "directory-link-cache",
        identity=_identity(),
        assets=(_asset(),),
        occurrences=_occurrences(),
        materialized_raw_by_sha256={digest: _VALID_JPEG},
        extractor_id="lesson-image-changed",
    )
    archive_directory = target / "primary_assets"
    external_directory = tmp_path / "outside-primary-assets"
    shutil.move(str(archive_directory), external_directory)
    _symlink_or_skip(archive_directory, external_directory, target_is_directory=True)

    with pytest.raises(ValueError, match="symbolic link or reparse point"):
        load_lesson_image_cache(target, expected_identity=_identity())


def test_image_cache_streams_archived_payload_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    digest = hashlib.sha256(_VALID_JPEG).hexdigest()
    target = build_lesson_image_cache(
        tmp_path / "streaming-load-cache",
        identity=_identity(),
        assets=(_asset(),),
        occurrences=_occurrences(),
        materialized_raw_by_sha256={digest: _VALID_JPEG},
        extractor_id="lesson-image-changed",
    )
    original_read_bytes = Path.read_bytes

    def guarded_read_bytes(path: Path) -> bytes:
        if path.suffix in {".jpg", ".bin"}:
            raise AssertionError("archived payload must not be loaded as one bytes object")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)

    loaded = load_lesson_image_cache(target, expected_identity=_identity())

    assert set(loaded.archived_primary_paths) == {digest}
