"""Holding scope changes independent jobs, never a 5m task's linked evidence."""
from copy import deepcopy
from datetime import timedelta

import pytest

from chanlun.screening import timeframe_policy
from cl_app.services.monitor_scope import independent_monitor_periods, minute_cadence, minute_scope_summary
from cl_app.services.monitor_view import paged_state
from cl_app.services.notification_policy import default_point_notifications, normalize_point_notifications, POINTS
from cl_app.services.signal_monitor import SignalMonitor, _available_monitor_periods
from tests.web.test_signal_monitor import setup
from tests.web.test_timeframe_monitor import activate

HELD, OTHER = 'a:SH.600088', 'a:SH.600000'


def state():
    symbols = [{'market': 'a', 'code': key.split(':')[1], 'origin': 'watchlist'} for key in (HELD, OTHER)]
    return {'holding_symbols': [HELD], 'holding_source_current': True, 'signals': {}, 'events': [], 'errors': [],
            'selection_checks': {}, 'seed': {'run_id': 'seed', 'codes': [HELD, OTHER], 'symbols': symbols,
                'settings': {'timeframe_policy': timeframe_policy.POLICY, 'frequencies': ['1m', '5m', '30m']}}}


def test_only_current_holdings_receive_independent_short_and_background_tasks():
    value = state()
    value['signals']['old'] = {'market': 'a', 'code': 'SH.600000', 'frequency': '1m', 'stage': 'confirmed'}
    value['holding_risk_plans'] = {'old-plan': {**value['signals']['old'], 'active': True}}
    assert independent_monitor_periods(value, OTHER) == {'5m'}
    assert independent_monitor_periods(value, HELD) == {'1m', '5m', '30m'}
    assert _available_monitor_periods(value, OTHER, 'revision') == {'5m'}
    for manual in (False, True):
        for notice in (False, True):
            decision = minute_cadence(value, OTHER, 'revision', 1000,
                                      manual_override=manual, notification_recheck=notice)
            assert not decision['eligible'] and decision['classification'] == 'auxiliary'


def test_membership_changes_and_source_failures_do_not_strand_holding_checks():
    value = state()
    value['holding_symbols'].append(OTHER)
    assert independent_monitor_periods(value, OTHER) == {'1m', '5m', '30m'}
    value['holding_symbols'].remove(OTHER)
    assert independent_monitor_periods(value, OTHER) == {'5m'}
    value['holding_source_current'] = False
    assert independent_monitor_periods(value, HELD) == independent_monitor_periods(value, OTHER) == {'1m', '5m', '30m'}


def test_ordinary_coverage_does_not_wait_for_disabled_independent_periods():
    value = state()
    value['selection_checks'] = {key + ':5m': {'revision': 'r', 'error': False, 'cutoff': 1000} for key in (HELD, OTHER)}
    scope = minute_scope_summary(value, 'r')
    assert (scope['foreground'], scope['background'], scope['auxiliary']) == (1, 0, 1)
    view = paged_state(value, {}, 'r', True, minute_scope=scope,
                       expected_cutoffs={key + ':5m': 1000 for key in (HELD, OTHER)})
    symbols = {row['code']: row for row in view['seed']['symbols']}
    assert symbols['SH.600000']['coverage_state'] == 'ready'
    assert symbols['SH.600000']['minute_monitoring'] == 'auxiliary'
    assert symbols['SH.600088']['coverage_state'] == 'pending'


@pytest.mark.parametrize('force', [False, True])
def test_real_scheduler_dispatches_5m_only_for_ordinary_and_preserves_fixed_linked_policy(setup, monkeypatch, force):
    activate(monkeypatch)
    service, manual, live, clock = setup
    current = service._load()
    seed = state()['seed']
    seed['settings'] = {**current['settings']['screening'], **seed['settings']}
    current.update(seed=seed, enabled=True, holding_source_current=True, holding_symbols=[HELD],
                   force=force, notification_since=0, next_check_at=0, pending_symbols_count=2)
    service._schedule_monitor(clock[0], None)
    assert live.started
    assert current['active_periods'][OTHER] == ['5m']
    assert '1m' in current['active_periods'][HELD]
    assert live.started[-1]['timeframe_policy'] == timeframe_policy.POLICY
    assert live.started[-1]['frequencies'] == ['1m', '5m', '30m']


