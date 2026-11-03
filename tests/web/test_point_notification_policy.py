"""Notification preferences never redefine signals or trust stale holding hints."""

from copy import deepcopy
from unittest.mock import Mock

import pytest

from cl_app.services import signal_monitor
from cl_app.services.notification_policy import (
    default_point_notifications, normalize_point_notifications,
    next_point_notification_since, point_notification_decision,
)
from cl_app.services.notification_validation import state_epoch
from tests.web.test_dingtalk_notifications import event
from tests.web.test_monitor_control_projection import control
from tests.web.test_notification_state_freshness import ready_event
from tests.web.test_signal_monitor import setup


@pytest.mark.parametrize('point', ['1buy', '2buy', '3buy'])
@pytest.mark.parametrize('stage', ['approaching', 'confirmed', 'invalidated'])
def test_ordinary_5m_buy_lifecycle_updates_remain_enabled(point, stage):
    row = {**event(stage), 'frequency': '5m', 'point_type': point}
    assert point_notification_decision({}, row, set()) == 'allow'
    for frequency in ('1m', '30m'):
        assert point_notification_decision({}, {**row, 'frequency': frequency}, set()) == 'deny'


def test_grouped_sell_preferences_migrate_once_without_changing_buy_or_holding_windows(setup):
    service, manual, live, clock = setup
    state = service._load()
    saved = {**default_point_notifications(), 'others': ['1buy', '2buy', '3buy', '1sell', '2sell', '3sell']}
    windows = {'others:5m:1buy': 10, 'holdings:1m:3sell': 20, 'others:5m:1sell': 30}
    state.update(point_notifications=saved, point_notification_since=windows, point_notification_version=11)
    service._save()
    restored = signal_monitor.SignalMonitor(service.root, screening=manual, monitor=live, now=service.now)._load()
    assert restored['point_notifications'] == default_point_notifications()
    assert restored['point_notification_since'] == windows
    assert restored['point_notification_version'] == 12
    again = signal_monitor.SignalMonitor(service.root, screening=manual, monitor=live, now=service.now)._load()
    assert again['point_notification_version'] == 12
    assert again['point_notifications'] == restored['point_notifications']
    with pytest.raises(ValueError):
        normalize_point_notifications(saved)


@pytest.mark.parametrize('kind', ['text', 'chart'])
def test_previously_queued_ordinary_5m_sell_is_retired_without_sending(ready_event, monkeypatch, tmp_path, kind):
    from cl_app.services.dingtalk_notifications import DingTalkOutbox
    service, _, original = ready_event
    row = {**original, 'point_type': '3sell', 'holding': False}
    service._load()['signals'][row['id']] = dict(row)
    service._load()['seed']['settings']['point_types'].append('3sell')
    monkeypatch.setattr(signal_monitor, 'holding_symbols', lambda: [])
    transport = Mock()
    box = DingTalkOutbox(tmp_path / 'ordinary_sells.sqlite', transport=transport,
        now=lambda: service.now().timestamp(), signal_validator=service._validate_notification_events)
    if kind == 'text':
        box.signal_events([row], since=0)
    else:
        box.enqueue_chart(row, 'https://example.invalid/fixture.png')
    box.deliver_one()
    with box._connect() as db:
        assert db.execute('SELECT status FROM outbox').fetchone()[0] == 'expired'
    transport.send.assert_not_called()
    transport.send_markdown.assert_not_called()


@pytest.mark.parametrize('point', ['1sell', '2sell', '3sell'])
@pytest.mark.parametrize('hint', [False, True])
def test_nontrade_period_sell_requires_actual_current_holding(point, hint):
    row = {**event('confirmed'), 'frequency': '1m', 'point_type': point, 'holding': hint}
    assert point_notification_decision({}, row, set()) == 'deny'
    assert point_notification_decision({}, row, None) == 'pending'
    assert point_notification_decision({}, row, {'a:SH.600088'}) == 'allow'


def test_unknown_membership_only_allows_buy_when_both_scopes_allow_it():
    row = event('confirmed')
    assert point_notification_decision({}, row, None) == 'allow'
    policy = default_point_notifications()
    policy['others'] = []
    state = {'point_notifications': policy}
    assert point_notification_decision(state, row, None) == 'pending'
    assert point_notification_decision(state, row, set()) == 'deny'
    assert point_notification_decision(state, row, {'a:SH.600088'}) == 'allow'
    policy['holdings'] = []
    assert point_notification_decision(state, row, None) == 'deny'


@pytest.mark.parametrize('field', ['frequencies', 'other_frequencies', 'holdings', 'others'])
def test_empty_selection_is_disabled_and_does_not_change_special_alerts(field):
    policy = default_point_notifications()
    policy[field] = []
    state = {'point_notifications': policy}
    holding = {'a:SH.600088'} if field in {'frequencies', 'holdings'} else set()
    assert point_notification_decision(state, event('confirmed'), holding) == 'deny'
    for special in ['holding_risk', 'consolidation_divergence']:
        assert point_notification_decision(state, {**event('confirmed'), 'signal_type': special}, holding) == 'allow'


def test_reenabled_combination_starts_now_without_invalidating_other_points():
    original = default_point_notifications()
    disabled = {**original, 'holdings': ['1buy', '2buy', '1sell', '2sell', '3sell']}
    since = next_point_notification_since(disabled, original, {}, 1001)
    state = {'point_notifications': original, 'point_notification_since': since}
    row = event('confirmed')
    assert point_notification_decision(state, row, {'a:SH.600088'}) == 'deny'
    assert point_notification_decision(state, row, set()) == 'allow'
    assert point_notification_decision(state, {**row, 'recorded_at': 1001}, {'a:SH.600088'}) == 'allow'
    assert point_notification_decision(state, {**row, 'point_type': '2buy'}, {'a:SH.600088'}) == 'allow'
    # The boundary persists when a later unrelated setting changes.
    assert next_point_notification_since(original, original, since, 1010) == since


