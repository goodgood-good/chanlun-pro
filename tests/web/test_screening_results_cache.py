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
        "source_revision": screening.source_revision(),
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


def test_source_change_rejects_the_saved_result_cache_and_hides_retired_results(cached_run, monkeypatch):
    manager = cached_run
    directory = manager._directory()
    state = json.loads((directory / "status.json").read_text(encoding="utf-8"))
    screening.write_json(directory / "status.json", {**state, "source_revision": "saved-engine"})
    monkeypatch.setattr(screening, "source_revision", lambda: "saved-engine")
    assert manager.results(persist=True)["source_current"] is True

    monkeypatch.setattr(screening, "source_revision", lambda: "changed-engine")
    reopened = screening.ScreeningManager(manager.root)
    read = Mock(wraps=reopened._read_results)
    monkeypatch.setattr(reopened, "_read_results", read)
    result = reopened.results()
    assert result["results_loading"] is False
    assert result["source_current"] is False
    assert result["selected"] == result["observations"] == []
    assert result["errors"] == []
    assert read.call_count == 1


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
    state = {"run_id": directory.name, "status": "running", "completed": 3, "total": 5,
             "worker_pid": os.getpid(), "source_revision": screening.source_revision()}
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


def _running(manager, *, completed=1, status="running"):
    directory = manager._directory()
    state = json.loads((directory / "status.json").read_text(encoding="utf-8"))
    state.update(status=status, completed=completed, total=100, worker_pid=os.getpid())
    screening.write_json(directory / "status.json", state)
    return state


