"""Shared history pacing reuses the proven priority queue, across processes."""

import json
import multiprocessing
import threading

import pytest

from chanlun.exchange.longbridge_history_limit import (
    LongbridgeHistoryLimiter,
    LongbridgeHistoryBusy,
)
from chanlun.exchange.quote_capacity import QuoteConnectionCancelled


class Clock:
    def __init__(self):
        self.now = 1000.0

    def read(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_a_single_sliding_window_stays_below_sixty_requests(tmp_path):
    clock = Clock()
    first = LongbridgeHistoryLimiter(
        tmp_path, clock=clock.read, monotonic=clock.read, sleep=clock.sleep
    )
    second = LongbridgeHistoryLimiter(
        tmp_path, clock=clock.read, monotonic=clock.read, sleep=clock.sleep
    )
    stamps = [
        (first if i % 2 else second).wait(cancelled=None)["admitted_at"]
        for i in range(61)
    ]
    assert stamps[-1] - stamps[0] >= 30.6 - 1e-7
    assert all(b - a >= 0.51 - 1e-7 for a, b in zip(stamps, stamps[1:]))
    assert all(sum(t <= stamp < t + 30 for stamp in stamps) <= 60 for t in stamps)
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_waiting_deadline_preserves_last_admission_and_releases_ticket(tmp_path):
    clock = Clock()
    gate = LongbridgeHistoryLimiter(
        tmp_path, clock=clock.read, monotonic=clock.read, sleep=clock.sleep
    )
    gate.wait(cancelled=None)
    with pytest.raises(LongbridgeHistoryBusy):
        gate.wait(timeout=0.01, cancelled=None)
    state = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert state["queue"] == [] and state["next_at"] == 1000.51
    assert gate.wait(cancelled=None)["admitted_at"] >= 1000.51


def test_cancelled_owner_never_acquires_an_admission(tmp_path):
    gate = LongbridgeHistoryLimiter(tmp_path)
    with pytest.raises(QuoteConnectionCancelled):
        gate.wait(cancelled=lambda: True)
    assert not list(tmp_path.glob("*.json"))


def test_chart_pages_use_background_priority_and_quotes_use_foreground(tmp_path):
    clock = Clock()
    gate = LongbridgeHistoryLimiter(
        tmp_path, clock=clock.read, monotonic=clock.read, sleep=clock.sleep
    )
    assert gate.wait(priority="prewarm", cancelled=None)["priority"] == "background"
    assert (
        gate.wait(priority="interactive", cancelled=None)["priority"] == "interactive"
    )


def test_actual_exchange_bounds_page_fanout_without_reducing_the_separate_call_pool(
    monkeypatch,
):
    from chanlun.exchange import exchange_cq as cq

    cls = cq.ExchangeChangQiao.__wrapped__
    monkeypatch.setattr(cq, "_build_longbridge_config", lambda: None)
    monkeypatch.setattr(cls, "_start_quote_ctx_watchdog", lambda self: None)
    exchange = cls()
    release = threading.Event()
    entered = threading.Event()
    lock = threading.Lock()
    counts = {"active": 0, "peak": 0, "done": 0}

    def page():
        with lock:
            counts["active"] += 1
            counts["peak"] = max(counts["peak"], counts["active"])
            if counts["active"] == 4:
                entered.set()
        release.wait(5)
        with lock:
            counts["active"] -= 1
            counts["done"] += 1

    try:
        futures = [exchange.executor.submit(page) for _ in range(20)]
        assert entered.wait(3)
        assert (
            counts["active"] == 4
            and exchange._quote_call_executor is not exchange.executor
        )
        release.set()
        for future in futures:
            future.result(timeout=5)
        assert counts["peak"] == 4 and counts["done"] == 20
    finally:
        release.set()
        exchange.executor.shutdown(wait=True)
        exchange._quote_call_executor.shutdown(wait=True)


def _worker(root, barrier, output):
    gate = LongbridgeHistoryLimiter(root)
    barrier.wait(10)
    for _ in range(2):
        output.put(gate.wait(timeout=10, cancelled=None)["admitted_at"])


def test_independent_processes_share_the_same_actual_admission_clock(tmp_path):
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(3)
    output = context.Queue()
    processes = [
        context.Process(target=_worker, args=(str(tmp_path), barrier, output))
        for _ in range(3)
    ]
    try:
        for process in processes:
            process.start()
        stamps = sorted(output.get(timeout=20) for _ in range(6))
        for process in processes:
            process.join(10)
        assert all(process.exitcode == 0 for process in processes)
        assert all(b - a >= 0.50 for a, b in zip(stamps, stamps[1:]))
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(5)
