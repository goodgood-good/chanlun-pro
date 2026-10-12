"""Holdings lead admission without reviving blocked work or sharing a symbol."""

from collections import Counter
from datetime import timedelta
import threading

import pytest

from cl_app.services.monitor_fairness import SERVICE_PATTERN, plan_batch
from tests.web.test_monitor_fair_service import fair_inputs
from tests.web.test_monitor_fair_recheck import configure_pool
from tests.web.test_monitor_incremental_priority import monitor as monitor, finish
from tests.web.test_monitor_live_priority import mixed as mixed


@pytest.mark.parametrize('frequency', ['1m', '5m', '30m'])
@pytest.mark.parametrize('cursor', range(len(SERVICE_PATTERN)))
def test_one_available_slot_goes_to_holding_before_any_ordinary_lane(frequency, cursor):
    symbols, periods, state, decisions, targets = fair_inputs(2)
    holding = {'market': 'a', 'code': 'held'}
    symbols.append(holding)
    periods['a:held'] = [frequency]
    targets[f'a:held:{frequency}'] = 300
    state['holding_symbols'] = ['a:held']
    tasks, next_cursor = plan_batch(symbols, periods, state=state, targets=targets,
        minute_decisions=decisions, urgent={'a:hot000', 'a:critical000'}, limit=1, cursor=cursor, now=360)
    assert [(task['key'], task['frequency']) for task in tasks] == [('a:held', frequency)]
    assert next_cursor == cursor, 'holdings do not consume the ordinary fairness cycle'


def test_every_eligible_holding_precedes_ordinary_tasks_without_duplicate_symbols():
    symbols, periods, state, decisions, targets = fair_inputs(5)
    state['holding_symbols'] = ['a:hot004', 'a:critical004', 'a:cold004', 'a:blocked']
    periods['a:critical004'] = ['1m', '5m', '30m']
    symbols.append({'market': 'a', 'code': 'blocked'})
    periods['a:blocked'] = []
    tasks, _ = plan_batch(symbols, periods, state=state, targets=targets,
        minute_decisions=decisions, urgent={'a:hot000'}, limit=8, cursor=0, now=360,
        excluded={'a:hot004'})
    assert {task['key'] for task in tasks[:2]} == {'a:critical004', 'a:cold004'}
    assert not ({task['key'] for task in tasks} & {'a:blocked', 'a:hot004'})
    assert len({task['key'] for task in tasks}) == len(tasks)


def test_holding_has_initial_minute_then_old_siblings_even_at_capacity_one():
    symbol = {'market': 'a', 'code': 'held'}
    key = 'a:held'
    periods = {key: ['1m', '5m', '30m']}
    state = {'holding_symbols': [key], 'attempted_monitor_periods': {},
        'monitor_pending_deadlines': {key+':'+frequency: {
            'revision': 'current', 'first_pending_at': 100, 'deadline': 120}
            for frequency in periods[key]}}
    sequence = []
    for turn in range(4):
        tasks, cursor = plan_batch([symbol], periods, state=state, targets={}, minute_decisions={},
                                  urgent=set(), limit=1, cursor=2, now=130+turn)
        task = tasks[0]
        sequence.append(task['frequency'])
        state['attempted_monitor_periods'][key+':'+task['frequency']] = {
            'revision': 'current', 'scheduled_at': 130+turn}
        state['monitor_critical_cursor'] = (state.get('monitor_critical_cursor', 0)
                                           + (task['lane'] == 'critical')) % 2
        assert cursor == 2
    assert sequence == ['1m', '5m', '30m', '1m']


def test_continuous_holding_work_keeps_the_original_fair_cycle_for_spare_slots():
    symbols, periods, state, decisions, targets = fair_inputs(20)
    symbols.append({'market': 'a', 'code': 'held'})
    periods['a:held'] = ['1m']
    state['holding_symbols'] = ['a:held']
    cursor, lanes = 0, []
    for _ in range(12):
        tasks, cursor = plan_batch(symbols, periods, state=state, targets=targets,
            minute_decisions=decisions, urgent=set(), limit=2, cursor=cursor, now=360)
        assert tasks[0]['key'] == 'a:held'
        lanes.append(tasks[1]['lane'])
        del periods[tasks[1]['key']]
    assert lanes == list(SERVICE_PATTERN)
    assert Counter(lanes) == {'hot': 8, 'critical': 3, 'cold': 1}


