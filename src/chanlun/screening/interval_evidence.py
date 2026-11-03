"""Causal lower-interval evidence in the declared native-segment framework.

Local author text: D:/缠论/chanlun_lesson_corpus/L061_*:130-163 compares
internal legs while successively narrowing the terminal interval. L065_*:
610-646 distinguishes this operational segment method from recursive grades.
A local comparison is kept as its own evidence type, never renamed a 1-buy.
"""
from decimal import Decimal

from .interval_clock import contains, minute_start


def payload(candidate):
    return candidate.get('point') or candidate['alert']


def identity(candidate):
    value = payload(candidate)
    return value.get('point_id') or value['alert_id']


def order(candidate):
    return payload(candidate)['available_at'], identity(candidate)


def catalog(snapshot):
    from .rules import _native_center_signal_source_valid, native_consolidation_source_valid
    centers = {c['center_id']: c for c in snapshot['levels'][0].get('centers', [])}
    points = [{'point': p, 'center': centers.get(p['center_id']), 'reasons': []}
              for p in snapshot['levels'][0].get('points', [])
              if p.get('status') == 'confirmed' and p.get('evidence_scope') == 'native_center_signal'
              and _native_center_signal_source_valid(snapshot, p, centers, snapshot['screening_segments'])]
    comparisons = [{'alert': a, 'reasons': []} for a in snapshot.get('native_consolidation_alerts', [])
                   if a.get('status') == 'confirmed' and native_consolidation_source_valid(snapshot, a)]
    return points + comparisons


def assess(main, point, lower, candidates, interval):
    if (main.get('analysis_scope') != 'native_centers' or lower.get('analysis_scope') != 'native_centers'
            or lower.get('source_frequency') != '1m' or lower.get('symbol') != main.get('symbol')
            or lower.get('source_closed_at') != main.get('source_closed_at')
            or lower.get('price_basis_revision') != main.get('price_basis_revision')
            or Decimal(str(lower.get('structure_price_quantum'))) != Decimal(str(main.get('structure_price_quantum')))):
        raise ValueError('LOWER_CONTEXT_MISMATCH')
    history_start = interval['start_after'] + (0 if interval.get('start_inclusive') else 60)
    if type(lower.get('source_started_at')) is not int or lower['source_started_at'] > history_start:
        raise ValueError('LOWER_HISTORY_INCOMPLETE')
    units = lower.get('screening_segments', [])
    positions = {u['unit_id']: i for i, u in enumerate(units)}
    by_id = {u['unit_id']: u for u in units}
    counts = {name: 0 for name in ('sources', 'direction', 'inside_interval', 'inside_pivot', 'same_price', 'contained')}
    matches = []
    for candidate in candidates:
        value = payload(candidate)
        counts['sources'] += 1
        local = 'alert' in candidate
        side = ('buy' if value.get('direction') == 'down' else 'sell') if local else value.get('side')
        if (value.get('status') != 'confirmed' or side != point['side']
                or value.get('source_kind') != 'segment' or value.get('structural_level') != 0
                or value.get('price_basis_revision') != main['price_basis_revision']
                or not value['anchor_at'] <= value['available_at'] <= lower['source_closed_at']):
            continue
        if not local and (value.get('evidence_scope') != 'native_center_signal'
                or point['point_type'].startswith('1') and (
                    value.get('point_type') != '1' + point['side']
                    or (value.get('divergence') or {}).get('kind') != 'native_two_center_divergence')
                or value.get('point_type') not in {n + point['side'] for n in ('1', '2', '3')}):
            continue
        counts['direction'] += 1
        if not contains(interval, value['anchor_at']):
            continue
        counts['inside_interval'] += 1
        if not contains(interval, value['anchor_at'], pivot=True):
            continue
        counts['inside_pivot'] += 1
        if value.get('anchor_tick') != point['anchor_tick']:
            continue
        counts['same_price'] += 1
        identifiers = (value.get('signal_leg_unit_ids', []) if local else
                       (value.get('divergence') or {}).get('signal_leg_unit_ids', []))
        if local:
            # Both comparison legs, and every connecting unit, must belong to
            # the parent's actual departure / first retest / first return.
            identifiers = [*value.get('compare_leg_unit_ids', []), *identifiers]
        if local or point['point_type'].startswith('1'):
            if not identifiers or any(key not in by_id for key in identifiers):
                continue
            left, right = min(positions[k] for k in identifiers), max(positions[k] for k in identifiers)
            legs = units[left:right + 1]
            if (minute_start(lower, legs[0]['start_time']) < interval['start_after']
                    or legs[-1]['end_time'] > interval['leg_end_at']):
                continue
            if local and (any(u.get('locked') is not True or u.get('available_at', lower['source_closed_at'] + 1) > lower['source_closed_at'] for u in legs)
                          or any(a['end_time'] != b['start_time'] or a['end_tick'] != b['start_tick']
                                 or a['direction'] == b['direction'] for a, b in zip(legs, legs[1:]))):
                continue
        counts['contained'] += 1
        matches.append(candidate)
    matches.sort(key=order)
    reason = ('LOWER_MATCHED' if matches else 'LOWER_NO_CORRESPONDING_STRUCTURE' if not counts['inside_interval']
              else 'LOWER_PIVOT_WINDOW_MISMATCH' if not counts['inside_pivot']
              else 'LOWER_PIVOT_PRICE_MISMATCH' if not counts['same_price'] else 'LOWER_LEGS_OUTSIDE_INTERVAL')
    diagnostics = {'schema': 'lower-interval-assessment-v2', 'checked_at': lower['source_closed_at'],
                   'reason': reason, 'counts': counts}
    if lower.get('native_consolidation_error'):
        diagnostics['warnings'] = ['LOWER_INTERNAL_COMPARISON_UNAVAILABLE']
    return matches, diagnostics
