from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from chanlun.decision_support.lesson_corpus import PdfIdentity, verify_pdf_identity
from chanlun.decision_support.lesson_image_cache import (
    IncrementalLessonImageCacheBuilder,
    load_lesson_image_cache,
)
from chanlun.decision_support.lesson_image_extractor import extract_page_image_evidence
from chanlun.decision_support.lesson_pdf import classify_lesson_text, detect_lesson_boundaries
from chanlun.decision_support.lesson_span_cache import load_lesson_span_cache


TRUSTED_IDENTITY = PdfIdentity(
    filename="教你炒股票108课天使版.pdf",
    size_bytes=1_352_725_597,
    page_count=2_533,
    sha256="867b1262af2d3430b98421df4c5372748eb75a4eb7600cd967ecdc374817429e",
)
EXTRACTOR_VERSION = "lesson-image/pdfplumber-0.11.10+pillow-12/2"
EXPECTED_ASSET_COUNT = 2_783
EXPECTED_OCCURRENCE_COUNT = 2_816
EXPECTED_PRIMARY_RAW_BYTES = 1_343_589_074


def _streaming_pdf_pages(pdf):
    if hasattr(pdf, "_pages"):
        raise RuntimeError("PDF pages were materialized before streaming caches were disabled")
    document = getattr(pdf, "doc", None)
    object_cache = getattr(document, "_cached_objs", None)
    resource_manager = getattr(pdf, "rsrcmgr", None)
    font_cache = getattr(resource_manager, "_cached_fonts", None)
    if not isinstance(object_cache, dict) or not isinstance(font_cache, dict):
        raise RuntimeError("PDF backend does not expose controllable streaming caches")
    document.caching = False
    object_cache.clear()
    resource_manager.caching = False
    font_cache.clear()
    return pdf.pages


def _text_context(spans):
    boundaries = detect_lesson_boundaries(spans)
    lesson_by_page: dict[int, int] = {}
    blocks_by_page = defaultdict(list)
    spans_by_page = defaultdict(list)
    for span in spans:
        spans_by_page[span.page_number].append(span)
    for boundary in boundaries:
        lesson_spans = tuple(
            span
            for page_number in range(boundary.page_start, boundary.page_end + 1)
            for span in spans_by_page.get(page_number, ())
        )
        result = classify_lesson_text(
            boundary.lesson_number,
            boundary.page_start,
            boundary.page_end,
            lesson_spans,
        )
        for page_number in range(boundary.page_start, boundary.page_end + 1):
            lesson_by_page[page_number] = boundary.lesson_number
        for block in result.blocks:
            blocks_by_page[block.page_number].append(block)
    return lesson_by_page, blocks_by_page


def archive_pdf_pages_incrementally(
    *,
    pages,
    target: Path,
    identity: PdfIdentity,
    lesson_by_page,
    blocks_by_page,
    extractor_version: str,
    expected_asset_count: int | None = None,
    expected_occurrence_count: int | None = None,
    expected_primary_raw_bytes: int | None = None,
) -> Path:
    if len(pages) != identity.page_count:
        raise RuntimeError("verified PDF page count changed before image extraction")
    with IncrementalLessonImageCacheBuilder(
        target,
        identity=identity,
        extractor_version=extractor_version,
    ) as builder:
        for page_number, page in enumerate(pages, start=1):
            result = None
            try:
                result = extract_page_image_evidence(
                    page,
                    page_number=page_number,
                    lesson_number=lesson_by_page.get(page_number),
                    source_pdf_sha256=identity.sha256,
                    page_text_blocks=tuple(blocks_by_page.get(page_number, ())),
                    classifier_version=extractor_version,
                )
                builder.add_batch(
                    assets=result.assets,
                    occurrences=result.occurrences,
                    primary_raw_by_sha256=result.primary_raw_by_sha256,
                    smask_raw_by_sha256=result.smask_raw_by_sha256,
                )
            finally:
                result = None
                close_page = getattr(page, "close", None)
                if callable(close_page):
                    close_page()
        progress = builder.progress
        expected = (
            expected_asset_count,
            expected_occurrence_count,
            expected_primary_raw_bytes,
        )
        actual = (
            progress.asset_descriptor_count,
            progress.occurrence_count,
            progress.primary_raw_stream_bytes,
        )
        if any(value is not None for value in expected) and any(
            expected_value is not None and expected_value != actual_value
            for expected_value, actual_value in zip(expected, actual)
        ):
            raise RuntimeError(
                "PDF image inventory invariant mismatch: "
                + json.dumps(
                    {
                        "asset_count": actual[0],
                        "occurrence_count": actual[1],
                        "total_primary_raw_stream_bytes": actual[2],
                    },
                    sort_keys=True,
                )
            )
        return builder.publish()


