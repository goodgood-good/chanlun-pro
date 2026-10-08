import json
import os
import sqlite3
import threading
import time

from cl_app.services.screening_retention import prune_runs


def run(root, index, days, now, status="completed"):
    folder = root / f"{index:032x}"
    folder.mkdir(parents=True)
    stamp = now - days * 86400
    (folder / "status.json").write_text(
        json.dumps(
            {
                "run_id": folder.name,
                "status": status,
                "started_at": stamp,
                "finished_at": stamp,
            }
        ),
        encoding="utf-8",
    )
    (folder / "evidence").mkdir()
    (folder / "evidence/data.bin").write_bytes(b"evidence")
    os.utime(folder, (stamp, stamp))
    return folder


def test_retention_preserves_latest_seed_reviews_notices_active_and_recent_runs(
    tmp_path,
):
    now = time.time()
    folders = {i: run(tmp_path, i, 20 + i, now) for i in range(1, 10)}
    active = run(tmp_path, 10, 30, now, "running")
    recent = run(tmp_path, 11, 1, now)
    (tmp_path / "latest.json").write_text(
        json.dumps({"run_id": folders[9].name}), encoding="utf-8"
    )
    with sqlite3.connect(tmp_path / "manual_reviews.sqlite3") as db:
        db.execute("CREATE TABLE reviews(source TEXT)")
        db.execute("INSERT INTO reviews VALUES (?)", (folders[8].name,))
    result = prune_runs(
        tmp_path,
        lambda: {folders[7].name, folders[6].name},
        now=now,
        keep_count=2,
        keep_days=7,
    )
    assert result["removed"] == 4 and result["freed_bytes"] > 0 and not result["error"]
    assert active.exists() and recent.exists()
    assert all(folders[i].exists() for i in (1, 6, 7, 8, 9))
    assert all(not folders[i].exists() for i in (2, 3, 4, 5))


def test_unreadable_references_and_cancellation_cannot_delete_runs(tmp_path):
    now = time.time()
    old = run(tmp_path, 1, 30, now)
    latest = run(tmp_path, 2, 1, now)
    (tmp_path / "latest.json").write_text(
        json.dumps({"run_id": latest.name}), encoding="utf-8"
    )
    result = prune_runs(tmp_path, lambda: None, now=now, keep_count=1)
    assert result["error"] and old.exists()
    stop = threading.Event()
    stop.set()
    assert prune_runs(tmp_path, (), now=now, keep_count=1, stop=stop)["removed"] == 0
    assert old.exists()
    (tmp_path / "manual_reviews.sqlite3").write_bytes(b"not a sqlite database")
    assert prune_runs(tmp_path, (), now=now, keep_count=1)["error"] and old.exists()


def test_references_are_rechecked_before_the_deletion_boundary(tmp_path):
    now = time.time()
    old = run(tmp_path, 1, 30, now)
    latest = run(tmp_path, 2, 1, now)
    (tmp_path / "latest.json").write_text(
        json.dumps({"run_id": latest.name}), encoding="utf-8"
    )
    reads = []

    def protected():
        reads.append(1)
        return set() if len(reads) == 1 else {old.name}

    result = prune_runs(tmp_path, protected, now=now, keep_count=1)
    assert result["removed"] == 0 and old.exists() and len(reads) >= 2


def test_active_result_reader_is_a_retention_reference_even_after_latest_changes(
    tmp_path,
):
    from types import SimpleNamespace
    from cl_app.services.screening import ScreeningManager

    now = time.time()
    old = run(tmp_path, 1, 30, now)
    latest = run(tmp_path, 2, 1, now)
    (tmp_path / "latest.json").write_text(
        json.dumps({"run_id": latest.name}), encoding="utf-8"
    )
    manager = ScreeningManager(tmp_path)
    manager._results_key = (str(old),)
    manager._progressive_results = SimpleNamespace(
        context=(str(old),), requested_context=(str(latest),)
    )
    assert manager.referenced_runs() == {old.name, latest.name}
    assert (
        prune_runs(tmp_path, manager.referenced_runs, now=now, keep_count=1)["removed"]
        == 0
    )


