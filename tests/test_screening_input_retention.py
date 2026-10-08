import gzip
import hashlib
import json
import os

from chanlun.screening.cache_retention import prune_inputs, save_reference


def data(root, identity, stamp):
    folder = root / "inputs"
    folder.mkdir(exist_ok=True)
    path = folder / (f"{identity:064x}." + "a" * 16 + ".parquet")
    path.write_bytes(b"x" * 100)
    os.utime(path, (stamp, stamp))
    return path


def cache(root, source):
    path = root / "stock_5m.json.gz"
    payload = json.dumps(
        {"source_input": {"file": source.relative_to(root).as_posix()}}
    )
    path.write_bytes(
        gzip.compress(
            json.dumps(
                {
                    "payload": payload,
                    "sha256": hashlib.sha256(payload.encode()).hexdigest(),
                }
            ).encode()
        )
    )
    return path


def test_retention_preserves_current_recent_and_external_files_while_enforcing_budget(
    tmp_path,
):
    current = data(tmp_path, 1, 10)
    orphan = data(tmp_path, 2, 20)
    recent = data(tmp_path, 3, 995)
    cache(tmp_path, current)
    unknown = tmp_path / "inputs/not-an-input.parquet"
    unknown.write_bytes(b"keep")
    report = prune_inputs(
        tmp_path, now=1000, max_bytes=200, grace_seconds=60, keep_days=10
    )
    assert report["removed"] == 1 and not orphan.exists()
    assert current.exists() and recent.exists() and unknown.exists()
    assert report["protected"] == 1 and report["remaining_bytes"] == 200
    assert cache_metadata(tmp_path).is_file()


def cache_metadata(root):
    return (root / "stock_5m.json.gz").with_suffix(".input-ref.json")


def test_corrupt_cache_defers_cleanup_and_a_changed_cache_invalidates_sidecar(tmp_path):
    old = data(tmp_path, 1, 10)
    new = data(tmp_path, 2, 10)
    path = cache(tmp_path, old)
    save_reference(path, {"file": old.relative_to(tmp_path).as_posix()})
    path.write_bytes(b"incomplete")
    assert prune_inputs(tmp_path, now=1000, max_bytes=0, grace_seconds=0)["error"]
    assert old.exists() and new.exists()
    cache(tmp_path, new)
    result = prune_inputs(tmp_path, now=1000, max_bytes=100, grace_seconds=0)
    assert result["removed"] == 1 and new.exists() and not old.exists()
