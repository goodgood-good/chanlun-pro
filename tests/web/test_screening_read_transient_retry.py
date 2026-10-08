"""Temporary I/O failures cannot permanently strand a completed screening run."""

from types import SimpleNamespace
from unittest.mock import Mock
from contextlib import contextmanager
import ctypes
import os

import pytest

from cl_app.services import signal_monitor
from tests.web.test_signal_monitor import setup as setup, _seed, signal
from tests.web.test_screening_results_cache import seed_run
from cl_app.services.screening import ScreeningManager


@contextmanager
def deny_file_reads(path):
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateFileW(str(path), 0x80000000, 0, None, 3, 0, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield
    finally:
        assert kernel.CloseHandle(handle)


@pytest.mark.parametrize("error", [OSError, PermissionError])
def test_same_completed_results_recover_after_three_temporary_failures(setup, monkeypatch, error):
    service, manual, _live, _clock, _cutoff = _seed(setup, [])
    status = manual.status()
    timer = [0.0]
    monkeypatch.setattr(signal_monitor, "time", SimpleNamespace(monotonic=lambda: timer[0]))
    read = Mock(side_effect=error("controlled temporary read failure"))
    monkeypatch.setattr(manual, "results", read)
    for stamp in (0, 15, 45):
        timer[0] = stamp
        assert service._screening_results_for_monitor(status)["status"] == "unavailable"
    assert read.call_count == 3
    recovered = {"status": "completed", "run_id": status["run_id"], "results_loading": False}
    read.side_effect = None
    read.return_value = recovered
    timer[0] = 105
    assert manual.status() == status
    assert service._screening_results_for_monitor(status) == recovered
    assert read.call_count == 4
    assert "screening_result_error" not in service._load()


def test_persistent_io_failure_has_bounded_backoff_instead_of_busy_polling(setup, monkeypatch):
    service, manual, _live, _clock, _cutoff = _seed(setup, [])
    timer = [0.0]
    monkeypatch.setattr(signal_monitor, "time", SimpleNamespace(monotonic=lambda: timer[0]))
    read = Mock(side_effect=PermissionError("controlled occupied result"))
    monkeypatch.setattr(manual, "results", read)
    status = manual.status()
    for stamp, expected in ((0, 1), (14, 1), (15, 2), (44, 2), (45, 3),
                            (104, 3), (105, 4), (164, 4), (165, 5)):
        timer[0] = stamp
        assert service._screening_results_for_monitor(status)["status"] == "unavailable"
        assert read.call_count == expected


def test_success_resets_the_temporary_failure_backoff(setup, monkeypatch):
    service, manual, _live, _clock, _cutoff = _seed(setup, [])
    timer = [0.0]
    monkeypatch.setattr(signal_monitor, "time", SimpleNamespace(monotonic=lambda: timer[0]))
    read = Mock(side_effect=OSError("controlled failure"))
    monkeypatch.setattr(manual, "results", read)
    status = manual.status()
    for stamp in (0, 15, 45):
        timer[0] = stamp
        service._screening_results_for_monitor(status)
    timer[0] = 105
    read.side_effect = None
    read.return_value = {"status": "completed", "results_loading": False}
    assert service._screening_results_for_monitor(status)["status"] == "completed"
    read.side_effect = OSError("a later independent failure")
    timer[0] = 106
    service._screening_results_for_monitor(status)
    assert service._load()["screening_result_error"]["attempts"] == 1
    count = read.call_count
    timer[0] = 120
    service._screening_results_for_monitor(status)
    assert read.call_count == count
    timer[0] = 121
    service._screening_results_for_monitor(status)
    assert read.call_count == count + 1


def test_pool_handoff_recovers_without_a_new_run_or_evidence_version(setup, monkeypatch):
    service, manual, live, _clock, cutoff = _seed(setup, [signal()])
    service.tick()
    live.finish(cutoff)
    service.tick()
    previous = service._load()["seed"]["run_id"]
    manual.start(manual.state["settings"])
    manual.rows = [signal("SH.600099")]
    manual.finish(cutoff)
    status = manual.status()
    original = manual.results
    read = Mock(side_effect=PermissionError("controlled temporary access failure"))
    monkeypatch.setattr(manual, "results", read)
    timer = [0.0]
    monkeypatch.setattr(signal_monitor, "time", SimpleNamespace(monotonic=lambda: timer[0]))
    monkeypatch.setattr(service, "_schedule_daily", lambda *args: None)
    scheduled = Mock()
    monkeypatch.setattr(service, "_schedule_monitor", scheduled)
    for stamp in (0, 15, 45):
        timer[0] = stamp
        service.tick()
        assert service._load()["seed"]["run_id"] == previous
    assert scheduled.call_count == 3
    read.side_effect = original
    timer[0] = 105
    service.tick()
    assert manual.status() == status
    state = service._load()
    assert state["seed"]["run_id"] == status["run_id"]
    assert state["seed"]["codes"] == ["a:SH.600099"]
    assert "screening_result_error" not in state
    assert scheduled.call_count == 4


@pytest.mark.skipif(os.name != "nt", reason="Windows read sharing semantics")
def test_real_file_sharing_failure_recovers_without_changing_evidence(setup, tmp_path, monkeypatch):
    service, _manual, _live, _clock = setup
    manager = ScreeningManager(tmp_path / "actual-results", warm_results=False)
    directory = seed_run(manager)
    service.screening = manager
    timer = [0.0]
    monkeypatch.setattr(signal_monitor, "time", SimpleNamespace(monotonic=lambda: timer[0]))
    version = manager.status()["evidence_version"]
    with deny_file_reads(directory / "results.jsonl"):
        for stamp in (0, 15, 45):
            timer[0] = stamp
            status = manager.status()
            assert status["evidence_version"] == version
            result = service._screening_results_for_monitor(status)
            if result.get("results_loading"):
                with pytest.raises(PermissionError):
                    manager._results_future.result(timeout=2)
                result = service._screening_results_for_monitor(status)
            assert result["status"] == "unavailable"
        assert service._load()["screening_result_error"]["attempts"] == 3
        assert service._load()["screening_result_error"]["detail"] == "PermissionError"
    timer[0] = 105
    status = manager.status()
    assert status["evidence_version"] == version
    result = service._screening_results_for_monitor(status)
    assert result["status"] == "completed"
    if result.get("results_loading"):
        manager._results_future.result(timeout=2)
        result = service._screening_results_for_monitor(status)
    assert result["results_through"] == 1 and not result["results_loading"]
    assert result["errors"][0]["code"] == "SH.600000"
    assert "screening_result_error" not in service._load()
