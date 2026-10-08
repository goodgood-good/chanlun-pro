"""A new completed candle must not wait behind the idle polling interval."""
from datetime import datetime, timedelta

import pytest

from chanlun.screening.markets import closed_bar_cutoff
from chanlun.screening.rules import CN
from cl_app.services.signal_monitor import SignalMonitor
from tests.web.test_signal_monitor import Jobs


def waiting_monitor(tmp_path, market, code, frequency, now, *, interval=60):
    clock = [now]
    manual, live = Jobs(tmp_path / 'manual'), Jobs(tmp_path / 'runs')
    service = SignalMonitor(tmp_path, screening=manual, monitor=live, now=lambda: clock[0])
    symbol = {'market': market, 'code': code, 'name': code}
    key = f'{market}:{code}'
    state = service._load()
    state.update(enabled=True, force=False, pending_symbols_count=0,
                 next_check_at=now.timestamp() + interval,
                 seed={'run_id': 'seed', 'codes': [key], 'symbols': [symbol],
                       'settings': {'frequencies': [frequency], 'point_types': ['3buy'], 'workers': 2}},
                 attempted_cutoffs={key: closed_bar_cutoff(market, code, now, frequency)})
    state['settings']['interval_seconds'] = interval
    service._save()
    return service, live, clock, key


@pytest.mark.parametrize('market,code,period,hour,minute', [
    ('a', 'SH.600000', '5m', 10, 4),
    ('a', 'SH.600000', '30m', 10, 29),
    ('us', 'AMD.US', '5m', 21, 39),
    ('hk', '00700', '5m', 10, 4),
    ('currency_spot', 'BTC/USDT', '1m', 10, 4),
])
def test_new_closed_clock_launches_before_idle_interval_expires(tmp_path, market, code, period, hour, minute):
    service, live, clock, key = waiting_monitor(
        tmp_path, market, code, period, datetime(2026, 9, 21, hour, minute, 40, tzinfo=CN))
    before = service._load()['attempted_cutoffs'][key]
    service._schedule_monitor(clock[0], None)
    assert not live.started  # The in-progress candle is not a trigger.
    clock[0] += timedelta(seconds=20)
    assert clock[0].timestamp() < service._load()['next_check_at']
    assert closed_bar_cutoff(market, code, clock[0], period) > before
    service._schedule_monitor(clock[0], None)
    assert len(live.started) == 1
    assert service._load()['active_cutoffs'][key] > before
    service._schedule_monitor(clock[0], None)
    assert len(live.started) == 1  # Never overlap an active batch.


def test_unchanged_clock_keeps_its_retry_deadline(tmp_path):
    service, live, clock, key = waiting_monitor(
        tmp_path, 'a', 'SH.600000', '5m', datetime(2026, 9, 21, 10, 1, tzinfo=CN))
    cutoff = service._load()['attempted_cutoffs'][key]
    service._load()['retry_symbols'][key] = {
        'cutoff': cutoff, 'failures': 1, 'retry_at': clock[0].timestamp() + 120}
    for seconds in (30, 60, 90, 119):
        service._schedule_monitor(clock[0] + timedelta(seconds=seconds), None)
        assert not live.started
    service._schedule_monitor(clock[0] + timedelta(seconds=120), None)
    assert len(live.started) == 1


def test_new_clock_resets_exhausted_retries_without_waiting(tmp_path):
    service, live, clock, key = waiting_monitor(
        tmp_path, 'a', 'SH.600000', '5m', datetime(2026, 9, 21, 10, 4, 40, tzinfo=CN))
    service._load()['retry_symbols'][key] = {
        'cutoff': service._load()['attempted_cutoffs'][key], 'failures': 3, 'retry_at': None}
    clock[0] += timedelta(seconds=20)
    service._schedule_monitor(clock[0], None)
    assert len(live.started) == 1
    assert key not in service._load()['retry_symbols']


def test_shorter_holding_period_can_wake_a_thirty_minute_pool(tmp_path):
    service, live, clock, key = waiting_monitor(
        tmp_path, 'a', 'SH.600000', '30m', datetime(2026, 9, 21, 10, 4, 40, tzinfo=CN), interval=300)
    state = service._load()
    state['holding_risk_plans'] = {key + ':1m': {
        'market': 'a', 'code': 'SH.600000', 'frequency': '1m', 'active': True}}
    state['attempted_cutoffs'][key] = closed_bar_cutoff('a', 'SH.600000', clock[0], '1m')
    clock[0] += timedelta(seconds=20)
    service._schedule_monitor(clock[0], None)
    assert len(live.started) == 1
    assert live.runtime_periods == {key: ['1m']}


def test_closed_market_history_does_not_bypass_idle_interval(tmp_path):
    service, live, clock, key = waiting_monitor(
        tmp_path, 'a', 'SH.600000', '5m', datetime(2026, 9, 21, 16, 0, tzinfo=CN))
    service._load()['attempted_cutoffs'][key] -= 300
    service._schedule_monitor(clock[0], None)
    assert not live.started
    clock[0] += timedelta(seconds=60)
    service._schedule_monitor(clock[0], None)
    assert len(live.started) == 1


def test_paused_monitor_never_wakes_on_a_new_clock(tmp_path):
    service, live, clock, _ = waiting_monitor(
        tmp_path, 'a', 'SH.600000', '5m', datetime(2026, 9, 21, 10, 4, 40, tzinfo=CN))
    service._load()['enabled'] = False
    clock[0] += timedelta(seconds=20)
    service._schedule_monitor(clock[0], None)
    assert not live.started
