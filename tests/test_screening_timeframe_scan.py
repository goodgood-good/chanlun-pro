"""Joined rows use on-demand dependencies, causal clocks and normal replay."""
from copy import deepcopy

import pytest

from chanlun.screening import runner, timeframe_scan, timeframe_policy as policy
from chanlun.screening.cache import REPLAY_CHECKS
from tests.test_screening_timeframe_policy import pair


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    main, point, lower, child = pair('3buy')
    center = {'center_id': 'C'}
    point['center_id'] = child['point']['center_id'] = 'C'
    main['levels'] = [{'points': [point], 'centers': [center]}]
    lower['levels'][0]['centers'] = [center]
    high = {**main, 'source_frequency': '30m', 'source_closed_at': 1800, 'levels': [{'centers': [], 'points': []}]}
    native = {'point': point, 'center': center, 'reasons': [], 'audit': {key: True for key in REPLAY_CHECKS}}
    rows = {period: {'frequency': period, 'selected': [deepcopy(native if period != '1m' else child)],
                    'observations': [], 'data_errors': [], 'reason_counts': {}, 'source_closed_at': snapshot['source_closed_at']}
            for period, snapshot in [('5m', main), ('1m', lower), ('30m', high)]}
    sources = {'5m': main, '1m': lower, '30m': high}
    calls, replays = [], []
    def raw_scan(stock, settings, contexts, run_dir, *, _inputs=None, **kwargs):
        period, = settings['frequencies']
        calls.append((period, {**deepcopy(contexts[period]), 'confirmation_only': kwargs.get('_confirmation_only', False)}))
        if _inputs is not None and not rows[period]['data_errors']:
            _inputs[period] = {'snapshot': deepcopy(sources[period]), 'frame': object(),
                              'reviewed': {}, 'cache_path': None, 'key': None}
        return {'rows': [deepcopy(rows[period])]}
    def review(snapshot, frame, candidates, code, frequency):
        replays.append(frequency)
        return [{**c, 'audit': {key: True for key in REPLAY_CHECKS}} for c in candidates], []
    monkeypatch.setattr(runner, 'scan_symbol', raw_scan)
    monkeypatch.setattr(runner, '_review_native_center_signals', review)
    monkeypatch.setattr(runner, '_save_evidence', lambda directory, code, period, frame, snapshot, **kwargs: {'period': period, **kwargs})
    monkeypatch.setattr(timeframe_scan, '_native_center_signal_source_valid', lambda *_: True)
    monkeypatch.setattr('chanlun.screening.rules._native_center_signal_source_valid', lambda *_: True)
    monkeypatch.setattr(runner, '_a_calendar_context', lambda observed, period, *_:
                        {'frequency': period, 'cutoff': int(observed.timestamp()) if period == '1m' else 1800})
    settings = {'frequencies': ['5m', '30m'], 'strategy': 'direct_structural', 'timeframe_policy': policy.POLICY,
                'point_types': ['3buy'], 'recent_sessions': 5, 'max_anchor_sessions': 20}
    def scan():
        return timeframe_scan.scan({'market': 'a', 'code': 'SH.600000', 'name': 'fixture'}, settings,
            {'1m': {'frequency': '1m', 'cutoff': 2400}, '5m': {'frequency': '5m', 'cutoff': 2400},
             '30m': {'frequency': '30m', 'cutoff': 1800}}, tmp_path)
    scan.sources = sources
    return scan, settings, rows, calls, replays


def test_dependencies_only_run_for_a_main_setup_and_background_does_not_publish(pipeline):
    scan, settings, rows, calls, replays = pipeline
    rows['5m']['selected'] = []
    result = scan()
    assert [period for period, _ in calls] == ['5m', '30m']
    assert all(not row['selected'] for row in result['rows'])
    assert replays == []
    assert calls[-1][1]['confirmation_only'] is True, 'background does not replay unused trade candidates'


def test_candidate_background_plan_preserves_the_complete_trade_result(pipeline):
    scan, settings, rows, calls, replays = pipeline
    reference = deepcopy(scan()['rows'][0])
    calls.clear(); replays.clear()
    settings.update(policy.screening_settings(settings))
    result = scan()['rows']
    assert settings['frequencies'] == ['5m']
    assert [{k:v for k,v in row.items() if k!='period_timings_ms'} for row in result] == [
        {k:v for k,v in reference.items() if k!='period_timings_ms'}]
    assert [period for period, _ in calls] == ['5m', '30m', '1m']


def test_period_costs_are_exclusive_and_cover_the_complete_join(pipeline,monkeypatch):
    from itertools import count
    ticks=count()
    monkeypatch.setattr(timeframe_scan.time,'monotonic',lambda:next(ticks)/1000)
    row=pipeline[0]()['rows'][0]
    measured=row['period_timings_ms']
    assert all(measured[p]>0 for p in ('5m','1m','30m'))
    assert sum(value for key,value in measured.items() if key!='total')==pytest.approx(measured['total'],abs=.003)


