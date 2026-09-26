"""Slow evidence reads are shared, versioned, and kept off page requests."""

import json
import os
from threading import Event
from unittest.mock import Mock

import pytest

from cl_app import create_app
from cl_app.services import screening, screening_workbench as workbench


def seed_run(manager, run_id="a" * 32, code="SH.600000"):
    directory = manager.root / run_id
    screening.write_json(manager.root / "latest.json", {"run_id": run_id})
    screening.write_json(directory / "status.json", {
        "run_id": run_id, "status": "completed", "completed": 1, "total": 1,
        "error_count": 1,
    })
    item = {"code": code, "rows": [{
        "code": code, "frequency": "5m", "selected": [], "recent_rejections": [],
        "reason_counts": {}, "data_errors": ["STALE_DATA"],
    }]}
    (directory / "results.jsonl").write_text(json.dumps(item) + "\n", encoding="utf-8")
    return directory


@pytest.fixture
def cached_run(tmp_path, monkeypatch):
    manager = screening.ScreeningManager(tmp_path / "screening")
    seed_run(manager)
    monkeypatch.setattr(workbench, "manager", manager)
    monkeypatch.setattr(workbench, "_archive_root", lambda: tmp_path / "archive")
    return manager


def test_repeated_reads_reuse_evidence_but_refresh_status_and_isolate_callers(cached_run, monkeypatch):
    manager = cached_run
    read = Mock(wraps=manager._read_results)
    monkeypatch.setattr(manager, "_read_results", read)
    first = manager.results()
    first["errors"][0]["code"] = "caller mutation"
    (manager.root / first["run_id"] / "cancel").touch()
    monkeypatch.setattr(manager, "_freshness", lambda _: {"freshness_state": "stale"})
    second = manager.results()
    assert second["errors"][0]["code"] == "SH.600000"
    assert second["cancel_requested"] is True
    assert second["freshness_state"] == "stale"
    assert read.call_count == 1


def test_results_progress_and_evidence_versions_each_invalidate_cache(cached_run, monkeypatch):
    manager = cached_run
    read = Mock(wraps=manager._read_results)
    monkeypatch.setattr(manager, "_read_results", read)
    manager.results()
    directory = manager._directory()
    path = directory / "results.jsonl"
    previous = path.stat()
    path.write_bytes(path.read_bytes().replace(b"SH.600000", b"SZ.000001"))
    os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns + 1_000_000))
    assert manager.results()["errors"][0]["code"] == "SZ.000001"
    assert read.call_count == 2

    state = json.loads((directory / "status.json").read_text())
    screening.write_json(directory / "status.json", {**state, "completed": 0})
    assert manager.results()["errors"] == []
    assert read.call_count == 3

    proof = directory / "evidence" / "snapshot.json.gz"
    proof.parent.mkdir()
    proof.write_bytes(b"version-one")
    manager.results()
    assert read.call_count == 4
    previous = proof.stat()
    proof.write_bytes(b"version-two")
    os.utime(proof, ns=(previous.st_atime_ns, previous.st_mtime_ns + 1_000_000))
    manager.results()
    assert read.call_count == 5
    proof.unlink()
    manager.results()
    assert read.call_count == 6


def test_completed_results_open_from_disk_after_restart_and_recheck_changes(cached_run, monkeypatch):
    manager = cached_run
    assert manager.results(persist=True)["errors"][0]["code"] == "SH.600000"
    directory = manager._directory()
    cache = directory / screening._RESULT_CACHE_FILE
    assert cache.is_file()

    reopened = screening.ScreeningManager(manager.root)
    read = Mock(side_effect=AssertionError("verified results should come from disk"))
    monkeypatch.setattr(reopened, "_read_results", read)
    result = reopened.results(background=True)
    assert result["results_loading"] is False
    assert result["errors"][0]["code"] == "SH.600000"
    assert read.call_count == 0

    path = directory / "results.jsonl"
    previous = path.stat()
    path.write_bytes(path.read_bytes().replace(b"SH.600000", b"SZ.000001"))
    os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns + 1_000_000))
    changed = screening.ScreeningManager(manager.root)
    assert changed.results()["errors"][0]["code"] == "SZ.000001"

    cache.write_bytes(b"broken")
    recovered = screening.ScreeningManager(manager.root)
    assert recovered.results()["errors"][0]["code"] == "SZ.000001"


def test_unrelated_source_change_keeps_saved_results_visible_with_stale_warning(cached_run, monkeypatch):
    manager = cached_run
    directory = manager._directory()
    state = json.loads((directory / "status.json").read_text(encoding="utf-8"))
    screening.write_json(directory / "status.json", {**state, "source_revision": "saved-engine"})
    monkeypatch.setattr(screening, "source_revision", lambda: "saved-engine")
    assert manager.results(persist=True)["source_current"] is True

    monkeypatch.setattr(screening, "source_revision", lambda: "changed-engine")
    reopened = screening.ScreeningManager(manager.root)
    read = Mock(side_effect=AssertionError("unrelated engine changes must not re-read frozen evidence"))
    monkeypatch.setattr(reopened, "_read_results", read)
    result = reopened.results(background=True)
    assert result["results_loading"] is False
    assert result["source_current"] is False
    assert result["errors"][0]["code"] == "SH.600000"
    assert read.call_count == 0


