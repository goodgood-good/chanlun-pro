"""Notification-only local QMT history; never downloads or replaces evidence.

30m reads the entire local store (count=-1, from the Unix epoch). This proves
local availability only, not the extent of the exchange's remote history.
Finite targets read a bounded calendar window if frozen data is insufficient.
The caller still validates sessions and builds independent display indicators.
"""
from pathlib import Path
import hashlib
import time

import pandas as pd

from .notification_history import (
    HistoryContractError, HistoryResult, TARGET_DAYS, _basis, _digest,
    _frame_contract, _frame_digest, _overlap,
)

SCHEMA = 'notification-qmt-local-history-v1'


def _reader_revision():
    import chanlun
    root = Path(chanlun.__file__).parent / 'exchange'
    digest = hashlib.sha256(Path(__file__).read_bytes())
    for name in ('exchange_qmt.py', 'qmt_time_contract.py', 'qmt_listing.py',
                 'price_basis.py', 'kline_precision.py'):
        digest.update((root / name).read_bytes())
    return digest.hexdigest()


def prepare_qmt_notification_history(event, frequency, primary_attrs, *, anchor_frame,
                                     aligned_cutoff, previous=None, max_pages=4,
                                     timeout_seconds=20., fetch=True, monotonic=time.monotonic):
    from chanlun.screening.rules import CN

    if frequency not in TARGET_DAYS or timeout_seconds <= 0:
        raise ValueError('invalid QMT notification history request')
    cutoff = pd.Timestamp(aligned_cutoff, unit='s', tz='UTC')
    if cutoff.timestamp() > event['source_closed_at']:
        raise HistoryContractError('cutoff_after_event')
    anchor = anchor_frame.copy(deep=True)
    _frame_contract(anchor, event['code'], frequency, cutoff)
    _basis(anchor, primary_attrs)
    if anchor.date.max() != cutoff:
        raise HistoryContractError('anchor_cutoff_mismatch')
    days = TARGET_DAYS[frequency]
    target = None if days is None else cutoff - pd.Timedelta(days=days)
    identity = _digest({'schema': SCHEMA, 'event': event.get('id'), 'code': event['code'],
                        'frequency': frequency, 'cutoff': aligned_cutoff,
                        'event_cutoff': event['source_closed_at'], 'anchor': _frame_digest(anchor),
                        'reader': _reader_revision()})
    began = monotonic()
    frame = anchor.copy(deep=True)
    reason, origin, compatible, error, reads = 'page_budget', False, True, None, 0

    def result():
        reached = target is not None and frame.date.min() <= target
        state = {'schema': SCHEMA, 'identity': identity, 'reason': reason,
                 'frame_hash': _frame_digest(frame), 'source_origin': origin,
                 'compatible': compatible, 'reads': reads, 'error_code': error}
        state['checksum'] = _digest(state)
        coverage = {'purpose': 'independent_background', 'consumer_validation_required': True,
                    'source_scope': 'qmt_local', 'source_label': 'QMT 本机可用历史',
                    'price_basis_overlap_verified': compatible,
                    'anchor_rows': len(anchor), 'anchor_overlap_rows': len(anchor) if compatible else 0,
                    'anchor_complete': compatible, 'target_days': days,
                    'requested_from': None if target is None else int(target.timestamp()),
                    'requested_to': int(cutoff.timestamp()),
                    'available_from': int(frame.date.min().timestamp()),
                    'available_to': int(frame.date.max().timestamp()), 'rows': len(frame),
                    'target_reached': bool(reached), 'source_origin': origin,
                    'reason': reason, 'error_code': error, 'pages': reads,
                    'can_resume': reason in {'page_budget', 'deadline', 'source_error'},
                    'compatible_with_anchor': compatible,
                    'elapsed_seconds': max(0., monotonic()-began)}
        return HistoryResult(frame.copy(deep=True), coverage, state)

    if target is not None and anchor.date.min() <= target:
        reason = 'frozen_target'
        return result()
    if event.get('market') != 'a' or primary_attrs.get('price_basis_provider') != 'qmt':
        reason = 'unsupported_source'
        return result()
    if previous is not None:
        saved = previous.state
        if (saved.get('schema') != SCHEMA or saved.get('identity') != identity
                or saved.get('checksum') != _digest({k:v for k,v in saved.items() if k != 'checksum'})
                or saved.get('frame_hash') != _frame_digest(previous.frame)):
            raise HistoryContractError('resume_identity_conflict')
        if not previous.coverage.get('can_resume'):
            return HistoryResult(previous.frame.copy(deep=True), dict(previous.coverage), dict(saved))
    if not fetch:
        return result()
    try:
        from chanlun.exchange.exchange_qmt import ExchangeQMT
        from chanlun.screening.runner import _resolve_suspensions
        from chanlun.screening.rules import trading_context

        query_start = ('19700101' if target is None else
                       (target.tz_convert(CN)-pd.Timedelta(days=14)).strftime('%Y%m%d'))
        observed = cutoff.tz_convert(CN).to_pydatetime()
        # Use the single-attempt adapter: notifications own retry/deadline
        # policy. No request count truncation, server read or history download.
        fresh = ExchangeQMT()._klines_once(event['code'], frequency, start_date=query_start,
            end_date=observed.strftime('%Y-%m-%d %H:%M:%S'),
            args={'exact_end': True, 'skip_download': True, 'local_only': True,
                  'dividend_type': primary_attrs['price_basis_adjustment']})
        reads = 1
        _frame_contract(fresh, event['code'], frequency, cutoff)
        _basis(fresh, primary_attrs)
        if fresh.date.max() != cutoff or _overlap(fresh, anchor) != len(anchor):
            raise HistoryContractError('local_history_disagrees_with_anchor')
        fresh.attrs['bar_time_label'] = 'end'
        context = {**trading_context(observed, frequency), 'symbol': event['code']}
        fresh = _resolve_suspensions(fresh, event['code'], context)
        frame = fresh.copy(deep=True)
        if frame.attrs.get('_screening_session_error'):
            reason, error = 'source_error', 'suspension_evidence_unavailable'
        elif monotonic()-began > timeout_seconds:
            reason = 'deadline'
        elif target is None:
            reason, origin = 'source_origin', True
        elif frame.date.min() <= target:
            reason = 'target_reached'
        else:
            # A bounded empty prefix does not prove local origin: an older
            # cached episode may precede a long suspension. State the limit.
            reason, error = 'unsupported_source', 'local_window_short'
    except HistoryContractError as exc:
        reason, compatible, error = 'contract_error', False, exc.code
    except Exception as exc:
        reason, error = 'source_error', type(exc).__name__
    return result()
