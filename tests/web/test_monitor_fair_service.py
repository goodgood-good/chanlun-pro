"""Independent request ownership and bounded service for all monitor lanes."""
from collections import Counter
from copy import deepcopy
from datetime import timedelta

import pytest

from cl_app.services.signal_monitor import SignalMonitor
from cl_app.services.monitor_scope import record_minute_checkpoint
from cl_app.services import monitor_requests
from cl_app.services.monitor_fairness import plan_batch
from tests.web.test_monitor_minute_cadence import cold as cold, KEY
from tests.web.test_monitor_incremental_priority import monitor as monitor, finish, periods, publish_candidate, consume_increment
from tests.web.test_signal_monitor import signal


def configure_three_periods(state, cutoff):
    state['seed']['settings']['frequencies'] = ['1m', '5m', '30m']
    for frequency in ['5m', '30m']:
        state['selection_checks'][KEY+':'+frequency] = {**state['selection_checks'][KEY+':1m'], 'cutoff': cutoff}


def test_scan_cancel_removes_only_scan_request_and_keeps_explicit_check(cold, monkeypatch):
    service, manual, live, clock, cutoff = cold
    state = service._load()
    configure_three_periods(state, cutoff)
    state['monitor_fair_cursor'] = 2  # Exercise an explicit minute request waiting behind one key period.
    monkeypatch.setattr(service, '_control_snapshot', lambda options=None: {})
    clock[0] += timedelta(minutes=1)
    service.check_now()
    service._schedule_monitor(clock[0], None)
    assert periods(live) == [['5m']]
    finish(service, live, clock)
    manual_generation = monitor_requests.sources(state['minute_force_pending'][KEY])['manual']
    publish_candidate(manual, code='SH.600088')
    manual.state['settings']['frequencies'] = ['1m']
    manual.rows[0]['frequency'] = '1m'
    consume_increment(service, manual, clock)
    assert len(monitor_requests.sources(state['minute_force_pending'][KEY])) == 3
    manual.state['status'] = 'cancelled'
    consume_increment(service, manual, clock)
    assert monitor_requests.sources(state['minute_force_pending'][KEY])['manual'] == manual_generation
    service._schedule_monitor(clock[0], None)
    assert periods(live) == [['1m']]
    finish(service, live, clock)
    assert KEY not in state['minute_force_pending']


def test_completion_acknowledges_only_sources_frozen_before_dispatch(cold, monkeypatch):
    service, manual, live, clock, _ = cold
    monkeypatch.setattr(service, '_control_snapshot', lambda options=None: {})
    clock[0] += timedelta(minutes=1)
    service.check_now()
    service._schedule_monitor(clock[0], None)
    publish_candidate(manual, code='SH.600088')
    manual.state['settings']['frequencies'] = ['1m']
    manual.rows[0]['frequency'] = '1m'
    consume_increment(service, manual, clock)
    finish(service, live, clock)
    pending = monitor_requests.sources(service._load()['minute_force_pending'][KEY])
    assert list(pending) == [f"screening:{manual.state['run_id']}"]
    service._schedule_monitor(clock[0], None)
    assert len(live.started) == 2
    finish(service, live, clock)
    assert KEY not in service._load()['minute_force_pending']


def test_manual_generation_survives_restart_and_old_result_cannot_ack_new_request(cold, monkeypatch):
    service, manual, live, clock, _ = cold
    monkeypatch.setattr(service, '_control_snapshot', lambda options=None: {})
    clock[0] += timedelta(minutes=1)
    service.check_now()
    service._schedule_monitor(clock[0], None)
    restored = SignalMonitor(service.root, screening=manual, monitor=live, now=lambda: clock[0])
    monkeypatch.setattr(restored, '_control_snapshot', lambda options=None: {})
    try:
        restored.check_now()
        assert restored._load()['minute_request_generation'] == 2
        finish(restored, live, clock)
        assert monitor_requests.sources(restored._load()['minute_force_pending'][KEY])['manual'] == 2
        restored._schedule_monitor(clock[0], None)
        assert len(live.started) == 2
    finally:
        restored._incremental_reader.close()


