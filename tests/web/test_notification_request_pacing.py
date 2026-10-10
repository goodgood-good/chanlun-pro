"""All transport attempts share persisted pacing, including failures/restarts."""
from contextlib import contextmanager
import json
import multiprocessing
import os
from pathlib import Path
import threading

import pytest

from cl_app.services.dingtalk_notifications import DeliveryError, DingTalkOutbox, DingTalkTransport
from tests.web.test_dingtalk_notifications import Transport, event


def notice(identity, stamp=1000.):
    # Holding priority deliberately makes separate receipts without a batching
    # wait. No actual holdings service or external sender is used by this test.
    return {**event('confirmed', identity=identity, stamp=stamp), 'holding': True}


def seed_queue(path, now):
    box = DingTalkOutbox(path, transport=Transport(), now=now, signal_validator=lambda rows: (rows, []))
    box.signal_events([notice('first')], since=0)
    box.signal_events([notice('second')], since=0)
    return box


class ProcessTransport:
    def __init__(self, clock, log, *, failure=False, entered=None, release=None, crash=False):
        self.clock, self.log = clock, log
        self.failure, self.entered, self.release, self.crash = failure, entered, release, crash

    def send(self, _text):
        if self.crash:
            # Model a delay after durable reservation but before HTTP starts.
            # Recovery must not assume that the old reservation time is the
            # latest possible request time once its owner has died.
            with self.clock.get_lock():
                self.clock.value += 31
        with Path(self.log).open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'pid': os.getpid(), 'started': self.clock.value}) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        if self.crash:
            os._exit(9)
        if self.entered is not None:
            self.entered.set()
            assert self.release.wait(10)
        with self.clock.get_lock():
            self.clock.value += .2
        if self.failure:
            raise DeliveryError('isolated HTTP rejection')


def process_dispatch(path, clock, log, failure=False, entered=None, release=None, crash=False, crash_validation=False):
    def validate(rows):
        if crash_validation:
            os._exit(8)
        return rows, []
    transport = ProcessTransport(clock, log, failure=failure, entered=entered, release=release, crash=crash)
    box = DingTalkOutbox(Path(path), transport=transport, now=lambda: clock.value, signal_validator=validate)
    box.deliver_one()


def run_process(context, *args, expected_exit=0):
    process = context.Process(target=process_dispatch, args=args)
    process.start()
    process.join(15)
    if process.is_alive():
        process.terminate()
        process.join(3)
        pytest.fail('isolated dispatch child did not finish')
    assert process.exitcode == expected_exit
    process.close()


def requests(log):
    return [json.loads(line) for line in log.read_text(encoding='utf-8').splitlines()] if log.exists() else []


@pytest.mark.parametrize('failure', [DeliveryError, RuntimeError])
def test_new_arrival_during_failed_request_cannot_bypass_four_seconds(tmp_path, failure):
    clock, starts = [1000.], []
    done = threading.Event()
    class Wake:
        def __init__(self):
            self.flag = threading.Event()
        def clear(self):
            self.flag.clear()
        def set(self):
            self.flag.set()
        def wait(self, timeout):
            if not self.flag.is_set():
                clock[0] += timeout
    class Sender(Transport):
        def send(self, text):
            starts.append(clock[0])
            clock[0] += .2
            if len(starts) == 1:
                box.signal_events([notice('second', clock[0])], since=0)
                raise failure('isolated failed HTTP request')
            super().send(text)
            box._stop.set()
            box._wake.set()
            done.set()
    box = DingTalkOutbox(tmp_path / 'queue.sqlite', transport=Sender(), now=lambda: clock[0],
                        signal_validator=lambda rows: (rows, []))
    box._wake = Wake()
    box.signal_events([notice('first')], since=0)
    try:
        box.start(lambda: True)
        assert done.wait(5)
    finally:
        box.stop()
    assert starts == pytest.approx([1000., 1004.2])
    assert starts[1] - starts[0] >= 4
    assert len(box.transport.messages) == 1


def test_failed_request_pacing_is_shared_by_separate_processes(tmp_path):
    context = multiprocessing.get_context('spawn')
    clock = context.Value('d', 1000.)
    path, log = tmp_path / 'queue.sqlite', tmp_path / 'requests.jsonl'
    seed_queue(path, lambda: clock.value)
    run_process(context, path, clock, log, True)
    assert clock.value == pytest.approx(1000.2)
    run_process(context, path, clock, log)
    assert len(requests(log)) == 1
    clock.value = 1004.199
    run_process(context, path, clock, log)
    assert len(requests(log)) == 1
    clock.value = 1004.2
    run_process(context, path, clock, log)
    assert [row['started'] for row in requests(log)] == pytest.approx([1000., 1004.2])


def test_live_transport_cannot_be_reclaimed_after_its_sqlite_lease_expires(tmp_path):
    context = multiprocessing.get_context('spawn')
    clock = context.Value('d', 1000.)
    entered, release = context.Event(), context.Event()
    path, log = tmp_path / 'queue.sqlite', tmp_path / 'requests.jsonl'
    seed_queue(path, lambda: clock.value)
    child = context.Process(target=process_dispatch, args=(path, clock, log, False, entered, release))
    child.start()
    peer = DingTalkOutbox(path, transport=ProcessTransport(clock, log), now=lambda: clock.value,
                         signal_validator=lambda rows: (rows, []))
    try:
        assert entered.wait(10)
        clock.value += 31
        assert peer.deliver_one() == pytest.approx(1031.1)
        assert len(requests(log)) == 1
    finally:
        release.set()
        child.join(10)
        if child.is_alive():
            child.terminate()
            child.join(3)
        assert child.exitcode == 0
        child.close()
    assert peer.deliver_one() == pytest.approx(1035.2)
    clock.value = 1035.2
    peer.deliver_one()
    assert len(requests(log)) == 2