def test_completed_run_warms_disk_cache_without_a_page_request(tmp_path, monkeypatch):
    manager = screening.ScreeningManager(tmp_path / "screening")
    release, warmed = Event(), Event()

    class Worker:
        pid = 1234

        def wait(self):
            assert release.wait(10)
            return 0

    monkeypatch.setattr(screening.subprocess, "Popen", lambda *args, **kwargs: Worker())
    save = manager._save_results_cache

    def track_cache(*args):
        save(*args)
        warmed.set()

    monkeypatch.setattr(manager, "_save_results_cache", track_cache)
    launched = manager.start({"scope": "all_a"})
    directory = manager.root / launched["run_id"]
    state = json.loads((directory / "status.json").read_text(encoding="utf-8"))
    screening.write_json(directory / "status.json", {**state, "status": "completed"})
    release.set()
    assert warmed.wait(10)
    assert (directory / screening._RESULT_CACHE_FILE).is_file()


def test_slow_workbench_returns_pending_and_shares_one_reader(cached_run, monkeypatch):
    manager = cached_run
    entered, release = Event(), Event()
    original = manager._read_results

    def slow_read(*args):
        entered.set()
        assert release.wait(10)
        return original(*args)

    read = Mock(side_effect=slow_read)
    monkeypatch.setattr(manager, "_read_results", read)
    app = create_app(test_config={"TESTING": True, "LOGIN_DISABLED": True, "VALIDATE_WEB_SECURITY": False})
    try:
        client = app.test_client()
        response = client.get("/screening/workbench")
        assert entered.wait(2)
        assert response.status_code == 202
        assert response.headers["Retry-After"] == "5"
        assert "no-store" in response.headers["Cache-Control"]
        pending = response.get_json()
        assert pending["status"]["results_loading"] is True
        assert pending["status"]["completed"] == 1
        assert pending["status"]["error_count"] == 1
        assert pending["candidates"] == []
        for _ in range(3):
            assert client.get("/screening/workbench").status_code == 202
        assert read.call_count == 1
        release.set()
        manager.results()
        response = client.get("/screening/workbench")
        assert response.status_code == 200
        assert response.get_json()["status"]["results_loading"] is False
        assert response.get_json()["diagnostics"]["errors"][0]["code"] == "SH.600000"
        assert read.call_count == 1
    finally:
        release.set()
        manager.results()
        app.extensions["shutdown_runtime_services"]()


def test_new_run_never_receives_old_inflight_results(cached_run, monkeypatch):
    manager = cached_run
    entered, release = Event(), Event()
    original = manager._read_results

    def slow_read(*args):
        entered.set()
        assert release.wait(10)
        return original(*args)

    read = Mock(side_effect=slow_read)
    monkeypatch.setattr(manager, "_read_results", read)
    try:
        assert manager.results(background=True)["results_loading"]
        assert entered.wait(2)
        seed_run(manager, "b" * 32, "SZ.000001")
        pending = manager.results(background=True)
        assert pending["run_id"] == "b" * 32
        assert pending["results_loading"] is True
        assert pending["errors"] == []
        assert read.call_count == 1
    finally:
        release.set()
    latest = manager.results()
    assert latest["run_id"] == "b" * 32
    assert latest["errors"][0]["code"] == "SZ.000001"
    assert read.call_count == 2


def test_failed_read_can_retry(cached_run, monkeypatch):
    manager = cached_run
    original = manager._read_results

    def fail_once(*args):
        if read.call_count == 1:
            raise ValueError("temporary read failure")
        return original(*args)

    read = Mock(side_effect=fail_once)
    monkeypatch.setattr(manager, "_read_results", read)
    with pytest.raises(ValueError, match="temporary read failure"):
        manager.results()
    assert manager.results()["errors"][0]["code"] == "SH.600000"
    assert read.call_count == 2


def test_monitor_reads_committed_slices_without_reprocessing_previous_symbols(cached_run):
    manager = cached_run
    directory = manager._directory()
    rows = [{"market": "a", "code": f"SH.{600000 + i}", "rows": [{
        "code": f"SH.{600000 + i}", "frequency": "5m", "selected": [], "observations": [],
        "recent_rejections": [], "reason_counts": {}, "data_errors": ["STALE_DATA"],
    }]} for i in range(4)]
    (directory / "results.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    state = {"run_id": directory.name, "status": "running", "completed": 3, "total": 5, "worker_pid": os.getpid()}
    screening.write_json(directory / "status.json", state)
    first = manager.committed_results(limit=2)
    assert first["results_through"] == 2
    assert [s["code"] for s in first["processed_symbols"]] == ["SH.600000", "SH.600001"]
    next_page = manager.committed_results(after_completed=2)
    assert next_page["results_through"] == 3
    assert [e["code"] for e in next_page["errors"]] == ["SH.600002"]
    assert next_page["status"] == "running"
    assert len(manager.results()["errors"]) == 3  # Full workbench reads remain full.
    screening.write_json(directory / "status.json", {**state, "completed": 4, "status": "completed"})
    last = manager.committed_results(after_completed=3)
    assert last["results_through"] == last["completed"] == 4
    assert [s["code"] for s in last["processed_symbols"]] == ["SH.600003"]
