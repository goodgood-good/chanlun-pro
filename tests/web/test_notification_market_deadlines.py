"""A post-close rebuild is a selection result, not a new real-time signal."""
from copy import deepcopy
from datetime import timedelta
import json

import pytest

from cl_app.services.dingtalk_notifications import DingTalkOutbox, _retain_recorded_times
from cl_app.services.notification_freshness import notification_deadline, notification_is_live
from cl_app.services.notification_handoff import pending_events
from tests.web.test_dingtalk_notifications import Transport, event
from tests.web.test_signal_monitor import setup
from tests.web.test_native_point_previews import _monitor_preview


@pytest.mark.parametrize('frequency', ['1m', '5m', '30m'])
@pytest.mark.parametrize('stage', ['approaching', 'confirmed'])
def test_late_discovery_cannot_start_a_fresh_delivery_window(tmp_path, frequency, stage):
    row = {**event(stage, stamp=8200), 'frequency':frequency,
           'signal_type':'native_point_preview' if stage == 'approaching' else None,
           'source_closed_at':1000, 'available_at':1000}
    box = DingTalkOutbox(tmp_path/'box.sqlite', transport=Transport(), now=lambda:8200)
    box.signal_events([row], since=0)
    assert not notification_is_live(row, 8200)
    assert not box.path.exists()
    assert pending_events({'dingtalk_enabled':True, 'notification_since':0, 'events':[row]}, 8200) == {}


def test_fresh_quotes_do_not_rejuvenate_an_old_confirmed_point(tmp_path):
    row = {**event('confirmed', stamp=8200), 'point_confirmed_at':1000}
    box = DingTalkOutbox(tmp_path/'box.sqlite', transport=Transport(), now=lambda:8200)
    box.signal_events([row], since=0)
    assert not box.path.exists()


def test_current_preview_does_not_expire_at_its_old_price_anchor():
    row = {**event('approaching', stamp=8200), 'anchor_at':1000,
           'signal_type':'native_point_preview', 'point_confirmed_at':None}
    assert notification_is_live(row, 8200)
    assert notification_deadline(row) == 10000


def test_refresh_and_restart_keep_the_original_market_deadline(tmp_path):
    row = {**event('confirmed', stamp=2500), 'source_closed_at':1000, 'available_at':1000}
    fresh = {**row, 'source_closed_at':2700, 'available_at':2700}
    merged, = _retain_recorded_times([fresh], [row])
    assert notification_deadline(merged) == 2800
    path, clock, transport = tmp_path/'box.sqlite', [2500], Transport()
    box = DingTalkOutbox(path, transport=transport, now=lambda:clock[0])
    box.signal_events([row], since=0)
    with box._connect() as db:
        assert db.execute('SELECT expires FROM outbox').fetchone()[0] == 2800
    clock[0] = 2800
    box = DingTalkOutbox(path, transport=transport, now=lambda:clock[0],
                        signal_validator=lambda rows:([fresh], []))
    box.deliver_one()
    assert not transport.messages
    assert box.snapshot()['history'][0]['status'] == 'expired'
    assert box.enqueue_chart(row, 'https://fixture.invalid/chart.png') is False


def test_legacy_queued_message_and_image_get_shortened_on_upgrade(tmp_path):
    path = tmp_path/'box.sqlite'
    row = {**event('confirmed', stamp=2500), 'source_closed_at':1000, 'available_at':1000}
    box = DingTalkOutbox(path, transport=Transport(), now=lambda:2500)
    box.signal_events([row], since=0)
    with box._connect() as db:
        db.execute("DELETE FROM delivery_settings WHERE key='market_event_deadlines_v1'")
        db.execute('UPDATE outbox SET expires=4300')
        db.execute("INSERT INTO outbox(id,kind,content,status,created,expires) VALUES ('legacy-chart','chart','{}','pending',2500,4300)")
        db.execute('INSERT INTO signal_payloads VALUES (?,?)', ('legacy-chart', json.dumps([row])))
    with box._connect() as db:
        assert [x[0] for x in db.execute('SELECT expires FROM outbox')] == [2800, 2800]


