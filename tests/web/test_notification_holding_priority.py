"""Priority stays independent of freshness authorization and HTTP pacing."""
from copy import deepcopy
import json
import sqlite3
import threading

import pytest

from cl_app.services.dingtalk_notifications import DingTalkOutbox
from tests.web.test_dingtalk_notifications import Transport, event
from tests.web.test_notification_charts import ImageTransport, PNG, image_event


def notice(identity, *, holding=False, stamp=1000., code=None):
    return {**event('confirmed', identity=identity, stamp=stamp), 'holding': holding,
            'code': code or ('SH.600088' if holding else 'SH.600099'), 'name': identity}


def test_due_holding_retry_regains_priority_over_older_ready_ordinary_work(tmp_path):
    clock, unknown = [1000.], [True]
    transport = Transport()
    def validate(rows):
        return ([], rows) if unknown[0] and rows[0]['holding'] else (rows, [])
    box = DingTalkOutbox(tmp_path/'outbox.sqlite', transport=transport, now=lambda: clock[0], signal_validator=validate)
    box.signal_events([notice('holding-first', holding=True)], since=0)
    box.deliver_one()
    assert transport.messages == []
    clock[0] = 1001
    box.signal_events([notice('ordinary-earlier-ready', stamp=1001)], since=0)
    unknown[0] = False
    clock[0] = 1005
    box.deliver_one()
    assert len(transport.messages) == 1 and 'holding-first' in transport.messages[0]
    clock[0] = 1009
    box.deliver_one()
    assert len(transport.messages) == 2 and 'ordinary-earlier-ready' in transport.messages[1]


def test_not_due_unknown_holding_yields_to_ready_work_without_authorizing_itself(tmp_path):
    clock, checked = [1000.], []
    transport = Transport()
    def validate(rows):
        checked.append(rows[0]['id'])
        return ([], rows) if rows[0]['holding'] else (rows, [])
    box = DingTalkOutbox(tmp_path/'outbox.sqlite', transport=transport, now=lambda: clock[0], signal_validator=validate)
    box.signal_events([notice('unknown-holding', holding=True)], since=0)
    box.deliver_one()
    clock[0] = 1001
    box.signal_events([notice('ready-ordinary', stamp=1001)], since=0)
    box.deliver_one()
    assert checked == ['unknown-holding', 'ready-ordinary']
    assert len(transport.messages) == 1 and 'ready-ordinary' in transport.messages[0]
    with box._read_connect() as db:
        pending = db.execute("SELECT status,expires FROM outbox WHERE status='retry'").fetchone()
    assert tuple(pending) == ('retry', 2800)


def test_ordinary_validation_yields_before_http_to_holding_enqueued_while_it_was_running(tmp_path):
    clock, added = [1000.], [False]
    transport = Transport()
    def validate(rows):
        if rows[0]['id'] == 'ordinary' and not added[0]:
            added[0] = True
            box.signal_events([notice('new-holding', holding=True, stamp=clock[0])], since=0)
        return rows, []
    box = DingTalkOutbox(tmp_path/'outbox.sqlite', transport=transport, now=lambda: clock[0], signal_validator=validate)
    ordinary = notice('ordinary')
    box.signal_events([ordinary], since=0)
    box.deliver_one()
    assert transport.messages == [], 'ordinary validation must not consume the HTTP slot ahead of new holding work'
    box.deliver_one()
    assert len(transport.messages) == 1 and 'new-holding' in transport.messages[0]
    clock[0] = 1003.99
    box.deliver_one()
    assert len(transport.messages) == 1
    clock[0] = 1004
    box.deliver_one()
    assert len(transport.messages) == 2 and 'ordinary' in transport.messages[1]
    box.signal_events([ordinary], since=0)
    assert box.snapshot()['pending'] == 0


def test_current_membership_overrides_stale_raw_priority_and_selects_the_holding_validation_lane(tmp_path):
    clock, transport = [1000.], Transport()
    seed = DingTalkOutbox(tmp_path/'outbox.sqlite', transport=transport, now=lambda: clock[0])
    seed.signal_events([notice('former', holding=True, code='SH.600001')], since=0)
    clock[0] += 1
    seed.signal_events([notice('current', code='SH.600002', stamp=clock[0])], since=0)
    checked = []
    def validate(rows):
        checked.append((rows[0]['id'], rows[0]['holding']))
        return rows, []
    box = DingTalkOutbox(seed.path, transport=transport, now=lambda: clock[0], signal_validator=validate,
                        holding_reader=lambda: {'a:SH.600002'})
    box.deliver_one()
    assert checked == [('current', True)]
    assert len(transport.messages) == 1 and 'current' in transport.messages[0]
    clock[0] += 4
    box.deliver_one()
    assert checked[-1] == ('former', False)
    assert len(transport.messages) == 2 and 'former' in transport.messages[1]


