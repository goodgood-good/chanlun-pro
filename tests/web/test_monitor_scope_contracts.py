"""Control changes and source-contract failures must not resurrect old work."""
from copy import deepcopy
from datetime import datetime, timedelta
from unittest.mock import Mock

import pytest

from chanlun.screening.rules import CN
from cl_app.services.screening import validate_settings
from cl_app.services.signal_monitor import configure_periods, _consolidation_signal, _signal
from tests.web.test_monitor_clock_wake import waiting_monitor
from tests.web.test_signal_monitor import setup as setup, _seed, signal, consolidation, legacy_full_period_batch


def running_periods(setup):
    service, manual, live, clock = setup
    manual.start(validate_settings({'scope': 'all_a_watchlist', 'frequencies': ['1m', '5m']}))
    manual.rows = [signal()]
    cutoff = int(clock[0].timestamp()) - 300
    manual.finish(cutoff)
    service.tick()
    legacy_full_period_batch(service, live)
    live.rows = [{**signal(), 'frequency': f, 'source_closed_at': cutoff} for f in ['1m', '5m']]
    live.state.update(source_revision='current', current_source_revision='current')
    return service, live, clock, cutoff


@pytest.mark.parametrize('late_error', [False, True])
def test_removed_period_cannot_return_with_an_inflight_result(setup, late_error):
    service, live, clock, cutoff = running_periods(setup)
    state = service._load()
    configure_periods(state, ['5m'], int(clock[0].timestamp()))
    if late_error:
        live.errors = [{'market': 'a', 'code': 'SH.600088', 'frequency': '1m',
                        'reasons': ['DATA_SOURCE_UNAVAILABLE'], 'error': 'late old-period error'}]
    live.finish(cutoff)
    service._consume_monitor()
    assert [item['frequency'] for item in state['signals'].values()] == ['5m']
    assert list(state['selection_checks']) == ['a:SH.600088:5m']
    assert not any(item.get('frequency') == '1m' for item in state['events'])
    assert not state['errors']
    assert state['processed_run'] == live.state['run_id']
    assert state['active_consumed_symbols'] == ['a:SH.600088']


def test_completely_removed_batch_is_acknowledged_without_a_missing_result_error(setup):
    service, live, clock, cutoff = running_periods(setup)
    live.state['settings']['symbols'][0]['_monitor_periods'] = ['1m']
    state = service._load()
    before = deepcopy(state['signals'])
    configure_periods(state, ['5m'], int(clock[0].timestamp()))
    live.finish(cutoff)
    service._consume_monitor()
    assert state['signals'] == before
    assert not state['errors'] and not state['selection_checks']
    assert state['processed_run'] == live.state['run_id']


def test_failure_from_completely_removed_period_does_not_retry_current_period(setup):
    service, live, clock, cutoff = running_periods(setup)
    live.state['settings']['symbols'][0]['_monitor_periods'] = ['1m']
    state = service._load()
    before = deepcopy(state['signals'])
    configure_periods(state, ['5m'], int(clock[0].timestamp()))
    live.state.update(status='failed', source_current=True, error='old 1m worker failed')
    service._consume_monitor()
    assert state['signals'] == before
    assert not state['retry_symbols'] and not state['errors']
    assert state['processed_run'] == live.state['run_id']
    assert state.get('last_error') != 'old 1m worker failed'


def test_period_removal_prunes_current_views_but_preserves_bound_risk_proof(setup):
    service, live, clock, cutoff = running_periods(setup)
    live.finish(cutoff)
    service._consume_monitor()
    state = service._load()
    key = 'a:SH.600088:1m'
    before = deepcopy(state['selection_checks'][key])
    state['holding_risk_plans'][key] = {'active': True, 'market': 'a', 'code': 'SH.600088', 'frequency': '1m'}
    state['holding_evidence'][key] = {'source_closed_at': cutoff}
    state['pending_notifications'] = {item['id']: deepcopy(item) for item in state['signals'].values()}
    state['unverified_signals'] = deepcopy(state['signals'])
    events = deepcopy(state['events'])
    configure_periods(state, ['5m'], int(clock[0].timestamp()))
    for name in ('signals', 'unverified_signals', 'pending_notifications'):
        assert all(item['frequency'] == '5m' for item in state[name].values())
    assert state['selection_checks'][key] == before
    assert key in state['holding_evidence']
    assert state['events'] == events  # A user control change is not a signal exit.


