"""Cold minute discovery preserves membership, notices and verified lifecycles."""
from copy import deepcopy
from datetime import datetime, timedelta
from unittest.mock import Mock

import pytest

from chanlun.screening.markets import closed_bar_cutoff
from chanlun.screening.rules import CN, POINT_TYPES
from cl_app.services.dingtalk_notifications import DingTalkOutbox
from cl_app.services.signal_monitor import SignalMonitor, _signal, signal_status_key
from tests.web.test_signal_monitor import Jobs, signal


@pytest.fixture
def lifecycle(tmp_path, monkeypatch):
    clock = [datetime(2026, 9, 21, 10, 1, 10, tzinfo=CN)]
    manual, live = Jobs(tmp_path / 'manual'), Jobs(tmp_path / 'runs')
    manual.state.update(current_source_revision='scope-test', source_current=True)
    live.state.update(current_source_revision='scope-test', source_current=True)
    service = SignalMonitor(tmp_path, screening=manual, monitor=live, now=lambda: clock[0])
    monkeypatch.setattr(service, 'start_runtime', lambda: None)
    monkeypatch.setattr('cl_app.services.signal_monitor.watchlist_symbols', lambda: [])
    monkeypatch.setattr('cl_app.services.signal_monitor.holding_symbols', lambda: [])
    symbol = {'market': 'a', 'code': 'SH.600000', 'name': 'fixture'}
    key = 'a:SH.600000'
    previous = closed_bar_cutoff('a', symbol['code'], clock[0] - timedelta(minutes=1), '1m')
    state = service._load()
    state.update(enabled=True, force=False, next_check_at=clock[0].timestamp() + 60,
                 seed={'run_id': 'seed', 'codes': [key], 'symbols': [symbol], 'screening_symbols': [symbol],
                       'settings': {'frequencies': ['1m'], 'point_types': list(POINT_TYPES), 'workers': 2}},
                 notification_since=0, notification_symbol_since={key: previous - 3600},
                 attempted_cutoffs={key: previous},
                 attempted_monitor_periods={key + ':1m': {'cutoff': previous, 'revision': 'scope-test'}},
                 selection_checks={key + ':1m': {'symbol': key, 'cutoff': previous, 'revision': 'scope-test',
                     'error': False, 'identities': [], 'checked_at': previous, 'input_fingerprint': 'a' * 64}})
    committed = live.committed_results
    def with_fingerprint(*args, **kwargs):
        result = committed(*args, **kwargs)
        result['verified_periods'] = [{**period, 'input_fingerprint': 'b' * 64}
                                      for period in result['verified_periods']]
        return result
    live.committed_results = with_fingerprint
    service._save()
    return service, manual, live, clock, key, previous


def queued_event(lifecycle, point_type='3buy'):
    _service, _manual, _live, clock, _key, previous = lifecycle
    row = signal('SH.600000')
    row['frequency'] = '1m'
    row['source_closed_at'] = previous
    row['point'].update(point_type=point_type, available_at=previous, anchor_at=previous - 60, anchor_price=10.)
    return {**_signal(row), 'recorded_at': clock[0].timestamp() - 10,
            'change': 'discovered', 'external_notifications': True}


@pytest.mark.parametrize('kind', ['text', 'chart'])
@pytest.mark.parametrize('check_state', ['missing', 'stale', 'failed', 'current_positive'])
def test_queued_minute_notice_with_empty_current_state_waits_for_real_recheck(lifecycle, tmp_path, kind, check_state):
    service, _, _, clock, key, _ = lifecycle
    event = queued_event(lifecycle)
    state = service._load()
    if check_state == 'missing':
        state['selection_checks'].clear()
    elif check_state == 'failed':
        state['selection_checks'][key + ':1m']['error'] = True
    elif check_state == 'current_positive':
        state['selection_checks'][key + ':1m'].update(
            cutoff=closed_bar_cutoff('a', event['code'], clock[0], '1m'), identities=[event['id']])
    transport = Mock()
    box = DingTalkOutbox(tmp_path / 'local_outbox.sqlite', transport=transport,
                        now=lambda: clock[0].timestamp(), signal_validator=service._validate_notification_events)
    if kind == 'text':
        box.signal_events([event], since=0)
    else:
        box.enqueue_chart(event, 'https://example.invalid/immutable.png')
    box.deliver_one()
    with box._connect() as db:
        receipt = dict(db.execute('SELECT status,expires,error FROM outbox').fetchone())
    assert receipt['status'] == 'retry' and receipt['error'] == '等待信号有效性复核'
    assert receipt['expires'] == event['recorded_at'] + 1800
    assert (key, '1m') in service._notification_rechecks
    assert service._pending_notification_symbols(state, clock[0].timestamp()) == {key}
    assert state['signals'] == {}  # A queued event must never fabricate a live point.
    transport.send.assert_not_called()
    transport.send_markdown.assert_not_called()

    cutoff = closed_bar_cutoff('a', event['code'], clock[0], '1m')
    state['signals'][event['id']] = {**event, 'source_closed_at': cutoff}
    state['selection_checks'][key + ':1m'] = {'symbol': key, 'cutoff': cutoff,
        'revision': 'scope-test', 'error': False, 'identities': [event['id']]}
    clock[0] += timedelta(seconds=5)
    box.deliver_one()
    with box._connect() as db:
        assert db.execute('SELECT status,expires FROM outbox').fetchone()[:] == ('sent', event['recorded_at'] + 1800)
    assert (transport.send.call_count, transport.send_markdown.call_count) == ((1, 0) if kind == 'text' else (0, 1))
    assert service._pending_notification_symbols(state, clock[0].timestamp()) == set()


