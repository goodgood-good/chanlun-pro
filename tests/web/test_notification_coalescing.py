"""Bounded cross-call packets preserve per-event identity and dispatch fences."""
from copy import deepcopy
from contextlib import contextmanager
import json
import sqlite3
import threading
from unittest.mock import Mock

import pytest

from cl_app.services.dingtalk_notifications import DingTalkOutbox, signal_key
from tests.web.test_dingtalk_notifications import Transport, event


def notice(identity, *, stamp=1000., **extra):
    return {**event('confirmed', identity=identity, stamp=stamp), 'frequency': '1m', 'name': identity, **extra}


def make_box(tmp_path, clock, *, validator=None, transport=None):
    return DingTalkOutbox(tmp_path / 'outbox.sqlite', transport=transport or Transport(),
                         now=lambda: clock[0], signal_validator=validator or (lambda rows: (rows, [])))


def packets(box):
    with box._connect() as db:
        return [{**dict(row), 'events': json.loads(row['events'])} for row in db.execute(
            'SELECT o.*,p.events FROM outbox o JOIN signal_payloads p ON p.outbox_id=o.id ORDER BY o.rowid')]


def drain(box, clock):
    for _ in range(100):
        if all(row['status'] in ('sent', 'expired') for row in packets(box)):
            return
        due = box.deliver_one()
        if due is not None:
            clock[0] = max(clock[0], due)
    pytest.fail('local packet queue failed to drain')


def test_twenty_separate_calls_coalesce_without_delaying_the_first_packet(tmp_path):
    clock = [1000.]
    box = make_box(tmp_path, clock)
    for index in range(20):
        box.signal_events([notice(f'point-{index}')], since=0)
        clock[0] += .005
    rows = packets(box)
    assert len(rows) == 4 and [len(row['events']) for row in rows] == [5] * 4
    assert {item['id'] for row in rows for item in row['events']} == {f'point-{i}' for i in range(20)}
    assert all(row['next_try'] == row['created'] for row in rows)
    assert clock[0] < rows[0]['created'] + .250
    box.deliver_one()
    assert len(box.transport.messages) == 1, 'opportunistic batching added a holding delay'
    clock[0] += .1
    drain(box, clock)
    assert len(box.transport.messages) == 4


def test_join_window_is_anchored_to_the_first_creation_and_never_extended(tmp_path):
    clock = [1000.]
    box = make_box(tmp_path, clock)
    box.signal_events([notice('first')], since=0)
    first = packets(box)[0]
    clock[0] += .2
    box.signal_events([notice('second', stamp=clock[0])], since=0)
    joined = packets(box)[0]
    assert joined['id'] == first['id'] and joined['created'] == joined['next_try'] == 1000.
    clock[0] += .1
    box.signal_events([notice('third', stamp=clock[0])], since=0)
    assert [len(row['events']) for row in packets(box)] == [2, 1]


@pytest.mark.parametrize('change', [{'market': 'us'}, {'frequency': '5m'}, {'stage': 'formed'}, {'holding': True}])
def test_packet_boundaries_preserve_market_period_stage_and_holding_priority(tmp_path, change):
    clock = [1000.]
    box = make_box(tmp_path, clock)
    box.signal_events([notice('ordinary')], since=0)
    box.signal_events([notice('different', **change)], since=0)
    assert len(packets(box)) == 2


def test_all_buy_and_sell_types_remain_present_in_cross_call_packets(tmp_path):
    clock = [1000.]
    box = make_box(tmp_path, clock)
    kinds = ['1buy', '2buy', '3buy', '1sell', '2sell', '3sell']
    for kind in kinds:
        box.signal_events([notice(kind, point_type=kind)], since=0)
    assert [item['point_type'] for row in packets(box) for item in row['events']] == kinds


def test_holding_risk_and_inline_consolidation_bypass_ordinary_packet_joining(tmp_path):
    clock = [1000.]
    box = make_box(tmp_path, clock)
    box.charts = Mock()
    values = [notice('ordinary'), notice('holding', holding=True),
              notice('risk', signal_type='holding_risk', point_type='risk_stop', stage='triggered'),
              notice('warning', signal_type='consolidation_divergence', point_type='consolidation_up', stage='warning', inline_chart=True)]
    for value in values:
        box.signal_events([value], since=0)
    rows = packets(box)
    assert len(rows) == 4 and all(len(row['events']) == 1 for row in rows)
    inline = next(row for row in rows if row['events'][0]['id'] == 'warning')
    assert inline['events'][0]['inline_chart'] is True
    assert inline['id'].startswith('signals:') and not inline['id'].startswith('signals:batch:')
    box.charts.enqueue.assert_called_once()
    # No real transport: inspect the due queue ordering inside SQLite.
    with box._connect() as db:
        first = db.execute('SELECT p.events FROM outbox o JOIN signal_payloads p ON p.outbox_id=o.id '
                           'ORDER BY holding_priority(p.events) DESC,o.created,o.rowid LIMIT 1').fetchone()[0]
    assert json.loads(first)[0]['id'] == 'risk'