def test_notification_off_while_worker_finishes_does_not_enqueue(setup):
    service, live, clock, cutoff = running_periods(setup)
    sender = Mock()
    sender.snapshot.return_value = {}
    service._notifications = sender
    service._load()['dingtalk_enabled'] = True
    service.configure_notifications({'enabled': False})
    live.finish(cutoff)
    service._consume_monitor()
    assert service._load()['dingtalk_enabled'] is False
    sender.signal_events.assert_not_called()


def current_event(service, *, consolidation_event=False):
    key = 'a:SH.600088'
    state = service._load()
    stamp = int(service.now().timestamp())
    event = (_consolidation_signal(consolidation('SH.600088', stamp))
             if consolidation_event else _signal({**signal(), 'source_closed_at': stamp}))
    event.update(holding=True, available_at=stamp-200, recorded_at=stamp)
    state.update(holding_source_current=True, holding_symbols=[key],
                 consolidation_enabled_at=0, consolidation_warning_enabled_at=0)
    state['signals'] = {event['id']: deepcopy(event)}
    state['selection_checks'][key + ':5m'] = {'revision': 'current', 'error': False,
                                             'cutoff': stamp, 'identities': [event['id']]}
    return event, {(key, '5m'): stamp}


def test_removed_holding_can_receive_ordinary_point_without_stale_holding_label(setup):
    service, *_ = _seed(setup, [signal()])
    service.tick()
    event, cutoffs = current_event(service)
    valid, pending = service._validate_notification_state([event], 'current', True, set(), cutoffs)
    assert len(valid) == 1 and not pending
    assert valid[0]['holding'] is False
    assert event['holding'] is True  # Do not rewrite the historical event.


def test_watchlist_refresh_updates_holding_flag_on_retained_ordinary_points(setup):
    service, *_ = _seed(setup, [signal()])
    service.tick()
    event, _ = current_event(service)
    state = service._load()
    service._sync_watchlist()
    assert event['id'] in state['signals']
    assert state['signals'][event['id']]['holding'] is False
    assert not state['holding_symbols']


def test_readded_holding_does_not_send_an_old_consolidation_queue_entry(setup):
    service, *_ = _seed(setup, [signal()])
    service.tick()
    event, cutoffs = current_event(service, consolidation_event=True)
    state = service._load()
    enabled = service.now().timestamp() - 100
    state['holding_notification_since']['a:SH.600088'] = enabled
    assert service._validate_notification_state([event], 'current', True, {'a:SH.600088'}, cutoffs) == ([], [])
    event['available_at'] = enabled
    state['signals'][event['id']]['available_at'] = enabled
    assert len(service._validate_notification_state([event], 'current', True, {'a:SH.600088'}, cutoffs)[0]) == 1


def contract_monitor(tmp_path, monkeypatch, periods=('1m', '5m')):
    service, live, clock, old_key = waiting_monitor(
        tmp_path, 'a', 'SH.600000', '5m', datetime(2026, 9, 21, 10, 4, tzinfo=CN))
    symbol = {'market': 'ny_futures', 'code': 'NY.CL00W', 'name': 'NY.CL00W'}
    key = 'ny_futures:NY.CL00W'
    state = service._load()
    state['seed'].update(codes=[key], symbols=[symbol])
    state['seed']['settings']['frequencies'] = list(periods)
    state.update(force=True, holding_source_current=True)
    monkeypatch.setattr('cl_app.services.signal_monitor.closed_bar_cutoff',
                        lambda market, code, now, frequency: int(now.timestamp()) // (int(frequency[:-1]) * 60)
                        * int(frequency[:-1]) * 60)
    service.screening.state.update(source_revision='contract-v1', current_source_revision='contract-v1')
    service._schedule_monitor(clock[0], None)
    live.state.update(source_revision='contract-v1', current_source_revision='contract-v1')
    legacy_full_period_batch(service, live)
    return service, live, clock, key


def finish_contract_error(service, live, clock, frequencies=('1m', '5m'), reason='BAR_TIME_LABEL_UNAVAILABLE'):
    live.errors = [{'market': 'ny_futures', 'code': 'NY.CL00W', 'frequency': frequency,
                    'reasons': [reason], 'error': 'source contract not verified'} for frequency in frequencies]
    live.finish(int(clock[0].timestamp()))
    service._consume_monitor()


