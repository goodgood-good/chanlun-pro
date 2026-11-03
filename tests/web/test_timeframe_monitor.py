"""The active application policy includes all three clocks and gated trades."""
from copy import deepcopy

from chanlun.screening import timeframe_policy as policy
from chanlun.screening.cache import REPLAY_CHECKS
from cl_app.services.screening import validate_settings
from cl_app.services.signal_monitor import SignalMonitor, monitor_settings, configure_periods, _signal
from cl_app.services.monitor_incremental import audited_members
from cl_app.services.notification_text import signal_status_lines
from tests.web import test_signal_monitor as legacy_monitor

setup = legacy_monitor.setup
signal = legacy_monitor.signal

REAL_ENABLED = policy.enabled


def activate(monkeypatch):
    # The shared old scheduler fixture isolates its historical one-clock cases.
    monkeypatch.setattr(policy, 'enabled', REAL_ENABLED)


def test_application_settings_require_the_current_policy():
    settings = validate_settings({'scope': 'all_a_watchlist', 'frequencies': ['5m', '30m']})
    assert settings['timeframe_policy'] == policy.POLICY


def test_historical_main_point_is_not_presented_as_a_new_confirmed_trade():
    from cl_app.services.screening_workbench import _current_rows
    item = signal()
    rows = _current_rows({'selected': [item], 'frozen_native_review': True}, 'old')
    assert rows[0]['point']['status'] == 'confirmed'
    assert rows[0]['trade_confirmation_pending'] is True
    assert rows[0]['selection_missing_conditions'] == ['lower_1m_confirmation']


def test_old_manual_seed_uses_fixed_monitor_roles(setup, monkeypatch):
    activate(monkeypatch)
    service, manual, live, clock = setup
    source = deepcopy(service._load()['settings']['screening'])
    source.pop('timeframe_policy')
    source['frequencies'] = ['5m']
    settings = monitor_settings(service._load(), source)
    assert settings['timeframe_policy'] == policy.POLICY
    assert settings['frequencies'] == policy.LIVE_PERIODS
    assert source['frequencies'] == ['5m'] and 'timeframe_policy' not in source
    configure_periods(service._load(), ['5m'], clock[0].timestamp())
    assert service._load()['monitor_frequencies'] == policy.LIVE_PERIODS


def test_upgrade_preserves_membership_and_switches_but_rechecks_old_trade_signals(setup, monkeypatch):
    activate(monkeypatch)
    service, manual, live, clock = setup
    state = service._load()
    state['settings']['screening'].pop('timeframe_policy')
    state['seed'] = {'run_id': 'a' * 32, 'settings': deepcopy(state['settings']['screening']),
                     'codes': ['a:SH.600088'], 'symbols': [{'market': 'a', 'code': 'SH.600088'}]}
    state['signals'] = {period: {'frequency': period, 'point_type': '3buy'} for period in policy.LIVE_PERIODS}
    state['dingtalk_enabled'] = True
    clock[0] = clock[0].replace(hour=15, minute=11)
    state['daily'] = {'session': clock[0].date().isoformat(),
                      'morning': {'status': 'completed'}, 'afternoon': {'status': 'completed', 'attempts': 3}}
    service._save()
    restored = SignalMonitor(service.root, screening=manual, monitor=live, now=lambda: clock[0])
    migrated = restored._load()
    assert migrated['settings']['screening']['timeframe_policy'] == policy.POLICY
    assert migrated['seed']['settings']['frequencies'] == policy.LIVE_PERIODS
    assert migrated['seed']['codes'] == ['a:SH.600088']
    assert migrated['monitor_frequencies'] == policy.LIVE_PERIODS
    assert migrated['dingtalk_enabled'] is True
    assert set(migrated['signals']) == {'1m'}
    assert migrated['force'] is True
    assert migrated['daily']['morning']['status'] == 'completed'
    assert migrated['daily']['afternoon']['status'] == 'updating'
    assert migrated['daily']['afternoon']['attempts'] == 0
    assert migrated['daily']['afternoon']['refresh_reason'] == 'timeframe_policy_updated'


def test_waiting_main_remains_in_the_monitored_candidate_pool():
    row = {**signal(), 'timeframe_policy': policy.POLICY, 'selection_status': 'approaching',
           'evidence': {'file': 'verified'}, 'evidence_verified': True, 'reasons': [policy.PENDING],
           'audit': {key: True for key in REPLAY_CHECKS},
           'nested_confirmation': {'policy': policy.POLICY, 'state': 'waiting'}}
    payload = {'processed_symbols': [{'market': 'a', 'code': row['code']}], 'observations': [row],
               'verified_periods': [{'market': 'a', 'code': row['code'], 'frequency': '5m', 'source_closed_at': 100}]}
    members, periods = audited_members(payload)
    assert list(members) == ['a:SH.600088'] and periods['a:SH.600088'] == ['5m']


def test_signal_clock_uses_the_later_confirmation_and_retains_roles():
    row = {**signal(), 'timeframe_policy': policy.POLICY, 'operating_role': 'trade',
           'selection_status': 'confirmed', 'selection_available_at': 120,
           'nested_confirmation': {'policy': policy.POLICY, 'state': 'confirmed', 'frequency': '1m'},
           'background_30m': {'direction': 'down', 'alignment': 'countertrend'}}
    event = _signal(row)
    assert event['available_at'] == event['selection_available_at'] == 120
    assert event['point_confirmed_at'] == row['point'].get('confirmed_at')
    assert event['operating_role'] == 'trade' and event['lower_confirmation_state'] == 'confirmed'
    text = '\n'.join(signal_status_lines(event))
    assert '逆势风险' in text and '5m 交易级别' in text and '1m 区间内结构确认：已确认' in text


def test_queued_ungated_trades_and_background_points_are_not_dispatched(setup, monkeypatch):
    activate(monkeypatch)
    service, manual, live, clock = setup
    state = service._load()
    state['seed'] = {'settings': deepcopy(state['settings']['screening'])}
    monkeypatch.setattr('cl_app.services.signal_monitor.notification_is_live', lambda *_: True)
    monkeypatch.setattr('cl_app.services.signal_monitor.point_notification_decision', lambda *_: 'allow')
    for period, lower in [('30m', 'confirmed'), ('5m', 'waiting'), ('5m', None)]:
        event = {'id': 'test', 'market': 'a', 'code': 'SH.600088', 'frequency': period,
                 'point_type': '3buy', 'stage': 'confirmed', 'timeframe_policy': policy.POLICY,
                 'lower_confirmation_state': lower}
        assert service._validate_notification_state([event], 'revision', True, set(), {}) == ([], [])