def test_due_holding_history_precedes_ordinary_live_and_survives_memory_trim(mixed, monkeypatch):
    service, live, clock, _, history, active = configure_pool(mixed, 20, 8)
    state = service._load()
    state['holding_symbols'] = [f"a:{history[-1]['code']}", f"us:{active[-1]['code']}"]
    service._publish_compute_budget = True
    service._compute_budget = {'isolated': True}
    monkeypatch.setattr('cl_app.services.signal_monitor.cpu_capacity', lambda: 16)
    monkeypatch.setattr('cl_app.services.signal_monitor.publish_monitor_demand', lambda *a, **k: {
        'monitor_capacity': 1, 'screening_capacity': 0, 'monitor_reserved': 1, 'limit': 1})
    service._schedule_monitor(clock[0], None)
    launched = live.started[-1]
    keys = [f"{stock['market']}:{stock['code']}" for stock in launched['symbols']]
    assert set(keys[:2]) == set(state['holding_symbols'])
    assert launched['workers'] == 1 and len(keys) == 4
    assert len(set(keys)) == len(keys)
    assert len(state['active_periods']) == len(keys)
    assert set(state['active_period_targets']) == {key+':5m' for key in keys}
    assert state['pending_symbols_count'] == 24


@pytest.mark.parametrize('retry_at,failures', [(120, 1), (None, 3)])
def test_holding_priority_does_not_bypass_backoff_or_exhausted_retry(mixed, retry_at, failures):
    service, live, clock, closed, history, _ = configure_pool(mixed, 8, 2, workers=2)
    state = service._load()
    blocked = f"a:{history[0]['code']}"
    state.update(force=False, holding_symbols=[blocked], force_pending_symbols=[])
    for key in state['attempted_cutoffs']:
        if key.startswith('us:'):
            state['attempted_cutoffs'][key] -= 60
    state['retry_symbols'][blocked] = {'cutoff': closed['a'], 'failures': failures,
        'retry_at': clock[0].timestamp()+retry_at if retry_at else None}
    service._schedule_monitor(clock[0], None)
    assert live.started
    assert blocked not in {f"{s['market']}:{s['code']}" for s in live.started[-1]['symbols']}
    assert state['retry_symbols'][blocked]['failures'] == failures


def test_new_holding_yields_an_ordinary_live_tail_after_short_wait_once(monitor):
    service, _, live, clock = monitor
    state = service._load()
    state['seed']['settings']['frequencies'] = ['1m']
    service._schedule_monitor(clock[0], None)
    held = {'market': 'a', 'code': 'SH.699999', 'name': 'held'}
    key = 'a:'+held['code']
    state['seed']['symbols'].append(held)
    state['seed']['codes'].append(key)
    state['holding_symbols'] = [key]
    clock[0] += timedelta(seconds=4)
    service._schedule_monitor(clock[0], None)
    assert not live.state.get('cancel_requested')
    clock[0] += timedelta(seconds=1)
    service._schedule_monitor(clock[0], None)
    assert live.state.get('cancel_requested')
    assert len(live.started) == 1, 'a yielded run must finish before another run starts'
    finish(service, live, clock, cancelled=True, committed=[])
    assert not state['retry_symbols'] and not state['errors']
    service._schedule_monitor(clock[0], None)
    assert live.started[-1]['symbols'][0]['code'] == held['code']
    # Even a new ordinary minute cannot repeatedly cancel the admitted holding.
    clock[0] += timedelta(minutes=1)
    service._schedule_monitor(clock[0], None)
    assert not live.state.get('cancel_requested')


def test_ordinary_work_cannot_yield_an_unfinished_background_holding(mixed):
    service, _, live, clock, closed = mixed
    state = service._load()
    state['seed']['symbols'] = [s for s in state['seed']['symbols'] if s['market'] == 'a']
    state['seed']['codes'] = ['a:'+s['code'] for s in state['seed']['symbols']]
    service._schedule_monitor(clock[0], None)
    assert state['active_monitor_mode'] == 'background'
    stock = {'market': 'us', 'code': 'LIVE.US', 'name': 'ordinary'}
    state['seed']['symbols'].append(stock)
    state['seed']['codes'].append('us:LIVE.US')
    clock[0] += timedelta(minutes=1)
    closed['us'] += 60
    service._schedule_monitor(clock[0], None)
    assert not live.state.get('cancel_requested')


def test_notification_membership_reader_is_bounded_pure_and_detached(monitor, monkeypatch):
    service, _, _, _ = monitor
    state = service._load()
    state.update(holding_source_current=True, holding_symbols=['a:SH.600088'])
    monkeypatch.setattr(service, '_load', lambda: pytest.fail('must not load state from disk'))
    snapshot = service._notification_holding_keys()
    snapshot.clear()
    assert state['holding_symbols'] == ['a:SH.600088']
    state['holding_source_current'] = False
    assert service._notification_holding_keys() is None
    state['holding_source_current'] = True
    results = []
    with service._lock:
        thread = threading.Thread(target=lambda: results.append(service._notification_holding_keys()))
        thread.start()
        thread.join(timeout=.5)
        assert not thread.is_alive()
    assert results == [None]
    service._state = None
    assert service._notification_holding_keys() is None
