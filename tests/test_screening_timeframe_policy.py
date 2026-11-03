"""Exact interval, clock, side, evidence and admission regressions."""
from copy import deepcopy

import pytest

from chanlun.screening import timeframe_policy as policy
from chanlun.screening.cache import REPLAY_CHECKS


def pair(kind='1buy'):
    side = 'buy' if kind.endswith('buy') else 'sell'
    point = {'point_id': 'main', 'center_id': 'C', 'point_type': kind, 'side': side, 'status': 'confirmed',
             'source_kind': 'segment', 'structural_level': 0, 'price_basis_revision': 'basis',
             'evidence_scope': 'native_center_signal', 'anchor_unit_id': 'M', 'anchor_at': 1800,
             'anchor_tick': 100, 'available_at': 2100, 'missing_conditions': [],
             'divergence': {'signal_leg_unit_ids': ['M']}}
    main = {'symbol': 'SH.600000', 'source_frequency': '5m', 'analysis_scope': 'native_centers',
            'source_started_at': 60, 'source_closed_at': 2400, 'price_basis_revision': 'basis',
            'structure_price_quantum': '.01',
            'screening_segments': [{'unit_id': 'M', 'start_time': 900, 'end_time': 1980}]}
    child = {**point, 'point_id': 'lower', 'point_type': '1' + side, 'anchor_at': 1740,
             'available_at': 2040, 'divergence': {'kind': 'native_two_center_divergence',
                                                'signal_leg_unit_ids': ['L']}}
    lower = {**main, 'source_frequency': '1m', 'levels': [{'points': [child], 'centers': []}],
             'screening_segments': [{'unit_id': 'L', 'start_time': 1200, 'end_time': 1860}]}
    candidate = {'point': child, 'reasons': [], 'audit': {key: True for key in REPLAY_CHECKS}}
    return main, point, lower, candidate


@pytest.mark.parametrize('kind', ['1buy', '2buy', '3buy', '1sell', '2sell', '3sell'])
def test_all_six_types_require_matching_lower_confirmation(kind):
    main, point, lower, candidate = pair(kind)
    interval, matches = policy.matches_for(main, point, lower, [candidate])
    joined = policy.attach({'point': point, 'reasons': []}, interval, matches[0])
    assert joined['selection_status'] == 'confirmed'
    assert joined['selection_available_at'] == max(point['available_at'], candidate['point']['available_at'])
    assert interval['kind'] == {'1': 'terminal_divergence', '2': 'first_retest_after_first',
                                '3': 'first_return_outside_center'}[kind[0]]
    waiting = policy.attach({'point': point, 'reasons': []}, interval)
    assert waiting['selection_status'] == 'approaching'
    assert waiting['reasons'] == [policy.PENDING]
    assert waiting['selection_missing_conditions'] == ['lower_1m_confirmation']
    assert point['status'] == 'confirmed', 'the original chart point is immutable'


@pytest.mark.parametrize('field,value', [('anchor_at', 1499), ('anchor_at', 1801),
    ('anchor_tick', 99), ('side', 'sell'), ('status', 'approaching'),
    ('price_basis_revision', 'different'), ('available_at', 2401), ('available_at', 1700)])
def test_unrelated_price_side_interval_or_future_lower_point_cannot_confirm(field, value):
    main, point, lower, candidate = pair()
    candidate['point'][field] = value
    assert policy.matches_for(main, point, lower, [candidate])[1] == []


@pytest.mark.parametrize('field,value', [('source_frequency', '5m'), ('symbol', 'SH.600001'),
    ('source_closed_at', 2460), ('price_basis_revision', 'different'),
    ('structure_price_quantum', '.001'), ('source_started_at', 900)])
def test_incompatible_or_incomplete_lower_input_is_rejected(field, value):
    main, point, lower, candidate = pair()
    lower[field] = value
    with pytest.raises(ValueError):
        policy.matches_for(main, point, lower, [candidate])


def test_first_class_requires_contained_smaller_divergence():
    main, point, lower, candidate = pair()
    candidate['point']['point_type'] = '2buy'
    assert not policy.matches_for(main, point, lower, [candidate])[1]
    candidate['point']['point_type'] = '1buy'
    lower['screening_segments'][0]['start_time'] = 600
    assert not policy.matches_for(main, point, lower, [candidate])[1]
    lower['screening_segments'][0]['start_time'] = 1200
    lower['screening_segments'][0]['end_time'] = 2100
    assert not policy.matches_for(main, point, lower, [candidate])[1]


def test_forming_main_and_unreplayed_lower_never_publish_confirmed():
    main, point, lower, candidate = pair()
    interval, _ = policy.matches_for(main, point, lower, [candidate])
    point.update(status='approaching', missing_conditions=['unfinished_segment_lock'])
    joined = policy.attach({'point': point, 'reasons': ['NOT_CONFIRMED']}, interval, candidate)
    assert joined['selection_status'] == 'approaching'
    point.update(status='confirmed', missing_conditions=[])
    candidate['audit'][REPLAY_CHECKS[0]] = False
    assert policy.attach({'point': point}, interval, candidate)['selection_status'] == 'approaching'


def test_frozen_binding_rechecks_catalog_and_audit(monkeypatch):
    main, point, lower, candidate = pair()
    monkeypatch.setattr('chanlun.screening.rules._native_center_signal_source_valid', lambda *_: True)
    interval, _, assessment = policy.assess_for(main, point, lower, [candidate])
    joined = policy.attach({'point': point, 'reasons': []}, interval, candidate, assessment=assessment)
    lower['screening_interval_signals'] = [deepcopy(candidate)]
    assert policy.saved_binding_valid(joined, main, lower)
    changed = deepcopy(joined)
    changed['nested_confirmation']['point']['anchor_tick'] += 1
    assert not policy.saved_binding_valid(changed, main, lower)
    lower['screening_interval_signals'][0]['audit'][REPLAY_CHECKS[0]] = False
    assert not policy.saved_binding_valid(joined, main, lower)


def test_countertrend_background_annotates_without_vetoing_trade(monkeypatch):
    monkeypatch.setattr('chanlun.screening.rules._native_center_body_source_valid', lambda *_: True)
    main, point, lower, candidate = pair()
    higher = {**main, 'source_frequency': '30m', 'source_closed_at': 1800,
              'levels': [{'centers': [
                  {'center_id': 'a', 'points': [{'time': 60}], 'envelope': {'dd_tick': 110, 'gg_tick': 130}},
                  {'center_id': 'b', 'points': [{'time': 1200}], 'envelope': {'dd_tick': 80, 'gg_tick': 100}}]}]}
    context = policy.background(higher, side='buy', expected_cutoff=1800)
    assert context['direction'] == 'down' and context['alignment'] == 'countertrend'
    interval, _ = policy.matches_for(main, point, lower, [candidate])
    joined = {**policy.attach({'point': point}, interval, candidate), 'background_30m': context}
    assert joined['selection_status'] == 'confirmed'
    assert policy.background(higher, side='buy', expected_cutoff=3600)['state'] == 'unavailable'
    assert policy.background(higher, side='buy', expected_cutoff=1800, as_of=1500)['state'] == 'unavailable'
    assert policy.background(higher, side='buy', expected_cutoff=1800, price_basis='other')['state'] == 'unavailable'