def test_legacy_event_without_market_uses_the_same_default_a_holding_identity(tmp_path):
    checked, transport = [], Transport()
    def validate(rows):
        checked.extend(deepcopy(rows))
        return rows, []
    box = DingTalkOutbox(tmp_path/'outbox.sqlite', transport=transport, now=lambda:1000., signal_validator=validate,
                        holding_reader=lambda:{'a:SH.600002'})
    legacy = notice('legacy-holding', code='SH.600002')
    del legacy['market']
    original = deepcopy(legacy)
    box.signal_events([notice('ordinary'), legacy], since=0)
    box.deliver_one()
    assert checked[0]['id'] == 'legacy-holding' and checked[0]['holding'] is True
    assert legacy == original, 'priority hint must not mutate the producers event'
    assert len(transport.messages) == 1 and 'legacy-holding' in transport.messages[0]


@pytest.mark.parametrize('reader', [lambda: None, lambda: (_ for _ in ()).throw(OSError('unavailable'))])
def test_unknown_membership_retains_hint_but_never_authorizes_a_removed_holding_risk(tmp_path, reader):
    transport, checked = Transport(), []
    risk = {**notice('removed-risk', holding=True), 'signal_type':'holding_risk', 'point_type':'risk_stop',
            'stage':'triggered', 'reference_point_type':'3buy', 'stop_price':11., 'trigger_price':10., 'latest_close':10.5}
    def validate(rows):
        checked.append(rows[0]['id'])
        return ([], []) if rows[0]['id'] == 'removed-risk' else (rows, [])
    box = DingTalkOutbox(tmp_path/'outbox.sqlite', transport=transport, now=lambda:1000.,
                        signal_validator=validate, holding_reader=reader)
    box.signal_events([notice('ordinary'), risk], since=0)
    box.deliver_one()
    assert checked == ['removed-risk'] and not transport.messages
    box.deliver_one()
    assert checked == ['removed-risk', 'ordinary'] and len(transport.messages) == 1
    assert 'removed-risk' not in transport.messages[0]


def test_membership_added_during_ordinary_validation_is_seen_before_http_reservation(tmp_path):
    held, added, checked = set(), [False], []
    transport = Transport()
    def validate(rows):
        checked.append(rows[0]['id'])
        if not added[0]:
            added[0] = True
            held.add('a:SH.600002')
            box.signal_events([notice('joined-holding', code='SH.600002')], since=0)
        return rows, []
    box = DingTalkOutbox(tmp_path/'outbox.sqlite', transport=transport, now=lambda:1000.,
                        signal_validator=validate, holding_reader=lambda:held)
    box.signal_events([notice('ordinary')], since=0)
    box.deliver_one()
    assert not transport.messages
    box.deliver_one()
    assert checked == ['ordinary', 'joined-holding']
    assert len(transport.messages) == 1 and 'joined-holding' in transport.messages[0]


def test_independent_producer_can_publish_holding_while_dispatch_validation_is_running(tmp_path):
    entered, release = threading.Event(), threading.Event()
    clock, transport, failures = [1000.], Transport(), []
    def validate(rows):
        entered.set()
        assert release.wait(5)
        return rows, []
    box = DingTalkOutbox(tmp_path/'outbox.sqlite', transport=transport, now=lambda:clock[0], signal_validator=validate)
    box.signal_events([notice('ordinary')], since=0)
    def dispatch():
        try:
            box.deliver_one()
        except Exception as exc:
            failures.append(exc)
    worker = threading.Thread(target=dispatch)
    worker.start()
    try:
        assert entered.wait(3)
        peer = DingTalkOutbox(box.path, transport=transport, now=lambda:clock[0], signal_validator=lambda rows:(rows, []))
        peer.signal_events([notice('peer-holding', holding=True)], since=0)
        peer.deliver_one()
        assert not transport.messages, 'shared dispatch lock must still prevent overlapping sends'
        release.set();worker.join(3)
        assert not worker.is_alive() and not failures and not transport.messages
        peer.deliver_one()
        assert len(transport.messages) == 1 and 'peer-holding' in transport.messages[0]
        clock[0] += 4
        peer.deliver_one()
        assert len(transport.messages) == 2 and 'ordinary' in transport.messages[1]
    finally:
        release.set();worker.join(5)


