"""Ancillary work must not turn an accepted worker launch into a false failure."""
from datetime import datetime
import json
from unittest.mock import Mock

import pytest

from chanlun.screening.rules import CN
from cl_app.services import screening
from cl_app.services.signal_monitor import SignalMonitor
from tests.web.test_monitor_control_ownership import monitor as monitor
from tests.web.test_screening_results_cache import seed_run
from tests.web.test_signal_monitor import Jobs


BODY = {"scope": "codes", "codes": ["SH.600000"]}


@pytest.fixture
def manager(tmp_path, monkeypatch):
    value = screening.ScreeningManager(tmp_path / "manual")
    process = Mock(pid=1234)
    monkeypatch.setattr(screening.subprocess, "Popen", Mock(return_value=process))
    return value


@pytest.mark.parametrize("phase", ["construct", "start"])
@pytest.mark.parametrize("error_type", [RuntimeError, OSError])
def test_optional_warmup_failure_keeps_the_launched_run(manager, monkeypatch, phase, error_type):
    thread = Mock()
    if phase == "construct":
        factory = Mock(side_effect=error_type("controlled warmup failure"))
    else:
        thread.start.side_effect = error_type("controlled warmup failure")
        factory = Mock(return_value=thread)
    monkeypatch.setattr(screening.threading, "Thread", factory)
    launched = manager.start(BODY)
    assert launched["status"] == "starting"
    assert manager.status()["run_id"] == launched["run_id"]
    assert screening.subprocess.Popen.call_count == 1
    with pytest.raises(RuntimeError, match="已有选股任务"):
        manager.start(BODY)
    assert screening.subprocess.Popen.call_count == 1


@pytest.mark.parametrize("error_type", [OSError, ValueError, KeyError])
def test_post_launch_status_failure_returns_accepted_identity_without_rewriting_worker_status(manager, monkeypatch, error_type):
    manager._warm_results = False
    original = manager._status
    def status(directory):
        if directory is not None:
            raise error_type("controlled status read failure")
        return original(directory)
    monkeypatch.setattr(manager, "_status", status)
    launched = manager.start(BODY)
    assert launched["status"] == "starting" and launched["status_unavailable"] is True
    directory = manager.root / launched["run_id"]
    saved = json.loads((directory / "status.json").read_text(encoding="utf-8"))
    assert saved["run_id"] == launched["run_id"] and saved["status"] == "starting"
    assert "status_unavailable" not in saved and "error" not in saved
    monkeypatch.setattr(manager, "_status", original)
    assert manager.status()["run_id"] == launched["run_id"]
    with pytest.raises(RuntimeError, match="已有选股任务"):
        manager.start(BODY)
    assert screening.subprocess.Popen.call_count == 1


def test_prelaunch_status_failure_still_prevents_a_second_worker(manager, monkeypatch):
    monkeypatch.setattr(manager, "status", Mock(side_effect=OSError("cannot verify existing worker")))
    with pytest.raises(OSError):
        manager.start(BODY)
    screening.subprocess.Popen.assert_not_called()


def test_actual_process_launch_failure_remains_a_failure(manager, monkeypatch):
    monkeypatch.setattr(screening.subprocess, "Popen", Mock(side_effect=OSError("process unavailable")))
    with pytest.raises(OSError, match="process unavailable"):
        manager.start(BODY)
    assert manager.status()["status"] == "failed"


def test_completed_worker_response_is_not_replaced_by_starting(manager, monkeypatch):
    def spawn(*args, **kwargs):
        directory = manager._directory()
        state = json.loads((directory / "status.json").read_text(encoding="utf-8"))
        screening.write_json(directory / "status.json", {**state, "status": "completed"})
        return Mock(pid=1234)
    monkeypatch.setattr(screening.subprocess, "Popen", spawn)
    manager._warm_results = False
    assert manager.start(BODY)["status"] == "completed"


@pytest.mark.parametrize("failure", ["warmup", "status"])
def test_daily_scheduler_retains_ownership_of_the_accepted_run(manager, monkeypatch, failure):
    now = datetime(2026, 9, 30, 15, 11, tzinfo=CN)
    live = Jobs(manager.root.parent / "live")
    service = SignalMonitor(manager.root.parent / "monitor", screening=manager, monitor=live, now=lambda: now)
    state = service._load()
    state["enabled"] = True
    if failure == "warmup":
        monkeypatch.setattr(screening.threading, "Thread", Mock(side_effect=RuntimeError("warmup unavailable")))
    else:
        manager._warm_results = False
        original = manager._status
        monkeypatch.setattr(manager, "_status", lambda directory: original(directory) if directory is None
                            else (_ for _ in ()).throw(OSError("status unavailable")))
    cutoff = int(now.replace(hour=15, minute=0).timestamp())
    service._schedule_daily(now, cutoff, {"status": "idle"})
    phase = state["daily"]["afternoon"]
    assert phase["run_id"] == manager._directory().name
    assert phase["status"] == "started" and phase["attempts"] == 1
    assert not state.get("startup_errors")


def test_result_remains_readable_on_demand_after_warmup_cannot_start(manager, monkeypatch):
    with monkeypatch.context() as fault:
        fault.setattr(screening.threading, "Thread", Mock(side_effect=RuntimeError("warmup unavailable")))
        launched = manager.start(BODY)
    seed_run(manager, launched["run_id"])
    result = manager.results()
    assert result["run_id"] == launched["run_id"] and not result["results_loading"]
    assert result["errors"][0]["code"] == "SH.600000"
    assert screening.subprocess.Popen.call_count == 1


def test_failed_diagnostic_logging_does_not_lose_a_successful_launch(manager, monkeypatch):
    logger = Mock()
    logger.warning.side_effect = OSError("log storage unavailable")
    monkeypatch.setattr(screening.logging, "getLogger", lambda *args: logger)
    monkeypatch.setattr(screening.threading, "Thread", Mock(side_effect=RuntimeError("warmup unavailable")))
    assert manager.start(BODY)["run_id"] == manager._directory().name


def test_live_scheduler_retains_ownership_when_initial_status_cannot_be_read(manager, monitor, monkeypatch):
    service, _manual, _live, state = monitor
    manager._warm_results = False
    service._monitor = manager
    original = manager._status
    monkeypatch.setattr(manager, "_status", lambda directory: original(directory) if directory is None
                        else (_ for _ in ()).throw(OSError("status unavailable")))
    service._schedule_monitor(service.now(), 300)
    assert state["active_monitor_run"] == manager._directory().name
    assert state["active_symbols"] == ["a:SH.600000"]
    assert state["attempted_cutoffs"]["a:SH.600000"] == 300
    assert not state.get("startup_errors")
