#!/usr/bin/env python3
"""Verify that the protected V3 specification and lesson corpus are unchanged."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INTEGRATION = ROOT / "audit" / "chanlun_live_integration"
EXPECTED = INTEGRATION / "workspace_manifest.json"
OUTPUT = INTEGRATION / "v31_protected_input_verification.json"
SPEC = ROOT / "audit" / "chanlun_live_strategy" / "complete_strategy_v3.md"
CORPUS = ROOT / "audit" / "chanlun_lesson_corpus"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _tree(root: Path) -> tuple[tuple[str, str, int], ...]:
    return tuple(
        (
            path.relative_to(root).as_posix(),
            _sha256_file(path),
            path.stat().st_size,
        )
        for path in sorted(
            (value for value in root.rglob("*") if value.is_file()),
            key=lambda value: value.as_posix(),
        )
    )


def _tree_hash(rows: tuple[tuple[str, str, int], ...]) -> str:
    payload = json.dumps(rows, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def main() -> int:
    expected = json.loads(EXPECTED.read_text(encoding="utf-8"))
    before_spec = expected["protected_spec"]
    before_corpus = expected["protected_corpus"]
    corpus_rows = _tree(CORPUS)
    after_spec = {
        "path": SPEC.relative_to(ROOT).as_posix(),
        "sha256": _sha256_file(SPEC),
        "bytes": SPEC.stat().st_size,
    }
    after_corpus = {
        "path": CORPUS.relative_to(ROOT).as_posix(),
        "file_count": len(corpus_rows),
        "bytes": sum(row[2] for row in corpus_rows),
        "tree_sha256": _tree_hash(corpus_rows),
    }
    spec_unchanged = before_spec == after_spec
    corpus_unchanged = before_corpus == after_corpus
    result = {
        "schema": "chanlun-v31-protected-input-verification/v1",
        "status": (
            "PASS_ZERO_CHANGE"
            if spec_unchanged and corpus_unchanged
            else "FAIL_CHANGED"
        ),
        "specification": {
            "before": before_spec,
            "after": after_spec,
            "unchanged": spec_unchanged,
        },
        "lesson_corpus": {
            "before": before_corpus,
            "after": after_corpus,
            "unchanged": corpus_unchanged,
        },
    }
    temporary = OUTPUT.with_suffix(OUTPUT.suffix + ".tmp")
    temporary.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, OUTPUT)
    print(
        json.dumps(
            {
                "status": result["status"],
                "spec_sha256": after_spec["sha256"],
                "corpus_tree_sha256": after_corpus["tree_sha256"],
                "corpus_files": after_corpus["file_count"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if result["status"] == "PASS_ZERO_CHANGE" else 3


if __name__ == "__main__":
    raise SystemExit(main())
