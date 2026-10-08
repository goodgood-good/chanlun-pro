import gzip
import hashlib
import json

import pytest

from chanlun.screening.cache import read_calculation, write_calculation
from chanlun.screening.evidence import (
    evidence_health,
    evidence_paths,
    evidence_confirmation_catalog,
)
from chanlun.screening.runner import _save_evidence
from tests.test_screening_cache_inputs import frame


@pytest.mark.parametrize("level", [1, 3, 9])
def test_new_evidence_and_legacy_gzip_levels_keep_the_same_checked_snapshot(
    tmp_path, level
):
    snapshot = {
        "symbol": "SH.600000",
        "source_frequency": "5m",
        "levels": [],
        "label": "录制证据",
    }
    manifest = _save_evidence(tmp_path, "SH.600000", "5m", frame(), snapshot)
    _parquet, packed = evidence_paths(tmp_path, "SH.600000", "5m")
    raw = gzip.decompress(packed.read_bytes())
    assert json.loads(raw) == snapshot
    encoded = gzip.compress(raw, compresslevel=level)
    packed.write_bytes(encoded)
    manifest.update(
        snapshot_sha256=hashlib.sha256(encoded).hexdigest(), snapshot_bytes=len(encoded)
    )
    assert evidence_health(tmp_path, "SH.600000", "5m", manifest) == {
        "complete": True,
        "verified": True,
    }
    assert evidence_confirmation_catalog(tmp_path, "SH.600000", "5m", manifest) == {}
    packed.write_bytes(encoded[:-4])
    assert not evidence_health(tmp_path, "SH.600000", "5m", manifest)["verified"]


@pytest.mark.parametrize("level", [1, 3, 9])
def test_calculation_cache_reads_legacy_and_new_compression_without_weakening_checks(
    tmp_path, level
):
    path = tmp_path / "calculation.json.gz"
    snapshot = {"symbol": "SH.600000", "source_frequency": "5m", "levels": []}
    write_calculation(
        path, "key", {"reason_counts": {}}, snapshot, {}, source_frame=frame()
    )
    original = read_calculation(path, "key")
    raw = gzip.decompress(path.read_bytes())
    path.write_bytes(gzip.compress(raw, compresslevel=level))
    assert read_calculation(path, "key") == original
    assert read_calculation(path, "different-key") is None
    damaged = json.loads(raw)
    damaged["sha256"] = "0" * 64
    path.write_bytes(gzip.compress(json.dumps(damaged).encode(), compresslevel=level))
    assert read_calculation(path, "key") is None