def test_first_member_pending_after_merge_has_a_distinct_recovery_receipt(tmp_path):
    clock = [1000.]
    unknown = [True]
    def validate(rows):
        return ([row for row in rows if row['id'] != 'first' or not unknown[0]],
                [row for row in rows if row['id'] == 'first' and unknown[0]])
    box = make_box(tmp_path, clock, validator=validate)
    original = [notice('first'), notice('second')]
    for row in original:
        box.signal_events([row], since=0)
    parent = packets(box)[0]['id']
    box.deliver_one()
    rows = packets(box)
    assert len(rows) == 2
    assert next(row for row in rows if row['id'] == parent)['events'][0]['id'] == 'second'
    waiting = next(row for row in rows if row['id'] != parent)
    assert waiting['events'][0]['id'] == 'first' and waiting['status'] == 'pending'
    restored = make_box(tmp_path, clock, validator=validate, transport=box.transport)
    restored.signal_events(original, since=0)
    assert len(packets(restored)) == 2
    unknown[0] = False
    clock[0] += 4
    restored.deliver_one()
    assert len(restored.transport.messages) == 2
    restored.signal_events(original, since=0)
    restored.deliver_one()
    assert len(restored.transport.messages) == 2


@pytest.mark.parametrize('status', ['retry', 'sending'])
def test_a_claimed_or_retry_packet_is_never_rewritten(tmp_path, status):
    clock = [1000.]
    box = make_box(tmp_path, clock)
    box.signal_events([notice('first')], since=0)
    with box._connect() as db:
        db.execute('UPDATE outbox SET status=?,attempts=?', (status, 1 if status == 'sending' else 0))
    before = deepcopy(packets(box)[0])
    box.signal_events([notice('second')], since=0)
    rows = packets(box)
    assert len(rows) == 2 and rows[0] == before


def test_arrival_during_validation_cannot_overwrite_the_inflight_payload(tmp_path):
    clock = [1000.]
    entered, release = threading.Event(), threading.Event()
    def validate(rows):
        entered.set()
        assert release.wait(5)
        return rows, []
    box = make_box(tmp_path, clock, validator=validate)
    box.signal_events([notice('first')], since=0)
    failures = []
    def deliver():
        try:
            box.deliver_one()
        except Exception as exc:
            failures.append(exc)
    thread = threading.Thread(target=deliver)
    thread.start()
    try:
        assert entered.wait(3)
        another = make_box(tmp_path, clock)
        another.signal_events([notice('second')], since=0)
        rows = packets(another)
        assert len(rows) == 2 and rows[0]['status'] == 'sending'
        assert [event['id'] for event in rows[0]['events']] == ['first']
    finally:
        release.set()
        thread.join(3)
    assert not failures and len(box.transport.messages) == 1
    assert 'first' in box.transport.messages[0] and 'second' not in box.transport.messages[0]


