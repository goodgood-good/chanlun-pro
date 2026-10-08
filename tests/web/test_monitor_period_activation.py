from copy import deepcopy
from datetime import timedelta

import pytest

from cl_app.services.signal_monitor import SignalMonitor, configure_periods
from cl_app.services.screening import validate_settings
from tests.web.test_signal_monitor import setup as setup, _seed, signal


def test_period_addition_preserves_existing_pool_signals_and_delivery_window(setup):
    service, manual, live, clock, cutoff = _seed(setup, [signal()])
    service.tick()
    live.rows = [signal()]
    live.finish(cutoff)
    service._consume_monitor()
    state = service._load()
    state['notification_since'] = 100
    state['pending_notifications'] = {'retained': {'frequency': '5m'}}
    before = deepcopy(state)
    configure_periods(state, ['1m', '5m'], int(clock[0].timestamp()))
    assert state['seed']['settings']['frequencies'] == ['1m', '5m']
    assert state['settings']['screening']['frequencies'] == ['1m', '5m']
    assert state['seed']['screening_frequencies'] == ['5m']
    assert state['notification_frequency_since'] == {'1m': int(clock[0].timestamp())}
    for key in ('signals', 'known', 'events', 'selection_checks', 'notification_since', 'pending_notifications'):
        assert state[key] == before[key]
    assert state['seed']['codes'] == before['seed']['codes']
    assert state['attempted_cutoffs'] == {}


@pytest.mark.parametrize('frequencies', [[], ['1m', '1m'], ['15m'], '1m', [True]])
def test_invalid_periods_do_not_partially_change_state(frequencies):
    state = {'settings': {'screening': validate_settings({'scope': 'all_a_watchlist'})}}
    before = deepcopy(state)
    with pytest.raises(ValueError):
        configure_periods(state, frequencies, 123)
    assert state == before


@pytest.mark.parametrize('frequency,offset,expected', [('1m', -60, False), ('1m', 0, True),
                                                        ('1m', 60, True), ('5m', -60, True)])
def test_new_period_uses_confirmation_time_and_does_not_suppress_five_minute_signals(
    setup, frequency, offset, expected
):
    service, manual, live, clock, cutoff = _seed(setup, [signal()])
    service.tick()
    enabled_at = int(clock[0].timestamp())
    configure_periods(service._load(), ['1m', '5m'], enabled_at)
    live.state['settings']['frequencies'] = ['1m', '5m']
    row = signal()
    row.update(frequency=frequency, source_closed_at=enabled_at + 60)
    row['point'].update(point_id='period-point', available_at=enabled_at + offset)
    live.rows = [row]
    clock[0] += timedelta(minutes=1)
    live.finish(enabled_at + 60)
    service._consume_monitor()
    event, = [e for e in service._load()['events'] if e.get('point_id') == 'period-point']
    assert event.get('external_notifications', True) is expected
    assert any(p.get('point_id') == 'period-point' for p in service._load()['signals'].values())
    if not expected:
        assert event['notification_suppressed_reason'] == 'CONFIRMED_BEFORE_PERIOD_ENABLED'


def test_period_choice_survives_restart_and_later_single_period_screening(setup, monkeypatch):
    service, manual, live, clock, cutoff = _seed(setup, [signal()])
    service.tick()
    service.enable({'frequencies': ['1m', '5m'], 'daily_scope': 'all_a_watchlist'})
    stamp = service._load()['notification_frequency_since']['1m']
    live.finish(cutoff)
    service._consume_monitor()
    service = SignalMonitor(service.root, screening=manual, monitor=live, now=lambda: clock[0])
    monkeypatch.setattr(service, 'start_runtime', lambda: None)
    service.enable({'daily_scope': 'all_a_watchlist'})
    assert service._load()['notification_frequency_since']['1m'] == stamp
    manual.start(validate_settings({'scope': 'all_a_watchlist', 'frequencies': ['5m']}))
    manual.rows = [signal()]
    manual.finish(cutoff)
    service.tick()
    assert manual.state['settings']['frequencies'] == ['5m']
    assert service._load()['seed']['screening_frequencies'] == ['5m']
    assert service._load()['seed']['settings']['frequencies'] == ['1m', '5m']
    assert live.started[-1]['frequencies'] == ['1m', '5m']


def test_repeat_save_keeps_activation_time_and_reenable_gets_a_new_boundary(setup):
    service, manual, live, clock, cutoff = _seed(setup, [signal()])
    service.tick()
    state = service._load()
    configure_periods(state, ['1m', '5m'], 1000)
    configure_periods(state, ['1m', '5m'], 1100)
    assert state['notification_frequency_since']['1m'] == 1000
    configure_periods(state, ['5m'], 1200)
    configure_periods(state, ['1m', '5m'], 1300)
    assert state['notification_frequency_since']['1m'] == 1300


def test_reenabled_period_rejects_a_previously_queued_confirmation(setup):
    service, manual, live, clock, cutoff = _seed(setup, [signal()])
    service.tick()
    state = service._load()
    configure_periods(state, ['1m', '5m'], 0)
    live.state['settings']['frequencies'] = ['1m', '5m']
    live.state.update(source_revision='revision', current_source_revision='revision')
    row = signal()
    row.update(frequency='1m', source_closed_at=cutoff)
    row['point'].update(point_id='old-queued', available_at=cutoff - 60)
    live.rows = [row]
    live.finish(cutoff)
    service._consume_monitor()
    event, = [e for e in state['events'] if e.get('point_id') == 'old-queued']
    expected = {('a:SH.600088', '1m'): cutoff}
    valid, pending = service._validate_notification_state([event], 'revision', True, None, expected)
    assert len(valid) == 1 and not pending
    state['notification_frequency_since']['1m'] = cutoff
    valid, pending = service._validate_notification_state([event], 'revision', True, None, expected)
    assert not valid and not pending
