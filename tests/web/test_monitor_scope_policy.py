"""A slower 1m check is allowed only after successful current negative proof."""

from copy import deepcopy

import pytest

from cl_app.services.monitor_scope import (
    CHECKPOINTS, build_minute_scope_index, minute_cadence,
    minute_scope_summary, record_minute_checkpoint,
)


KEY = 'a:SH.600000'
REVISION = 'verified-runtime'
CUTOFF = 1_000


def state():
    stock = {'market': 'a', 'code': 'SH.600000', 'name': 'fixture'}
    result = {'seed': {'run_id': 'screening-source', 'codes': [KEY], 'symbols': [stock],
                       'screening_symbols': [deepcopy(stock)], 'settings': {'frequencies': ['1m', '5m', '30m']}},
              'holding_symbols': [], 'signals': {}, 'unverified_signals': {},
              'selection_checks': {f'{KEY}:1m': {'symbol': KEY, 'revision': REVISION,
                  'error': False, 'identities': [], 'input_fingerprint': 'verified-input',
                  'cutoff': CUTOFF, 'checked_at': 2_000}}}
    assert record_minute_checkpoint(result, KEY, REVISION, CUTOFF)
    return result


@pytest.mark.parametrize('delta,due', [(0, False), (60, False), (240, False), (299, False), (300, True), (301, True)])
def test_five_minute_boundary_uses_closed_1m_clock_and_keeps_classification(delta, due):
    value = minute_cadence(state(), KEY, REVISION, CUTOFF + delta)
    assert value == {'classification': 'background', 'cold': True, 'eligible': due,
        'due': due, 'reason': 'verified_negative', 'cadence_seconds': 300,
        'last_check_cutoff': CUTOFF, 'next_due_at': CUTOFF + 300, 'clock_known': True}


def test_attempted_cutoff_and_other_period_success_do_not_advance_minute_checkpoint():
    value = state()
    value['attempted_cutoffs'] = {KEY: CUTOFF + 240}
    value['attempted_monitor_periods'] = {f'{KEY}:1m': {'cutoff': CUTOFF + 240, 'revision': REVISION}}
    value['selection_checks'][f'{KEY}:5m'] = {**value['selection_checks'][f'{KEY}:1m'], 'cutoff': CUTOFF + 240}
    before = deepcopy(value[CHECKPOINTS])
    assert not record_minute_checkpoint(value, KEY, REVISION, CUTOFF + 240)
    assert value[CHECKPOINTS] == before
    assert minute_cadence(value, KEY, REVISION, CUTOFF + 299)['eligible'] is False
    assert minute_cadence(value, KEY, REVISION, CUTOFF + 300)['eligible'] is True


@pytest.mark.parametrize('field,new_value', [
    ('error', True), ('error', None), ('revision', 'old-runtime'), ('cutoff', True),
    ('cutoff', '1000'), ('cutoff', None), ('identities', None), ('identities', ()),
    ('input_fingerprint', ''), ('input_fingerprint', None),
])
def test_insufficient_or_failed_proof_never_enters_background(field, new_value):
    value = state()
    value['selection_checks'][f'{KEY}:1m'][field] = new_value
    previous = deepcopy(value[CHECKPOINTS])
    assert minute_cadence(value, KEY, REVISION, CUTOFF)['cold'] is False
    assert not record_minute_checkpoint(value, KEY, REVISION, CUTOFF)
    assert value[CHECKPOINTS] == previous


@pytest.mark.parametrize('kind', ['1buy', '2buy', '3buy', '1sell', '2sell', '3sell'])
def test_any_current_buy_or_sell_immediately_restores_minute_cadence(kind):
    value = state()
    value['selection_checks'][f'{KEY}:1m']['identities'] = ['new-point']
    value['signals']['new-point'] = {'market': 'a', 'code': 'SH.600000', 'frequency': '1m',
                                    'stage': 'confirmed', 'point_type': kind}
    result = minute_cadence(value, KEY, REVISION, CUTOFF + 60)
    assert result['classification'] == 'realtime' and result['eligible']
    assert result['reason'] == 'active_minute_signal'


