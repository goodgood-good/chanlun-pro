"""Minute cadence changes dispatch only; durable pool and proof remain intact."""
from copy import deepcopy
from datetime import timedelta

import pytest

from cl_app.services.signal_monitor import SignalMonitor
from cl_app.services.monitor_scope import record_minute_checkpoint
from tests.web.test_monitor_incremental_priority import monitor as monitor, finish, periods, publish_candidate, consume_increment
from tests.web.test_signal_monitor import signal


KEY = 'a:SH.600088'


@pytest.fixture
def cold(monitor):
    service, manual, live, clock = monitor
    clock[0] = clock[0].replace(minute=30, second=0)
    cutoff = int(clock[0].timestamp())
    state = service._load()
    state['seed']['settings']['frequencies'] = ['1m']
    state['seed']['settings']['point_types'] = ['3buy', '3sell']
    state.update(force=False, force_pending_symbols=[], pending_symbols_count=0,
                 next_check_at=cutoff+600, minute_cadence_policy_version=1,
                 attempted_cutoffs={KEY: cutoff}, notification_since=123,
                 notification_symbol_since={KEY: 100}, known={'saved': {'updated_at': 100}})
    state['selection_checks'] = {KEY+':1m': {
        'symbol': KEY, 'revision': 'current', 'cutoff': cutoff, 'checked_at': cutoff,
        'error': False, 'identities': [], 'input_fingerprint': 'verified-input'}}
    assert record_minute_checkpoint(state, KEY, 'current', cutoff)
    original = live.committed_results

    def fingerprinted(*args, **kwargs):
        result = original(*args, **kwargs)
        for period in result.get('verified_periods', []):
            period['input_fingerprint'] = 'verified-next-input'
        return result

    live.committed_results = fingerprinted
    return service, manual, live, clock, cutoff


def test_cold_minute_runs_at_five_closed_minutes_without_mutating_membership(cold):
    service, _, live, clock, cutoff = cold
    state = service._load()
    before = deepcopy({key: state.get(key) for key in
        ('seed', 'known', 'notification_since', 'notification_symbol_since', 'holding_risk_plans')})
    for minute in range(1, 5):
        clock[0] += timedelta(minutes=1)
        service._schedule_monitor(clock[0], None)
        assert not live.started
        assert state['minute_cadence_checkpoints'][KEY]['cutoff'] == cutoff
    clock[0] += timedelta(minutes=1)
    service._schedule_monitor(clock[0], None)
    assert periods(live) == [['1m']]
    finish(service, live, clock)
    assert state['minute_cadence_checkpoints'][KEY]['cutoff'] == cutoff+300
    for key, value in before.items():
        assert state.get(key) == value
    clock[0] += timedelta(minutes=1)
    service._schedule_monitor(clock[0], None)
    assert len(live.started) == 1


@pytest.mark.parametrize('point_type', ['3buy', '3sell'])
def test_either_buy_or_sell_discovery_resumes_the_next_minute(cold, point_type):
    service, _, live, clock, cutoff = cold
    clock[0] += timedelta(minutes=5)
    service._schedule_monitor(clock[0], None)
    row = signal()
    row.update(frequency='1m', source_closed_at=cutoff+300)
    row['point'].update(point_type=point_type, available_at=cutoff+300)
    live.rows = [row]
    finish(service, live, clock)
    state = service._load()
    assert any(item['point_type'] == point_type for item in state['signals'].values())
    assert state['notification_since'] == 123 and state['notification_symbol_since'][KEY] == 100
    clock[0] += timedelta(minutes=1)
    service._schedule_monitor(clock[0], None)
    assert len(live.started) == 2 and periods(live) == [['1m']]


@pytest.mark.parametrize('blanket', ['deferred', 'force_pending', 'pending_count', 'faster_attempt'])
def test_ordinary_blanket_queues_cannot_turn_cold_minute_into_every_minute(cold, blanket):
    service, _, live, clock, cutoff = cold
    state = service._load()
    if blanket == 'deferred':
        state['deferred_monitor_periods'] = {KEY: ['1m']}
    elif blanket == 'force_pending':
        state['force_pending_symbols'] = [KEY]
    elif blanket == 'pending_count':
        state['pending_symbols_count'] = 1
    else:
        state['attempted_cutoffs'][KEY] = cutoff-60
    for _ in range(4):
        clock[0] += timedelta(minutes=1)
        service._schedule_monitor(clock[0], None)
    assert not live.started