@pytest.mark.parametrize('legacy', [7, {'source': 'screening', 'run_id': 'old', 'through': 2}])
def test_legacy_request_tokens_are_upgraded_without_losing_another_source(legacy):
    state = {'minute_force_pending': {KEY: deepcopy(legacy)}}
    original = monitor_requests.snapshot(state, KEY)
    monitor_requests.add(state, KEY, 'screening:new', {'run_id': 'new', 'through': 3})
    monitor_requests.acknowledge(state, KEY, original)
    assert list(monitor_requests.sources(state['minute_force_pending'][KEY])) == ['screening:new']


def test_new_manual_generation_exceeds_a_legacy_inflight_generation_after_restart(cold, monkeypatch):
    service, manual, live, clock, _ = cold
    state = service._load()
    state['minute_force_pending'] = {KEY: 7}
    state.pop('minute_request_generation', None)
    clock[0] += timedelta(minutes=1)
    service._schedule_monitor(clock[0], None)
    restored = SignalMonitor(service.root, screening=manual, monitor=live, now=lambda: clock[0])
    monkeypatch.setattr(restored, '_control_snapshot', lambda options=None: {})
    try:
        restored.check_now()
        assert restored._load()['minute_request_generation'] == 8
        finish(restored, live, clock)
        assert monitor_requests.sources(restored._load()['minute_force_pending'][KEY])['manual'] == 8
        restored.pause()
        restored._load()['enabled'] = True
        restored._schedule_monitor(clock[0], None)
        assert len(live.started) == 2
    finally:
        restored._incremental_reader.close()


def fair_inputs(per_lane=20):
    symbols, periods, decisions, targets = [], {}, {}, {}
    for lane, frequency in [('hot', '1m'), ('critical', '5m'), ('cold', '1m')]:
        for index in range(per_lane):
            symbol = {'market': 'a', 'code': f'{lane}{index:03}', 'name': lane}
            key = 'a:'+symbol['code']
            symbols.append(symbol)
            periods[key] = [frequency]
            decisions[key] = {'cold': lane == 'cold', 'next_due_at': 300}
            targets[key+':'+frequency] = 300
    state = {'holding_symbols': [], 'selection_checks': {}, 'attempted_cutoffs': {}}
    return symbols, periods, state, decisions, targets


def test_every_full_service_cycle_guarantees_eight_three_one_without_duplicate_symbols():
    symbols, periods_, state, decisions, targets = fair_inputs()
    tasks, cursor = plan_batch(symbols, periods_, state=state, targets=targets, minute_decisions=decisions,
                               urgent=set(), limit=12, cursor=0, now=300)
    assert Counter(task['lane'] for task in tasks) == {'hot': 8, 'critical': 3, 'cold': 1}
    assert len({task['key'] for task in tasks}) == 12
    assert cursor == 0


def test_small_batches_preserve_the_cycle_and_empty_lane_shares_are_borrowed():
    symbols, periods_, state, decisions, targets = fair_inputs()
    cursor, lanes = 0, []
    for _ in range(12):
        tasks, cursor = plan_batch(symbols, periods_, state=state, targets=targets, minute_decisions=decisions,
                                   urgent=set(), limit=1, cursor=cursor, now=300)
        lanes.append(tasks[0]['lane'])
        periods_.pop(tasks[0]['key'])
    assert Counter(lanes) == {'hot': 8, 'critical': 3, 'cold': 1}
    hot = [symbol for symbol in symbols if symbol['name'] == 'hot']
    tasks, _ = plan_batch(hot, periods_, state=state, targets=targets, minute_decisions=decisions,
                          urgent=set(), limit=12, cursor=5, now=300)
    assert len(tasks) == 12 and all(task['lane'] == 'hot' for task in tasks)


def test_holdings_and_notification_repairs_lead_their_lane():
    symbols, periods_, state, decisions, targets = fair_inputs()
    state['holding_symbols'] = ['a:hot019']
    tasks, _ = plan_batch(symbols, periods_, state=state, targets=targets, minute_decisions=decisions,
                          urgent={'a:hot018'}, limit=2, cursor=0, now=300)
    assert [task['key'] for task in tasks] == ['a:hot019', 'a:hot018']


