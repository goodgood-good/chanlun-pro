"""Calculate dependencies only for 5m setups; keep 1m short trades separate."""
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import hashlib
import json
import time

from .cache import REPLAY_CHECKS, candidate_signature, write_calculation
from .rules import CN, POINT_TYPES, _native_center_signal_source_valid
from . import timeframe_policy as policy


def _peer_context(runner, stock, settings, main_context, frequency):
    observed = datetime.fromtimestamp(main_context['cutoff'], CN)
    args = observed, frequency, settings['recent_sessions'], settings['max_anchor_sessions']
    context = (runner._a_calendar_context(*args) if stock.get('market', 'a') == 'a'
               else runner._external_calendar_context(stock['market'], stock['code'], *args))
    if main_context.get('live_updates'):
        context['live_updates'] = True
    if main_context.get('_history_provider') in {'longbridge','usmart'}:
        context['_history_provider']=main_context['_history_provider']
    prepared = stock.get('_completed_inputs', {}).get(frequency)
    if prepared and prepared.get('cutoff') == context.get('cutoff'):
        context['_screening_tail_prepared'] = prepared
    return context


def _confirmed_candidates(snapshot):
    centers = {c['center_id']: c for c in snapshot['levels'][0].get('centers', [])}
    return [{'point': p, 'center': centers.get(p['center_id']), 'reasons': []}
            for p in snapshot['levels'][0].get('points', [])
            if p.get('status') == 'confirmed' and p.get('evidence_scope') == 'native_center_signal'
            and _native_center_signal_source_valid(snapshot, p, centers, snapshot['screening_segments'])]


def scan(stock, settings, contexts, run_dir, *, cache_root=None, revision=None, force_rebuild=False):
    from . import runner
    began = time.monotonic()
    base = {key: value for key, value in settings.items() if key != 'timeframe_policy'}
    rows = []
    options = dict(cache_root=cache_root, revision=revision, force_rebuild=force_rebuild)
    for frequency in settings['frequencies']:
        if Path(run_dir, 'cancel').exists():
            break
        inputs = {}
        period_started = time.monotonic()
        result = runner.scan_symbol(stock, {**base, 'frequencies': [frequency]}, contexts, run_dir,
                                    _inputs=inputs if frequency == '5m' else None,
                                    _interval_strength=frequency == '1m',
                                    _confirmation_only=frequency == '30m' and stock.get('holding') is not True, **options)
        for row in result['rows']:
            row['period_timings_ms'] = {frequency:round((time.monotonic()-period_started)*1000,3)}
            row.update(timeframe_policy=policy.POLICY, operating_role=policy.ROLES[frequency])
            if frequency == '30m':
                # Preserve the calculation cache, diagnostics and holding risk
                # checks; 30m points are background, not entry/exit alerts.
                row.update(selected=[], observations=[])
            elif frequency == '1m':
                for candidate in (*row['selected'], *row.get('observations', [])):
                    candidate.update(timeframe_policy=policy.POLICY, operating_role='short_term')
            elif not row.get('data_errors') and '5m' in inputs:
                _join(runner, stock, base, contexts['5m'], run_dir, row, inputs['5m'], options)
            measured = row['period_timings_ms']
            total = (time.monotonic()-period_started)*1000
            measured['coordination'] = round(max(0,total-sum(measured.values())),3)
            measured['total'] = round(total,3)
            rows.append(row)
    return {'code': stock['code'], 'market': stock.get('market', 'a'), 'name': stock['name'],
            'rows': rows, 'seconds': round(time.monotonic() - began, 3)}


