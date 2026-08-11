from __future__ import annotations

from pathlib import Path

import pytest

from chanlun.decision_support.lesson_corpus import PdfIdentity
from chanlun.decision_support.lesson_image_cache import load_lesson_image_cache
from tests.decision_support.test_lesson_image_extractor import _Page
from tools import extract_chanlun_pdf_images as image_tool


class _ClosablePage(_Page):
    def __init__(self) -> None:
        super().__init__()
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class _StreamingPdfFixture:
    def __init__(self) -> None:
        self.doc = type(
            "Doc",
            (),
            {"caching": True, "_cached_objs": {43: object()}},
        )()
        self.rsrcmgr = type(
            "ResourceManager",
            (),
            {"caching": True, "_cached_fonts": {44: object()}},
        )()
        self.pages_requested = False

    @property
    def pages(self) -> tuple[object, ...]:
        self.pages_requested = True
        assert self.doc.caching is False
        assert self.doc._cached_objs == {}
        assert self.rsrcmgr.caching is False
        assert self.rsrcmgr._cached_fonts == {}
        return ()


def test_tool_disables_pdfminer_global_caches_before_materializing_pages() -> None:
    pdf = _StreamingPdfFixture()

    pages = image_tool._streaming_pdf_pages(pdf)

    assert pages == ()
    assert pdf.pages_requested is True


def test_tool_archives_each_page_incrementally_and_deduplicates_reused_asset(
    tmp_path: Path,
) -> None:
    identity = PdfIdentity("source.pdf", 10, 2, "a" * 64)
    target = tmp_path / "image-cache"

    published = image_tool.archive_pdf_pages_incrementally(
        pages=(_Page(), _Page()),
        target=target,
        identity=identity,
        lesson_by_page={},
        blocks_by_page={},
        extractor_id="lesson-image-changed",
    )

    inventory = load_lesson_image_cache(published, expected_identity=identity)
    assert len(inventory.assets) == 1
    assert len(inventory.occurrences) == 4
    assert len(inventory.archived_primary_paths) == 1
    assert len(inventory.archived_smask_paths) == 1
    assert sum(1 for _ in (published / "primary_assets").glob("*.jpg")) == 1
    report = image_tool._report(inventory, 0.25, "created", published)
    assert report["archived_primary_asset_count"] == 1
    assert report["archived_smask_asset_count"] == 1
    assert report["archive_mode"] == "incremental_atomic_staging"


def test_tool_releases_each_page_cache_before_inventory_validation(
    tmp_path: Path,
) -> None:
    identity = PdfIdentity("source.pdf", 10, 2, "a" * 64)
    target = tmp_path / "rejected-cache"
    pages = (_ClosablePage(), _ClosablePage())

    with pytest.raises(RuntimeError, match="invariant mismatch"):
        image_tool.archive_pdf_pages_incrementally(
            pages=pages,
            target=target,
            identity=identity,
            lesson_by_page={},
            blocks_by_page={},
            extractor_id="lesson-image-changed",
            expected_asset_count=2,
        )

    assert [page.close_calls for page in pages] == [1, 1]
    assert not target.exists()


def test_tool_does_not_publish_incremental_archive_when_inventory_gate_fails(
    tmp_path: Path,
) -> None:
    identity = PdfIdentity("source.pdf", 10, 1, "a" * 64)
    target = tmp_path / "rejected-cache"

    with pytest.raises(RuntimeError, match="invariant mismatch"):
        image_tool.archive_pdf_pages_incrementally(
            pages=(_Page(),),
            target=target,
            identity=identity,
            lesson_by_page={},
            blocks_by_page={},
            extractor_id="lesson-image-changed",
            expected_asset_count=2,
            expected_occurrence_count=2,
            expected_primary_raw_bytes=1,
        )

    assert not target.exists()
    assert not tuple(tmp_path.glob(".rejected-cache.staging-*"))
