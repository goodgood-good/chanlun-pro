"""Causal continuation of an already admitted, unconfirmed warning.

Local author text (annotations are not used as rules):
D:/缠论/chanlun_lesson_corpus/L061_*:97-121: keep observing the same
comparison until evidence refutes it; L024_*:226-238,310-319,334-379:
MACD area comparison and provisional doubling; L021_*:247-271: second
point's first-point/return relationship; L020_*:427-454: first return and
core boundary. L054_*:181-196 describes boundary contact as oscillation.
L053_*:235-256 also discusses broader second-point alternatives. This module
maintains the existing confirmed-parent/nonbreaking-return branch; it does
not claim that branch is the complete original definition of second points.

Engineering policy: admission still uses the existing conditional structure
and early trigger. Continuation never creates a new warning or confirms a
segment. Actual C >= A refutes the area-decay hypothesis; losing 2C < A or
one-bar contraction alone does not. Exact core contact remains unconfirmed
observation, distinct from crossing the boundary or a completed failed return.
"""
from copy import deepcopy
from decimal import Decimal
import math

SCHEMA = 'warning-lifecycle-v1'
UNIT_FIELDS = ('unit_id', 'direction', 'start_time', 'end_time', 'start_tick',
               'end_tick', 'low_tick', 'high_tick', 'locked', 'confirmed_at', 'price_basis_revision')
REASONS = {
    'EVIDENCE_UNAVAILABLE': '当前证据不足，保留原预警等待复核',
    'PRICE_BASIS_CHANGED': '价格基准变化，等待同一基准的结构重新核验',
    'STRUCTURE_REPLACED': '原中枢或比较段已被新的结构划分替代',
    'COMPLETED_WITHOUT_SIGNAL': '原候选段已确认完成，但未形成对应买卖点或背驰',
    'AREA_DECAY_REFUTED': '当前同向累计面积已不小于参考段，原面积衰减假设不再成立',
    'PARENT_EXTREME_BROKEN': '回试突破所绑定的一类点极值，原二类候选的不破极值分支不再成立',
    'CORE_BOUNDARY_BROKEN': '首次回试或回抽越过中枢边界，原第三类候选条件不再成立',
    'BOUNDARY_CONTACT': '回试或回抽触及中枢边界，继续观察，不作确认',
    'CANDIDATE_UPDATED': '候选拐点已更新，原结构假设仍在观察中',
    'STRUCTURE_OBSERVING': '原结构假设尚未被否定，继续观察并等待完成',
    'CONFIRMED': '原候选已形成对应确认信号',
}


def _units(snapshot):
    return {u['unit_id']: u for u in snapshot['screening_segments']}


def _points(snapshot):
    return [p for level in snapshot['levels'] for p in level.get('points', [])]


def bind_warning(snapshot, event):
    """Bind immutable comparison ownership from the warning's verified source."""
    consolidation = event.get('signal_type') == 'consolidation_divergence'
    if consolidation:
        source = next(a for a in snapshot.get('native_consolidation_alerts', [])
                      if a.get('hypothesis_id') == event.get('hypothesis_id') and a['status'] == 'warning')
        from chanlun.screening.rules import native_consolidation_source_valid
        if not native_consolidation_source_valid(snapshot, source):
            raise ValueError('invalid original consolidation proof')
        tail_id = source['signal_leg_unit_ids'][-1]
        fixed_ids = source['source_unit_ids'][:-1]
        comparison = source
    else:
        source = next(p for p in _points(snapshot) if p['point_id'] == event.get('point_id'))
        from .native_point_preview import is_point_preview, preview_source_valid
        centers = {c['center_id']: c for level in snapshot['levels'] for c in level['centers']}
        if not is_point_preview(source) or not preview_source_valid(snapshot, source, centers, snapshot['screening_segments']):
            raise ValueError('invalid original point preview proof')
        tail_id = source['anchor_unit_id']
        fixed_ids = [key for key in source['related_unit_ids'] if key != tail_id]
        comparison = source.get('divergence') or {}
    units = _units(snapshot)
    center = next(c for level in snapshot['levels'] for c in level['centers'] if c['center_id'] == source['center_id'])
    parent = next((p for p in _points(snapshot) if p['point_id'] == source.get('parent_point_id')), None)
    tail = units[tail_id]
    owners = {center['center_id']}
    if comparison.get('first_center_id'):
        owners.add(comparison['first_center_id'])
    if parent and (parent.get('divergence') or {}).get('first_center_id'):
        owners.add(parent['divergence']['first_center_id'])
    frames = {c['center_id']: {'core':deepcopy(c['core']), 'view_id':c.get('view_id')}
              for level in snapshot['levels'] for c in level['centers'] if c['center_id'] in owners}
    if len(frames) != len(owners):
        raise ValueError('warning comparison center evidence is incomplete')
    return {
        'schema': SCHEMA, 'symbol': snapshot['symbol'], 'frequency': snapshot['source_frequency'],
        'price_basis_revision': snapshot['price_basis_revision'], 'bound_at': snapshot['source_closed_at'],
        'signal_type': event['signal_type'], 'point_type': event['point_type'],
        'center_id': center['center_id'], 'core': deepcopy(center['core']),
        'center_frames': frames,
        'preview_key': source.get('preview_key'), 'hypothesis_id': source.get('hypothesis_id'),
        'tail_start': tail['start_time'], 'direction': tail['direction'],
        'fixed_units': [{k: units[key].get(k) for k in UNIT_FIELDS} for key in fixed_ids],
        'parent_point_id': source.get('parent_point_id'), 'parent_tick': parent.get('anchor_tick') if parent else None,
        'compare_ids': comparison.get('compare_leg_unit_ids', []),
        'signal_start': units[comparison['signal_leg_unit_ids'][0]]['start_time'] if comparison else tail['start_time'],
        'anchor_at': source['anchor_at'], 'anchor_tick': source['anchor_tick'],
    }