@pytest.mark.parametrize('kind', ['text', 'chart'])
def test_recheck_with_a_current_negative_retires_the_queued_notice(lifecycle, tmp_path, kind):
    service, _, _, clock, key, _ = lifecycle
    event = queued_event(lifecycle)
    state = service._load()
    transport = Mock()
    box = DingTalkOutbox(tmp_path / 'negative_outbox.sqlite', transport=transport,
                        now=lambda: clock[0].timestamp(), signal_validator=service._validate_notification_events)
    if kind == 'text':
        box.signal_events([event], since=0)
    else:
        box.enqueue_chart(event, 'https://example.invalid/immutable.png')
    box.deliver_one()
    assert (key, '1m') in service._notification_rechecks
    state['selection_checks'][key + ':1m'].update(
        cutoff=closed_bar_cutoff('a', event['code'], clock[0], '1m'), identities=[], error=False)
    clock[0] += timedelta(seconds=5)
    box.deliver_one()
    with box._connect() as db:
        assert db.execute('SELECT status FROM outbox').fetchone()[0] == 'expired'
    assert service._pending_notification_symbols(state, clock[0].timestamp()) == set()
    assert not state['signals']
    transport.send.assert_not_called()
    transport.send_markdown.assert_not_called()


@pytest.mark.parametrize('reason', ['current_negative', 'known_exit', 'newer_status', 'expired',
                                    'unauthorized', 'removed_symbol', 'removed_period', 'disabled_type'])
def test_missing_state_does_not_resurrect_a_cancelled_or_ineligible_notice(lifecycle, reason):
    service, _, _, clock, key, _ = lifecycle
    event = queued_event(lifecycle)
    state = service._load()
    cutoff = closed_bar_cutoff('a', event['code'], clock[0], '1m')
    if reason == 'current_negative':
        state['selection_checks'][key + ':1m'].update(cutoff=cutoff)
    elif reason in ('known_exit', 'newer_status'):
        state['known'][event['id']] = {'stage': 'left' if reason == 'known_exit' else 'confirmed',
            'updated_at': clock[0].timestamp(),
            'notification_state': 'newer-confirmation-state' if reason == 'newer_status' else signal_status_key(event)}
    elif reason == 'expired':
        event['recorded_at'] = clock[0].timestamp() - 1800
    elif reason == 'unauthorized':
        event['external_notifications'] = False
    elif reason == 'removed_symbol':
        state['seed']['codes'] = []
    elif reason == 'removed_period':
        state['seed']['settings']['frequencies'] = ['5m']
    else:
        state['seed']['settings']['point_types'] = ['1sell']
    assert service._validate_notification_events([event]) == ([], [])
    assert not service._notification_rechecks and not state['signals']


def checkpoint(lifecycle):
    from cl_app.services.monitor_scope import record_minute_checkpoint
    service, _, _, _, key, previous = lifecycle
    record_minute_checkpoint(service._load(), key, 'scope-test', previous)
    service._save()


def finish_minute(lifecycle, rows):
    service, _, live, clock, _, _ = lifecycle
    cutoff = closed_bar_cutoff('a', 'SH.600000', clock[0], '1m')
    live.rows = rows
    live.finish(cutoff)
    live.state.update(source_revision='scope-test', current_source_revision='scope-test')
    service._consume_monitor()
    return cutoff


@pytest.mark.parametrize('point_type', ['1buy', '1sell', '2buy', '2sell', '3buy', '3sell'])
def test_discovery_promotes_each_formal_point_without_resetting_admission(lifecycle, point_type):
    service, _, live, clock, key, _ = lifecycle
    checkpoint(lifecycle)
    before = deepcopy(service._load()['notification_symbol_since'])
    service._schedule_monitor(clock[0], None)
    assert not live.started
    clock[0] = clock[0].replace(minute=5)
    service._schedule_monitor(clock[0], None)
    assert service._load()['active_periods'][key] == ['1m']
    row = signal('SH.600000')
    cutoff = closed_bar_cutoff('a', row['code'], clock[0], '1m')
    row.update(frequency='1m', source_closed_at=cutoff)
    row['point'].update(point_type=point_type, available_at=cutoff, anchor_at=cutoff - 60, anchor_price=10.)
    finish_minute(lifecycle, [row])
    state = service._load()
    assert state['notification_symbol_since'] == before
    assert state['seed']['codes'] == [key]
    assert any(item['frequency'] == '1m' and item['point_type'] == point_type for item in state['signals'].values())
    assert all(event.get('notification_suppressed_reason') != 'CONFIRMED_BEFORE_POOL_ADMISSION' for event in state['events'])
    clock[0] += timedelta(minutes=1)
    service._schedule_monitor(clock[0], None)
    assert len(live.started) == 2 and service._load()['active_periods'][key] == ['1m']


