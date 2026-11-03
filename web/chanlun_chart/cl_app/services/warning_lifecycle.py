"""Recheck warning ownership against immutable, hash-verified period evidence."""
from copy import deepcopy
from pathlib import Path
import re

from chanlun.core.strict_structure.warning_lifecycle import SCHEMA, REASONS, bind_warning, assess_warning
from chanlun.screening.evidence import load_evidence


def warning_reference(event):
    # Keep one original proof, never a recursive chain of every polling update.
    reference = event.get('warning_reference') or event.get('invalidation_reference')
    if reference:
        return warning_reference(reference)
    return deepcopy({k: v for k, v in event.items()
        if k not in {'warning_hypothesis', 'warning_assessment', 'invalidation_reference'}})


def invalidation_update(assessment, period, run_id):
    return {'source_closed_at':period['source_closed_at'], 'evidence':deepcopy(period.get('evidence')),
            'evidence_run_id':run_id, 'latest_close':assessment.get('latest_close', period.get('latest_close')),
            'invalidation_checked_at':period['source_closed_at'], 'warning_assessment':assessment,
            'invalidation_reason':assessment['reason'], 'invalidation_reason_code':assessment['reason_code'],
            'metrics_at':period['source_closed_at'],
            **{k:assessment.get(k) for k in ('macd_area_a', 'macd_area_c', 'macd_area_ratio')}}


def read_warning_source(event, runs_root, cache, *, preserve_origin=False):
    run = event.get('evidence_run_id', '')
    manifest = event.get('evidence') or {}
    if not re.fullmatch(r'[a-f0-9]{32}', run):
        raise ValueError('warning evidence run is missing')
    key = (run, event['code'], event['frequency'], manifest.get('snapshot_sha256'), manifest.get('parquet_sha256'))
    if key not in cache:
        try:
            frame, snapshot, health = load_evidence(Path(runs_root) / run, event['code'], event['frequency'], manifest)
            if not health['verified']:raise ValueError('Unverified warning source')
        except (OSError,ValueError):
            from .warning_evidence import load_preserved
            frame,snapshot,health=load_preserved(event,runs_root)
        if not health['verified'] or snapshot['source_closed_at'] != event['source_closed_at']:
            raise ValueError('warning evidence has another cutoff')
        cache[key] = frame, snapshot
    if preserve_origin:
        from .warning_evidence import preserve
        try:preserve(event,runs_root)
        except (OSError,ValueError,KeyError):
            # Retention I/O cannot change a conclusion from source bytes which
            # were already verified above. Missing archive sources stay explicit.
            pass
    return cache[key]


def recheck_warning(previous, period, run_id, runs_root, cache):
    reference = warning_reference(previous)
    try:
        hypothesis = previous.get('warning_hypothesis')
        if not hypothesis:
            _, original = read_warning_source(reference, runs_root, cache,preserve_origin=True)
            hypothesis = bind_warning(original, reference)
        current = {**previous, **period, 'evidence_run_id': run_id}
        frame, snapshot = read_warning_source(current, runs_root, cache)
        outcome = assess_warning(hypothesis, snapshot, frame)
    except (OSError, ValueError, TypeError, KeyError, StopIteration):
        return {'schema': SCHEMA, 'state': 'pending', 'reason_code': 'EVIDENCE_UNAVAILABLE',
                'reason': REASONS['EVIDENCE_UNAVAILABLE'], 'checked_at': period.get('source_closed_at')}, None
    if outcome['state'] not in {'observing', 'candidate_updated'}:
        return outcome, None
    item = {**previous, 'source_closed_at': period['source_closed_at'],
            'latest_close': outcome['latest_close'], 'evidence': deepcopy(period['evidence']),
            'evidence_run_id': run_id, 'warning_reference': reference,
            'warning_hypothesis': deepcopy(hypothesis), 'warning_assessment': outcome,
            'warning_state': outcome['state'], 'preview_trigger_active': False,
            'candidate_updated': outcome['state'] == 'candidate_updated',
            'candidate_previous_price': (previous.get('anchor_price') if outcome['anchor_price'] != previous.get('anchor_price')
                                         else previous.get('candidate_previous_price', reference.get('anchor_price'))),
            **{k: outcome[k] for k in ('anchor_at', 'anchor_price', 'macd_area_a', 'macd_area_c',
                'macd_area_ratio', 'estimated_area_c', 'estimate_factor') if k in outcome}}
    if previous['signal_type'] == 'consolidation_divergence':
        outside = (outcome['anchor_tick'] > hypothesis['core']['zg_tick'] if hypothesis['direction'] == 'up'
                   else outcome['anchor_tick'] < hypothesis['core']['zd_tick'])
        item['core_position'] = 'breakout' if outside else 'inside'
    # A provisional entry reference follows the candidate; it is not a bound
    # holding stop. Holding risk plans have their own independent lifecycle.
    if previous['point_type'].startswith(('1', '2')) and item.get('exit_plan'):
        item['exit_plan'] = deepcopy(item['exit_plan'])
        stop = item['exit_plan'].get('stop_loss') or {}
        if stop.get('status') == 'provisional':
            stop.update(price=outcome['anchor_price'], price_tick=outcome['anchor_tick'])
    return outcome, item
