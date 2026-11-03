"""Admission, continued observation and refutation are different decisions."""
from copy import deepcopy
from datetime import timedelta

import pandas as pd
import pytest

from chanlun.core.strict_structure.warning_lifecycle import SCHEMA, bind_warning, assess_warning
from tests.core.strict_structure.test_native_consolidation_preview import sample, build, snapshot
from tests.core.strict_structure.test_native_consolidation import Areas


def consolidation_case(down=False):
    values, center, _, frame, hist = sample(down)
    hist[:30] = [-10. if down else 10.] * 30
    dates = list(frame.date)
    strength = Areas({u.unit_id: sum(abs(v) for t, v in zip(dates, hist)
        if u.market_start <= t <= u.market_end and (v < 0 if down else v > 0)) or 1. for u in values})
    parts = values, center, strength, frame, hist
    alerts = build(parts)
    source = snapshot(parts, alerts)
    source.update(symbol='TEST', source_frequency='5m', point_preview_checked=True,
                  chart={'higher_macd_raw_hist': hist}, center_processing={'complete': True})
    alert = next(a for a in alerts if a['comparison_form'] == 'entry_departure')
    event = {'signal_type':'consolidation_divergence', 'point_type':'consolidation_down' if down else 'consolidation_up',
             'hypothesis_id':alert['hypothesis_id']}
    return bind_warning(source, event), source, frame


@pytest.mark.parametrize('down', [False, True])
def test_momentum_reexpansion_and_loss_of_doubled_area_margin_do_not_refute_an_existing_warning(down):
    h, source, frame = consolidation_case(down)
    original = deepcopy(source)
    sign = -1 if down else 1
    for size in (.01, 10.):
        current = deepcopy(source)
        current['chart']['higher_macd_raw_hist'][-1] = sign * size
        outcome = assess_warning(h, current, frame)
        assert outcome['state'] == 'observing'
        assert outcome['macd_area_c'] < outcome['macd_area_a']
    assert outcome['estimated_area_c'] >= outcome['macd_area_a']
    assert source == original


@pytest.mark.parametrize('down', [False, True])
def test_actual_comparison_area_reaching_reference_really_refutes_warning(down):
    h, source, frame = consolidation_case(down)
    initial = assess_warning(h, source, frame)
    source['chart']['higher_macd_raw_hist'][-1] = (-1 if down else 1) * (initial['macd_area_a'] - initial['macd_area_c'])
    outcome = assess_warning(h, source, frame)
    assert outcome['state'] == 'invalidated' and outcome['reason_code'] == 'AREA_DECAY_REFUTED'


@pytest.mark.parametrize('down', [False, True])
def test_price_extreme_moves_within_same_unfinished_comparison(down):
    h, source, frame = consolidation_case(down)
    frame = frame.copy()
    frame.loc[frame.index[-1], 'low' if down else 'high'] = h['anchor_tick'] + (-1 if down else 1)
    outcome = assess_warning(h, source, frame)
    assert outcome['state'] == 'candidate_updated'
    assert outcome['anchor_tick'] != h['anchor_tick']
    assert source['screening_segments'][-1]['locked'] is False


def point_case(kind='3sell'):
    buy = kind.endswith('buy')
    h, source, frame = consolidation_case(buy)
    h.update(signal_type='native_point_preview', point_type=kind, preview_key='original-candidate',
             parent_tick=h['anchor_tick'] + (-2 if buy else 2))
    h['core'] = {**h['core'], 'zd_tick':h['anchor_tick'] + (-8 if buy else 4),
                 'zg_tick':h['anchor_tick'] + (-4 if buy else 8)}
    source['levels'][0]['centers'][0]['core'] = deepcopy(h['core'])
    h['center_frames'][h['center_id']]['core'] = deepcopy(h['core'])
    return h, source, frame