def test_no_candidate_has_no_independent_background_task(pipeline):
    scan, settings, rows, calls, replays = pipeline
    settings.update(policy.screening_settings(settings))
    rows['5m']['selected'] = []
    result = scan()['rows']
    assert len(result) == 1 and result[0]['selected'] == []
    assert [period for period, _ in calls] == ['5m'] and not replays


def test_later_center_exclusion_skips_its_unused_confirmation_dependency(pipeline, monkeypatch):
    from chanlun.screening.first_center_sequence import CenterSequences
    scan, settings, _rows, calls, replays = pipeline
    settings['alert_mode'] = 'native_center_signals'
    monkeypatch.setattr(CenterSequences, 'assess', lambda *_: {'state': 'later'})
    result = scan()['rows'][0]
    assert not result['selected'] and not result['observations']
    assert result['selection_policy_exclusions'] == 1
    assert result['recent_rejections'][0]['reasons'] == ['THIRD_BUY_NOT_FIRST_UP_CENTER']
    assert [period for period, _ in calls] == ['5m', '30m']
    assert not replays


def test_confirmed_main_publishes_only_after_lower_replay_at_same_cutoff(pipeline):
    scan, settings, rows, calls, replays = pipeline
    result = scan()
    trade, higher = result['rows']
    assert trade['selected'][0]['nested_confirmation']['state'] == 'confirmed'
    assert trade['selected'][0]['operating_role'] == 'trade'
    assert trade['confirmation_evidence'] == {'period': '1m', 'namespace': 'trade_5m'}
    assert higher['selected'] == [] and higher['operating_role'] == 'background'
    assert next(context['cutoff'] for period, context in calls if period == '1m') == 2400
    assert replays == ['1m']


def test_lower_data_failure_keeps_main_only_as_a_waiting_setup(pipeline):
    scan, settings, rows, calls, replays = pipeline
    rows['1m']['data_errors'] = ['DATA_GAPS']
    trade = scan()['rows'][0]
    assert trade['selected'] == []
    candidate, = trade['observations']
    assert candidate['point']['status'] == 'confirmed'
    assert candidate['selection_status'] == 'approaching'
    assert candidate['nested_confirmation']['state'] == 'unavailable'
    assert candidate['nested_confirmation']['data_errors'] == ['DATA_GAPS']
    assert not replays


def test_failed_lower_replay_never_leaks_a_confirmed_main(pipeline, monkeypatch):
    scan, settings, rows, calls, replays = pipeline
    monkeypatch.setattr(runner, '_review_native_center_signals', lambda *args: ([], args[2]))
    trade = scan()['rows'][0]
    assert not trade['selected']
    assert trade['observations'][0]['nested_confirmation']['data_errors'] == ['LOWER_REPLAY_FAILED']


def test_short_term_output_stays_separate_from_the_trade_confirmation(pipeline):
    scan, settings, rows, calls, replays = pipeline
    settings['frequencies'] = ['1m']
    # No main scan is needed when the worker is assigned a short-term period.
    result = scan()['rows'][0]
    assert result['operating_role'] == result['selected'][0]['operating_role'] == 'short_term'
    assert 'nested_confirmation' not in result['selected'][0]
    assert [period for period, _ in calls] == ['1m']


def test_a_forming_main_is_checked_early_but_cannot_become_a_confirmed_trade(pipeline):
    scan, _settings, rows, calls, _replays = pipeline
    candidate = rows['5m']['selected'].pop()
    candidate['point'].update(status='approaching', confirmed_at=None, missing_conditions=['unfinished_segment_lock'])
    candidate['reasons'] = ['NOT_CONFIRMED']
    rows['5m']['observations'] = [candidate]
    result = scan()['rows'][0]
    assert any(period == '1m' for period, _ in calls)
    assert not result['selected']
    item, = result['observations']
    assert item['nested_confirmation']['check_state'] == 'confirmed'
    assert item['selection_status'] == 'approaching'
    assert item['reasons'] == ['NOT_CONFIRMED']


def test_a_failed_earlier_match_does_not_discard_a_later_replayed_match(pipeline, monkeypatch):
    scan, _settings, _rows, _calls, _replays = pipeline
    lower = scan.sources['1m']
    later = deepcopy(lower['levels'][0]['points'][0])
    later.update(point_id='later-valid', available_at=2100)
    lower['levels'][0]['points'].append(later)
    monkeypatch.setattr(runner, '_review_native_center_signals', lambda snapshot, frame, candidates, *_:
        ([{**c, 'audit': {key: True for key in REPLAY_CHECKS}} for c in candidates if c['point']['point_id'] == 'later-valid'], []))
    candidate, = scan()['rows'][0]['selected']
    assert candidate['nested_confirmation']['point']['point_id'] == 'later-valid'