@pytest.mark.parametrize('ordinary', [list(POINTS), list(POINTS[:3]), ['2buy'], []])
def test_saved_legacy_policy_upgrades_once_and_keeps_buy_activation_windows(setup, ordinary):
    service, manual, live, clock = setup
    current = service._load()
    current['point_notifications'] = {'frequencies': ['1m', '5m', '30m'], 'holdings': list(POINTS), 'others': ordinary}
    current['point_notification_version'] = 7
    current['point_notification_since'] = {'holdings:1m:1buy': 42}
    service._save()
    def open_service():
        return SignalMonitor(service.root, screening=manual, monitor=live, now=lambda: clock[0])
    upgraded = deepcopy(open_service()._load())
    expected = [point for point in ordinary if point in POINTS[:3]]
    assert upgraded['point_notifications']['others'] == expected
    assert upgraded['point_notifications']['other_frequencies'] == ['5m']
    assert upgraded['point_notification_version'] == 8
    assert upgraded['point_notification_since']['holdings:1m:1buy'] == 42
    assert upgraded['point_notification_since'] == {'holdings:1m:1buy': 42}
    clock[0] += timedelta(seconds=10)
    restored = open_service()._load()
    assert restored['point_notification_since'] == upgraded['point_notification_since']
    assert restored['point_notification_version'] == 8


def test_explicit_legacy_post_keeps_its_type_choices_and_cannot_enable_nonholding_1m():
    old = {'frequencies': ['1m', '5m'], 'holdings': ['3sell'], 'others': ['1buy']}
    assert normalize_point_notifications(old) == {**old, 'other_frequencies': ['5m']}
    policy = default_point_notifications()
    policy['other_frequencies'] = ['1m']
    with pytest.raises(ValueError):
        normalize_point_notifications(policy)


def test_watchlist_only_start_and_holding_changes_keep_roles_and_retire_old_minute_signals(setup, monkeypatch):
    activate(monkeypatch)
    service, _, _, clock = setup
    held = [{'market': 'a', 'code': 'SH.600088', 'origin': 'watchlist', 'holding': True}]
    monkeypatch.setattr('cl_app.services.signal_monitor.holding_symbols', lambda: deepcopy(held))
    monkeypatch.setattr('cl_app.services.signal_monitor.watchlist_symbols',
                        lambda: [{'market': 'a', 'code': 'SH.600088', 'origin': 'watchlist'}])
    current = service._load()
    current.update(enabled=True, seed=None)
    service._sync_watchlist()
    assert current['seed']['settings']['frequencies'] == ['1m', '5m', '30m']
    assert _available_monitor_periods(current, HELD, 'r') == {'1m', '5m', '30m'}
    current['signals']['minute'] = {**held[0], 'id': 'minute', 'frequency': '1m', 'point_type': '1buy'}
    current['signals']['trade'] = {**held[0], 'id': 'trade', 'frequency': '5m', 'point_type': '1buy'}
    current['errors'] = [{**held[0], 'frequency': period, 'error': 'fixture'} for period in ('1m', '5m')]
    current['minute_force_pending'] = {HELD: 1}
    held.clear()
    service._sync_watchlist()
    assert set(current['signals']) == {'trade'}
    assert current['signals']['trade']['holding'] is False
    assert [row['frequency'] for row in current['errors']] == ['5m']
    assert current['minute_force_pending'] == {}
    assert _available_monitor_periods(current, HELD, 'r') == {'5m'}
    service._notification_rechecks[(HELD, '1m')] = {'revision': 'r', 'cutoff': 1000, 'expires': clock[0].timestamp() + 300}
    assert service._pending_notification_symbols(current, clock[0].timestamp()) == set()
    held.append({'market': 'a', 'code': 'SH.600088', 'origin': 'watchlist', 'holding': True})
    service._sync_watchlist()
    assert _available_monitor_periods(current, HELD, 'r') == {'1m', '5m', '30m'}
    assert HELD in current['force_pending_symbols']
