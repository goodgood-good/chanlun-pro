"""Monitor maintenance preserves uncertain or newly referenced evidence."""

import json
import os
from unittest.mock import Mock

import pytest

from cl_app.services import signal_monitor
from tests.web.test_monitor_control_ownership import monitor as monitor


@pytest.fixture
def runs(monitor):
    service, manual, live, state = monitor
    live.root.mkdir(parents=True)
    paths = []
    for index in range(8):
        path = live.root / f"{index:032x}"
        path.mkdir()
        (path / "status.json").write_text(json.dumps({"run_id": path.name, "status": "completed"}), encoding="utf-8")
        (path / "proof.bin").write_bytes(b"temporary test evidence")
        os.utime(path, (1000 + index, 1000 + index))
        paths.append(path)
    live.status.return_value = {"status": "completed", "run_id": paths[-1].name}
    service.notifications.referenced_runs.return_value = set()
    return service, live, state, paths


@pytest.mark.parametrize("broken", ["missing_file", "invalid_json", "missing_status", "missing_run_id", "unknown_status", "wrong_run_id", "not_object"])
def test_unverifiable_run_is_retained_without_stopping_other_cleanup(runs, broken):
    service, live, state, paths = runs
    path = paths[4]
    status = path / "status.json"
    if broken == "missing_file":
        status.unlink()
    elif broken == "invalid_json":
        status.write_text("{", encoding="utf-8")
    else:
        value = {"run_id": path.name, "status": "completed"}
        if broken == "missing_status": value.pop("status")
        elif broken == "missing_run_id": value.pop("run_id")
        elif broken == "unknown_status": value["status"] = "waiting"
        elif broken == "wrong_run_id": value["run_id"] = "f" * 32
        else: value = []
        status.write_text(json.dumps(value), encoding="utf-8")
    os.utime(path, (1004, 1004))
    service._prune_runs()
    assert path.exists(), "unverifiable metadata authorized evidence deletion"
    assert all(not candidate.exists() for candidate in paths[:4])
    assert all(candidate.exists() for candidate in paths[5:])


@pytest.mark.parametrize("references", [None, {}, 12, [{"bad": "value"}], ["invalid-run-id"]])
def test_unknown_notification_references_abort_cleanup(runs, references):
    service, live, state, paths = runs
    service.notifications.referenced_runs.return_value = references
    service._prune_runs()
    assert all(path.exists() for path in paths)


def test_state_reference_added_during_notification_lookup_is_preserved(runs):
    service, live, state, paths = runs

    def references():
        state["signals"]["new-reference"] = {"evidence": {"status": "complete"}, "evidence_run_id": paths[4].name}
        return set()

    service.notifications.referenced_runs.side_effect = references
    service._prune_runs()
    assert paths[4].exists()


def test_notification_reference_added_during_metadata_read_is_preserved(runs, monkeypatch):
    service, live, state, paths = runs
    original = signal_monitor.read_state_json

    def read(path):
        result = original(path)
        if path == paths[4] / "status.json":
            service.notifications.referenced_runs.return_value = {paths[4].name}
        return result

    monkeypatch.setattr(signal_monitor, "read_state_json", read)
    service._prune_runs()
    assert paths[4].exists(), "cleanup used an obsolete notification reference set"


def test_run_that_becomes_active_before_delete_is_preserved(runs, monkeypatch):
    service, live, state, paths = runs
    original = signal_monitor.read_state_json
    changed = []

    def read(path):
        result = original(path)
        if path == paths[4] / "status.json" and not changed:
            changed.append(True)
            path.write_text(json.dumps({"run_id": paths[4].name, "status": "running"}), encoding="utf-8")
        return result

    monkeypatch.setattr(signal_monitor, "read_state_json", read)
    service._prune_runs()
    assert paths[4].exists(), "a changed active run was deleted from an old terminal read"


def test_unavailable_latest_status_does_not_break_monitor_processing(runs):
    service, live, state, paths = runs
    live.status.side_effect = OSError("controlled unavailable status")
    service._prune_runs()
    assert all(path.exists() for path in paths)


def test_cleanup_does_not_copy_unrelated_state_payloads(runs):
    service, live, state, paths = runs

    class NotNeeded(dict):
        def __deepcopy__(self, memo):
            raise AssertionError("cleanup copied unrelated state")

    state["known"] = NotNeeded()
    state["selection_checks"] = NotNeeded()
    service._prune_runs()
    assert all(not path.exists() for path in paths[:5])
    assert all(path.exists() for path in paths[5:])


def test_bad_old_metadata_does_not_prevent_next_scheduler_step(runs, monkeypatch):
    service, live, state, paths = runs
    (paths[4] / "status.json").write_text("{", encoding="utf-8")
    os.utime(paths[4], (1004, 1004))
    monkeypatch.setattr(service, "_sync_watchlist", lambda: None)
    monkeypatch.setattr(service, "_consume_monitor", service._prune_runs)
    monkeypatch.setattr(service, "_schedule_daily", lambda *args: None)
    scheduled = Mock()
    monkeypatch.setattr(service, "_schedule_monitor", scheduled)
    monkeypatch.setattr(signal_monitor, "closed_bar_cutoff", lambda *args: 300)
    service.tick()
    scheduled.assert_called_once()
    assert paths[4].exists()


@pytest.mark.parametrize("collection", ["signals", "holding_evidence"])
def test_evidence_without_a_run_identity_prevents_deletion(runs, collection):
    service, live, state, paths = runs
    state[collection]["unresolved"] = {"evidence": {"status": "complete"}}
    service._prune_runs()
    assert all(path.exists() for path in paths), "missing evidence ownership was treated as no reference"
