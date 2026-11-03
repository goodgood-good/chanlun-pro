import json
import pytest

from cl_app.services.dingtalk_notifications import DingTalkOutbox
from cl_app.services.notification_validation import ValidatedEvents
from tests.web.test_dingtalk_notifications import Transport,event


def test_validation_rejection_retains_original_identity_and_exact_reason(tmp_path):
    original=event(stamp=1000)
    def reject(events):
        result=ValidatedEvents();result.reject(events[0],'SIGNAL_EXIT_CONFIRMED')
        return result,[]
    box=DingTalkOutbox(tmp_path/'box.sqlite',transport=Transport(),signal_validator=reject,now=lambda:1001)
    box.signal_events([original],since=0);box.deliver_one()
    row=box.snapshot()['history'][0]
    assert row['status']=='expired' and row['reason_code']=='SIGNAL_EXIT_CONFIRMED'
    assert '确认原信号退出' in row['error']
    with box._read_connect() as db:
        assert json.loads(db.execute('SELECT events FROM signal_payloads').fetchone()[0])[0]['id']==original['id']
        details=json.loads(db.execute('SELECT details FROM delivery_decisions').fetchone()[0])
        assert details[0]['reason']=='SIGNAL_EXIT_CONFIRMED'


def test_deadline_expiration_is_distinct_from_structure_rejection(tmp_path):
    clock=[1000];box=DingTalkOutbox(tmp_path/'box.sqlite',transport=Transport(),now=lambda:clock[0])
    box.signal_events([event(stamp=1000)],since=0)
    clock[0]=2800;box.deliver_one()
    row=box.snapshot()['history'][0]
    assert row['status']=='expired' and row['reason_code']=='MARKET_EVENT_EXPIRED'


def test_partial_delivery_preserves_each_rejected_signal_reason(tmp_path):
    rows = [event(identity='accepted', stamp=1000), event(identity='rejected', stamp=1000)]
    def validate(events):
        result = ValidatedEvents()
        result.append(events[0])
        result.reject(events[1], 'SIGNAL_EXIT_CONFIRMED')
        return result, []
    transport = Transport()
    box = DingTalkOutbox(tmp_path / 'box.sqlite', transport=transport, signal_validator=validate, now=lambda: 1001)
    box.signal_events(rows, since=0)
    box.deliver_one()
    assert len(transport.messages) == 1
    with box._read_connect() as db:
        receipt = db.execute('SELECT status FROM outbox').fetchone()
        assert receipt['status'] == 'sent'
        decision = db.execute('SELECT details FROM delivery_decisions').fetchone()
        assert decision is not None
        details = json.loads(decision['details'])
        assert [(row['id'], row['reason']) for row in details] == [('rejected', 'SIGNAL_EXIT_CONFIRMED')]
    history = box.snapshot()['history'][0]
    assert history['reason_code'] == 'PARTIAL_RECHECK_REJECTIONS' and history['rejection_count'] == 1
    assert '部分信号' in history['decision_message']


@pytest.mark.parametrize('finish', ['sent', 'expired'])
def test_partial_rejection_survives_wait_retry_and_restart(tmp_path, finish):
    clock = [1001]
    def validate(events):
        result = ValidatedEvents()
        result.reject(events[0], 'NOTIFICATION_POLICY_DISABLED')
        return result, events[1:]
    path = tmp_path / 'box.sqlite'
    transport = Transport()
    box = DingTalkOutbox(path, transport=transport, signal_validator=validate, now=lambda: clock[0])
    box.signal_events([event(identity='rejected'), event(identity='waiting')], since=0)
    box.deliver_one()
    assert not transport.messages
    clock[0] = 1007 if finish == 'sent' else 2800
    box = DingTalkOutbox(path, transport=transport, signal_validator=lambda rows: (rows, []), now=lambda: clock[0])
    box.deliver_one()
    with box._read_connect() as db:
        assert db.execute('SELECT status FROM outbox').fetchone()[0] == finish
        details = json.loads(db.execute('SELECT details FROM delivery_decisions').fetchone()[0])
    expected = {('rejected', 'NOTIFICATION_POLICY_DISABLED')}
    if finish == 'expired':
        expected.add(('waiting', 'MARKET_EVENT_EXPIRED'))
    assert {(row['id'], row['reason']) for row in details} == expected
    assert len(transport.messages) == (1 if finish == 'sent' else 0)


@pytest.mark.parametrize('during_validation', [False, True])
def test_partial_clock_expiration_keeps_original_identity(during_validation, tmp_path):
    clock = [1001]
    def validate(rows):
        clock[0] = 2800
        return rows, []
    transport = Transport()
    box = DingTalkOutbox(tmp_path / 'box.sqlite', transport=transport,
                        signal_validator=validate, now=lambda: clock[0])
    box.signal_events([event(identity='old', stamp=1000), event(identity='fresh', stamp=1001)], since=0)
    clock[0] = 2799 if during_validation else 2800
    box.deliver_one()
    assert len(transport.messages) == 1
    with box._read_connect() as db:
        details = json.loads(db.execute('SELECT details FROM delivery_decisions').fetchone()[0])
        delivered = json.loads(db.execute('SELECT events FROM signal_payloads').fetchone()[0])
    assert [row['id'] for row in delivered] == ['fresh']
    assert [(row['id'], row['reason']) for row in details] == [('old', 'MARKET_EVENT_EXPIRED')]
    assert details[0]['recorded_at'] == details[0]['source_closed_at'] == 1000


def test_repeated_rejection_evidence_is_deduplicated(tmp_path):
    from cl_app.services.notification_decisions import clock_rejections, record
    box = DingTalkOutbox(tmp_path / 'box.sqlite', transport=Transport(), now=lambda: 1001)
    box.signal_events([event()], since=0)
    with box._connect() as db:
        identity = db.execute('SELECT id FROM outbox').fetchone()[0]
        for _ in range(3):
            record(db, identity, 2800, 'MARKET_EVENT_EXPIRED', clock_rejections([event()], 2800))
        assert len(json.loads(db.execute('SELECT details FROM delivery_decisions').fetchone()[0])) == 1


def test_failed_validation_processing_does_not_commit_a_partial_rejection(tmp_path, monkeypatch):
    def validate(events):
        result = ValidatedEvents()
        result.append(events[0])
        result.reject(events[1], 'SIGNAL_EXIT_CONFIRMED')
        return result, []
    box = DingTalkOutbox(tmp_path / 'box.sqlite', transport=Transport(),
                        signal_validator=validate, now=lambda: 1001)
    box.signal_events([event(identity='one'), event(identity='two')], since=0)
    monkeypatch.setattr('cl_app.services.dingtalk_notifications._retain_recorded_times',
                        lambda *args: (_ for _ in ()).throw(ValueError('incomplete validator response')))
    box.deliver_one()
    with box._read_connect() as db:
        assert db.execute('SELECT status FROM outbox').fetchone()[0] == 'retry'
        assert len(json.loads(db.execute('SELECT events FROM signal_payloads').fetchone()[0])) == 2
        assert db.execute('SELECT COUNT(*) FROM delivery_decisions').fetchone()[0] == 0
