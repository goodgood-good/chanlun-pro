"""No HTTP traffic: shared uSMART pacing, deadlines, and corruption handling."""
import json
import multiprocessing
import os
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from chanlun.exchange.errors import USmartAPIError
from chanlun.exchange.usmart_rate_limit import (
    SCHEMA, USmartRateLimiter, USmartRateLimitTimeout, USmartRateLimitStateError,
    USmartRateLimitCancelled,
)
from chanlun.persistence.file_lock import interprocess_file_lock

HOST = "https://quote.example:8443"
CHANNEL = "public-channel"


class Clock:
    def __init__(self):
        self.now = 100.0
        self.waits = []

    def read(self):
        return self.now

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


def limiter(root, clock, **kwargs):
    return USmartRateLimiter(root, clock=clock.read, monotonic=clock.read,
                            sleep=kwargs.get("sleep", clock.sleep))


def request(gate, endpoint="kline", **kwargs):
    return gate.wait(host=HOST, channel=CHANNEL, endpoint=endpoint, **kwargs)


@pytest.mark.parametrize("endpoint,spacing", [("kline", .5), ("realtime", .5), ("basicinfo", 3)])
def test_endpoint_contract_is_smooth_not_a_minute_burst(tmp_path, endpoint, spacing):
    clock = Clock()
    gate = limiter(tmp_path, clock)
    stamps = []
    for _ in range(4):
        request(gate, endpoint)
        stamps.append(clock.now)
    assert stamps == [100 + spacing * i for i in range(4)]
    assert clock.waits == [spacing] * 3


def test_instances_share_public_identity_without_auth_token_partition(tmp_path):
    clock = Clock()
    first, second = limiter(tmp_path, clock), limiter(tmp_path, clock)
    first.wait(host="HTTPS://QUOTE.EXAMPLE/", channel=CHANNEL, endpoint="kline")
    second.wait(host="https://quote.example:443", channel=CHANNEL,
                endpoint="/quotes-openservice/api/v1/KLINE/")
    assert clock.now == 100.5
    assert len(list(tmp_path.glob("*.json"))) == 1
    raw = "".join(p.read_text(encoding="utf8") for p in tmp_path.glob("*.json"))
    assert CHANNEL not in raw and "quote.example" not in raw
    assert "token" not in raw and "password" not in raw


def test_endpoint_and_public_channel_buckets_are_independent(tmp_path):
    clock = Clock()
    gate = limiter(tmp_path, clock)
    request(gate, "kline")
    request(gate, "realtime")
    gate.wait(host=HOST, channel="another-channel", endpoint="kline")
    assert clock.now == 100
    assert len(list(tmp_path.glob("*.json"))) == 3


def test_timeout_does_not_bypass_or_consume_a_future_slot(tmp_path):
    clock = Clock()
    gate = limiter(tmp_path, clock)
    request(gate, "basicinfo")
    path, = tmp_path.glob("*.json")
    before = path.read_text(encoding="utf8")
    with pytest.raises(USmartRateLimitTimeout) as caught:
        request(gate, "basicinfo", timeout=2)
    assert isinstance(caught.value, USmartAPIError)
    assert caught.value.retryable is False and caught.value.status_code is None
    assert caught.value.__cause__ is None
    assert path.read_text(encoding="utf8") == before
    request(gate, "basicinfo", timeout=4)
    assert clock.now == 103


def test_lock_is_released_before_waiting(tmp_path):
    clock = Clock()
    locks_seen = []
    def sleep(seconds):
        lock, = tmp_path.glob("*.lock")
        with interprocess_file_lock(lock, timeout_seconds=.01):
            locks_seen.append(True)
        clock.sleep(seconds)
    gate = limiter(tmp_path, clock, sleep=sleep)
    request(gate)
    request(gate)
    assert locks_seen == [True]


def test_contended_metadata_lock_obeys_the_call_deadline(tmp_path):
    gate = USmartRateLimiter(tmp_path)
    request(gate)
    lock, = tmp_path.glob("*.lock")
    with interprocess_file_lock(lock, timeout_seconds=.01):
        started = time.monotonic()
        with pytest.raises(USmartRateLimitTimeout):
            request(gate, timeout=.03)
        assert time.monotonic() - started < .3


@pytest.mark.parametrize("content", ["{", "[]", '{"schema":"wrong"}',
                                    '{"schema":"usmart-quote-rate-v1","next_at":NaN,"interactive_until":0}'])
def test_damaged_state_fails_closed_without_resetting_budget(tmp_path, content):
    clock = Clock()
    gate = limiter(tmp_path, clock)
    request(gate)
    path, = tmp_path.glob("*.json")
    path.write_text(content, encoding="utf8")
    with pytest.raises(USmartRateLimitStateError) as caught:
        request(gate)
    assert caught.value.retryable is False
    assert path.read_text(encoding="utf8") == content