def test_independent_instances_atomically_merge_and_mark_duplicate_ingestion(tmp_path):
    clock = [1000.]
    first, second = make_box(tmp_path, clock), make_box(tmp_path, clock)
    with first._connect():
        pass
    barrier = threading.Barrier(2)
    failures = []
    def enqueue(box, indexes):
        try:
            barrier.wait(3)
            for index in indexes:
                box.signal_events([notice(str(index))], since=0)
        except Exception as exc:
            failures.append(exc)
    workers = [threading.Thread(target=enqueue, args=(box, range(20))) for box in (first, second)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(5)
    assert not failures and all(not worker.is_alive() for worker in workers)
    rows = packets(first)
    assert len(rows) == 4
    ids = [event['id'] for row in rows for event in row['events']]
    assert len(ids) == len(set(ids)) == 20


def test_append_and_event_marker_roll_back_together_before_crash_recovery(tmp_path, monkeypatch):
    clock = [1000.]
    box = make_box(tmp_path, clock)
    first, second = notice('first'), notice('second')
    box.signal_events([first], since=0)
    before = packets(box)
    connect = box._connect
    class Interrupted:
        def __init__(self, db):
            self.db = db
        def __getattr__(self, name):
            return getattr(self.db, name)
        def execute(self, sql, parameters=()):
            if sql.startswith('INSERT OR IGNORE INTO markers VALUES') and parameters[0] == signal_key(second):
                raise OSError('fixture crash before merged event marker commit')
            return self.db.execute(sql, parameters)
    @contextmanager
    def failing_connect(**kwargs):
        with connect(**kwargs) as db:
            yield Interrupted(db)
    with monkeypatch.context() as fault:
        fault.setattr(box, '_connect', failing_connect)
        with pytest.raises(OSError, match='fixture crash'):
            box.signal_events([second], since=0)
    assert packets(box) == before and not box.has(signal_key(second))
    restored = make_box(tmp_path, clock)
    restored.signal_events([first, second], since=0)
    assert len(packets(restored)) == 1
    assert [item['id'] for item in packets(restored)[0]['events']] == ['first', 'second']


def test_packet_merge_cannot_extend_an_old_members_individual_deadline(tmp_path):
    clock = [3000.]
    box = make_box(tmp_path, clock)
    old = notice('old', stamp=clock[0] - 1799.95)
    box.signal_events([old], since=0)
    clock[0] += .01
    young = notice('young', stamp=clock[0])
    box.signal_events([young], since=0)
    assert len(packets(box)) == 1 and len(packets(box)[0]['events']) == 2
    clock[0] += .1
    box.deliver_one()
    assert 'young' in box.transport.messages[0] and 'old' not in box.transport.messages[0]
    assert packets(box)[0]['expires'] == young['recorded_at'] + 1800


def test_cross_call_packet_sizes_respect_utf8_limits(tmp_path):
    clock = [1000.]
    box = make_box(tmp_path, clock)
    for index in range(20):
        box.signal_events([notice(str(index), name='很长的中文公司名称' * 10,
                                  code='LONG' + str(index) + 'X' * 72)], since=0)
    rows = packets(box)
    assert all(len(row['content'].encode('utf-8')) <= 3300 and len(row['events']) <= 5 for row in rows)
    assert sum(len(row['events']) for row in rows) == 20


def test_validation_and_transport_timings_accumulate_without_resetting_queue_age(tmp_path):
    clock = [1000.]
    attempts = [0]
    def validate(rows):
        attempts[0] += 1
        clock[0] += 2 if attempts[0] == 1 else 3
        return ([], rows) if attempts[0] == 1 else (rows, [])
    transport = Transport()
    original = transport.send
    def send(text):
        clock[0] += .25
        original(text)
    transport.send = send
    box = make_box(tmp_path, clock, validator=validate, transport=transport)
    box.signal_events([notice('measured')], since=0)
    box.deliver_one()
    clock[0] += 5
    box.deliver_one()
    with box._connect() as db:
        row = dict(db.execute('SELECT * FROM delivery_timings').fetchone())
    assert row['first_claimed'] == 1000 and row['last_claimed'] == 1007
    assert row['validation_attempts'] == 2 and row['validation_seconds'] == 5
    assert row['validation_finished'] - row['validation_started'] == 3
    assert row['request_finished'] - row['request_started'] == .25
    measures = box.latency_summary()['groups'][0]['measures']
    assert measures['validation']['average_seconds'] == 3
    assert measures['validation_total']['average_seconds'] == 5
    assert measures['queue_before_first_claim']['average_seconds'] == 0
    assert measures['request']['average_seconds'] == .25


def test_legacy_latency_read_does_not_create_a_timing_table_or_invent_zero_durations(tmp_path, monkeypatch):
    clock = [1000.]
    box = make_box(tmp_path, clock)
    box.signal_events([notice('legacy')], since=0)
    box.deliver_one()
    with box._connect() as db:
        db.execute('DROP TABLE delivery_timings')
    monkeypatch.setattr(box, '_connect', lambda **_: pytest.fail('read-only metrics attempted a schema write'))
    summary = box.latency_summary()
    assert not summary['error']
    measures = summary['groups'][0]['measures']
    assert not set(measures) & {'validation', 'validation_total', 'request', 'queue_before_first_claim'}
    with sqlite3.connect(box.path.as_uri() + '?mode=ro', uri=True) as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name='delivery_timings'").fetchone() is None


def test_diagnostics_expire_even_when_no_provider_request_is_made(tmp_path):
    clock = [1000.]
    box = make_box(tmp_path, clock, validator=lambda rows: ([], []))
    box.signal_events([notice('old')], since=0)
    box.deliver_one()
    with box._connect() as db:
        assert db.execute('SELECT COUNT(*) FROM delivery_timings').fetchone()[0] == 1
    clock[0] += 31 * 86400
    box.signal_events([notice('new', stamp=clock[0])], since=0)
    box.deliver_one()
    with box._connect() as db:
        assert db.execute('SELECT COUNT(*) FROM delivery_timings').fetchone()[0] == 1
        assert db.execute('SELECT first_claimed FROM delivery_timings').fetchone()[0] == clock[0]
    assert not box.transport.messages


@pytest.mark.parametrize('blocked_by', ['validity', 'inline_image'])
def test_waiting_without_an_http_request_does_not_idle_before_another_ready_notice(tmp_path, blocked_by):
    from tests.web.test_notification_dispatch_wake import capture_next_wait
    clock = [1000.]
    def validate(rows):
        return ([], rows) if blocked_by == 'validity' and rows[0]['id'] == 'waiting' else (rows, [])
    box = make_box(tmp_path, clock, validator=validate)
    waiting = notice('waiting', holding=True)
    if blocked_by == 'inline_image':
        box.charts = Mock()
        waiting.update(signal_type='consolidation_divergence', point_type='consolidation_up', stage='warning', inline_chart=True)
    box.signal_events([waiting], since=0)
    box.signal_events([notice('ready')], since=0)
    assert capture_next_wait(box) <= .1
    assert not box.transport.messages
    box.deliver_one()
    assert len(box.transport.messages) == 1 and 'ready' in box.transport.messages[0]
