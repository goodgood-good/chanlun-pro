import json
import time
import pytest
from cl_app.services import screening


def test_scheduler_reads_current_lifecycle_without_enumerating_large_geometry(
    tmp_path, monkeypatch
):
    manager = screening.ScreeningManager(tmp_path, warm_results=False)
    directory = tmp_path / ("a" * 32)
    directory.mkdir()
    value = {
        "run_id": directory.name,
        "status": "completed",
        "completed": 1,
        "source_revision": "rev",
        "settings": {"frequencies": ["5m"]},
    }

    def save():
        (directory / "status.json").write_text(json.dumps(value), encoding="utf-8")

    save()
    monkeypatch.setattr(manager, "_directory", lambda: directory)
    manager._revision = "rev"
    manager._revision_checked = time.monotonic()

    def forbidden(*args, **kwargs):
        pytest.fail("only the full UI status should enumerate frozen geometry")

    monkeypatch.setattr(screening, "evidence_version", forbidden)
    monkeypatch.setattr(manager, "_freshness", forbidden)
    current = manager.scheduler_status()
    assert current["source_current"] and current["completed"] == 1
    assert "evidence_version" not in current
    value["completed"] = 2
    save()
    assert manager.scheduler_status()["completed"] == 2
    manager._revision_checked = None
    monkeypatch.setattr(screening, "source_revision", lambda: "new")
    assert manager.scheduler_status()["source_current"] is False
    with pytest.raises(pytest.fail.Exception):
        manager.status()
