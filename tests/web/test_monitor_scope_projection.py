"""Planned minute checks remain distinct from stale or failed evidence."""
from copy import deepcopy

import pytest

from cl_app.services.monitor_view import paged_state


def inputs():
    symbols = [{'market': 'a', 'code': code, 'name': code} for code in ('SH.600000', 'SH.600001')]
    checks = {f"a:{row['code']}:{period}": {'revision': 'current', 'error': False, 'cutoff': 1200}
              for row in symbols for period in ('1m', '5m')}
    raw = {'seed': {'run_id': 'seed', 'symbols': symbols, 'codes': [f"a:{s['code']}" for s in symbols],
                    'settings': {'frequencies': ['1m', '5m']}},
           'signals': {}, 'events': [], 'errors': [], 'selection_checks': checks,
           'holding_symbols': ['a:SH.600001']}
    expected = {key: 1260 if key.endswith(':1m') and 'SH.600000:' in key else 1200 for key in checks}
    scope = {'per_symbol': {'a:SH.600000': {'classification': 'background', 'eligible': False,
                                         'due': False, 'clock_known': True},
                            'a:SH.600001': {'classification': 'realtime', 'eligible': True}}}
    return raw, expected, scope


def test_planned_background_wait_is_not_reported_as_late_and_keeps_full_pool():
    raw, expected, scope = inputs()
    before = deepcopy(raw)
    result = paged_state(raw, {}, 'current', True, expected_cutoffs=expected, minute_scope=scope)
    rows = {row['code']: row for row in result['seed']['symbols']}
    assert rows['SH.600000']['coverage_state'] == 'scheduled'
    assert rows['SH.600000']['minute_monitoring'] == 'background'
    assert rows['SH.600001']['minute_monitoring'] == 'realtime'
    assert result['coverage'] == {'total': 2, 'ready': 1, 'updating': 0, 'limited': 0, 'pending': 0, 'scheduled': 1}
    assert result['seed']['count'] == 2 and raw == before


@pytest.mark.parametrize('condition,health', [('due', 'updating'), ('five_minute_late', 'updating'),
                                            ('error', 'limited'), ('missing', 'pending')])
def test_scope_cannot_hide_due_checks_other_period_lag_or_uncertain_evidence(condition, health):
    raw, expected, scope = inputs()
    key = 'a:SH.600000:1m'
    if condition == 'due':
        scope['per_symbol']['a:SH.600000'].update(eligible=True, due=True)
    elif condition == 'five_minute_late':
        raw['selection_checks']['a:SH.600000:5m']['cutoff'] = 900
    elif condition == 'error':
        raw['selection_checks'][key]['error'] = True
    else:
        raw['selection_checks'].pop(key)
    result = paged_state(raw, {}, 'current', True, expected_cutoffs=expected, minute_scope=scope)
    row = next(row for row in result['seed']['symbols'] if row['code'] == 'SH.600000')
    assert row['coverage_state'] == health
    assert result['coverage']['scheduled'] == 0
