"""The user's fixed physical-period roles and causal 5m/1m association.

Local author sources: D:/缠论/chanlun_lesson_corpus/L027_*:265-301,
L061_*:97-121 (refine the corresponding terminal interval), and
L031_*:2248-2260 (lower reversal timing for a third-class return).
The exact 30m/5m/1m mapping and admission conditions are application policy;
they do not assert that candle periods prove a recursive Chan decomposition.
"""
from copy import deepcopy

from .cache import REPLAY_CHECKS
from .nesting import STRATEGY, main_interval

POLICY = '30m_background_5m_trade_1m_short_v2'
ROLES = {'30m': 'background', '5m': 'trade', '1m': 'short_term'}
ROLE_NAMES = {'30m': '大级别背景', '5m': '交易级别', '1m': '短差级别'}
SCAN_PERIODS = ['5m']
LIVE_PERIODS = ['1m', '5m', '30m']
PENDING = 'LOWER_CONFIRMATION_PENDING'


def enabled(settings):
    return settings.get('timeframe_policy') == POLICY


def screening_settings(settings):
    """Full screening computes 30m only through each retained 5m candidate.

    Explicit 30m-only work and the separate live-monitor settings keep their
    own periods. This changes task planning, not the joined evidence rules.
    """
    if enabled(settings) and '5m' in settings.get('frequencies', []):
        return {**settings, 'frequencies': [p for p in settings['frequencies'] if p != '30m']}
    return settings


def interval_for(snapshot, point):
    interval = main_interval(snapshot, point)
    units = {u['unit_id']: u for u in snapshot['screening_segments']}
    interval['leg_end_at'] = units[interval['unit_ids'][-1]]['end_time']
    interval['kind'] = {'1': 'terminal_divergence', '2': 'first_retest_after_first',
                        '3': 'first_return_outside_center'}[point['point_type'][0]]
    return interval


def assess_for(main, point, lower, candidates):
    from .interval_evidence import assess
    interval = interval_for(main, point)
    matches, diagnostics = assess(main, point, lower, candidates, interval)
    return interval, matches, diagnostics


def matches_for(main, point, lower, candidates):
    interval, matches, _ = assess_for(main, point, lower, candidates)
    return interval, matches


def attach(candidate, interval=None, lower=None, *, data_errors=(), assessment=None):
    from .interval_evidence import payload
    point = candidate['point']
    lower_ready = lower is not None and all(lower.get('audit', {}).get(key) is True for key in REPLAY_CHECKS)
    main_ready = point.get('status') == 'confirmed'
    witness = payload(lower) if lower_ready else None
    missing = list(point.get('missing_conditions', []))
    if not lower_ready:
        missing.append('lower_1m_confirmation')
    check_state = ('confirmed' if lower_ready else 'replay_failed' if 'LOWER_REPLAY_FAILED' in data_errors
                   else 'unavailable' if data_errors else 'no_match' if assessment is not None else 'not_checked')
    return {**candidate, 'timeframe_policy': POLICY, 'operating_role': 'trade',
            'selection_status': 'confirmed' if main_ready and lower_ready else 'approaching',
            'selection_available_at': max(point['available_at'], witness['available_at'] if lower_ready else 0),
            'selection_missing_conditions': list(dict.fromkeys(missing)),
            'reasons': list(dict.fromkeys([r for r in candidate.get('reasons', []) if r != PENDING]
                                         + ([] if lower_ready else [PENDING]))),
            'nested_confirmation': {'strategy': STRATEGY, 'policy': POLICY, 'frequency': '1m',
                'state': 'confirmed' if lower_ready else 'unavailable' if data_errors else 'waiting',
                'check_state': check_state, 'assessment': deepcopy(assessment),
                'interval': interval,
                'evidence_type': ('internal_divergence' if 'alert' in lower else 'native_point') if lower_ready else None,
                'point': deepcopy(lower.get('point')) if lower_ready else None,
                'divergence': deepcopy(lower.get('alert')) if lower_ready else None,
                'audit': dict(lower['audit']) if lower_ready else {}, 'data_errors': list(data_errors)}}


