"""Offline worker connection capacity; no provider SDK contexts are created."""
from contextlib import contextmanager
import multiprocessing
import os
from queue import Empty
import sys
from types import SimpleNamespace

import pytest

from chanlun.exchange import quote_capacity as capacity


@pytest.fixture
def worker_context(monkeypatch, tmp_path):
    monkeypatch.setattr(capacity.multiprocessing, "current_process", lambda: SimpleNamespace(name="SpawnProcess-test"))
    monkeypatch.setattr(capacity, "_worker_pid", None)
    monkeypatch.setattr(capacity, "_run_dir", None)
    monkeypatch.setattr(capacity, "_slot_stack", None)
    monkeypatch.setattr(capacity, "_budget_root", lambda: tmp_path / "slots")
    # The fake cleanup models the provider's close-before-release contract.
    closed = []
    def close():
        closed.append("context_closed")
        capacity.release_worker_quote_slot()
    monkeypatch.setitem(sys.modules, "chanlun.exchange.exchange_cq", SimpleNamespace(release_worker_quote_context=close))
    yield tmp_path, closed
    capacity.release_worker_quote_slot()


def test_main_process_does_not_acquire_close_or_import_provider(monkeypatch, tmp_path):
    monkeypatch.setattr(capacity, "_worker_pid", None)
    monkeypatch.setattr(capacity.multiprocessing, "current_process", lambda: SimpleNamespace(name="MainProcess"))
    monkeypatch.setattr(capacity, "_budget_root", lambda: pytest.fail("main must not acquire worker capacity"))
    monkeypatch.setitem(sys.modules, "chanlun.exchange.exchange_cq", SimpleNamespace(
        release_worker_quote_context=lambda: pytest.fail("main provider must stay open")))
    with capacity.worker_quote_scope(tmp_path):
        capacity.acquire_worker_quote_slot()
        capacity.release_worker_quote_resources()
    assert not capacity.is_worker_quote_process()


def test_failed_worker_closes_provider_before_returning_capacity(worker_context, monkeypatch):
    run_dir, events = worker_context
    actual = capacity.interprocess_file_lock
    @contextmanager
    def ordered_lock(*args, **kwargs):
        with actual(*args, **kwargs):
            try:
                yield
            finally:
                events.append("slot_released")
    monkeypatch.setattr(capacity, "interprocess_file_lock", ordered_lock)
    with pytest.raises(ValueError, match="failed fetch"):
        with capacity.worker_quote_scope(run_dir):
            capacity.acquire_worker_quote_slot()
            capacity.acquire_worker_quote_slot()  # One slot per worker context.
            raise ValueError("failed fetch")
    assert events[:2] == ["context_closed", "slot_released"]
    assert capacity._slot_stack is None
    assert capacity.is_worker_quote_process()  # Idle watchdog still recognizes worker.
    with pytest.raises(RuntimeError, match="Idle screening worker"):
        capacity.acquire_worker_quote_slot()


@pytest.mark.parametrize("fails", [False, True])
def test_external_history_releases_quote_before_cpu_work(worker_context, monkeypatch, fails):
    from chanlun.screening import markets
    run_dir, events = worker_context
    frame = object()
    def fetch(*args):
        capacity.acquire_worker_quote_slot()
        events.append("history_fetched")
        if fails:
            raise ValueError("failed history")
        return frame
    monkeypatch.setattr(markets, "_external_frame", fetch)
    with capacity.worker_quote_scope(run_dir):
        if fails:
            with pytest.raises(ValueError, match="failed history"):
                markets.external_frame("fake", "5m", {"market": "us"})
        else:
            assert markets.external_frame("fake", "5m", {"market": "us"}) is frame
        assert events[:2] == ["history_fetched", "context_closed"]
        assert capacity._slot_stack is None
        events.append("cpu_calculation")
    assert events.index("context_closed") < events.index("cpu_calculation")


def _hold_slot(root, ready, release, active, peak):
    from pathlib import Path
    from chanlun.exchange import quote_capacity as child_capacity
    child_capacity._budget_root = lambda: Path(root) / "slots"
    with child_capacity.worker_quote_scope(root):
        child_capacity.acquire_worker_quote_slot()
        with active.get_lock():
            active.value += 1
            peak.value = max(peak.value, active.value)
        ready.put(os.getpid())
        if not release.wait(15):
            raise RuntimeError("test release timeout")
        with active.get_lock():
            active.value -= 1
        child_capacity.release_worker_quote_slot()


def test_quote_connection_limit_is_shared_between_spawned_processes(tmp_path):
    context = multiprocessing.get_context("spawn")
    ready, release = context.Queue(), context.Event()
    active, peak = context.Value("i", 0), context.Value("i", 0)
    workers = [context.Process(target=_hold_slot, args=(str(tmp_path), ready, release, active, peak))
               for _ in range(capacity.WORKER_QUOTE_SLOTS + 1)]
    try:
        for worker in workers:
            worker.start()
        initial = {ready.get(timeout=15) for _ in range(capacity.WORKER_QUOTE_SLOTS)}
        assert len(initial) == capacity.WORKER_QUOTE_SLOTS
        with pytest.raises(Empty):
            ready.get(timeout=.2)
        release.set()
        assert ready.get(timeout=10) not in initial
        for worker in workers:
            worker.join(10)
            assert worker.exitcode == 0
        assert peak.value == capacity.WORKER_QUOTE_SLOTS
        assert active.value == 0
    finally:
        release.set()
        for worker in workers:
            worker.join(2)
            if worker.is_alive():
                worker.terminate()
                worker.join(2)
        ready.close()
