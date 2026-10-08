from pathlib import Path
import hashlib
import json
import os

from chanlun.screening.evidence import evidence_version


def previous(directory):
    paths = [
        Path(directory) / "results.jsonl",
        *sorted((Path(directory) / "evidence").glob("*")),
    ]
    states = []
    for path in paths:
        try:
            stat = path.stat()
            states.append((path.name, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
        except FileNotFoundError:
            states.append((path.name, None))
    return hashlib.sha256(json.dumps(states).encode()).hexdigest()


def test_version_is_identical_and_detects_all_existing_file_mutations(tmp_path):
    assert evidence_version(tmp_path) == previous(tmp_path)
    root = tmp_path / "evidence"
    root.mkdir()
    for name in ["z.json.gz", "A.parquet", "b.parquet", "hidden.tmp"]:
        (root / name).write_bytes(b"initial evidence")
    (tmp_path / "results.jsonl").write_bytes(b"one\n")
    changes = [
        lambda: (root / "A.parquet").write_bytes(b"changed evidence"),
        lambda: (root / "A.parquet").unlink(),
        lambda: (root / "B_new.parquet").write_bytes(b"new"),
        lambda: (root / "b.parquet").rename(root / "renamed.parquet"),
        lambda: (tmp_path / "results.jsonl").write_bytes(b"one\ntwo\n"),
    ]
    token = evidence_version(tmp_path)
    assert token == previous(tmp_path)
    for change in changes:
        change()
        updated = evidence_version(tmp_path)
        assert updated != token and updated == previous(tmp_path)
        token = updated


def test_same_size_edit_with_new_mtime_still_invalidates_the_token(tmp_path):
    root = tmp_path / "evidence"
    root.mkdir()
    path = root / "saved.json.gz"
    path.write_bytes(b"old")
    stat = path.stat()
    before = evidence_version(tmp_path)
    path.write_bytes(b"new")
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    assert evidence_version(tmp_path) != before
    assert evidence_version(tmp_path) == previous(tmp_path)