@pytest.mark.parametrize('body', [None, {}, [], {'enabled': True},
    {'frequencies': [], 'holdings': [], 'others': ['4sell']},
    {'frequencies': ['1m', '1m'], 'holdings': [], 'others': []},
    {'frequencies': '5m', 'holdings': [], 'others': []},
    {'frequencies': ['60m'], 'holdings': [], 'others': []},
    {'frequencies': [], 'holdings': [None], 'others': []}])
def test_malformed_policy_is_rejected_without_silent_defaults(body):
    with pytest.raises(ValueError):
        normalize_point_notifications(body)


def test_policy_control_persists_independently_and_projects_once(control):
    client, service, jobs, notifications, calls = control
    original = deepcopy(service._load())
    policy = {'frequencies': ['5m', '30m'], 'holdings': ['3buy', '3sell'], 'others': []}
    reply = client.post('/monitor/notification-policy?view=paged&signals_page=2&signals_size=25', json=policy)
    assert reply.status_code == 200
    body = reply.get_json()
    assert body['point_notifications'] == normalize_point_notifications(policy) and body['point_notification_version'] == 1
    assert 'point_notification_since' not in body and len(calls) == 1
    assert body['pagination']['signals']['page'] == 2
    for key in ['enabled', 'dingtalk_enabled', 'settings', 'seed', 'signals', 'known', 'holding_risk_plans']:
        assert service._load()[key] == original[key]
    notifications.invalidate_point_notification_policy.assert_called_once()
    notifications.begin_window.assert_not_called()
    jobs.cancel.assert_not_called()
    fresh = signal_monitor.SignalMonitor(service.root, screening=jobs, monitor=jobs, notifications=notifications, now=service.now)
    assert fresh._load()['point_notifications'] == normalize_point_notifications(policy)
    assert client.post('/monitor/notification-policy', json=policy).status_code == 200
    notifications.invalidate_point_notification_policy.assert_called_once()


def test_invalid_page_and_policy_do_not_mutate(control):
    client, service, _, notifications, calls = control
    before = deepcopy(service._load())
    assert client.post('/monitor/notification-policy?view=paged&signals_size=5000', json=default_point_notifications()).status_code == 400
    assert client.post('/monitor/notification-policy', json={'frequencies': [], 'other_frequencies': ['1m'], 'holdings': [], 'others': ['3sell']}).status_code == 400
    assert service._load() == before and calls == []
    notifications.invalidate_point_notification_policy.assert_not_called()


def test_save_failure_restores_in_memory_policy(control, monkeypatch):
    client, service, _, _, _ = control
    before = deepcopy(service._load())
    monkeypatch.setattr(service, '_save', Mock(side_effect=OSError('fixture disk unavailable')))
    response = client.post('/monitor/notification-policy', json={'frequencies': [], 'holdings': [], 'others': []})
    assert response.status_code == 409
    assert service._load() == before


def test_policy_control_requires_login_and_csrf(monkeypatch):
    from cl_app import create_app
    fake = Mock()
    monkeypatch.setattr('cl_app.blueprints.monitor.service', fake)
    app = create_app(test_config={'TESTING': True, 'VALIDATE_WEB_SECURITY': False})
    client = app.test_client()
    assert client.post('/monitor/notification-policy', json=default_point_notifications()).status_code in (400, 401)
    app.config['LOGIN_DISABLED'] = True
    assert client.post('/monitor/notification-policy', json=default_point_notifications()).status_code == 400
    fake.configure_point_notifications.assert_not_called()


def test_real_validator_requires_actual_holdings_for_a_5m_sell(ready_event, monkeypatch):
    service, _, original = ready_event
    row = {**original, 'point_type': '3sell', 'holding': False}
    state = service._load()
    state['signals'][row['id']] = dict(row)
    state['seed']['settings']['point_types'].append('3sell')
    reader = Mock(return_value=[])
    monkeypatch.setattr(signal_monitor, 'holding_symbols', reader)
    assert service._validate_notification_events([row]) == ([], [])
    reader.assert_called_once()
    reader.return_value = [{'market': 'a', 'code': 'SH.600088'}]
    valid, pending = service._validate_notification_events([row])
    assert not pending and len(valid) == 1 and valid[0]['holding'] is True
    reader.side_effect = RuntimeError('holding source unavailable')
    assert service._validate_notification_events([row]) == ([], [row])


def test_policy_change_during_dependency_read_cannot_use_old_authorization(ready_event, monkeypatch):
    from threading import Event
    service, live, row = ready_event
    epoch = state_epoch(service._load(), 0, [row])
    original = live.status
    committed = Event()
    # A status hook mutates policy only to reproduce this race. Do not have
    # configure's response projection recursively call the same status hook.
    monkeypatch.setattr(service, '_control_snapshot', lambda page_options: {})

    def changed():
        service.configure_point_notifications({'frequencies': [], 'holdings': [], 'others': []})
        committed.set()
        return original()

    monkeypatch.setattr(live, 'status', changed)
    assert service._validate_notification_events([row])[0] == []
    assert committed.wait(2), 'the bounded dependency read did not finish its policy update'
    assert state_epoch(service._load(), 0, [row]) != epoch
    assert service._validate_notification_events([row]) == ([], [])
