"""A current run elsewhere cannot hide stale proof at an unchanged market clock."""
from copy import deepcopy
from datetime import timedelta

import pytest

from cl_app.services.monitor_scope import record_minute_checkpoint
from tests.web.test_monitor_minute_cadence import cold as cold, KEY
from tests.web.test_monitor_incremental_priority import monitor as monitor, periods


def latest_other_market(cold, monkeypatch):
    service, _, live, clock, cutoff = cold
    clock[0] += timedelta(days=2)
    state = service._load()
    state.update(active_monitor_run='latest-btc', processed_run='latest-btc', active_seed=state['seed']['run_id'],
                 force=False, force_pending_symbols=[], pending_symbols_count=0, deferred_monitor_periods={},
                 source_recheck_revision='previous', next_check_at=clock[0].timestamp()+600)
    live.state.update(run_id='latest-btc', status='completed', source_current=True,
                      source_revision='current', current_source_revision='current', completed=1,
                      settings={'frequencies':['1m'], 'symbols':[{'market':'currency_spot','code':'BTC/USDT'}]})
    monkeypatch.setattr('cl_app.services.signal_monitor.closed_bar_cutoff',
                        lambda market, code, now, frequency: cutoff if market == 'a' else int(now.timestamp()) // 60 * 60)
    return service, live, clock, cutoff, state


def test_old_negative_revision_is_admitted_once_without_new_bar_or_whole_pool_force(cold, monkeypatch):
    service, live, clock, cutoff, state = latest_other_market(cold, monkeypatch)
    state['selection_checks'][KEY+':1m']['revision'] = 'previous'
    state['minute_cadence_checkpoints'][KEY]['revision'] = 'previous'
    state['attempted_monitor_periods'] = {KEY+':1m': {'revision':'previous', 'cutoff':cutoff}}
    current_symbol = {'market':'a', 'code':'SH.600089', 'name':'already current'}
    current_key = 'a:SH.600089'
    state['seed']['symbols'].append(current_symbol)
    state['seed']['screening_symbols'].append(current_symbol)
    state['seed']['codes'].append(current_key)
    state['selection_checks'][current_key+':1m'] = {
        **state['selection_checks'][KEY+':1m'], 'symbol':current_key, 'revision':'current'}
    state['attempted_cutoffs'][current_key] = cutoff
    record_minute_checkpoint(state, current_key, 'current', cutoff)
    preserved = deepcopy({key:state[key] for key in ('known','notification_since','notification_symbol_since')})
    service._schedule_monitor(clock[0], None)
    assert len(live.started) == 1
    assert [row['code'] for row in live.started[0]['symbols']] == ['SH.600088']
    assert periods(live) == [['1m']]
    assert state['force'] is False and state['source_recheck_revision'] == 'previous'
    live.finish(cutoff)
    service._consume_monitor()
    service._schedule_monitor(clock[0], None)
    assert len(live.started) == 1
    assert state['selection_checks'][KEY+':1m']['revision'] == 'current'
    assert state['minute_cadence_checkpoints'][KEY]['revision'] == 'current'
    assert all(state[key] == value for key,value in preserved.items())


@pytest.mark.parametrize('with_retry', [False, True])
def test_current_attempt_preserves_existing_backoff_instead_of_version_replaying(cold, monkeypatch, with_retry):
    service, live, clock, cutoff, state = latest_other_market(cold, monkeypatch)
    state['selection_checks'][KEY+':1m']['revision'] = 'previous'
    state['attempted_monitor_periods'] = {KEY+':1m': {'revision':'current', 'cutoff':cutoff}}
    if with_retry:
        state['retry_symbols'][KEY] = {'cutoff':cutoff, 'period_cutoff':cutoff, 'periods':['1m'],
                                      'failures':1, 'retry_at':clock[0].timestamp()+60}
    service._schedule_monitor(clock[0], None)
    assert not live.started
    if with_retry:
        clock[0] += timedelta(seconds=60)
        service._schedule_monitor(clock[0], None)
        assert len(live.started) == 1 and periods(live) == [['1m']]


def test_current_failed_check_does_not_get_an_extra_revision_override(cold, monkeypatch):
    service, live, clock, cutoff, state = latest_other_market(cold, monkeypatch)
    state['selection_checks'][KEY+':1m']['error'] = True
    state['attempted_monitor_periods'] = {KEY+':1m': {'revision':'previous', 'cutoff':cutoff}}
    state['retry_symbols'][KEY] = {'cutoff':cutoff, 'period_cutoff':cutoff, 'periods':['1m'],
                                  'failures':3, 'retry_at':None}
    service._schedule_monitor(clock[0], None)
    assert not live.started


def test_stale_longer_period_is_rechecked_without_waking_a_verified_cold_minute(cold, monkeypatch):
    service, live, clock, cutoff, state = latest_other_market(cold, monkeypatch)
    state['seed']['settings']['frequencies'] = ['1m', '5m', '30m']
    for frequency in ['5m', '30m']:
        state['selection_checks'][KEY+':'+frequency] = {
            **state['selection_checks'][KEY+':1m'], 'revision':'previous' if frequency == '30m' else 'current'}
        state.setdefault('attempted_monitor_periods', {})[KEY+':'+frequency] = {
            'revision':'previous' if frequency == '30m' else 'current', 'cutoff':cutoff}
    service._schedule_monitor(clock[0], None)
    assert len(live.started) == 1 and periods(live) == [['30m']]
    checkpoint = deepcopy(state['minute_cadence_checkpoints'][KEY])
    live.finish(cutoff)
    service._consume_monitor()
    service._schedule_monitor(clock[0], None)
    assert len(live.started) == 1
    assert state['minute_cadence_checkpoints'][KEY] == checkpoint


def test_revision_repair_of_one_period_does_not_bypass_a_current_siblings_backoff(cold, monkeypatch):
    service, live, clock, cutoff, state = latest_other_market(cold, monkeypatch)
    state['seed']['settings']['frequencies'] = ['1m', '5m']
    state['selection_checks'][KEY+':1m']['error'] = True
    state['selection_checks'][KEY+':5m'] = {
        **state['selection_checks'][KEY+':1m'], 'revision':'previous', 'error':False}
    state['attempted_monitor_periods'] = {
        KEY+':1m': {'revision':'current', 'cutoff':cutoff},
        KEY+':5m': {'revision':'previous', 'cutoff':cutoff}}
    state['retry_symbols'][KEY] = {'cutoff':cutoff, 'period_cutoff':cutoff, 'periods':['1m'],
                                  'failures':1, 'retry_at':clock[0].timestamp()+60}
    retry = deepcopy(state['retry_symbols'][KEY])
    service._schedule_monitor(clock[0], None)
    assert periods(live) == [['5m']]
    live.finish(cutoff)
    service._consume_monitor()
    service._schedule_monitor(clock[0], None)
    assert len(live.started) == 1 and state['retry_symbols'][KEY] == retry


@pytest.mark.parametrize('exhausted', [False, True])
def test_revision_only_admission_cannot_defer_current_error_with_old_attempt(cold, monkeypatch, exhausted):
    service, live, clock, cutoff, state = latest_other_market(cold, monkeypatch)
    state['seed']['settings']['frequencies'] = ['1m', '30m']
    current_check = deepcopy(state['selection_checks'][KEY+':1m'])
    state['selection_checks'][KEY+':1m']['revision'] = 'previous'
    state['minute_cadence_checkpoints'][KEY]['revision'] = 'previous'
    state['selection_checks'][KEY+':30m'] = {**current_check, 'error':True}
    state['attempted_monitor_periods'] = {
        KEY+':1m': {'revision':'previous', 'cutoff':cutoff},
        KEY+':30m': {'revision':'previous', 'cutoff':cutoff}}
    state['retry_symbols'][KEY] = {'cutoff':cutoff, 'period_cutoff':cutoff, 'periods':['30m'],
                                  'failures':3 if exhausted else 1,
                                  'retry_at':None if exhausted else clock[0].timestamp()+60}
    retry = deepcopy(state['retry_symbols'][KEY])
    service._schedule_monitor(clock[0], None)
    assert periods(live) == [['1m']]
    assert '30m' not in state.get('deferred_monitor_periods', {}).get(KEY, [])
    live.finish(cutoff)
    service._consume_monitor()
    service._schedule_monitor(clock[0], None)
    assert len(live.started) == 1 and state['retry_symbols'][KEY] == retry
    clock[0] += timedelta(seconds=60)
    service._schedule_monitor(clock[0], None)
    if exhausted:
        assert len(live.started) == 1
    else:
        assert len(live.started) == 2 and periods(live) == [['30m']]