def test_explicit_check_survives_other_period_batches_then_expires_once(cold, monkeypatch):
    service, _, live, clock, cutoff = cold
    state = service._load()
    state['seed']['settings']['frequencies'] = ['1m', '5m', '30m']
    for frequency in ['5m', '30m']:
        state['selection_checks'][f'{KEY}:{frequency}'] = {
            **state['selection_checks'][KEY+':1m'], 'cutoff': cutoff}
    monkeypatch.setattr(service, '_control_snapshot', lambda options=None: {})
    clock[0] += timedelta(minutes=1)
    service.check_now()
    for expected in ['30m', '5m', '1m']:
        service._schedule_monitor(clock[0], None)
        assert periods(live) == [[expected]]
        assert KEY in state['minute_force_pending']
        finish(service, live, clock)
    assert state['minute_force_pending'] == {}
    assert state['minute_cadence_checkpoints'][KEY]['cutoff'] == cutoff+60
    clock[0] += timedelta(minutes=1)
    service._schedule_monitor(clock[0], None)
    assert len(live.started) == 3


def test_five_and_thirty_minute_due_checks_are_unchanged(cold):
    service, _, live, clock, cutoff = cold
    state = service._load()
    state['seed']['settings']['frequencies'] = ['1m', '5m', '30m']
    for frequency in ['5m', '30m']:
        state['selection_checks'][f'{KEY}:{frequency}'] = {
            **state['selection_checks'][KEY+':1m'], 'cutoff': cutoff}
    clock[0] += timedelta(minutes=5)
    for expected in ['5m', '1m']:
        service._schedule_monitor(clock[0], None)
        assert periods(live) == [[expected]]
        finish(service, live, clock)
    clock[0] += timedelta(minutes=1)
    service._schedule_monitor(clock[0], None)
    assert len(live.started) == 2
    clock[0] += timedelta(minutes=24)
    for expected in ['30m', '5m', '1m']:
        service._schedule_monitor(clock[0], None)
        assert periods(live) == [[expected]]
        finish(service, live, clock)


def test_restart_preserves_closed_clock_and_does_not_reset_cold_wait(cold):
    service, manual, live, clock, cutoff = cold
    service._save()
    restored = SignalMonitor(service.root, screening=manual, monitor=live, now=lambda: clock[0])
    try:
        clock[0] += timedelta(minutes=4)
        restored._schedule_monitor(clock[0], None)
        assert not live.started
        assert restored._load()['minute_cadence_checkpoints'][KEY]['cutoff'] == cutoff
        clock[0] += timedelta(minutes=1)
        restored._schedule_monitor(clock[0], None)
        assert periods(live) == [['1m']]
    finally:
        restored._incremental_reader.close()


def test_closed_market_does_not_advance_cadence_from_wall_time(cold, monkeypatch):
    service, _, live, clock, cutoff = cold
    monkeypatch.setattr('cl_app.services.signal_monitor.closed_bar_cutoff', lambda *args: cutoff)
    clock[0] += timedelta(days=2)
    service._schedule_monitor(clock[0], None)
    assert not live.started
    assert service._load()['minute_cadence_checkpoints'][KEY]['cutoff'] == cutoff
    monkeypatch.setattr('cl_app.services.signal_monitor.closed_bar_cutoff', lambda *args: cutoff+300)
    service._schedule_monitor(clock[0], None)
    assert periods(live) == [['1m']]


def test_cold_clock_does_not_preempt_background_until_its_period_is_due(cold):
    service, _, live, clock, cutoff = cold
    state = service._load()
    live.start({**state['seed']['settings'], 'symbols': state['seed']['symbols']})
    state.update(active_monitor_run=live.state['run_id'], active_seed='baseline',
                 active_monitor_mode='background', active_scheduled_at=cutoff,
                 active_symbols=[KEY], active_cutoffs={KEY: cutoff})
    clock[0] += timedelta(minutes=1)
    service._schedule_monitor(clock[0], None)
    assert not live.state.get('cancel_requested')
    clock[0] += timedelta(minutes=4)
    service._schedule_monitor(clock[0], None)
    assert live.state.get('cancel_requested')


