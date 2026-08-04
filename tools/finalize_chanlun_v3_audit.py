#!/usr/bin/env python3
"""Fingerprint the v3 integration worktree and protected audit inputs."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "audit" / "chanlun_live_integration" / "workspace_manifest.json"
SPEC = ROOT / "audit" / "chanlun_live_strategy" / "complete_strategy_v3.md"
CORPUS = ROOT / "audit" / "chanlun_lesson_corpus"
INTEGRATION = ROOT / "audit" / "chanlun_live_integration"


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
    encoded = json.dumps(rows, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _git(*args: str) -> str:
    result = subprocess.run(
        ("git", *args),
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


def _changed_code_paths() -> tuple[Path, ...]:
    tracked = tuple(
        value
        for value in _git("diff", "--name-only", "HEAD").splitlines()
        if value
    )
    untracked = tuple(
        value
        for value in _git("ls-files", "--others", "--exclude-standard").splitlines()
        if value
    )
    relative = set(tracked)
    relative.update(
        value
        for value in untracked
        if value.startswith(("src/", "tests/", "tools/"))
    )
    return tuple(
        sorted(
            (ROOT / value for value in relative if (ROOT / value).is_file()),
            key=lambda value: value.as_posix(),
        )
    )


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    encoded = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    with temporary.open("wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def main() -> int:
    if not SPEC.is_file() or not CORPUS.is_dir():
        raise RuntimeError("protected strategy specification or corpus is missing")
    changed = _changed_code_paths()
    code_rows = tuple(
        (
            path.relative_to(ROOT).as_posix(),
            _sha256_file(path),
            path.stat().st_size,
        )
        for path in changed
    )
    corpus_rows = _tree(CORPUS)
    artifacts = tuple(
        (
            path.relative_to(ROOT).as_posix(),
            _sha256_file(path),
            path.stat().st_size,
        )
        for path in sorted(
            (
                value
                for value in INTEGRATION.glob("*")
                if value.is_file() and value.resolve() != OUTPUT.resolve()
            ),
            key=lambda value: value.as_posix(),
        )
    )
    baseline = json.loads(
        (INTEGRATION / "frozen_structure_baseline.json").read_text(encoding="utf-8")
    )
    core_contract = baseline["core_contract"]
    payload = {
        "schema": "chanlun-v3-workspace-manifest/v1",
        "git_head": _git("rev-parse", "HEAD"),
        "git_head_commit": _git("show", "-s", "--format=%H %cI %s", "HEAD"),
        "changed_code_and_test_files": code_rows,
        "changed_code_and_test_tree_sha256": _tree_hash(code_rows),
        "protected_spec": {
            "path": SPEC.relative_to(ROOT).as_posix(),
            "sha256": _sha256_file(SPEC),
            "bytes": SPEC.stat().st_size,
        },
        "protected_corpus": {
            "path": CORPUS.relative_to(ROOT).as_posix(),
            "file_count": len(corpus_rows),
            "bytes": sum(row[2] for row in corpus_rows),
            "tree_sha256": _tree_hash(corpus_rows),
        },
        "frozen_core": {
            "scope": core_contract["frozen_scope"],
            "file_count": len(core_contract["files"]),
            "core_contract_sha256": core_contract["core_contract_sha256"],
            "representative_output_sha256": tuple(
                row["output_sha256"]
                for row in core_contract["representative_outputs"]
            ),
        },
        "audit_artifacts": artifacts,
        "audit_artifact_tree_sha256": _tree_hash(artifacts),
        "excluded_preexisting_unrelated_paths": (
            ".playwright-cli/",
            "2026072510712998.dmp",
            "20260725110105013.dmp",
        ),
    }
    _atomic_json(OUTPUT, payload)
    print(
        json.dumps(
            {
                "output": str(OUTPUT),
                "git_head": payload["git_head"],
                "changed_file_count": len(code_rows),
                "workspace_sha256": payload["changed_code_and_test_tree_sha256"],
                "spec_sha256": payload["protected_spec"]["sha256"],
                "corpus_tree_sha256": payload["protected_corpus"]["tree_sha256"],
                "core_contract_sha256": payload["frozen_core"]["core_contract_sha256"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