def assess_warning(hypothesis, snapshot, frame):
    """Return observing/updated/invalidated/confirmed/pending from closed inputs.

    A missing history window or incomplete computation is never negative proof.
    This function does not examine future bars or a previous outcome.
    """
    def result(state, reason, **details):
        return {'schema': SCHEMA, 'state': state, 'reason_code': reason,
                'reason': REASONS[reason], 'checked_at': snapshot.get('source_closed_at'), **details}

    try:
        h = hypothesis
        cutoff = snapshot['source_closed_at']
        dates = [int(t.timestamp()) for t in frame.date]
        if (h.get('schema') != SCHEMA or snapshot['symbol'] != h['symbol']
                or snapshot['source_frequency'] != h['frequency'] or cutoff < h['bound_at']
                or snapshot.get('segment_construction', {}).get('status') == 'unresolved'
                or snapshot.get('center_processing', {}).get('complete') is False
                or frame.empty or dates[-1] != cutoff or any(a >= b for a, b in zip(dates, dates[1:]))):
            return result('pending', 'EVIDENCE_UNAVAILABLE')
        consolidation = h['signal_type'] == 'consolidation_divergence'
        if (snapshot.get('native_consolidation_error') if consolidation
                else snapshot.get('point_preview_checked') is not True):
            return result('pending', 'EVIDENCE_UNAVAILABLE')
        if snapshot['price_basis_revision'] != h['price_basis_revision']:
            return result('pending', 'PRICE_BASIS_CHANGED')
        confirmed = (any(a.get('hypothesis_id') == h['hypothesis_id'] and a.get('status') == 'confirmed'
                         and a.get('available_at', math.inf) <= cutoff
                         for a in snapshot.get('native_consolidation_alerts', [])) if consolidation else
                     any(p.get('preview_key') == h['preview_key'] and p.get('status') == 'confirmed'
                         and p.get('available_at', math.inf) <= cutoff
                         for p in _points(snapshot)))
        if confirmed:
            return result('confirmed', 'CONFIRMED')
        units = _units(snapshot)
        earliest = min([h['tail_start'], *(u['start_time'] for u in h['fixed_units'])])
        if int(frame.date.iloc[0].timestamp()) > earliest:
            return result('pending', 'EVIDENCE_UNAVAILABLE')
        centers = {c['center_id']: c for level in snapshot['levels'] for c in level['centers']}
        center = centers.get(h['center_id'])
        if (not center or center.get('core') != h['core'] or center.get('superseded_at') is not None
                or any(key not in centers or centers[key].get('core') != original['core']
                       or centers[key].get('view_id') != original['view_id']
                       for key, original in h.get('center_frames', {}).items())
                or any(key not in units or any(units[key].get(k) != u.get(k) for k in UNIT_FIELDS)
                       for u in h['fixed_units'] for key in [u['unit_id']])):
            return result('invalidated', 'STRUCTURE_REPLACED')
        tails = [u for u in units.values() if u['start_time'] == h['tail_start'] and u['direction'] == h['direction']]
        if len(tails) != 1:
            return result('invalidated', 'STRUCTURE_REPLACED')
        tail = tails[0]
        if tail.get('available_at', cutoff) > cutoff:
            return result('pending', 'EVIDENCE_UNAVAILABLE')
        if tail['locked']:
            return result('invalidated', 'COMPLETED_WITHOUT_SIGNAL')
        quantum = Decimal(str(snapshot['structure_price_quantum']))
        if h['tail_start'] not in dates:
            return result('pending', 'EVIDENCE_UNAVAILABLE')
        start = dates.index(h['tail_start'])
        # Only the same unfinished leg may move its candidate endpoint.
        side = 'high' if h['direction'] == 'up' else 'low'
        prices = [Decimal(str(v)) for v in frame[side].iloc[start:]]
        if (not quantum.is_finite() or quantum <= 0 or not prices or not all(v.is_finite() for v in prices)
                or not math.isfinite(float(frame.close.iloc[-1]))):
            return result('pending', 'EVIDENCE_UNAVAILABLE')
        extreme = (max if side == 'high' else min)(prices)
        anchor_tick = int((extreme / quantum).to_integral_value())
        anchor_at = (tail['end_time'] if anchor_tick == tail['end_tick']
                     else dates[start + prices.index(extreme)])
        metrics = {'anchor_tick': anchor_tick, 'anchor_at': anchor_at, 'anchor_price': float(anchor_tick * quantum),
                   'latest_close': float(frame.close.iloc[-1]), 'price_basis_revision': snapshot['price_basis_revision']}
        kind = h['point_type']
        if kind.startswith('3'):
            boundary = h['core']['zg_tick' if kind.endswith('buy') else 'zd_tick']
            if anchor_tick < boundary if kind.endswith('buy') else anchor_tick > boundary:
                return result('invalidated', 'CORE_BOUNDARY_BROKEN', boundary_tick=boundary, **metrics)
            if anchor_tick == boundary:
                return result('observing', 'BOUNDARY_CONTACT', boundary_tick=boundary, **metrics)
        elif kind.startswith('2'):
            if h['parent_tick'] is None:
                return result('pending', 'EVIDENCE_UNAVAILABLE')
            if anchor_tick < h['parent_tick'] if kind.endswith('buy') else anchor_tick > h['parent_tick']:
                return result('invalidated', 'PARENT_EXTREME_BROKEN', boundary_tick=h['parent_tick'], **metrics)
        elif kind.startswith('1') or consolidation:
            hist = snapshot.get('chart', {}).get('higher_macd_raw_hist')
            if not isinstance(hist, list) or len(hist) != len(dates) or not h['compare_ids']:
                return result('pending', 'EVIDENCE_UNAVAILABLE')
            sign = 1 if h['direction'] == 'up' else -1
            values = [max(0., sign * float(v)) for v in hist]
            if not all(math.isfinite(float(v)) for v in hist):
                return result('pending', 'EVIDENCE_UNAVAILABLE')
            a_start = dates.index(units[h['compare_ids'][0]]['start_time'])
            a_end = dates.index(units[h['compare_ids'][-1]]['end_time'])
            c_start = dates.index(h['signal_start'])
            # A formed-but-unlocked first-point leg has its own measurement
            # endpoint. Do not add the following leg's columns while waiting
            # for confirmation. Still-forming/repriced legs use the current
            # cutoff, as does the existing consolidation comparison policy.
            c_end = (dates.index(tail['end_time']) + 1 if not consolidation and not tail.get('forming')
                     and anchor_tick == tail['end_tick'] else len(dates))
            area_a, area_c = sum(values[a_start:a_end + 1]), sum(values[c_start:c_end])
            if area_a <= 0 or area_c <= 0:
                return result('pending', 'EVIDENCE_UNAVAILABLE')
            metrics.update(macd_area_a=area_a, macd_area_c=area_c, macd_area_ratio=area_c / area_a,
                           estimated_area_c=2 * area_c, estimate_factor=2, measurement_end_at=dates[c_end - 1])
            if area_c >= area_a:
                return result('invalidated', 'AREA_DECAY_REFUTED', **metrics)
        else:
            return result('pending', 'EVIDENCE_UNAVAILABLE')
        updated = anchor_tick != h['anchor_tick'] or anchor_at != h['anchor_at']
        return result('candidate_updated' if updated else 'observing',
                      'CANDIDATE_UPDATED' if updated else 'STRUCTURE_OBSERVING', **metrics)
    except (KeyError, TypeError, ValueError, IndexError, ArithmeticError):
        return result('pending', 'EVIDENCE_UNAVAILABLE')