def test_crashed_request_owner_gets_one_fresh_recovery_fence(tmp_path):
    context = multiprocessing.get_context('spawn')
    clock = context.Value('d', 1000.)
    path, log = tmp_path / 'queue.sqlite', tmp_path / 'requests.jsonl'
    seed_queue(path, lambda: clock.value)
    run_process(context, path, clock, log, False, None, None, True, expected_exit=9)
    assert clock.value == 1031
    peer = DingTalkOutbox(path, transport=ProcessTransport(clock, log), now=lambda: clock.value,
                         signal_validator=lambda rows: (rows, []))
    assert peer.deliver_one() == 1035
    clock.value = 1034.9
    assert peer.deliver_one() == 1035, 'recovery cooldown was incorrectly extended on every poll'
    assert len(requests(log)) == 1
    clock.value = 1035
    peer.deliver_one()
    assert [row['started'] for row in requests(log)] == [1031, 1035]


def test_crash_during_validation_does_not_consume_the_http_interval(tmp_path):
    context = multiprocessing.get_context('spawn')
    clock = context.Value('d', 1000.)
    path, log = tmp_path / 'queue.sqlite', tmp_path / 'requests.jsonl'
    seed_queue(path, lambda: clock.value)
    run_process(context, path, clock, log, False, None, None, False, True, expected_exit=8)
    assert requests(log) == []
    clock.value = 1000.2
    peer = DingTalkOutbox(path, transport=ProcessTransport(clock, log), now=lambda: clock.value,
                         signal_validator=lambda rows: (rows, []))
    peer.deliver_one()
    assert [row['started'] for row in requests(log)] == [1000.2]


def test_receipt_commit_failure_preserves_the_unknown_request_fence(tmp_path, monkeypatch):
    clock = [1000.]
    sent = []
    transport = Transport()
    def send(text):
        sent.append(clock[0])
        clock[0] += .2
    transport.send = send
    box = seed_queue(tmp_path / 'queue.sqlite', lambda: clock[0])
    box.transport = transport
    connect = box._connect
    @contextmanager
    def failed_commit(**kwargs):
        with connect(**kwargs) as db:
            yield db
            if sent:
                raise OSError('isolated receipt commit interrupted')
    with monkeypatch.context() as fault:
        fault.setattr(box, '_connect', failed_commit)
        with pytest.raises(OSError, match='commit interrupted'):
            box.deliver_one()
    restored = DingTalkOutbox(box.path, transport=transport, now=lambda: clock[0],
                             signal_validator=lambda rows: (rows, []))
    assert restored.deliver_one() == pytest.approx(1004.2)
    assert sent == [1000.]


def test_known_preflight_failure_does_not_consume_an_http_interval(tmp_path, monkeypatch):
    clock = [1000.]
    monkeypatch.setattr(DingTalkTransport, 'configuration', staticmethod(lambda: {'configured': False}))
    box = seed_queue(tmp_path / 'queue.sqlite', lambda: clock[0])
    box.transport = DingTalkTransport()
    box.deliver_one()
    with box._connect() as db:
        assert db.execute("SELECT value FROM delivery_settings WHERE key='next_request_at'").fetchone() is None
        assert db.execute("SELECT value FROM delivery_settings WHERE key='request_inflight'").fetchone()[0] == 0
    peer = DingTalkOutbox(box.path, transport=Transport(), now=lambda: clock[0],
                         signal_validator=lambda rows: (rows, []))
    peer.deliver_one()
    assert len(peer.transport.messages) == 1


def test_receipt_local_counter_scope_is_explicit_in_metrics(tmp_path):
    clock = [1000.]
    box = seed_queue(tmp_path / 'queue.sqlite', lambda: clock[0])
    box.deliver_one()
    group = box.latency_summary()['groups'][0]
    assert {'validation', 'validation_total', 'request'} <= set(group['receipt_local_measures'])
    assert 'detection_to_sent' not in group['receipt_local_measures']


def test_deferred_subset_metrics_explicitly_remain_local_to_its_own_receipt(tmp_path):
    clock, calls = [1000.], [0]
    def validate(rows):
        calls[0] += 1
        clock[0] += 2 if calls[0] == 1 else 1
        return (rows[:1], rows[1:]) if calls[0] == 1 else (rows, [])
    box = DingTalkOutbox(tmp_path / 'queue.sqlite', transport=Transport(), now=lambda: clock[0], signal_validator=validate)
    box.signal_events([event('confirmed', identity='ready'), event('confirmed', identity='waiting')], since=0)
    box.deliver_one()
    clock[0] += 4
    box.deliver_one()
    with box._read_connect() as db:
        receipts = [(json.loads(row[0]), row[1], row[2]) for row in db.execute(
            'SELECT p.events,t.validation_attempts,t.validation_seconds FROM signal_payloads p '
            'JOIN delivery_timings t ON t.outbox_id=p.outbox_id')]
    waiting = next(row for row in receipts if row[0][0]['id'] == 'waiting')
    assert waiting[1:] == (1, 1.), 'a child receipt must not claim ownership of its parent batch counter'
    assert all('validation_total' in group['receipt_local_measures'] for group in box.latency_summary()['groups'])