def test_storage_failure_is_a_typed_provider_failure(tmp_path, monkeypatch):
    clock = Clock()
    gate = limiter(tmp_path, clock)
    def fail(*args):
        raise PermissionError("controlled metadata store failure")
    monkeypatch.setattr("chanlun.exchange.usmart_rate_limit.os.replace", fail)
    with pytest.raises(USmartRateLimitStateError):
        request(gate)
    assert not list(tmp_path.glob("*.json"))


def test_wall_clock_rollback_does_not_reset_the_shared_budget(tmp_path):
    clock = Clock()
    gate = limiter(tmp_path, clock)
    request(gate)
    clock.now = 95
    with pytest.raises(USmartRateLimitStateError):
        request(gate, timeout=1)
    path, = tmp_path.glob("*.json")
    assert json.loads(path.read_text(encoding="utf8"))["next_at"] == 100.5


def test_foreground_wait_reserves_the_next_slot_from_background(tmp_path):
    clock = Clock()
    background = limiter(tmp_path, clock)
    request(background, priority="background")
    def sleep(seconds):
        clock.sleep(seconds)
        with pytest.raises(USmartRateLimitTimeout):
            request(background, timeout=.02, priority="background")
    foreground = limiter(tmp_path, clock, sleep=sleep)
    request(foreground)
    assert clock.now == pytest.approx(100.52)


def test_interrupted_foreground_waiter_is_removed_immediately(tmp_path):
    clock = Clock()
    gate = limiter(tmp_path, clock)
    request(gate, priority="background")
    def interrupted(_seconds):
        raise RuntimeError("caller disappeared")
    foreground = limiter(tmp_path, clock, sleep=interrupted)
    with pytest.raises(RuntimeError, match="caller disappeared"):
        request(foreground)
    clock.now = 100.5
    request(gate, priority="background")
    assert clock.now == 100.5
    path, = tmp_path.glob("*.json")
    assert json.loads(path.read_text(encoding="utf8"))["queue"] == []


def test_legacy_bucket_migration_preserves_the_next_permit(tmp_path):
    clock = Clock()
    gate = limiter(tmp_path, clock)
    request(gate)
    path, = tmp_path.glob("*.json")
    path.write_text(json.dumps({"schema": "usmart-quote-rate-v1", "next_at": 100.5,
                                "interactive_until": 100.55}), encoding="utf8")
    metrics = request(gate, priority="background")
    assert metrics["wait_seconds"] == .5
    assert metrics["admitted_at"] == 100.5
    assert json.loads(path.read_text(encoding="utf8"))["schema"] == SCHEMA


def test_cancellation_removes_only_its_waiter_without_resetting_pacing(tmp_path):
    clock = Clock()
    cancel = [False]
    def sleep(seconds):
        clock.sleep(seconds)
        cancel[0] = True
    gate = limiter(tmp_path, clock, sleep=sleep)
    request(gate)
    with pytest.raises(USmartRateLimitCancelled):
        request(gate, cancelled=lambda: cancel[0])
    path, = tmp_path.glob("*.json")
    state = json.loads(path.read_text(encoding="utf8"))
    assert state["queue"] == []
    assert state["next_at"] == 100.5


def test_timeout_waiter_is_removed_and_reports_wait_metrics(tmp_path):
    clock = Clock()
    gate = limiter(tmp_path, clock)
    request(gate, "basicinfo")
    with pytest.raises(USmartRateLimitTimeout) as error:
        request(gate, "basicinfo", timeout=1)
    assert error.value.wait_seconds == 1
    assert error.value.queue_depth == 1
    path, = tmp_path.glob("*.json")
    assert json.loads(path.read_text(encoding="utf8"))["queue"] == []


def test_provider_priority_respects_prewarm_marker_and_worker_process(monkeypatch):
    from chanlun.exchange import lb_priority
    monkeypatch.setattr(lb_priority.multiprocessing, "current_process", lambda: SimpleNamespace(name="MainProcess"))
    assert lb_priority.quote_request_priority() == "interactive"
    with lb_priority.lb_low_priority():
        assert lb_priority.quote_request_priority() == "background"
    assert lb_priority.quote_request_priority() == "interactive"
    monkeypatch.setattr(lb_priority.multiprocessing, "current_process", lambda: SimpleNamespace(name="SpawnProcess-1"))
    assert lb_priority.quote_request_priority() == "background"


def _process_request(root, endpoint, timeout, barrier, results):
    granted_at = [None]
    def clock():
        granted_at[0] = time.time()
        return granted_at[0]
    gate = USmartRateLimiter(root, clock=clock)
    barrier.wait(timeout=15)
    try:
        request(gate, endpoint, timeout=timeout, priority="background")
        results.put(("granted", granted_at[0]))
    except USmartRateLimitTimeout:
        results.put(("timeout", None))


