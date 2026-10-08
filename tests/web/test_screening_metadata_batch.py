"""Batch evidence metadata probes without changing prefix invalidation rules."""
import hashlib
import json
import os
from pathlib import Path

import pytest

from cl_app.services import screening_progress as progress


def watched(directory, names):
    data = b'{"record":1}\n'
    (directory / "results.jsonl").write_bytes(data)
    return {"digest": hashlib.sha256(data), "size": len(data), "through": 1,
            "files": {str(path): progress._file_state(path) for path in names}}


@pytest.mark.skipif(os.name != "nt", reason="Windows directory enumeration caches file metadata")
def test_prefix_check_does_not_probe_each_existing_or_missing_file(tmp_path, monkeypatch):
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    paths = [evidence / f"proof-{i}.json.gz" for i in range(80)]
    for path in paths[:8]:
        path.write_bytes(b"proof")
    guard = watched(tmp_path, paths)
    original = Path.stat
    probes = []

    def stat(path, *args, **kwargs):
        if path in paths:
            probes.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat)
    assert progress._unchanged(tmp_path, guard)
    assert not probes, "prefix validation still probes every evidence path separately"


@pytest.mark.skipif(os.name != "nt", reason="Windows directory enumeration caches file metadata")
def test_new_chunk_collects_evidence_metadata_in_a_batch(tmp_path, monkeypatch):
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "SH.600000_5m.json.gz").write_bytes(b"snapshot")
    row = {"code": "SH.600000", "rows": [{"code": "SH.600000", "frequency": "5m"}]}
    (tmp_path / "results.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    original = Path.stat
    probes = []

    def stat(path, *args, **kwargs):
        if path.parent == evidence:
            probes.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat)
    guard = progress._guard(tmp_path, None, 1)
    assert len(guard["files"]) == 2 and guard["through"] == 1
    assert not probes, "new chunks still request each file's attributes separately"


@pytest.mark.parametrize("change", ["created", "removed", "modified"])
def test_watched_file_changes_still_invalidate_the_prefix(tmp_path, change):
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    path = evidence / "proof.json.gz"
    if change != "created":
        path.write_bytes(b"old")
    guard = watched(tmp_path, [path])
    if change == "removed":
        path.unlink()
    else:
        path.write_bytes(b"new and different")
    assert not progress._unchanged(tmp_path, guard)


def test_unrelated_new_evidence_does_not_invalidate_a_verified_prefix(tmp_path):
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    path = evidence / "watched.json.gz"
    path.write_bytes(b"proof")
    guard = watched(tmp_path, [path, evidence / "still-missing.parquet"])
    (evidence / "later-uncommitted.json.gz").write_bytes(b"new work")
    assert progress._unchanged(tmp_path, guard)


def test_directory_listing_failure_falls_back_to_accessible_file_attributes(tmp_path, monkeypatch):
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    path = evidence / "proof.json.gz"
    path.write_bytes(b"proof")
    guard = watched(tmp_path, [path])

    def blocked(*args, **kwargs):
        raise PermissionError("directory enumeration unavailable")

    monkeypatch.setattr(os, "scandir", blocked)
    assert progress._unchanged(tmp_path, guard)


def test_missing_evidence_directories_and_multiple_parents_keep_the_same_meaning(tmp_path):
    first, second = tmp_path / "one", tmp_path / "two"
    first.mkdir()
    second.mkdir()
    present = first / "present"
    present.write_bytes(b"proof")
    paths = [present, second / "absent", tmp_path / "missing-dir" / "absent"]
    guard = watched(tmp_path, paths)
    assert progress._unchanged(tmp_path, guard)
    (second / "absent").write_bytes(b"now present")
    assert not progress._unchanged(tmp_path, guard)


@pytest.mark.skipif(os.name != "nt", reason="Windows case-insensitive path aliases")
def test_case_aliases_preserve_each_expected_version(tmp_path):
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    path = evidence / "Proof.JSON"
    path.write_bytes(b"proof")
    alias = evidence / "proof.json"
    guard = watched(tmp_path, [path, alias])
    assert progress._unchanged(tmp_path, guard)
    version = guard["files"][str(alias)]
    guard["files"][str(alias)] = (version[0] + 1, *version[1:])
    assert not progress._unchanged(tmp_path, guard)
