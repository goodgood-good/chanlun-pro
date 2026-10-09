"""No HTTP traffic: shared uSMART pacing, deadlines, and corruption handling."""
import json
import multiprocessing
from pathlib import Path
import time

import pytest

from chanlun.exchange.errors import USmartAPIError
from chanlun.exchange.usmart_rate_limit import (
    USmartRateLimiter, USmartRateLimitTimeout, USmartRateLimitStateError,
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
    with pytest.raises(USmartRateLimitTimeout):
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
    assert clock.now == 100.5


def test_dead_foreground_waiter_priority_expires_promptly(tmp_path):
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
    assert clock.now == pytest.approx(100.55)


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