@pytest.mark.parametrize("endpoint,count,timeout,expected", [("kline", 5, 5, 5), ("basicinfo", 4, .2, 1)])
def test_independent_processes_share_one_endpoint_budget(tmp_path, endpoint, count, timeout, expected):
    context = multiprocessing.get_context("spawn")
    barrier, results = context.Barrier(count), context.Queue()
    workers = [context.Process(target=_process_request,
                               args=(str(tmp_path), endpoint, timeout, barrier, results)) for _ in range(count)]
    try:
        for worker in workers:
            worker.start()
        rows = [results.get(timeout=20) for _ in workers]
        for worker in workers:
            worker.join(10)
            assert worker.exitcode == 0
        granted = sorted(stamp for status, stamp in rows if status == "granted")
        assert len(granted) == expected
        spacing = 3 if endpoint == "basicinfo" else .5
        assert all(b - a >= spacing - .00001 for a, b in zip(granted, granted[1:]))
        assert len(list(tmp_path.glob("*.json"))) == 1
    finally:
        for worker in workers:
            worker.join(2)
            if worker.is_alive():
                worker.terminate()
                worker.join(2)
        results.close()


def _paced_requests(root, priority, count, barrier, results):
    # Real process scheduling and the production request/deadline clocks.
    scale = 1.0
    gate = USmartRateLimiter(root, clock=lambda: time.time() * scale,
                            monotonic=lambda: time.monotonic() * scale,
                            sleep=lambda seconds: time.sleep(seconds / scale))
    barrier.wait(timeout=20)
    metrics = []
    try:
        for _ in range(count):
            metrics.append(request(gate, priority=priority))  # Default ten-second budget.
        results.put({"priority": priority, "metrics": metrics})
    except USmartAPIError as exc:
        results.put({"error": type(exc).__name__, "metrics": metrics,
                     "cause":type(exc.__cause__).__name__ if exc.__cause__ else None})


def _run_paced_contenders(root, specifications):
    context = multiprocessing.get_context("spawn")
    barrier, results = context.Barrier(len(specifications)), context.Queue()
    workers = [context.Process(target=_paced_requests,
                               args=(str(root), priority, count, barrier, results))
               for priority, count in specifications]
    try:
        for worker in workers:
            worker.start()
        rows = [results.get(timeout=25) for _ in workers]
        for worker in workers:
            worker.join(5)
            assert worker.exitcode == 0
        assert not [row for row in rows if "error" in row], rows
        return [entry for row in rows for entry in row["metrics"]]
    finally:
        for worker in workers:
            worker.join(1)
            if worker.is_alive():
                worker.terminate()
                worker.join(1)
        results.close()


def test_sixteen_spawn_workers_get_fifo_turns_across_consecutive_pages(tmp_path):
    metrics = _run_paced_contenders(tmp_path, [("background", 2)] * 16)
    admitted = sorted(metrics, key=lambda m: m["admitted_at"])
    assert len(admitted) == 32
    assert [m["enqueued_at"] for m in admitted] == sorted(m["enqueued_at"] for m in admitted)
    assert all(b["admitted_at"] - a["admitted_at"] >= .5 - .00001 for a, b in zip(admitted, admitted[1:]))
    assert max(m["wait_seconds"] for m in metrics) < 10
    assert max(m["queue_depth"] for m in metrics) >= 2


def test_continuous_foreground_cannot_starve_background(tmp_path):
    metrics = _run_paced_contenders(tmp_path, [("interactive", 24), ("background", 4)])
    foreground = [m for m in metrics if m["priority"] == "interactive"]
    background = [m for m in metrics if m["priority"] == "background"]
    assert len(background) == 4
    for waiting in background:
        earlier = [m for m in foreground
                   if waiting["enqueued_at"] <= m["admitted_at"] < waiting["admitted_at"]]
        assert len(earlier) <= 3
        assert waiting["wait_seconds"] < 3


def _die_with_queued_request(root, enqueued):
    def stop(_seconds):
        enqueued.set()
        os._exit(0)
    gate = USmartRateLimiter(root, sleep=stop)
    request(gate, priority="background")
    request(gate, priority="background")


def test_dead_process_is_removed_before_its_request_deadline(tmp_path):
    context = multiprocessing.get_context("spawn")
    enqueued = context.Event()
    child = context.Process(target=_die_with_queued_request, args=(str(tmp_path), enqueued))
    try:
        child.start()
        assert enqueued.wait(10)
        child.join(5)
        assert child.exitcode == 0
        path, = tmp_path.glob("*.json")
        assert any(item["pid"] == child.pid for item in json.loads(path.read_text(encoding="utf8"))["queue"])
        metrics = request(USmartRateLimiter(tmp_path), priority="background")
        assert metrics["wait_seconds"] < 2
        assert json.loads(path.read_text(encoding="utf8"))["queue"] == []
    finally:
        child.join(1)
        if child.is_alive():
            child.terminate()
            child.join(1)