def _report(inventory, elapsed_seconds: float, cache_action: str, target: Path) -> dict[str, object]:
    role_counts = dict(
        sorted(Counter(item.source_role.value for item in inventory.occurrences).items())
    )
    xref_occurrences = Counter(item.xref for item in inventory.occurrences)
    return {
        "archive_mode": "incremental_atomic_staging",
        "archived_primary_asset_count": len(inventory.archived_primary_paths),
        "archived_smask_asset_count": len(inventory.archived_smask_paths),
        "asset_count": len(inventory.assets),
        "cache_action": cache_action,
        "cache_path": str(target),
        "elapsed_seconds": round(elapsed_seconds, 3),
        "extractor_version": EXTRACTOR_VERSION,
        "materialized_chart_asset_count": len(inventory.materialized_paths),
        "materialized_chart_bytes": sum(
            path.stat().st_size for path in inventory.materialized_paths.values()
        ),
        "occurrence_count": len(inventory.occurrences),
        "reused_xref_count": sum(1 for count in xref_occurrences.values() if count > 1),
        "role_counts": role_counts,
        "smask_asset_count": sum(1 for asset in inventory.assets if asset.smask_xref is not None),
        "source_pdf_sha256": TRUSTED_IDENTITY.sha256,
        "total_primary_raw_stream_bytes": sum(asset.raw_size_bytes for asset in inventory.assets),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Inventory and classify all image draws in the trusted Chanlun PDF")
    parser.add_argument(
        "--pdf",
        type=Path,
        default=Path("C:/Users/lc/Desktop/教你炒股票108课天使版.pdf"),
    )
    parser.add_argument(
        "--text-cache",
        type=Path,
        default=ROOT / "audit" / "chanlun_pdf_extract",
    )
    parser.add_argument(
        "--target",
        type=Path,
        default=ROOT / "audit" / "chanlun_pdf_inventory",
    )
    args = parser.parse_args()
    started = time.monotonic()
    verified = verify_pdf_identity(args.pdf, TRUSTED_IDENTITY)
    spans = load_lesson_span_cache(args.text_cache, expected_identity=verified)
    target = args.target.resolve()
    if target.exists():
        inventory = load_lesson_image_cache(target, expected_identity=verified)
        cache_action = "reused"
    else:
        import pdfplumber

        lesson_by_page, blocks_by_page = _text_context(spans)
        with pdfplumber.open(args.pdf) as pdf:
            archive_pdf_pages_incrementally(
                pages=_streaming_pdf_pages(pdf),
                target=target,
                identity=verified,
                lesson_by_page=lesson_by_page,
                blocks_by_page=blocks_by_page,
                extractor_version=EXTRACTOR_VERSION,
                expected_asset_count=EXPECTED_ASSET_COUNT,
                expected_occurrence_count=EXPECTED_OCCURRENCE_COUNT,
                expected_primary_raw_bytes=EXPECTED_PRIMARY_RAW_BYTES,
            )
        inventory = load_lesson_image_cache(target, expected_identity=verified)
        cache_action = "created"
    report = _report(inventory, time.monotonic() - started, cache_action, target)
    if (
        report["asset_count"] != EXPECTED_ASSET_COUNT
        or report["occurrence_count"] != EXPECTED_OCCURRENCE_COUNT
        or report["total_primary_raw_stream_bytes"] != EXPECTED_PRIMARY_RAW_BYTES
    ):
        raise RuntimeError("published PDF image inventory no longer satisfies trusted invariants")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
