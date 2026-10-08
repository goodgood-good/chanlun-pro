"""Failed full-result initialization must release its reservation and waiters."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from tests.web.test_screening_results_cache import cached_run as cached_run, seed_run


def complete(manager):
    pending = manager.results(background=True)
    if pending["results_loading"]:
        manager._results_future.result(timeout=2)
    result = manager.results()
    assert not result["results_loading"]
    return result


def test_thread_start_failure_can_retry_the_same_full_result(cached_run, monkeypatch):
    manager = cached_run
    with monkeypatch.context() as fault:
        fault.setattr(threading.Thread, "start", lambda self: (_ for _ in ()).throw(RuntimeError("reader thread unavailable")))
        with pytest.raises(RuntimeError, match="reader thread unavailable"):
            manager.results(background=True)
    assert complete(manager)["errors"][0]["code"] == "SH.600000"


@pytest.mark.parametrize("background", [False, True])
@pytest.mark.parametrize("error_type", [OSError, MemoryError])
def test_cache_initialization_error_does_not_leave_a_permanent_pending_result(cached_run, monkeypatch, background, error_type):
    manager = cached_run
    with monkeypatch.context() as fault:
        fault.setattr(manager, "_load_results_cache",
                      lambda *args: (_ for _ in ()).throw(error_type("cache initialization unavailable")))
        with pytest.raises(error_type, match="cache initialization unavailable"):
            manager.results(background=background)
    assert complete(manager)["errors"][0]["code"] == "SH.600000"


@pytest.mark.parametrize("replacement", [False, True])
def test_waiting_callers_are_released_when_cache_initialization_fails(cached_run, monkeypatch, replacement):
    manager = cached_run
    entered, release, waiting = threading.Event(), threading.Event(), threading.Event()
    original = manager._load_results_cache
    attempts = []

    def load(*args):
        attempts.append(1)
        if len(attempts) == 1:
            entered.set()
            assert release.wait(5)
            raise MemoryError("controlled first cache failure")
        return original(*args)

    monkeypatch.setattr(manager, "_load_results_cache", load)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(manager.results, background=True)
        pending = None
        try:
            assert entered.wait(2)
            pending = manager._results_future
            original_result = pending.result

            def result(*args, **kwargs):
                waiting.set()
                return original_result(*args, **kwargs)

            monkeypatch.setattr(pending, "result", result)
            if replacement:
                seed_run(manager, "b" * 32, "SZ.000001")
            following = pool.submit(manager.results)
            assert waiting.wait(2)
            release.set()
            with pytest.raises(MemoryError, match="controlled first cache failure"):
                first.result(timeout=2)
            if replacement:
                value = following.result(timeout=2)
                assert value["run_id"] == "b" * 32
                assert value["errors"][0]["code"] == "SZ.000001"
            else:
                with pytest.raises(MemoryError, match="controlled first cache failure"):
                    following.result(timeout=2)
        finally:
            release.set()
            if pending is not None and not pending.done():
                pending.set_exception(MemoryError("controlled test cleanup"))
    result = complete(manager)
    assert result["errors"][0]["code"] == ("SZ.000001" if replacement else "SH.600000")