def _append_error(manager, code):
    with (manager._directory() / "results.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"code": code, "rows": [], "error": "fixture failure"}) + "\n")


def test_growing_run_publishes_verified_prefix_without_rechecking_or_clearing_it(cached_run, monkeypatch):
    manager = cached_run
    _running(manager)
    entered, release = Event(), Event()
    original = manager._read_results
    calls = []

    def read(directory, state, **kwargs):
        calls.append(kwargs)
        if kwargs.get("after_completed") == 1:
            entered.set()
            assert release.wait(10)
        return original(directory, state, **kwargs)

    monkeypatch.setattr(manager, "_read_results", read)
    assert manager.results()["results_through"] == 1
    _append_error(manager, "SZ.000001")
    # Appended but not yet committed rows cannot enter the page or invalidate
    # its already verified prefix.
    first = manager.results(background=True)
    assert first["results_through"] == 1 and not first["results_loading"]
    assert len(calls) == 1
    _running(manager, completed=2)
    try:
        pending = manager.results(background=True)
        assert entered.wait(2)
        assert pending["results_loading"] and pending["results_available"]
        assert [row["code"] for row in pending["errors"]] == ["SH.600000"]
        _append_error(manager, "SZ.000002")
        _running(manager, completed=3)
        # More writes while the reader is busy must not starve publication.
        assert manager.results(background=True)["results_through"] == 1
        future = manager._progressive_results.future
        release.set()
        future.result(timeout=5)
        advanced = manager.results(background=True)
        assert advanced["results_through"] == 2
        assert len(advanced["errors"]) == 2
        final = manager.results()
        assert final["results_through"] == 3
        assert len(final["errors"]) == len(final["processed_symbols"]) == 3
        assert [call["after_completed"] for call in calls] == [0, 1, 2]
        assert calls[1]["record_offset"] > 0
        assert calls[2]["record_offset"] > calls[1]["record_offset"]
    finally:
        release.set()


@pytest.mark.parametrize("change", ["records", "evidence_added", "evidence_removed", "evidence_changed", "progress_reduced"])
def test_progressive_prefix_rechecks_mutation_or_regression(cached_run, monkeypatch, change):
    manager = cached_run
    _running(manager)
    proof = manager._directory() / "evidence" / "SH.600000_5m.json.gz"
    proof.parent.mkdir()
    if change in {"evidence_removed", "evidence_changed"}:
        proof.write_bytes(b"old")
    read = Mock(wraps=manager._read_results)
    monkeypatch.setattr(manager, "_read_results", read)
    assert len(manager.results()["errors"]) == 1
    if change == "records":
        path = manager._directory() / "results.jsonl"
        path.write_bytes(path.read_bytes().replace(b"SH.600000", b"SZ.000001"))
    elif change == "progress_reduced":
        _running(manager, completed=0)
    elif change == "evidence_removed":
        proof.unlink()
    else:
        proof.write_bytes(b"new-evidence")
    result = manager.results()
    if change == "progress_reduced":
        assert result["errors"] == [] and result["results_through"] == 0
    else:
        assert read.call_count == 2
        assert result["errors"][0]["code"] == ("SZ.000001" if change == "records" else "SH.600000")


def test_new_active_run_never_reuses_a_previous_prefix_or_inflight_chunk(cached_run, monkeypatch):
    manager = cached_run
    _running(manager)
    original = manager._read_results
    entered, release = Event(), Event()

    def read(directory, state, **kwargs):
        if kwargs.get("after_completed") == 1:
            entered.set()
            assert release.wait(10)
        return original(directory, state, **kwargs)

    monkeypatch.setattr(manager, "_read_results", read)
    manager.results()
    _append_error(manager, "SZ.000001")
    _running(manager, completed=2)
    try:
        manager.results(background=True)
        assert entered.wait(2)
        seed_run(manager, "b" * 32, "SH.600002")
        _running(manager)
        result = manager.results(background=True)
        assert result["run_id"] == "b" * 32 and result["errors"] == []
        assert result["results_through"] == 0
        release.set()
        result = manager.results()
        assert [row["code"] for row in result["errors"]] == ["SH.600002"]
    finally:
        release.set()


@pytest.mark.parametrize("status", ["completed", "cancelled", "interrupted"])
def test_progressive_results_finish_or_cancel_with_verified_rows_intact(cached_run, monkeypatch, status):
    manager = cached_run
    _running(manager)
    read = Mock(wraps=manager._read_results)
    monkeypatch.setattr(manager, "_read_results", read)
    manager.results()
    _running(manager, status=status)
    result = manager.results()
    assert len(result["errors"]) == 1
    assert result["results_partial"] is (status != "completed")
    assert read.call_count == 1
    if status == "completed":
        reopened = screening.ScreeningManager(manager.root)
        monkeypatch.setattr(reopened, "_read_results", Mock(side_effect=AssertionError("cache should be reused")))
        assert reopened.results()["errors"] == result["errors"]


def test_mutation_during_append_validation_cannot_publish_a_mixed_prefix(cached_run, monkeypatch):
    manager = cached_run
    _running(manager)
    original = manager._read_results
    entered, release = Event(), Event()

    def read(directory, state, **kwargs):
        if kwargs.get("after_completed"):
            entered.set()
            assert release.wait(10)
        return original(directory, state, **kwargs)

    monkeypatch.setattr(manager, "_read_results", read)
    manager.results()
    _append_error(manager, "SZ.000001")
    _running(manager, completed=2)
    try:
        manager.results(background=True)
        assert entered.wait(2)
        future = manager._progressive_results.future
        path = manager._directory() / "results.jsonl"
        path.write_bytes(path.read_bytes().replace(b"SH.600000", b"SH.600002"))
        release.set()
        with pytest.raises(ValueError, match="证据发生变化"):
            future.result(timeout=5)
        repaired = manager.results()
        assert [row["code"] for row in repaired["errors"]] == ["SH.600002", "SZ.000001"]
    finally:
        release.set()


def test_progressive_reader_bounds_each_batch_and_handles_split_uncommitted_utf8(cached_run, monkeypatch):
    from cl_app.services.screening_progress import ProgressiveResults
    manager = cached_run
    monkeypatch.setattr(ProgressiveResults, "batch_size", 2)
    for code in ("SZ.000001", "SZ.000002", "SZ.000003"):
        _append_error(manager, code)
    path = manager._directory() / "results.jsonl"
    with path.open("ab") as stream:
        stream.write(b'{"code":"' + "样例".encode("utf-8")[:1])
    _running(manager, completed=4)
    read = Mock(wraps=manager._read_results)
    monkeypatch.setattr(manager, "_read_results", read)
    result = manager.results()
    assert len(result["errors"]) == 4
    assert [call.kwargs["after_completed"] for call in read.call_args_list] == [0, 2]
    assert [call.args[1]["completed"] for call in read.call_args_list] == [2, 4]


def test_background_reader_publishes_batches_and_drains_backlog_without_page_polls(cached_run, monkeypatch):
    from cl_app.services.screening_progress import ProgressiveResults
    manager = cached_run
    monkeypatch.setattr(ProgressiveResults, "batch_size", 2)
    for code in ("SZ.000001", "SZ.000002", "SZ.000003"):
        _append_error(manager, code)
    _running(manager, completed=4)
    original = manager._read_results
    entered, release = Event(), Event()

    def read(directory, state, **kwargs):
        if kwargs.get("after_completed") == 2:
            entered.set()
            assert release.wait(10)
        return original(directory, state, **kwargs)

    monkeypatch.setattr(manager, "_read_results", read)
    try:
        manager.results(background=True)
        # Reaching the second chunk requires no intervening poll.
        assert entered.wait(2)
        partial = manager.results(background=True)
        assert partial["results_through"] == 2 and len(partial["errors"]) == 2
        assert partial["results_loading"] and partial["results_available"]
        future = manager._progressive_results.future
        release.set()
        future.result(timeout=5)
        final = manager.results(background=True)
        assert final["results_through"] == 4 and not final["results_loading"]
        assert len(final["errors"]) == 4
    finally:
        release.set()
