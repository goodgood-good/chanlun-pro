"""Pre-verify the next exact window's prefix; never reuse a truncated CL state."""
from datetime import datetime, timedelta
import gzip
import hashlib
import json
from pathlib import Path
import time

from .cache import REPLAY_CHECKS, input_fingerprint, _save_source_input, _write_calculation_payload
from .rules import CN

SCHEMA = 'screening-rolling-proofs-v1'


def proof_path(cache_path):
    path = Path(cache_path)
    return path.with_name(path.name.removesuffix('.json.gz') + '.rolling.json.gz')


def window_targets(settings, contexts):
    from . import runner
    from .timeframe_policy import enabled
    from chanlun.exchange._lookback import get_lookback_timedelta
    if not enabled(settings) or '5m' not in settings.get('frequencies', []):
        return {}
    cutoff = contexts.get('5m', {}).get('cutoff')
    if type(cutoff) is not int:
        return {}
    closed = datetime.fromtimestamp(cutoff, CN)
    if (closed.hour, closed.minute) != (15, 0):
        return {}  # Keep midday analysis free of optional next-window work.
    for days in range(1, 15):
        future = runner._a_calendar_context(closed + timedelta(days=days), '5m',
            settings.get('recent_sessions', 5), settings.get('max_anchor_sessions', 20))
        stamp = future.get('cutoff')
        if type(stamp) is not int or stamp <= cutoff:
            continue
        future_closed = datetime.fromtimestamp(stamp, CN)
        start = min((future_closed - get_lookback_timedelta('5m', market='a')).date().isoformat(), future['history_from'])
        return {'5m': start}
    return {}  # An unavailable future calendar cannot alter the main scan.


def _payload(path):
    wrapper = json.loads(gzip.decompress(path.read_bytes()))
    raw = wrapper['payload'].encode('utf-8')
    if hashlib.sha256(raw).hexdigest() != wrapper['sha256']:
        raise ValueError('Unverified rolling-proof source')
    return json.loads(raw)


def prepare(stock, settings, contexts, run_dir, revision, result, targets):
    """Optional after-close maintenance; all failures leave the trade result intact."""
    output = {'state': 'skipped', 'proofs': 0}
    try:
        if stock.get('market', 'a') != 'a' or stock.get('_monitor_periods', settings['frequencies']) != ['5m']:
            return output
        row = next(iter(result.get('rows', [])), {})
        if row.get('data_errors') or not row.get('input_fingerprint') or not targets.get('5m'):
            return output
        from .markets import storage_symbol
        root = Path(run_dir).parent / 'analysis_cache'
        cached = _payload(root / f"{storage_symbol(stock['code'], 'a')}_5m.json.gz")
        source = cached.get('source_input') or {}
        if source.get('fingerprint') != row['input_fingerprint']:
            return output
        reviewed = {key:value for key,value in cached.get('reviewed', {}).items()
                    if not key.startswith(('point-preview:', 'consolidation-preview:'))
                    and all(value.get(name) is True for name in REPLAY_CHECKS)}
        if not reviewed:
            return output
        path = (root / source['file']).resolve()
        if path.parent != (root / 'inputs').resolve() or hashlib.sha256(path.read_bytes()).hexdigest() != source['sha256']:
            raise ValueError('Rolling input changed before pre-verification')
        import pandas as pd
        frame = pd.read_parquet(path)
        if input_fingerprint(frame, stock['code'], '5m') != source['fingerprint']:
            raise ValueError('Rolling input differs from its verified source')
        suspension = (row.get('data_quality') or {}).get('suspension_evidence')
        if suspension:
            frame.attrs['screening_suspensions'] = suspension
        prefix = frame.loc[frame.date >= datetime.fromisoformat(targets['5m']).replace(tzinfo=CN)].reset_index(drop=True)
        prefix.attrs = dict(frame.attrs)
        if not 200 <= len(prefix) < len(frame):
            return output
        target = proof_path(root / f"{storage_symbol(stock['code'], 'a')}_5m.json.gz")
        fingerprint = input_fingerprint(prefix, stock['code'], '5m')
        try:
            prior = _payload(target)
            prior_source = prior.get('source_input') or {}
            prior_path = (root / prior_source.get('file', '')).resolve()
            if (prior.get('schema') == SCHEMA and prior.get('calculation_contract_revision') == cached.get('calculation_contract_revision')
                    and prior_source.get('fingerprint') == fingerprint and prior.get('reviewed')
                    and prior_path.parent == (root / 'inputs').resolve()
                    and hashlib.sha256(prior_path.read_bytes()).hexdigest() == prior_source.get('sha256')):
                return {**output, 'state': 'reused', 'proofs': len(prior.get('reviewed', {}))}
        except (OSError, ValueError, KeyError, TypeError):
            pass
        from . import runner
        warm_inputs = {}
        base = {key:value for key,value in settings.items() if key != 'timeframe_policy'}
        warmed = runner.scan_symbol(stock, {**base, 'frequencies': ['5m']}, contexts, run_dir,
            cache_root=None, revision=revision, force_rebuild=True, _inputs=warm_inputs,
            _frozen_frames={'5m': prefix}, _publish_evidence=False)
        warmed_row = next(iter(warmed.get('rows', [])), {})
        if warmed_row.get('data_errors') or '5m' not in warm_inputs:
            return {**output, 'reason': 'unverified_prefix'}
        values = warm_inputs['5m']
        reviewed = {key:value for key,value in values['reviewed'].items()
                    if not key.startswith(('point-preview:', 'consolidation-preview:'))
                    and all(value.get(name) is True for name in REPLAY_CHECKS)}
        if not reviewed:
            return output
        payload = {'schema': SCHEMA, 'reviewed': reviewed,
                   'calculation_revision': values['calculation_revision'],
                   'calculation_contract_revision': values['contract_revision'],
                   'source_input': _save_source_input(target, prefix, stock['code'], '5m'),
                   'window_start': int(prefix.date.iloc[0].timestamp()), 'created_at': time.time()}
        _write_calculation_payload(target, payload)
        return {**output, 'state': 'prepared', 'proofs': len(reviewed), 'bars': len(prefix),
                'window_start': payload['window_start']}
    except Exception as exc:
        return {**output, 'state': 'unavailable', 'error_type': type(exc).__name__}
