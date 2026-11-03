from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from chanlun.screening import timeframe_policy as policy
from chanlun.screening.cache import REPLAY_CHECKS
from tests.test_screening_timeframe_policy import pair


def test_opening_print_belongs_to_the_first_five_minute_candle_only():
    main, point, lower, child = pair('3buy')
    opening = int(datetime(2026, 9, 29, 9, 30, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp())
    point.update(anchor_at=opening + 300, available_at=opening + 600)
    main.update(source_closed_at=opening + 1200)
    main['screening_segments'][0].update(start_time=opening + 300, end_time=opening + 600)
    lower.update(source_closed_at=main['source_closed_at'])
    child['point'].update(anchor_at=opening, available_at=opening + 500)
    interval, matches = policy.matches_for(main, point, lower, [child])
    assert interval['pivot_start_inclusive'] and interval['start_inclusive'] and matches
    point['anchor_at'] += 300
    child['point']['anchor_at'] += 300
    assert not policy.matches_for(main, point, lower, [child])[1]
    point['anchor_at'] -= 300
    child['point']['anchor_at'] -= 300
    main['symbol'] = lower['symbol'] = 'AAPL.US'
    assert not policy.matches_for(main, point, lower, [child])[1]


def comparison_pair():
    main, point, lower, _ = pair()
    legs = []
    for index, (start, end) in enumerate([(100, 90), (90, 99), (99, 85)]):
        legs.append({'unit_id': 'u'+str(index), 'start_time': 900 + index * 240, 'end_time': 1140 + index * 240,
                     'start_tick': start, 'end_tick': end, 'direction': 'down' if start > end else 'up',
                     'locked': True, 'available_at': 1800})
    lower['screening_segments'] = legs
    alert = {'alert_id': 'local', 'status': 'confirmed', 'direction': 'down', 'source_kind': 'segment',
             'structural_level': 0, 'price_basis_revision': 'basis', 'anchor_at': 1600, 'anchor_tick': 100,
             'available_at': 2100, 'compare_leg_unit_ids': ['u0'], 'signal_leg_unit_ids': ['u2'],
             'comparison_form': 'center_oscillation', 'macd_area_ratio': .4}
    return main, point, lower, {'alert': alert, 'audit': {key: True for key in REPLAY_CHECKS}}


@pytest.mark.parametrize('kind', ['1buy', '2buy', '3buy'])
def test_replayed_internal_comparison_keeps_its_identity_and_requires_a_contained_chain(kind):
    main, point, lower, child = comparison_pair()
    point['point_type'] = kind
    interval, matches, assessment = policy.assess_for(main, point, lower, [child])
    assert matches == [child]
    result = policy.attach({'point': point}, interval, child, assessment=assessment)
    assert result['selection_status'] == 'confirmed'
    assert result['nested_confirmation']['evidence_type'] == 'internal_divergence'
    assert result['nested_confirmation']['point'] is None
    assert result['nested_confirmation']['divergence']['alert_id'] == 'local'
    lower['screening_segments'][0]['start_time'] = 600
    assert not policy.matches_for(main, point, lower, [child])[1], 'the reference leg cannot be outside the parent interval'


@pytest.mark.parametrize('damage', ['disconnected', 'unlocked', 'outside_pivot', 'wrong_price', 'future'])
def test_unrelated_or_unfinished_local_comparisons_cannot_confirm(damage):
    main, point, lower, child = comparison_pair()
    if damage == 'disconnected':
        lower['screening_segments'][1]['start_tick'] += 1
    elif damage == 'unlocked':
        lower['screening_segments'][1]['locked'] = False
    elif damage == 'outside_pivot':
        child['alert']['anchor_at'] = 1499
    elif damage == 'wrong_price':
        child['alert']['anchor_tick'] += 1
    else:
        child['alert']['available_at'] = lower['source_closed_at'] + 1
    assert not policy.matches_for(main, point, lower, [child])[1]


def test_negative_assessment_is_recomputed_instead_of_trusting_saved_waiting_flags(monkeypatch):
    main, point, lower, child = pair('3buy')
    monkeypatch.setattr('chanlun.screening.rules._native_center_signal_source_valid', lambda *_: True)
    child['point']['anchor_at'] = 1000
    interval, matches, assessment = policy.assess_for(main, point, lower, [child])
    assert not matches and assessment['reason'] == 'LOWER_PIVOT_WINDOW_MISMATCH'
    candidate = policy.attach({'point': point}, interval, assessment=assessment)
    assert policy.saved_binding_valid(candidate, main, lower)
    changed = deepcopy(candidate)
    changed['nested_confirmation']['assessment']['reason'] = 'LOWER_MATCHED'
    assert not policy.saved_binding_valid(changed, main, lower)