def test_post_close_preview_stays_visible_but_does_not_notify_then_recovers_on_new_bar(setup, monkeypatch):
    clock = setup[3]
    clock[0] = clock[0].replace(hour=17, minute=5)
    service, live, row, finish = _monitor_preview(setup, monkeypatch)
    first, = service._load()['events']
    assert first['source_closed_at'] < clock[0].timestamp() - 1800
    assert first['external_notifications'] is False
    assert first['notification_suppressed_reason'] == 'MARKET_EVENT_EXPIRED'
    assert first['id'] in service._load()['signals']
    assert service._validate_notification_events([first]) == ([], [])
    clock[0] += timedelta(days=1)
    clock[0] = clock[0].replace(hour=9, minute=36)
    cutoff = int(clock[0].replace(minute=35).timestamp())
    current = deepcopy(row)
    current['source_closed_at'] = current['point']['available_at'] = cutoff
    current['center_sequence']['known_at'] = cutoff
    service.check_now(); service.tick()
    live.rows = [current]; live.finish(cutoff); service.tick()
    latest = service._load()['events'][0]
    assert latest['id'] == first['id'] and latest.get('external_notifications', True)
    assert latest['first_warned_at'] == clock[0].timestamp()
    assert service._validate_notification_events([latest])[0]


def test_old_queued_preview_is_rejected_before_slow_validity_dependencies(setup, monkeypatch):
    service, _, _, _ = _monitor_preview(setup, monkeypatch)
    row = service._load()['events'][0]
    setup[3][0] += timedelta(hours=2)
    def unexpected(*args, **kwargs):
        pytest.fail('stale evidence must not request expensive revalidation')
    monkeypatch.setattr(service._notification_dependency_reads, 'read', unexpected)
    assert service._validate_notification_events([row]) == ([], [])


def test_market_break_does_not_turn_an_existing_warning_into_a_new_occurrence(setup, monkeypatch):
    service, _, row, finish = _monitor_preview(setup, monkeypatch)
    original, = service._load()['events']
    setup[3][0] += timedelta(hours=2)
    finish([row])
    assert service._load()['known'][original['id']]['notification_authorized'] is True
    assert service._load()['events'] == [original]
    assert service._load()['signals'][original['id']]['first_warned_at'] == original['first_warned_at']


@pytest.mark.parametrize('market', ['a', 'us', 'currency_spot'])
def test_current_market_event_remains_sendable_independent_of_market_session(tmp_path, market):
    row = {**event('confirmed'), 'market':market}
    box = DingTalkOutbox(tmp_path/'box.sqlite', transport=Transport(), now=lambda:1001)
    box.signal_events([row], since=0); box.deliver_one()
    assert len(box.transport.messages) == 1


def test_validator_cannot_refresh_market_clocks_across_original_deadline(tmp_path):
    clock = [2700]
    row = {**event('confirmed', stamp=2500), 'source_closed_at':1000, 'available_at':1000}
    def refresh(rows):
        clock[0] = 2800
        return [{**rows[0], 'source_closed_at':2800, 'available_at':2800}], []
    box = DingTalkOutbox(tmp_path/'box.sqlite', transport=Transport(), now=lambda:clock[0], signal_validator=refresh)
    box.signal_events([row], since=0); box.deliver_one()
    assert not box.transport.messages
    assert box.snapshot()['history'][0]['status'] == 'expired'


def test_mixed_batch_prunes_expired_market_event_and_sends_fresh_member(tmp_path):
    clock = [2500]
    old = {**event('confirmed', stamp=2500, identity='old'), 'code':'OLD',
           'source_closed_at':1000, 'available_at':1000}
    new = {**event('confirmed', stamp=2500, identity='new'), 'code':'NEW',
           'source_closed_at':2000, 'available_at':2000}
    box = DingTalkOutbox(tmp_path/'box.sqlite', transport=Transport(), now=lambda:clock[0])
    box.signal_events([old, new], since=0)
    clock[0] = 2800; box.deliver_one()
    assert len(box.transport.messages) == 1
    assert 'NEW' in box.transport.messages[0] and 'OLD' not in box.transport.messages[0]


def test_stale_chart_never_enters_render_queue(tmp_path, monkeypatch):
    monkeypatch.setattr('cl_app.services.notification_charts.public_base', lambda:'https://fixture.invalid')
    row = {**event('confirmed', stamp=8200), 'source_closed_at':1000, 'available_at':1000,
           'evidence':{'fixture':True}, 'evidence_run_id':'a'*32}
    box = DingTalkOutbox(tmp_path/'box.sqlite', transport=Transport(), now=lambda:8200, charts=True)
    box.charts.enqueue([row], 8200)
    assert box.charts.snapshot()['pending'] == 0
