import os
import pytest
from cl_app.services import warning_evidence
from tests.web.test_notification_evidence_archive import source


def test_origin_survives_run_cleanup_and_remains_hash_verified(tmp_path):
    from tests.web.test_warning_lifecycle import frozen_warning
    from chanlun.screening.evidence import evidence_paths

    event, _, _ = frozen_warning(tmp_path / "runs")
    paths = evidence_paths(
        tmp_path / "runs" / event["evidence_run_id"], event["code"], event["frequency"]
    )
    warning_evidence.preserve(event, tmp_path / "runs")
    for path in paths:
        path.unlink()
    frame, snapshot, health = warning_evidence.load_preserved(event, tmp_path / "runs")
    assert not frame.empty and snapshot
    objects = list((tmp_path / "warning_evidence/objects").iterdir())
    for path in objects:
        os.utime(path, (1, 1))
    state = {"signals": {"id": event}}
    warning_evidence.cleanup(tmp_path / "runs", state, now=10 * 86400)
    assert all(path.exists() for path in objects)
    warning_evidence.cleanup(tmp_path / "runs", {}, now=10 * 86400)
    assert not any(path.exists() for path in objects)


def test_corrupt_saved_origin_is_never_reused(tmp_path):
    event, paths = source(tmp_path / "runs")
    warning_evidence.preserve(event, tmp_path / "runs")
    for path in paths:
        path.unlink()
    saved = next((tmp_path / "warning_evidence/objects").glob("*.parquet"))
    saved.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="Corrupt"):
        warning_evidence.load_preserved(event, tmp_path / "runs")