def _join(runner, stock, settings, main_context, run_dir, row, main, options):
    candidates = [*row['selected'], *row.get('observations', [])]
    if settings.get('alert_mode') == 'native_center_signals' and any(c['point']['point_type'] == '3buy' for c in candidates):
        from .first_center_sequence import CenterSequences, first_verified
        snapshot = main['snapshot']
        centers = {c['center_id']: c for c in snapshot['levels'][0].get('centers', [])}
        valid = {p['point_id']: _native_center_signal_source_valid(snapshot, p, centers, snapshot['screening_segments'])
                 for p in snapshot['levels'][0].get('points', [])}
        sequence = CenterSequences(snapshot, valid)
        retained = []
        for candidate in candidates:
            if candidate['point']['point_type'] != '3buy':
                retained.append(candidate)
                continue
            proof = sequence.assess(candidate['point'])
            if first_verified(proof):
                retained.append(candidate)
                continue
            reason = ('THIRD_BUY_NOT_FIRST_UP_CENTER' if proof.get('state') == 'later' else 'THIRD_BUY_CENTER_SEQUENCE_UNKNOWN')
            row.setdefault('recent_rejections', []).append({**candidate, 'center_sequence': proof, 'reasons': [reason]})
            counts = row.setdefault('reason_counts', {})
            counts[reason] = counts.get(reason, 0) + 1
            row['selection_policy_exclusions'] = row.get('selection_policy_exclusions', 0) + 1
        candidates = retained
        row['selected'] = [c for c in retained if c['point']['status'] == 'confirmed']
        row['observations'] = [c for c in retained if c['point']['status'] != 'confirmed']
    if not candidates or Path(run_dir, 'cancel').exists():
        return
    from .interval_evidence import catalog, identity, order
    if stock.get('market')=='us':
        from chanlun import config
        provider=main['frame'].attrs.get('price_basis_provider')
        if str(config.US_HISTORY_KLINE_SOURCE).lower()=='dual' and provider in {'longbridge','usmart'}:
            main_context={**main_context,'_history_provider':provider}
    lower, lower_errors, peer_inputs = None, [], {}
    background_started = time.monotonic()
    background_context = _peer_context(runner, stock, settings, main_context, '30m')
    runner.scan_symbol(stock, {**settings, 'frequencies': ['30m']}, {'30m': background_context}, run_dir,
                       _confirmation_only=True, _inputs=peer_inputs, **options)
    higher = peer_inputs.get('30m')
    if higher is not None:
        row['background_evidence'] = runner._save_evidence(run_dir, stock['code'], '30m', higher['frame'], higher['snapshot'], namespace='trade_5m')
        higher_history=(getattr(higher['frame'],'attrs',{}) or {}).get('_screening_history_cache')
        if isinstance(higher_history,dict):
            row['background_history_refresh']=dict(higher_history)
    row.setdefault('period_timings_ms', {})['30m'] = round((time.monotonic()-background_started)*1000,3)
    # Formation and lower evidence are independent clocks. A provisional main
    # interval may already have a completed lower reversal, without becoming a
    # confirmed 5m trade. Every candidate now receives an actual check result.
    lower_started = time.monotonic()
    lower_context = _peer_context(runner, stock, settings, main_context, '1m')
    requested_starts = []
    for candidate in candidates:
        try:
            requested_starts.append(policy.interval_for(main['snapshot'], candidate['point'])['start_after'])
        except (KeyError, TypeError, ValueError, ArithmeticError):
            pass
    if requested_starts and lower_context.get('history_from'):
        interval_day = datetime.fromtimestamp(min(requested_starts), CN).date().isoformat()
        lower_context['history_from'] = min(lower_context['history_from'], interval_day)
    lower_result = runner.scan_symbol(stock, {**settings, 'frequencies': ['1m'], 'point_types': list(POINT_TYPES)},
        {'1m': lower_context}, run_dir, _confirmation_only=True, _interval_strength=True, _inputs=peer_inputs, **options)
    lower = peer_inputs.get('1m')
    lower_errors = next(iter(lower_result['rows']), {}).get('data_errors', [])
    closing_refresh = next(iter(lower_result['rows']), {}).get('closing_bar_refresh')
    if closing_refresh:
        row['confirmation_closing_bar_refresh'] = closing_refresh
    history_refresh=next(iter(lower_result['rows']),{}).get('history_refresh')
    if history_refresh:
        row['confirmation_history_refresh']=dict(history_refresh)
    if lower is None and not lower_errors:
        lower_errors = ['LOWER_DATA_ERROR']
    raw_lower = catalog(lower['snapshot']) if lower else []
    bindings, matched = [], {}
    for candidate in candidates:
        interval, matches, assessment, errors = None, [], None, list(lower_errors)
        try:
            interval = policy.interval_for(main['snapshot'], candidate['point'])
            if lower:
                interval, matches, assessment = policy.assess_for(main['snapshot'], candidate['point'], lower['snapshot'], raw_lower)
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            errors = [str(exc) if str(exc) in {'LOWER_HISTORY_INCOMPLETE', 'LOWER_CONTEXT_MISMATCH'} else 'MAIN_INTERVAL_UNAVAILABLE']
        matched.update({identity(child): deepcopy(child) for child in matches})
        bindings.append((candidate, interval, matches, assessment, errors))
    verified, pending_points, pending_comparisons = {}, [], []
    def signature(child):
        return ('consolidation:' + hashlib.sha256(json.dumps(child['alert'], sort_keys=True).encode()).hexdigest()
                if 'alert' in child else candidate_signature(child))
    for key, child in matched.items():
        audit = lower['reviewed'].get(signature(child))
        if audit and all(audit.get(name) is True for name in REPLAY_CHECKS):
            verified[key] = {**child, 'audit': dict(audit)}
        else:
            (pending_comparisons if 'alert' in child else pending_points).append(child)
    if pending_points:
        accepted, _ = runner._review_native_center_signals(lower['snapshot'], lower['frame'], pending_points, stock['code'], '1m')
        verified.update({identity(child): child for child in accepted})
    if pending_comparisons:
        accepted, _ = runner._review_consolidation_alerts(lower['snapshot'], lower['frame'], pending_comparisons, stock['code'], '1m')
        verified.update({identity(child): child for child in accepted})
    for child in verified.values():
        lower['reviewed'][signature(child)] = dict(child['audit'])
    selected, waiting = [], []
    for candidate, interval, matches, assessment, errors in bindings:
        choices = [verified[identity(child)] for child in matches if identity(child) in verified]
        child = min(choices, key=order) if choices else None
        if matches and child is None:
            errors = ['LOWER_REPLAY_FAILED']
        elif child is None and assessment and assessment.get('warnings'):
            errors = list(assessment['warnings'])
        joined = policy.attach(candidate, interval, child, data_errors=errors, assessment=assessment)
        joined['background_30m'] = policy.background(
            higher['snapshot'] if higher else None, side=candidate['point']['side'],
            expected_cutoff=background_context.get('cutoff'), as_of=main['snapshot']['source_closed_at'],
            price_basis=main['snapshot']['price_basis_revision'])
        (selected if joined['selection_status'] == 'confirmed' else waiting).append(joined)
    row.update(selected=selected, observations=waiting, confirmation_frequency='1m',
               background_source_closed_at=background_context.get('cutoff'))
    if lower:
        snapshot = deepcopy(lower['snapshot'])
        snapshot['screening_interval_signals'] = [child for child in verified.values() if 'point' in child]
        snapshot['screening_interval_divergences'] = [child for child in verified.values() if 'alert' in child]
        row['confirmation_evidence'] = runner._save_evidence(run_dir, stock['code'], '1m', lower['frame'], snapshot, namespace='trade_5m')
        row['confirmation_source_closed_at'] = snapshot['source_closed_at']
        if lower['cache_path'] is not None and (pending_points or pending_comparisons) and verified:
            try:
                write_calculation(lower['cache_path'], lower['key'], {'reason_counts': {}}, lower['snapshot'], lower['reviewed'],
                                  source_frame=lower['frame'], calculation_revision=lower['calculation_revision'],
                                  contract_revision=lower.get('contract_revision'))
            except OSError:
                pass
    row.setdefault('period_timings_ms', {})['1m'] = round((time.monotonic()-lower_started)*1000,3)