def test_actual_scheduler_does_not_put_sixty_four_cold_minutes_before_one_hot(cold, monkeypatch):
    service, _, live, clock, cutoff = cold
    state = service._load()
    stocks = [{'market': 'a', 'code': f'SH.{600000+i}', 'name': 'cold'} for i in range(64)]
    stocks.append({'market': 'a', 'code': 'SH.699999', 'name': 'hot'})
    keys = ['a:'+stock['code'] for stock in stocks]
    state['seed'].update(symbols=stocks, screening_symbols=deepcopy(stocks), codes=keys)
    state['selection_checks'] = {}
    state['minute_cadence_checkpoints'] = {}
    state['attempted_cutoffs'] = {}
    for key in keys:
        observed = cutoff+240 if key == keys[-1] else cutoff
        state['selection_checks'][key+':1m'] = {'symbol': key, 'revision': 'current', 'error': False,
            'cutoff': observed, 'checked_at': observed, 'input_fingerprint': 'a'*64,
            'identities': ['hot'] if key == keys[-1] else []}
        state['attempted_cutoffs'][key] = observed
        record_minute_checkpoint(state, key, 'current', observed)
    state['signals'] = {'hot': {'market': 'a', 'code': 'SH.699999', 'frequency': '1m', 'stage': 'confirmed'}}
    clock[0] += timedelta(minutes=5)
    monkeypatch.setattr('cl_app.services.signal_monitor.cpu_capacity', lambda: 16)
    service._schedule_monitor(clock[0], None)
    assert live.started[-1]['symbols'][0]['code'] == 'SH.699999'
    assert Counter(state['active_service_lanes'].values()) == {'hot': 1, 'cold': 63}


def test_active_cold_work_yields_to_new_hot_minute_once_then_keeps_completion_chance(cold):
    service, _, live, clock, cutoff = cold
    state = service._load()
    clock[0] += timedelta(minutes=5)
    service._schedule_monitor(clock[0], None)
    assert state['active_service_lanes'] == {KEY: 'cold'}
    hot = {'market': 'a', 'code': 'SH.699999', 'name': 'new hot'}
    state['seed']['symbols'].append(hot)
    state['seed']['screening_symbols'].append(hot)
    state['seed']['codes'].append('a:SH.699999')
    clock[0] += timedelta(seconds=5)
    service._schedule_monitor(clock[0], None)
    assert live.state.get('cancel_requested')
    finish(service, live, clock, cancelled=True, committed=[])
    service._schedule_monitor(clock[0], None)
    assert live.started[-1]['symbols'][0]['code'] == hot['code']
    row = signal(hot['code'])
    row.update(frequency='1m', source_closed_at=cutoff+300)
    live.rows = [row]
    live.committed = [live.started[-1]['symbols'][0]]
    live.state.update(completed=1, cutoffs={'1m': cutoff+300})
    service._consume_monitor()
    clock[0] += timedelta(minutes=1)
    service._schedule_monitor(clock[0], None)
    assert not live.state.get('cancel_requested'), 'The same cold provider task must not be killed on every new minute'


def test_zero_resource_capacity_preserves_request_and_cursor_until_recovery(cold, monkeypatch):
    service, _, live, clock, _ = cold
    monkeypatch.setattr(service, '_control_snapshot', lambda options=None: {})
    service._publish_compute_budget = True
    service._compute_budget = {'isolated': True}
    capacity = [0]
    calls = []
    def publish(*args, **kwargs):
        calls.append(kwargs['demand'])
        return {'monitor_capacity': capacity[0], 'screening_capacity': 0, 'monitor_reserved': 0, 'limit': capacity[0]}
    monkeypatch.setattr('cl_app.services.signal_monitor.publish_monitor_demand', publish)
    clock[0] += timedelta(minutes=1)
    service.check_now()
    pending = deepcopy(service._load()['minute_force_pending'])
    cursor = service._load().get('monitor_fair_cursor', 0)
    service._schedule_monitor(clock[0], None)
    assert not live.started and not service._load().get('startup_errors')
    assert service._load()['minute_force_pending'] == pending
    assert service._load().get('monitor_fair_cursor', 0) == cursor
    assert service.poll_interval() == .5
    capacity[0] = 16
    service._schedule_monitor(clock[0], None)
    assert len(live.started) == 1 and live.started[0]['workers'] >= 1
    assert calls == [1, 1]