def test_negative_discovery_keeps_membership_and_checkpoint_across_restart(lifecycle, monkeypatch):
    service, manual, live, clock, key, _ = lifecycle
    checkpoint(lifecycle)
    baseline = deepcopy(service._load()['notification_symbol_since'])
    clock[0] = clock[0].replace(minute=5)
    service._schedule_monitor(clock[0], None)
    finish_minute(lifecycle, [])
    assert service._load()['seed']['codes'] == [key]
    assert not service._load()['events']
    restored = SignalMonitor(service.root, screening=manual, monitor=live, now=lambda: clock[0])
    monkeypatch.setattr(restored, 'start_runtime', lambda: None)
    clock[0] += timedelta(minutes=1)
    restored._schedule_monitor(clock[0], None)
    assert len(live.started) == 1
    assert restored._load()['seed']['codes'] == [key]
    assert restored._load()['notification_symbol_since'] == baseline


@pytest.mark.parametrize('protected_by', ['holding', 'watchlist', 'bound_risk', 'pending_notice'])
def test_cold_minute_checkpoint_cannot_postpone_protected_work(lifecycle, protected_by):
    service, _, live, clock, key, _ = lifecycle
    checkpoint(lifecycle)
    state = service._load()
    if protected_by == 'holding':
        state['holding_symbols'] = [key]
        state['holding_source_current'] = True
    elif protected_by == 'watchlist':
        state['seed']['symbols'][0]['origin'] = 'watchlist'
    elif protected_by == 'bound_risk':
        state['holding_risk_plans'][key + ':1m'] = {
            'market': 'a', 'code': 'SH.600000', 'frequency': '1m', 'active': True}
    else:
        event = queued_event(lifecycle)
        assert service._validate_notification_events([event]) == ([], [event])
    service._schedule_monitor(clock[0], None)
    assert len(live.started) == 1 and state['active_periods'][key] == ['1m']


def test_cancelled_discovery_is_not_a_negative_checkpoint_or_membership_exit(lifecycle, monkeypatch):
    from cl_app.services.monitor_scope import minute_cadence
    service, manual, live, clock, key, _ = lifecycle
    checkpoint(lifecycle)
    clock[0] = clock[0].replace(minute=5)
    service._schedule_monitor(clock[0], None)
    state = service._load()
    state['yielding_monitor_run'] = state['active_monitor_run']
    live.committed = []
    live.state.update(status='cancelled', completed=0, source_revision='scope-test', source_current=True)
    service._consume_monitor()
    assert state['seed']['codes'] == [key] and not state['events']
    restored = SignalMonitor(service.root, screening=manual, monitor=live, now=lambda: clock[0])
    monkeypatch.setattr(restored, 'start_runtime', lambda: None)
    current = closed_bar_cutoff('a', 'SH.600000', clock[0], '1m')
    decision = minute_cadence(restored._load(), key, 'scope-test', current)
    assert decision['eligible'] is True, 'an uncommitted cancellation advanced the five-minute discovery clock'
    assert restored._load()['seed']['codes'] == [key]


def test_checking_only_five_minutes_cannot_exit_an_untouched_minute_signal(lifecycle):
    service, _, live, clock, key, previous = lifecycle
    checkpoint(lifecycle)
    state = service._load()
    event = queued_event(lifecycle, '1sell')
    state['signals'][event['id']] = deepcopy(event)
    state['selection_checks'][key + ':1m']['identities'] = [event['id']]
    state['seed']['settings']['frequencies'] = ['1m', '5m']
    state['selection_checks'][key + ':5m'] = {'symbol': key, 'cutoff': previous - 300,
        'revision': 'scope-test', 'error': False, 'identities': [], 'input_fingerprint': 'c' * 64}
    state['monitor_fair_cursor'] = 2  # Isolate a critical-period service slot for this lifecycle check.
    service._schedule_monitor(clock[0], None)
    assert state['active_periods'][key] == ['5m']
    before = deepcopy(state['signals'][event['id']])
    live.rows = []
    live.finish(closed_bar_cutoff('a', 'SH.600000', clock[0], '5m'))
    live.state.update(source_revision='scope-test', current_source_revision='scope-test')
    service._consume_monitor()
    assert state['signals'][event['id']] == before
    assert not any(row.get('change') == 'left' and row['frequency'] == '1m' for row in state['events'])
