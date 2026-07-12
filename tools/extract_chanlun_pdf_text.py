from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from chanlun.decision_support.lesson_corpus import PdfIdentity, verify_pdf_identity
from chanlun.decision_support.lesson_pdf import classify_lesson_text, detect_lesson_boundaries
from chanlun.decision_support.lesson_pdf_extractor import extract_page_spans
from chanlun.decision_support.lesson_span_cache import (
    build_lesson_span_cache,
    load_lesson_span_cache,
)


TRUSTED_IDENTITY = PdfIdentity(
    filename="教你炒股票108课天使版.pdf",
    size_bytes=1_352_725_597,
    page_count=2_533,
    sha256="867b1262af2d3430b98421df4c5372748eb75a4eb7600cd967ecdc374817429e",
)
EXTRACTOR_VERSION = "lesson-pdf/pdfplumber-0.11.10/2"


def _analysis(spans: tuple[object, ...]) -> dict[str, object]:
    boundaries = detect_lesson_boundaries(
        spans,
        expected_first_page=7,
        expected_last_page=TRUSTED_IDENTITY.page_count,
    )
    roles: Counter[str] = Counter()
    reply_records = 0
    closed_reply_records = 0
    ambiguous_reply_records = 0
    skipped_running_matter = 0
    for boundary in boundaries:
        lesson_spans = tuple(
            span
            for span in spans
            if boundary.page_start <= span.page_number <= boundary.page_end
        )
        result = classify_lesson_text(
            boundary.lesson_number,
            boundary.page_start,
            boundary.page_end,
            lesson_spans,
        )
        roles.update(block.source_role.value for block in result.blocks)
        reply_records += result.reply_record_count
        closed_reply_records += result.closed_reply_record_count
        ambiguous_reply_records += result.ambiguous_reply_record_count
        skipped_running_matter += result.skipped_running_matter_count
    return {
        "ambiguous_reply_record_count": ambiguous_reply_records,
        "closed_reply_record_count": closed_reply_records,
        "lesson_boundaries": [
            {
                "lesson_number": boundary.lesson_number,
                "page_end": boundary.page_end,
                "page_start": boundary.page_start,
                "title": boundary.title,
            }
            for boundary in boundaries
        ],
        "lesson_count": len(boundaries),
        "reply_record_count": reply_records,
        "role_counts": dict(sorted(roles.items())),
        "skipped_running_matter_count": skipped_running_matter,
        "span_count": len(spans),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract and audit the trusted Chanlun PDF text stream")
    parser.add_argument(
        "--pdf",
        type=Path,
        default=Path("C:/Users/lc/Desktop/教你炒股票108课天使版.pdf"),
    )
    parser.add_argument(
        "--target",
        type=Path,
        default=ROOT / "audit" / "chanlun_pdf_extract",
    )
    args = parser.parse_args()
    started = time.monotonic()
    verified = verify_pdf_identity(args.pdf, TRUSTED_IDENTITY)
    target = args.target.resolve()
    if target.exists():
        spans = load_lesson_span_cache(target, expected_identity=verified)
        cache_action = "reused"
    else:
        import pdfplumber

        extracted = []
        with pdfplumber.open(args.pdf) as pdf:
            if len(pdf.pages) != verified.page_count:
                raise RuntimeError("verified PDF page count changed before extraction")
            for page_number in range(7, verified.page_count + 1):
                extracted.extend(
                    extract_page_spans(pdf.pages[page_number - 1], page_number=page_number)
                )
        spans = tuple(extracted)
        build_lesson_span_cache(
            target,
            identity=verified,
            spans=spans,
            extractor_version=EXTRACTOR_VERSION,
            first_page=7,
            last_page=verified.page_count,
        )
        spans = load_lesson_span_cache(target, expected_identity=verified)
        cache_action = "created"
    report = {
        "cache_action": cache_action,
        "cache_path": str(target),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "extractor_version": EXTRACTOR_VERSION,
        "source_pdf": {
            "filename": verified.filename,
            "page_count": verified.page_count,
            "sha256": verified.sha256,
            "size_bytes": verified.size_bytes,
        },
        **_analysis(spans),
    }
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