def test_priority_snapshots_are_read_outside_outbox_and_sqlite_locks(tmp_path):
    transport, reads = Transport(), []
    box = DingTalkOutbox(tmp_path/'outbox.sqlite', transport=transport, now=lambda:1000., signal_validator=lambda rows:(rows, []))
    with box._connect():
        pass
    def reader():
        assert box._lock.acquire(blocking=False), 'holding state reader called inside the outbox lock'
        box._lock.release()
        with sqlite3.connect(box.path, timeout=.05) as db:
            db.execute('BEGIN IMMEDIATE')
            db.rollback()
        reads.append(1)
        return {'a:SH.600088'}
    box.holding_reader = reader
    box.signal_events([notice('held', holding=True)], since=0)
    box.deliver_one()
    assert len(reads) == 3 and len(transport.messages) == 1


def test_bound_risk_precedes_holding_buy_and_sell_without_inventing_a_side_priority(tmp_path):
    clock, transport = [1000.], Transport()
    box = DingTalkOutbox(tmp_path/'outbox.sqlite', transport=transport, now=lambda:clock[0], signal_validator=lambda rows:(rows, []))
    ordinary = notice('ordinary')
    held_buy = notice('held-buy', holding=True)
    held_sell = {**notice('held-sell', holding=True), 'point_type':'3sell'}
    risk = {**notice('bound-risk', holding=True), 'signal_type':'holding_risk', 'point_type':'risk_stop',
            'stage':'triggered', 'reference_point_type':'3buy', 'stop_price':11., 'trigger_price':10., 'latest_close':10.5}
    for row in (ordinary, held_buy, held_sell, risk):
        box.signal_events([row], since=0)
    for _ in range(4):
        box.deliver_one();clock[0] += 4
    assert [next(name for name in ('ordinary','held-buy','held-sell','bound-risk') if name in text)
            for text in transport.messages] == ['bound-risk','held-buy','held-sell','ordinary']


def test_expired_higher_priority_and_not_due_retry_do_not_defer_ready_ordinary(tmp_path):
    clock, transport = [1000.], Transport()
    box = DingTalkOutbox(tmp_path/'outbox.sqlite', transport=transport, now=lambda:clock[0], signal_validator=lambda rows:(rows, []))
    box.signal_events([notice('expired-held', holding=True)], since=0)
    clock[0] = 2800
    box.signal_events([notice('ordinary', stamp=2800)], since=0)
    box.deliver_one()
    assert len(transport.messages) == 1 and 'ordinary' in transport.messages[0]
    with box._read_connect() as db:
        assert db.execute("SELECT COUNT(*) FROM outbox WHERE status='expired'").fetchone()[0] == 1


@pytest.mark.parametrize('lane', ['initial', 'history'])
def test_chart_due_holding_keeps_its_tier_after_waiting_and_uses_current_membership(tmp_path, monkeypatch, lane):
    from chanlun import config
    monkeypatch.setattr(config, 'ALERT_CHART_PUBLIC_BASE_URL', 'https://example.test')
    clock, calls, checked = [1000.], [], []
    held = {'a:SH.600002'}
    def validate(rows):
        checked.append((rows[0]['id'], rows[0]['holding']))
        return rows, []
    box = DingTalkOutbox(tmp_path/'outbox.sqlite', transport=ImageTransport(), now=lambda:clock[0],
                        signal_validator=validate, charts=True, holding_reader=lambda:held)
    box.charts.check_public = False
    box.charts.renderer = lambda row, _: calls.append(row['id']) or PNG
    rows = [{**image_event(identity='ordinary'), 'code':'SH.600001', 'holding':False},
            {**image_event(identity='current-held'), 'code':'SH.600002', 'holding':False}]
    box.charts.enqueue(rows, 1000.)
    with box.charts._db() as db:
        db.execute("UPDATE jobs SET error='等待信号有效性复核' WHERE json_extract(event,'$.id')='current-held'")
        if lane == 'history':
            db.execute("UPDATE jobs SET initial_image_hash='old-image'")
    assert box.charts.process_one(lane)
    assert calls == ['current-held']
    assert checked == ([('current-held', True)] if lane == 'initial' else [])
    with box.charts._read_db() as db:
        saved = json.loads(db.execute("SELECT event FROM jobs WHERE json_extract(event,'$.id')='current-held'").fetchone()[0])
    assert saved == rows[1], 'priority metadata must not rewrite frozen chart evidence'