def test_static_contract_failure_suspends_new_bar_retries_and_keeps_diagnosis(tmp_path, monkeypatch):
    service, live, clock, key = contract_monitor(tmp_path, monkeypatch)
    finish_contract_error(service, live, clock)
    state = service._load()
    assert set(state['contract_blocks']) == {key + ':1m', key + ':5m'}
    assert not state['retry_symbols']
    before_errors = deepcopy(state['errors'])
    for minutes in (1, 5, 60):
        clock[0] += timedelta(minutes=minutes)
        service._schedule_monitor(clock[0], None)
    assert len(live.started) == 1
    assert state['seed']['codes'] == [key] and state['errors'] == before_errors
    assert all(check['error'] for check in state['selection_checks'].values())


def test_suspended_contract_does_not_preempt_an_independent_background_batch(tmp_path, monkeypatch):
    service, live, clock, key = contract_monitor(tmp_path, monkeypatch)
    finish_contract_error(service, live, clock)
    state = service._load()
    historical = {'market': 'a', 'code': 'SH.600000', 'name': 'historical'}
    state['seed']['symbols'].append(historical)
    state['seed']['codes'].append('a:SH.600000')
    yesterday = int(clock[0].timestamp()) - 86400
    monkeypatch.setattr('cl_app.services.signal_monitor.closed_bar_cutoff',
                        lambda market, code, now, frequency: yesterday if market == 'a' else int(now.timestamp()))
    state['force'] = True
    service._schedule_monitor(clock[0], None)
    assert state['active_monitor_mode'] == 'background'
    assert [s['market'] for s in live.started[-1]['symbols']] == ['a']
    live.state.update(source_revision='contract-v1', current_source_revision='contract-v1')
    clock[0] += timedelta(minutes=1)
    service._schedule_monitor(clock[0], None)
    assert not live.state.get('cancel_requested')


@pytest.mark.parametrize('recovery', ['manual', 'revision'])
def test_contract_can_be_rechecked_on_user_request_or_source_change(tmp_path, monkeypatch, recovery):
    service, live, clock, key = contract_monitor(tmp_path, monkeypatch)
    finish_contract_error(service, live, clock)
    if recovery == 'manual':
        monkeypatch.setattr(service, '_control_snapshot', lambda options=None: {})
        service.check_now()
    else:
        live.state.update(source_current=False, current_source_revision='contract-v2')
    service._schedule_monitor(clock[0], None)
    assert len(live.started) == 2
    legacy_full_period_batch(service, live)
    live.errors = []
    live.state.update(source_revision='contract-v2', current_source_revision='contract-v2')
    live.finish(int(clock[0].timestamp()))
    service._consume_monitor()
    assert not service._load()['contract_blocks']
    assert not service._load()['errors']


def test_manual_check_during_old_batch_is_not_lost_when_that_batch_fails(tmp_path, monkeypatch):
    service, live, clock, key = contract_monitor(tmp_path, monkeypatch)
    monkeypatch.setattr(service, '_control_snapshot', lambda options=None: {})
    service.check_now()
    finish_contract_error(service, live, clock)
    assert not service._load()['contract_blocks']
    service._schedule_monitor(clock[0], None)
    assert len(live.started) == 2


def test_one_blocked_period_does_not_block_the_other_or_readd_it_to_the_request(tmp_path, monkeypatch):
    service, live, clock, key = contract_monitor(tmp_path, monkeypatch)
    finish_contract_error(service, live, clock, ('1m',))
    assert not service._load()['retry_symbols']
    clock[0] += timedelta(minutes=5)
    service._schedule_monitor(clock[0], None)
    assert len(live.started) == 2
    assert live.started[-1]['symbols'][0]['_monitor_periods'] == ['5m']


@pytest.mark.parametrize('reason', ['DATA_SOURCE_UNAVAILABLE', 'INVALID_BARS', 'ENGINE_ERROR'])
def test_temporary_or_bar_specific_failures_still_retry_next_clock(tmp_path, monkeypatch, reason):
    service, live, clock, key = contract_monitor(tmp_path, monkeypatch)
    finish_contract_error(service, live, clock, reason=reason)
    assert not service._load()['contract_blocks']
    clock[0] += timedelta(minutes=1)
    service._schedule_monitor(clock[0], None)
    assert len(live.started) == 2
