"""Live bars cannot starve already approved closed-market rechecks."""
from datetime import timedelta
from unittest.mock import Mock

import pytest

from cl_app.services import signal_monitor
from tests.web.test_monitor_live_priority import mixed as mixed
from tests.web.test_signal_monitor import signal


def configure_pool(mixed, live_count, history_count, *, workers=16):
    service, _, live, clock, closed = mixed
    history = [{"market": "a", "code": f"SH.{600000+i}", "name": str(i)}
               for i in range(history_count)]
    active = [{"market": "us", "code": f"LIVE{i:02}.US", "name": str(i)}
              for i in range(live_count)]
    stocks = [*history, *active]
    state = service._load()
    state["settings"]["monitor_workers"] = workers
    state["holding_symbols"] = [f"us:{s['code']}" for s in active[:2]]
    state["seed"].update(symbols=stocks, codes=[f"{s['market']}:{s['code']}" for s in stocks])
    state["attempted_cutoffs"] = {f"{s['market']}:{s['code']}": closed[s['market']] for s in stocks}
    service._save()
    return service, live, clock, closed, history, active


def finish_all(service, live, closed):
    live.finish(closed['us'])
    for _ in range(10):
        service._consume_monitor()
        if service._load()['processed_run'] == live.state['run_id']:
            return
    pytest.fail("bounded monitor result prefix was not consumed")