@pytest.mark.parametrize('buy', [False, True])
def test_third_point_update_contact_and_crossing_have_distinct_outcomes(buy):
    h, source, frame = point_case('3buy' if buy else '3sell')
    side, sign = ('low', -1) if buy else ('high', 1)
    frame.loc[frame.index[-1], side] = h['anchor_tick'] + sign
    assert assess_warning(h, source, frame)['state'] == 'candidate_updated'
    frame.loc[frame.index[-1], side] = h['core']['zg_tick' if buy else 'zd_tick']
    assert assess_warning(h, source, frame)['reason_code'] == 'BOUNDARY_CONTACT'
    frame.loc[frame.index[-1], side] += sign
    assert assess_warning(h, source, frame)['reason_code'] == 'CORE_BOUNDARY_BROKEN'


@pytest.mark.parametrize('buy', [False, True])
def test_second_point_uses_parent_extreme_instead_of_moving_candidate_extreme(buy):
    h, source, frame = point_case('2buy' if buy else '2sell')
    side, sign = ('low', -1) if buy else ('high', 1)
    frame.loc[frame.index[-1], side] = h['anchor_tick'] + sign
    assert assess_warning(h, source, frame)['state'] == 'candidate_updated'
    frame.loc[frame.index[-1], side] = h['parent_tick'] + sign
    outcome = assess_warning(h, source, frame)
    assert outcome['state'] == 'invalidated' and outcome['reason_code'] == 'PARENT_EXTREME_BROKEN'


@pytest.mark.parametrize('fault', ['basis', 'unresolved', 'history', 'cutoff', 'macd', 'incomplete'])
def test_missing_or_incompatible_evidence_is_pending_never_invalidation(fault):
    h, source, frame = consolidation_case()
    if fault == 'basis':
        source['price_basis_revision'] = 'another-basis'
    if fault == 'unresolved':
        source['segment_construction'] = {'status':'unresolved'}
    if fault == 'history':
        frame = frame.iloc[-2:].copy()
    if fault == 'cutoff':
        source['source_closed_at'] += 60
    if fault == 'macd':
        source['chart']['higher_macd_raw_hist'][-1] = float('nan')
    if fault == 'incomplete':
        source['center_processing']['complete'] = False
    assert assess_warning(h, source, frame)['state'] == 'pending'


def test_same_candidate_confirmation_is_not_reported_as_invalidation():
    h, source, frame = point_case()
    source['levels'][0]['points'] = [{'preview_key':h['preview_key'], 'status':'confirmed', 'available_at':source['source_closed_at']}]
    assert assess_warning(h, source, frame)['state'] == 'confirmed'
    source['levels'][0]['points'] = []
    source['screening_segments'][-1]['locked'] = True
    assert assess_warning(h, source, frame)['reason_code'] == 'COMPLETED_WITHOUT_SIGNAL'


def test_changed_fixed_comparison_source_cannot_be_retained():
    h, source, frame = consolidation_case()
    source['screening_segments'][0]['end_tick'] += 1
    assert assess_warning(h, source, frame)['reason_code'] == 'STRUCTURE_REPLACED'


def test_future_close_is_never_used():
    h, source, frame = consolidation_case()
    future = frame.iloc[[-1]].copy()
    future['date'] += timedelta(minutes=5)
    future['high'] = 10000.
    assert assess_warning(h, source, pd.concat([frame, future]))['state'] == 'pending'
    assert h['schema'] == SCHEMA


def test_formed_first_leg_does_not_absorb_following_leg_momentum_while_waiting_for_lock():
    h, source, frame = point_case('1sell')
    source['chart']['higher_macd_raw_hist'][-1] = 1000.
    outcome = assess_warning(h, source, frame)
    assert outcome['state'] == 'observing'
    assert outcome['measurement_end_at'] == source['screening_segments'][-1]['end_time']
    source['screening_segments'][-1]['forming'] = True
    assert assess_warning(h, source, frame)['reason_code'] == 'AREA_DECAY_REFUTED'