def test_cancelled_explicit_check_keeps_its_override_through_restart(cold, monkeypatch):
    service, manual, live, clock, cutoff = cold
    monkeypatch.setattr(service, '_control_snapshot', lambda options=None: {})
    clock[0] += timedelta(minutes=1)
    service.check_now()
    service._schedule_monitor(clock[0], None)
    state = service._load()
    state['yielding_monitor_run'] = live.state['run_id']
    finish(service, live, clock, cancelled=True, committed=[])
    assert state['minute_cadence_checkpoints'][KEY]['cutoff'] == cutoff
    assert KEY in state['minute_force_pending']
    restored = SignalMonitor(service.root, screening=manual, monitor=live, now=lambda: clock[0])
    try:
        restored._schedule_monitor(clock[0], None)
        assert len(live.started) == 2 and periods(live) == [['1m']]
    finally:
        restored._incremental_reader.close()


def test_first_upgrade_adopts_current_successful_proof_once_without_extra_scan(cold):
    service, _, live, clock, cutoff = cold
    state = service._load()
    state.pop('minute_cadence_policy_version')
    state.pop('minute_cadence_checkpoints')
    protected = deepcopy({key: state[key] for key in ('seed', 'signals', 'known', 'notification_since')})
    service._schedule_monitor(clock[0], None)
    assert not live.started
    assert state['minute_cadence_policy_version'] == 1
    assert state['minute_cadence_checkpoints'][KEY]['cutoff'] == cutoff
    for key, value in protected.items():
        assert state[key] == value
    state['seed']['run_id'] = 'next-seed'
    state['attempted_monitor_periods'] = {KEY+':1m': {'revision': 'current', 'cutoff': cutoff}}
    service._schedule_monitor(clock[0], None)
    assert periods(live) == [['1m']]
    assert state['minute_cadence_checkpoints'][KEY]['seed_run_id'] == 'baseline'
    finish(service, live, clock)
    assert state['minute_cadence_checkpoints'][KEY]['seed_run_id'] == 'next-seed'


def test_error_does_not_advance_checkpoint_and_retry_remains_bounded(cold):
    service, _, live, clock, cutoff = cold
    clock[0] += timedelta(minutes=5)
    service._schedule_monitor(clock[0], None)
    live.errors = [{'market': 'a', 'code': 'SH.600088', 'frequency': '1m', 'reasons': ['ENGINE_ERROR']}]
    finish(service, live, clock)
    state = service._load()
    assert state['minute_cadence_checkpoints'][KEY]['cutoff'] == cutoff
    assert state['selection_checks'][KEY+':1m']['error'] is True
    service._schedule_monitor(clock[0], None)
    assert len(live.started) == 1
    clock[0] += timedelta(minutes=1)
    service._schedule_monitor(clock[0], None)
    assert len(live.started) == 2


@pytest.mark.parametrize('candidate_period', ['1m', '5m'])
def test_audited_increment_promotes_existing_cold_member_only_for_a_minute_point(cold, candidate_period):
    service, manual, live, clock, cutoff = cold
    clock[0] += timedelta(minutes=1)
    publish_candidate(manual, code='SH.600088')
    manual.state['settings']['frequencies'] = [candidate_period]
    manual.rows[0]['frequency'] = candidate_period
    consume_increment(service, manual, clock)
    state = service._load()
    token = deepcopy(state.get('minute_force_pending', {}).get(KEY))
    consume_increment(service, manual, clock)
    assert state.get('minute_force_pending', {}).get(KEY) == token
    service._schedule_monitor(clock[0], None)
    if candidate_period == '5m':
        assert not live.started and token is None
        return
    assert token['source'] == 'screening'
    assert periods(live) == [['1m']]
    finish(service, live, clock)
    assert KEY not in state['minute_force_pending']
    clock[0] += timedelta(minutes=1)
    service._schedule_monitor(clock[0], None)
    assert len(live.started) == 1
    assert state['notification_symbol_since'][KEY] == 100


def test_cancelled_screening_revokes_its_unconsumed_minute_promotion(cold):
    service, manual, live, clock, _ = cold
    clock[0] += timedelta(minutes=1)
    publish_candidate(manual, code='SH.600088')
    manual.state['settings']['frequencies'] = ['1m']
    manual.rows[0]['frequency'] = '1m'
    consume_increment(service, manual, clock)
    assert KEY in service._load()['minute_force_pending']
    manual.state['status'] = 'cancelled'
    consume_increment(service, manual, clock)
    service._schedule_monitor(clock[0], None)
    assert not live.started
    assert service._load()['seed']['codes'] == [KEY]
