"""Audited first-class history is visible for review, never for admission."""
from copy import deepcopy

from .cache import REPLAY_CHECKS, candidate_signature
from .confirmation import native_selection_lifetime_reasons, confirmation_catalog


def history_detail(point, points, following, valid):
    reasons = native_selection_lifetime_reasons(point, points, following, valid)
    if point.get('point_type') not in {'1buy', '1sell'} or not reasons:
        return None
    successors = [{key: p.get(key) for key in ('point_id', 'point_type', 'anchor_at', 'available_at')}
                  for p in points if p.get('parent_point_id') == point['point_id']
                  and p.get('point_type') == ('2buy' if point['side'] == 'buy' else '2sell')
                  and p.get('status') == 'confirmed' and valid.get(p.get('point_id')) is True
                  and p.get('anchor_at', 0) > point['anchor_at']
                  and p.get('available_at', 0) >= point['available_at']]
    return {'reasons': reasons, 'successors': successors,
            'simultaneous_confirmation': any(p['available_at'] == point['available_at'] for p in successors)}


def review_history(snapshot, frame, rejected, reviewed, review, code, frequency):
    from .rules import _native_center_signal_source_valid
    if not any(item.get('point', {}).get('point_type') in {'1buy', '1sell'}
               and set(item.get('reasons', [])) & {'FIRST_ADVANCED_TO_SECOND', 'CONFIRMING_SEGMENT_COMPLETED'}
               for item in rejected):
        return [], False
    points = snapshot['levels'][0].get('points', [])
    centers = {c['center_id']: c for c in snapshot['levels'][0].get('centers', [])}
    following = confirmation_catalog(snapshot)
    valid = {p['point_id']: _native_center_signal_source_valid(snapshot, p, centers, snapshot['screening_segments']) for p in points}
    candidates, pending, details = [], [], {}
    for item in rejected:
        point = item.get('point') or {}
        if point.get('status') != 'confirmed' or not valid.get(point.get('point_id')):
            continue
        detail = history_detail(point, points, following, valid)
        if detail is None or not set(item.get('reasons', [])) <= {'FIRST_ADVANCED_TO_SECOND', 'CONFIRMING_SEGMENT_COMPLETED'}:
            continue
        child = {'point': deepcopy(point), 'center': deepcopy(centers[point['center_id']]), 'reasons': []}
        details[point['point_id']] = detail
        checks = reviewed.get(candidate_signature(child))
        if checks and all(checks.get(key) is True for key in REPLAY_CHECKS):
            candidates.append({**child, 'audit': dict(checks)})
        else:
            pending.append(child)
    if pending:
        accepted, _ = review(snapshot, frame, pending, code, frequency)
        for candidate in accepted:
            reviewed[candidate_signature(candidate)] = dict(candidate['audit'])
        candidates.extend(accepted)
    return [{**candidate, 'historical_only': True, 'selection_status': 'historical',
             'history': details[candidate['point']['point_id']]} for candidate in candidates], bool(pending)