@pytest.mark.parametrize('protection,reason', [
    ('holding', 'holding'), ('watchlist', 'watchlist'), ('risk', 'active_risk_plan'),
    ('unverified', 'unverified_signal'), ('recovery', 'candidate_recovery_pending'),
    ('notification', 'pending_minute_notification'), ('independent', 'independent_member'),
])
def test_required_protected_sources_stay_realtime(protection, reason):
    value = state()
    stock = {'market': 'a', 'code': 'SH.600000', 'frequency': '1m'}
    if protection == 'holding':
        value['holding_symbols'] = [KEY]
    elif protection == 'watchlist':
        value['seed']['symbols'][0]['origin'] = 'watchlist'
    elif protection == 'risk':
        value['holding_risk_plans'] = {'plan': {**stock, 'active': True, 'frequency': '30m'}}
    elif protection == 'unverified':
        value['unverified_signals']['point'] = stock
    elif protection == 'recovery':
        value['seed']['screening_rechecks'] = {KEY: stock}
    elif protection == 'notification':
        value['pending_notifications'] = {'notification': {**stock, 'stage': 'confirmed'}}
    elif protection == 'independent':
        value['seed']['screening_symbols'] = []
    actual = minute_cadence(value, KEY, REVISION, CUTOFF)
    assert actual['eligible'] and not actual['cold'] and actual['reason'] == reason


@pytest.mark.parametrize('kind', ['other_signal', 'other_unverified', 'other_notification', 'disabled_notification',
                                   'suppressed_notification', 'unauthorized_notification', 'expired_stage', 'inactive_risk'])
def test_unrelated_or_inactive_metadata_does_not_force_a_minute_check(kind):
    value = state()
    item = {'market': 'a', 'code': 'SH.600000', 'frequency': '5m', 'stage': 'confirmed'}
    if kind == 'other_signal':
        value['signals'] = {'p': item}
    elif kind == 'other_unverified':
        value['unverified_signals'] = {'p': item}
    elif kind == 'inactive_risk':
        value['holding_risk_plans'] = {'p': {**item, 'active': False}}
    else:
        event = {**item, 'frequency': '1m'}
        if kind == 'other_notification':
            event['frequency'] = '5m'
        elif kind == 'disabled_notification':
            value['dingtalk_enabled'] = False
        elif kind == 'suppressed_notification':
            event['external_notifications'] = False
        elif kind == 'unauthorized_notification':
            event['notification_authorized'] = False
        elif kind == 'expired_stage':
            event['stage'] = 'invalidated'
        value['pending_notifications'] = {'p': event}
    assert minute_cadence(value, KEY, REVISION, CUTOFF)['cold']


def test_explicit_force_and_specific_minute_notification_recheck_override_wait():
    value = state()
    for options, reason in [({'manual_override': True}, 'explicit_check'),
                            ({'notification_recheck': True}, 'minute_notification_recheck')]:
        decision = minute_cadence(value, KEY, REVISION, CUTOFF, **options)
        assert decision['eligible'] and not decision['cold'] and decision['reason'] == reason
    assert minute_cadence(value, KEY, REVISION, CUTOFF)['eligible'] is False


@pytest.mark.parametrize('change', ['missing', 'seed', 'revision', 'unproved_future', 'readmitted'])
def test_new_candidate_or_unproved_checkpoint_receives_first_live_check(change):
    value = state()
    if change == 'missing':
        value.pop(CHECKPOINTS)
    elif change == 'seed':
        value['seed']['run_id'] = 'new-screening-source'
    elif change == 'revision':
        value[CHECKPOINTS][KEY]['revision'] = 'another-version'
    elif change == 'unproved_future':
        value[CHECKPOINTS][KEY]['cutoff'] += 60
    else:
        value['notification_symbol_since'] = {KEY: 2_001}
    assert minute_cadence(value, KEY, REVISION, CUTOFF + 60)['classification'] == 'realtime'


