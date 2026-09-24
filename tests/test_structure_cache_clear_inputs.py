"""A later authorized cache clear must not break frozen input references."""

import json
from pathlib import Path
import subprocess
import sys
import pytest


@pytest.mark.skipif(sys.platform != "win32", reason="Windows maintenance script")
def test_structure_cache_clear_preserves_immutable_input_snapshots(tmp_path):
    root = tmp_path / "data"
    audit = tmp_path / "audit"
    audit.mkdir()
    (audit / "screening_preflight.json").write_text("{}", encoding="utf-8")
    records = []
    inputs = []
    for folder in ["screening/analysis_cache", "signal_monitor/runs/analysis_cache"]:
        cache = root / folder
        (cache / "inputs").mkdir(parents=True)
        record = cache / "sample_5m.json.gz"
        record.write_bytes(b"calculation")
        original = cache / "inputs/exact.parquet"
        original.write_bytes(b"original immutable input")
        records.append(record)
        inputs.append(original)
    script = Path(__file__).resolve().parents[1] / "script/clear_screening_caches.ps1"
    subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-File",
            str(script),
            "-DataRoot",
            str(root),
            "-AuditRoot",
            str(audit),
            "-StructureOnly",
            "-Apply",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    manifest = json.loads((audit / "cache_clear.json").read_text(encoding="utf-8"))
    assert manifest["applied"] is True
    assert all(not p.exists() for p in records)
    assert all(p.read_bytes() == b"original immutable input" for p in inputs)
    for record in records:
        assert (
            Path(manifest["backup_root"]) / record.relative_to(root)
        ).read_bytes() == b"calculation"
