from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from chanlun.decision_support.certified_lesson_package import certify_lesson_package


def main() -> int:
    parser = argparse.ArgumentParser(description="Build and certify the canonical Chanlun lesson evidence package")
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
        "--image-cache",
        type=Path,
        default=ROOT / "audit" / "chanlun_pdf_inventory",
    )
    parser.add_argument(
        "--target",
        type=Path,
        default=ROOT / "audit" / "chanlun_lesson_corpus",
    )
    args = parser.parse_args()
    started = time.monotonic()
    target = certify_lesson_package(
        args.target,
        pdf_path=args.pdf,
        text_cache_root=args.text_cache,
        image_cache_root=args.image_cache,
    )
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    print(
        json.dumps(
            {
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "image_inventory": manifest["inventory"],
                "lesson_count": manifest["coverage"]["lesson_count"],
                "package_path": str(target),
                "quarantine_counts": manifest["quarantine_counts"],
                "role_counts": manifest["role_counts"],
                "source_pdf_sha256": manifest["source_pdf"]["sha256"],
                "source_record_count": manifest["source_record_count"],
                "status": manifest["status"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