def test_lower_memory_capacity_trims_wave_and_keeps_unstarted_work_queued(cold, monkeypatch):
    service, _, live, clock, _ = cold
    state = service._load()
    stocks = [{'market': 'a', 'code': f'SH.{600000+i}', 'name': 'fixture'} for i in range(20)]
    state['seed'].update(symbols=stocks, screening_symbols=stocks, codes=['a:'+row['code'] for row in stocks])
    state['force'] = True
    service._publish_compute_budget = True
    service._compute_budget = {'isolated': True}
    monkeypatch.setattr('cl_app.services.signal_monitor.cpu_capacity', lambda: 16)
    monkeypatch.setattr('cl_app.services.signal_monitor.publish_monitor_demand', lambda *args, **kwargs: {
        'monitor_capacity': 2, 'screening_capacity': 0, 'monitor_reserved': 2, 'limit': 2})
    clock[0] += timedelta(minutes=1)
    service._schedule_monitor(clock[0], None)
    assert live.started[-1]['workers'] == 2
    assert len(live.started[-1]['symbols']) == 8
    assert state['pending_symbols_count'] == 12
    assert state['monitor_queue_counts']['hot'] == 12
    assert state['monitor_fair_cursor'] == 8
    assert len(state['minute_force_pending']) == 20


def test_history_demand_borrows_idle_capacity_but_releases_it_to_a_parallel_scan(cold, monkeypatch):
    service, manual, live, clock, cutoff = cold
    state = service._load()
    state['force'] = True
    stocks = [{'market': 'a', 'code': f'SH.{600000+i}', 'name': 'old'} for i in range(40)]
    state['seed'].update(symbols=stocks, screening_symbols=stocks, codes=['a:'+s['code'] for s in stocks])
    clock[0] += timedelta(days=2)
    monkeypatch.setattr('cl_app.services.signal_monitor.closed_bar_cutoff', lambda *args: cutoff)
    monkeypatch.setattr('cl_app.services.signal_monitor.cpu_capacity', lambda: 16)
    service._schedule_monitor(clock[0], None)
    assert live.started[-1]['workers'] == 16
    assert service._last_compute_limits['monitor_demand'] == 2
    live.finish(cutoff)
    service._consume_monitor()
    service._consume_monitor()  # Preserve the bounded committed-prefix reader.
    manual.state['status'] = 'running'
    service._schedule_monitor(clock[0], None)
    assert live.started[-1]['workers'] == 2
    assert service._last_compute_limits['screening_capacity'] == 14


@pytest.mark.parametrize('configured', [2, 4])
def test_capacity_lease_never_reserves_more_than_configured_monitor_workers(cold, monkeypatch, configured):
    service, _, _, _, _ = cold
    state = service._load()
    state['settings']['monitor_workers'] = configured
    service._publish_compute_budget = True
    service._compute_budget = {'isolated': True}
    demands = []
    def publish(*args, **kwargs):
        demands.append(kwargs['demand'])
        return {'monitor_capacity': configured, 'screening_capacity': 16-configured,
                'monitor_reserved': configured, 'limit': 16}
    monkeypatch.setattr('cl_app.services.signal_monitor.publish_monitor_demand', publish)
    monkeypatch.setattr('cl_app.services.signal_monitor.cpu_capacity', lambda: 16)
    assert service._monitor_capacity(800, True) == configured
    assert demands == [configured]


def test_paused_or_empty_pool_hides_old_queue_sample(cold):
    service, _, _, _, _ = cold
    state = service._load()
    state.update(monitor_queue_counts={'hot': 10, 'critical': 20, 'cold': 30}, monitor_queue_at=123)
    state['enabled'] = False
    assert service.scheduling_activity()['waiting'] == {'hot': 0, 'critical': 0, 'cold': 0}
    assert service.scheduling_activity()['planned_at'] is None
    state['enabled'] = True
    state['seed']['codes'] = []
    assert service.scheduling_activity()['waiting'] == {'hot': 0, 'critical': 0, 'cold': 0}