@pytest.mark.parametrize("live_count", [20, 70])
def test_continuously_new_live_bars_still_drain_historical_queue(mixed, monkeypatch, live_count):
    service, live, clock, closed, history, active = configure_pool(mixed, live_count, 37)
    monkeypatch.setattr(signal_monitor, 'cpu_capacity', lambda: 16)
    urgent = f"us:{active[-1]['code']}"
    monkeypatch.setattr(service, '_pending_notification_symbols', lambda *_: {urgent})
    expected_history = {s['code'] for s in history}
    seen_history, seen_live = set(), set()
    for _ in range(6):
        service._schedule_monitor(clock[0], None)
        batch = live.started[-1]
        symbols = batch['symbols']
        historical = [s for s in symbols if s['market'] == 'a']
        current = [s for s in symbols if s['market'] == 'us']
        if seen_history != expected_history:
            assert historical, "a perpetually due live market starved the closed-market queue"
        assert len(symbols) <= signal_monitor.MONITOR_BATCH_SIZE
        assert batch['workers'] <= 16
        assert len(historical) <= min(batch['workers'], signal_monitor.MONITOR_BATCH_SIZE // 4)
        assert symbols == [*current, *historical], "live checks must start before historical work"
        assert {s['code'] for s in (*active[:2], active[-1])} <= {s['code'] for s in current}
        if live_count == 20:
            assert len(current) == 20
        assert service._load()['active_monitor_mode'] == 'live'
        seen_history.update(s['code'] for s in historical)
        seen_live.update(s['code'] for s in current)
        finish_all(service, live, closed)
        # Every completed batch is followed by another newly closed live bar.
        clock[0] += timedelta(minutes=1)
        closed['us'] += 60
        if seen_history == expected_history and len(seen_live) == live_count:
            break
    assert seen_history == expected_history
    assert seen_live == {s['code'] for s in active}, "ordinary live members must also rotate"
    assert not any(key.startswith('a:') for key in service._load()['force_pending_symbols'])


def test_fair_share_respects_retry_backoff_and_keeps_due_history_rotating(mixed, monkeypatch):
    service, live, clock, closed, history, _ = configure_pool(mixed, 20, 5, workers=4)
    state = service._load()
    blocked = f"a:{history[0]['code']}"
    state['force'] = False
    state['force_pending_symbols'] = [f"a:{s['code']}" for s in history[1:]]
    for key in state['attempted_cutoffs']:
        if key.startswith('us:'):
            state['attempted_cutoffs'][key] -= 60
    state['retry_symbols'][blocked] = {'cutoff': closed['a'], 'failures': 1,
                                        'retry_at': clock[0].timestamp() + 120}
    service._schedule_monitor(clock[0], None)
    symbols = live.started[-1]['symbols']
    assert history[0]['code'] not in {s['code'] for s in symbols}
    assert {s['code'] for s in history[1:]} <= {s['code'] for s in symbols}
    finish_all(service, live, closed)
    clock[0] += timedelta(seconds=120)
    closed['us'] += 120
    service._schedule_monitor(clock[0], None)
    assert history[0]['code'] in {s['code'] for s in live.started[-1]['symbols']}
    assert state['retry_symbols'][blocked]['failures'] == 1


def test_critical_live_work_is_not_displaced_when_it_fills_the_batch(mixed, monkeypatch):
    service, live, clock, _, _, active = configure_pool(mixed, 4, 5, workers=4)
    monkeypatch.setattr(signal_monitor, 'MONITOR_BATCH_SIZE', 4)
    service._load()['holding_symbols'] = [f"us:{s['code']}" for s in active]
    service._schedule_monitor(clock[0], None)
    assert live.started[-1]['symbols'] == active
    assert len(service._load()['force_pending_symbols']) == 5


def test_fair_recheck_shares_the_existing_budget_with_a_full_screening_run(mixed, monkeypatch):
    service, live, clock, _, _, _ = configure_pool(mixed, 20, 8)
    monkeypatch.setattr(signal_monitor, 'cpu_capacity', lambda: 16)
    manual = mixed[1]
    manual.state = {'status': 'running', 'effective_workers': 14, 'settings': {'workers': 16}}
    service._schedule_monitor(clock[0], None)
    batch = live.started[-1]
    assert batch['workers'] == 2
    assert len(batch['symbols']) == signal_monitor.MONITOR_DURING_SCREENING_BATCH_SIZE == 16
    assert sum(s['market'] == 'a' for s in batch['symbols']) == 2
    assert sum(s['market'] == 'us' for s in batch['symbols']) == 14


def test_mixed_batch_notifies_new_live_point_without_replaying_historical_point(mixed, monkeypatch):
    service, live, clock, closed, history, active = configure_pool(mixed, 1, 1, workers=4)
    notifications = Mock()
    notifications.snapshot.return_value = {}
    service._notifications = notifications
    state = service._load()
    state.update(dingtalk_enabled=True, notification_since=0)
    state['selection_checks'][f"a:{history[0]['code']}:5m"] = {
        'symbol': f"a:{history[0]['code']}", 'cutoff': closed['a'], 'revision': 'previous',
        'identities': [], 'error': True,
    }
    service._schedule_monitor(clock[0], None)
    assert service._load()['active_monitor_mode'] == 'live'
    old = signal(history[0]['code'])
    fresh = {**signal(active[0]['code']), 'market': 'us'}
    for row, market in ((old, 'a'), (fresh, 'us')):
        row['source_closed_at'] = row['point']['available_at'] = closed[market]
        row['point']['point_id'] = f'{market}-point'
    live.rows = [old, fresh]
    live.state['source_revision'] = 'current'
    committed = live.committed_results

    def market_clocks(*args, **kwargs):
        result = committed(*args, **kwargs)
        for period in result['verified_periods']:
            period['source_closed_at'] = closed[period['market']]
        return result

    monkeypatch.setattr(live, 'committed_results', market_clocks)
    finish_all(service, live, closed)
    events = {event['market']: event for event in service._load()['events']}
    assert events['a']['external_notifications'] is False
    assert events['a']['notification_suppressed_reason'] == 'ALGORITHM_RECLASSIFICATION'
    assert events['us'].get('external_notifications', True) is True
    queued = [event for call in notifications.signal_events.call_args_list for event in call.args[0]]
    assert queued and {event['market'] for event in queued} == {'us'}