def test_symlink_outside_the_run_root_is_never_followed_for_deletion(tmp_path):
    import subprocess
    import pytest

    root = tmp_path / "runs"
    root.mkdir()
    external = tmp_path / "user-data"
    external.mkdir()
    (external / "keep").write_text("keep", encoding="utf-8")
    now = time.time()
    latest = run(root, 2, 1, now)
    (root / "latest.json").write_text(
        json.dumps({"run_id": latest.name}), encoding="utf-8"
    )
    try:
        (root / ("a" * 32)).symlink_to(external, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            pytest.skip("symlinks are not permitted on this test machine")
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "New-Item -ItemType Junction -Path $env:CHANLUN_TEST_LINK -Target $env:CHANLUN_TEST_TARGET | Out-Null",
            ],
            env={
                **os.environ,
                "CHANLUN_TEST_LINK": str(root / ("a" * 32)),
                "CHANLUN_TEST_TARGET": str(external),
            },
            capture_output=True,
            timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        assert result.returncode == 0, result.stderr.decode(errors="replace")
        assert (root / ("a" * 32)).resolve() == external.resolve()
    assert prune_runs(root, (), now=now, keep_count=1)["removed"] == 0
    assert (external / "keep").read_text(encoding="utf-8") == "keep"


def test_stopped_worker_is_persisted_and_expires_after_the_reconciliation_grace(
    tmp_path, monkeypatch
):
    from cl_app.services import screening, screening_lifecycle

    now = time.time()
    stopped = run(tmp_path, 1, 20, now, "running")
    latest = run(tmp_path, 2, 1, now)
    metadata = json.loads((stopped / "status.json").read_text())
    metadata["worker_pid"] = 123456
    (stopped / "status.json").write_text(json.dumps(metadata), encoding="utf-8")
    (tmp_path / "latest.json").write_text(
        json.dumps({"run_id": latest.name}), encoding="utf-8"
    )
    monkeypatch.setattr(screening, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(screening_lifecycle, "pid_alive", lambda pid: False)
    manager = screening.ScreeningManager(tmp_path)
    assert manager._status(stopped)["status"] == "interrupted"
    saved = json.loads((stopped / "status.json").read_text(encoding="utf-8"))
    assert saved["status"] == "interrupted" and saved["interruption_detected_at"] >= now
    assert prune_runs(tmp_path, (), now=now + 1, keep_count=1)["removed"] == 0
    assert prune_runs(tmp_path, (), now=now + 8 * 86400, keep_count=1)["removed"] == 1
    assert not stopped.exists() and latest.exists()


def test_retention_reconciles_unvisited_crash_but_preserves_live_or_unknown_workers(
    tmp_path, monkeypatch
):
    from cl_app.services import screening_lifecycle

    now = time.time()
    folders = {i: run(tmp_path, i, 20, now, "running") for i in (1, 2, 3)}
    for index, folder in folders.items():
        state = json.loads((folder / "status.json").read_text())
        state["worker_pid"] = index
        (folder / "status.json").write_text(json.dumps(state), encoding="utf-8")
    latest = run(tmp_path, 4, 1, now)
    (tmp_path / "latest.json").write_text(
        json.dumps({"run_id": latest.name}), encoding="utf-8"
    )
    monkeypatch.setattr(
        screening_lifecycle, "pid_alive", lambda pid: {1: False, 2: True, 3: None}[pid]
    )
    result = prune_runs(tmp_path, (), now=now, keep_count=1)
    assert result["reconciled"] == 1 and not result["error"]
    assert (
        json.loads((folders[1] / "status.json").read_text(encoding="utf-8"))["status"] == "interrupted"
    )
    for index in (2, 3):
        assert (
            json.loads((folders[index] / "status.json").read_text(encoding="utf-8"))["status"]
            == "running"
        )
    later = prune_runs(tmp_path, (), now=now + 8 * 86400, keep_count=1)
    assert later["removed"] == 1 and all(folders[i].exists() for i in (2, 3))


def test_concurrent_worker_completion_wins_over_a_stale_interruption_read(tmp_path):
    from cl_app.services.screening_lifecycle import reconcile_interrupted

    now = time.time()
    folder = run(tmp_path, 1, 20, now, "running")
    original = json.loads((folder / "status.json").read_text())
    original["worker_pid"] = 1234
    (folder / "status.json").write_text(json.dumps(original), encoding="utf-8")
    completed = {**original, "status": "completed", "finished_at": now}

    def check(_pid):
        (folder / "status.json").write_text(json.dumps(completed), encoding="utf-8")
        return False

    assert reconcile_interrupted(folder, original, now=now, alive=check) == completed
    assert json.loads((folder / "status.json").read_text()) == completed


def test_process_probe_distinguishes_current_missing_and_unknown_processes():
    from cl_app.services.screening_lifecycle import pid_alive

    assert pid_alive(os.getpid()) is True
    assert pid_alive(2147483647) is False
    assert pid_alive(None) is None