def test_successful_negative_after_check_resets_background_interval_without_rewinding():
    value = state()
    check = value['selection_checks'][f'{KEY}:1m']
    check.update(cutoff=CUTOFF + 300, checked_at=2_300)
    assert record_minute_checkpoint(value, KEY, REVISION, CUTOFF + 300)
    assert minute_cadence(value, KEY, REVISION, CUTOFF + 599)['eligible'] is False
    assert minute_cadence(value, KEY, REVISION, CUTOFF + 600)['eligible'] is True
    before = deepcopy(value[CHECKPOINTS])
    check['cutoff'] = CUTOFF
    assert not record_minute_checkpoint(value, KEY, REVISION, CUTOFF)
    assert value[CHECKPOINTS] == before


def test_closed_market_clock_does_not_advance_with_wall_clock_and_reopens_due():
    value = state()
    value['last_check_at'] = 1_000_000
    value['next_check_at'] = 2_000_000
    assert minute_cadence(value, KEY, REVISION, CUTOFF)['eligible'] is False
    assert minute_cadence(value, KEY, REVISION, CUTOFF + 80_000)['eligible'] is True
    assert minute_cadence(value, KEY, REVISION, CUTOFF - 60)['reason'] == 'closed_clock_reversed'
    assert minute_cadence(value, KEY, REVISION, None)['eligible'] is True


def test_summary_stays_compact_and_classification_counts_do_not_change_when_due():
    value = state()
    before = deepcopy(value)
    waiting = minute_scope_summary(value, REVISION, {KEY: CUTOFF + 60})
    due = minute_scope_summary(value, REVISION, {KEY: CUTOFF + 300})
    for summary in (waiting, due):
        assert {k:v for k,v in summary.items() if k != 'per_symbol'} == {
            'frequency': '1m', 'foreground': 0, 'background': 1, 'auxiliary': 0, 'interval_seconds': 300, 'total': 1}
        assert set(summary['per_symbol']) == {KEY}
        assert 'signals' not in summary['per_symbol'][KEY]
    assert waiting['per_symbol'][KEY]['due'] is False
    assert due['per_symbol'][KEY]['due'] is True
    unknown = minute_scope_summary(value, REVISION)['per_symbol'][KEY]
    assert unknown['due'] is None and unknown['clock_known'] is False
    assert value == before


def test_summary_excludes_disabled_minute_period_except_bound_risk():
    value = state()
    value['seed']['settings']['frequencies'] = ['5m', '30m']
    assert minute_scope_summary(value, REVISION)['total'] == 0
    value['holding_risk_plans'] = {'p': {'market': 'a', 'code': 'SH.600000', 'frequency': '1m', 'active': True}}
    assert minute_scope_summary(value, REVISION)['foreground'] == 1


def test_prebuilt_index_avoids_rescanning_signal_or_membership_lists():
    value = state()
    index = build_minute_scope_index(value)
    class Forbidden:
        def __iter__(self):
            raise AssertionError('scope decision rescanned members')
        def values(self):
            raise AssertionError('scope decision rescanned signals')
    value['signals'] = Forbidden()
    value['unverified_signals'] = Forbidden()
    value['seed']['symbols'] = Forbidden()
    value['seed']['screening_symbols'] = Forbidden()
    for offset in range(800):
        assert minute_cadence(value, KEY, REVISION, CUTOFF + offset, scope_index=index)['cold']


@pytest.mark.parametrize('field', ['unverified_signals', 'signals', 'holding_risk_plans', 'pending_notifications'])
def test_opaque_private_rows_are_not_copied_and_cannot_justify_background(field):
    value = state()
    class NotCopied(dict):
        def __deepcopy__(self, memo):
            raise AssertionError('private payload copied by summary')
    value[field] = NotCopied(large_internal_payload='not visible')
    summary = minute_scope_summary(value, REVISION, {KEY: CUTOFF})
    assert summary['foreground'] == 1 and summary['background'] == 0
    assert 'large_internal_payload' not in repr(summary)


@pytest.mark.parametrize('field', ['check', 'checkpoint'])
def test_unreadable_check_or_checkpoint_is_realtime(field):
    value = state()
    if field == 'check':
        value['selection_checks'][f'{KEY}:1m'] = 'unreadable'
    else:
        value[CHECKPOINTS][KEY] = 'unreadable'
    assert minute_scope_summary(value, REVISION)['foreground'] == 1