def saved_binding_valid(candidate, main, lower):
    from .interval_evidence import catalog, identity, order
    record = candidate.get('nested_confirmation') or {}
    try:
        if record.get('policy') != POLICY or candidate.get('timeframe_policy') != POLICY:
            return False
        try:
            interval = interval_for(main, candidate['point'])
        except (KeyError, TypeError, ValueError, ArithmeticError):
            if record.get('data_errors') != ['MAIN_INTERVAL_UNAVAILABLE']:
                return False
            expected = attach({'point': candidate['point']}, data_errors=['MAIN_INTERVAL_UNAVAILABLE'])
            return all(candidate.get(key) == expected[key] for key in (
                'nested_confirmation', 'selection_status', 'selection_available_at', 'selection_missing_conditions'))
        match, assessment = None, None
        errors = record.get('data_errors', [])
        if lower is not None:
            raw = catalog(lower)
            try:
                interval, matches, assessment = assess_for(main, candidate['point'], lower, raw)
            except ValueError as exc:
                if errors != [str(exc)]:
                    return False
                matches = []
            source = {identity(c): c for c in raw}
            eligible = {identity(c) for c in matches}
            frozen = [*lower.get('screening_interval_signals', []), *lower.get('screening_interval_divergences', [])]
            verified = []
            for child in frozen:
                key = identity(child)
                if key not in eligible:
                    continue
                original = source.get(key)
                field = 'alert' if 'alert' in child else 'point'
                if (original is None or child.get(field) != original.get(field)
                        or not all(child.get('audit', {}).get(k) is True for k in REPLAY_CHECKS)):
                    return False
                verified.append(child)
            match = min(verified, key=order) if verified else None
            if match and errors:
                return False
            if matches and not match and errors != ['LOWER_REPLAY_FAILED']:
                return False
            if not matches and errors == ['LOWER_REPLAY_FAILED']:
                return False
            if not matches and assessment and errors != assessment.get('warnings', []):
                return False
        expected = attach({'point': candidate['point']}, interval, match,
                          data_errors=errors, assessment=assessment)
        return all(candidate.get(key) == expected[key] for key in (
            'nested_confirmation', 'selection_status', 'selection_available_at', 'selection_missing_conditions'))
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return False


def background(snapshot, *, side, expected_cutoff, as_of=None, price_basis=None):
    """30m context annotates risk and never vetoes a 5m reversal."""
    from .rules import _native_center_body_source_valid
    if (snapshot is None or snapshot.get('source_frequency') != '30m' or snapshot.get('source_closed_at') != expected_cutoff
            or type(expected_cutoff) is not int or as_of is not None and expected_cutoff > as_of
            or price_basis is not None and snapshot.get('price_basis_revision') != price_basis):
        return {'frequency': '30m', 'state': 'unavailable', 'direction': 'unknown', 'alignment': 'unverified'}
    units = snapshot.get('screening_segments', [])
    by_id = {u['unit_id']: u for u in units}
    positions = {u['unit_id']: i for i, u in enumerate(units)}
    centers = [c for c in snapshot['levels'][0].get('centers', [])
               if _native_center_body_source_valid(snapshot, c, by_id, positions, expected_cutoff)]
    centers.sort(key=lambda c: (c.get('frame_available_at', 0), c['points'][0]['time']))
    direction = 'range' if centers else 'unknown'
    if len(centers) >= 2:
        previous, current = centers[-2:]
        if current['envelope']['dd_tick'] > previous['envelope']['gg_tick']:
            direction = 'up'
        elif current['envelope']['gg_tick'] < previous['envelope']['dd_tick']:
            direction = 'down'
    intent = 'up' if side == 'buy' else 'down'
    alignment = ('unverified' if direction == 'unknown' else 'range' if direction == 'range'
                 else 'aligned' if direction == intent else 'countertrend')
    return {'frequency': '30m', 'state': 'verified' if centers else 'unavailable',
            'source_closed_at': expected_cutoff, 'direction': direction, 'alignment': alignment,
            'center_id': centers[-1]['center_id'] if centers else None,
            'price_basis_revision': snapshot['price_basis_revision'],
            **({'history_repair': {key: value for key, value in snapshot['history_repair'].items() if key != 'bars'}}
               if snapshot.get('history_repair') else {})}
