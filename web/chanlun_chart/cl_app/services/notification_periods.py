"""Additional chart periods, bounded by the delivered signal's market clock."""
from datetime import datetime
from pathlib import Path


class HistoryUnavailable(ValueError):
    """History cannot be represented as verified display context."""
    def __init__(self, coverage):
        self.coverage = coverage
        self.next_retry = coverage.get('next_retry', 2)
        super().__init__('通知背景历史尚不可用')


class HistoryPending(HistoryUnavailable):
    """A normal bounded preparation slice, not a failed chart attempt."""


class HistorySourceError(HistoryUnavailable):
    """An actual provider/storage failure, separate from pagination progress."""


def context_evidence(event, runs_root, frequency, primary_attrs):
    import pandas as pd
    from chanlun.screening.evidence import evidence_paths
    from chanlun.screening.markets import market_context
    from chanlun.screening.rules import CN, frame_quality, trading_context
    from chanlun.screening.runner import fetch_frame, snapshot_for

    observed = datetime.fromtimestamp(event['source_closed_at'], CN)
    if event['market'] == 'a':
        context = trading_context(observed, frequency, 5, 20)
    else:
        context = market_context(event['market'], event['code'], observed, frequency)
    if context['cutoff'] > event['source_closed_at']:
        raise ValueError('context clock is later than the signal')
    source, _ = evidence_paths(Path(runs_root) / event['evidence_run_id'],
                               event['code'], frequency, event['market'])
    frame = pd.read_parquet(source) if source.is_file() else fetch_frame(event['code'], frequency, context)
    if frame is None or frame.empty:
        raise ValueError('context candles are missing')
    frame = frame.loc[frame.date <= pd.Timestamp(context['cutoff'], unit='s', tz='UTC')].copy()
    frame.attrs['screening_market'] = event['market']
    if frame_quality(frame, context)['errors']:
        raise ValueError('context candles failed validation')
    for key in ('price_basis_revision', 'structure_price_quantum'):
        if frame.attrs.get(key) != primary_attrs.get(key) or not frame.attrs.get(key):
            raise ValueError('context price basis differs from the signal')
    snapshot = snapshot_for(frame, event['code'], frequency)
    return frame, snapshot


def _history_scope(event, frequency, primary_attrs):
    import hashlib
    import json
    from chanlun.core import macd, macd_htf
    from .notification_history import _reader_revision
    revision = hashlib.sha256(Path(__file__).read_bytes())
    for module in (macd, macd_htf):
        revision.update(Path(module.__file__).read_bytes())
    provider_name = {'a':'notification_qmt_history','currency_spot':'notification_binance_history'}.get(event['market'])
    if provider_name and Path(__file__).with_name(provider_name+'.py').is_file():
        from importlib import import_module
        provider = import_module('.'+provider_name,__package__)
        revision.update(provider._reader_revision().encode())
    identity = {'event': event.get('id'), 'evidence': event.get('evidence'),
                'run': event.get('evidence_run_id'), 'market': event['market'],
                'code': event['code'], 'frequency': frequency,
                'event_cutoff': event['source_closed_at'],
                'basis': {k:primary_attrs.get(k) for k in ('structure_price_quantum',
                    'price_basis_revision', 'price_basis_provider', 'price_basis_adjustment')},
                'reader': _reader_revision(), 'display_reader': revision.hexdigest()}
    return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':'),
                                    allow_nan=False).encode()).hexdigest()


def _history_context(event, frequency):
    from chanlun.screening.markets import market_context
    from chanlun.screening.rules import CN, trading_context
    observed = datetime.fromtimestamp(event['source_closed_at'], CN)
    context = (trading_context(observed, frequency, 5, 20) if event['market'] == 'a'
               else market_context(event['market'], event['code'], observed, frequency))
    if context['cutoff'] > event['source_closed_at']:
        raise ValueError('context clock is later than the signal')
    return context


def _background_frame(frame, context, event, frequency):
    """Validate the entire actual history, including dates before 730 days."""
    import pandas as pd
    from chanlun.screening.markets import _session_mask
    from chanlun.screening.rules import frame_quality
    context = dict(context)
    if event['market'] in {'us','currency_spot'}:
        import exchange_calendars as xcals
        name = {'us':'XNYS','currency_spot':'24/7'}[event['market']]
        timezone = 'America/New_York' if event['market'] == 'us' else 'UTC'
        first = frame.date.min().tz_convert(timezone)
        cutoff = pd.Timestamp(context['cutoff'], unit='s', tz='UTC')
        calendar = xcals.get_calendar(name, start=(first-pd.Timedelta(days=7)).date(),
                                     end=(cutoff+pd.Timedelta(days=1)).date())
        schedule = calendar.schedule.loc[first.date():cutoff.tz_convert(calendar.tz).date()]
        step = int(frequency[:-1])*60
        periods = []
        for row in schedule.itertuples():
            opening = int(row.open.timestamp())
            closing = min(int(row.close.timestamp()), context['cutoff'])
            if closing >= opening+step:
                periods.append((opening, closing-(closing-opening)%step))
        if not periods:
            raise HistoryUnavailable({'state':'invalid', 'reason':'calendar_coverage_unknown'})
        context.update(session_periods=periods, session_timezone=str(calendar.tz),
                       calendar_coverage_start=first.date().isoformat())
        stamps = frame.date.astype('int64')//1_000_000_000
        frame = frame.loc[_session_mask(stamps, periods, step)].copy()
    else:
        frame = frame.copy(deep=True)
    frame.attrs['screening_market'] = event['market']
    quality = frame_quality(frame, context)
    if quality['errors']:
        raise HistoryUnavailable({'state':'invalid', 'reason':'history_validation_failed',
                                  'validation':quality})
    return frame


def _background_snapshot(frame, event, frequency):
    """Independent structures/indicators for display, never a new alert source."""
    from copy import deepcopy
    from chanlun.screening.runner import snapshot_for
    snapshot = deepcopy(snapshot_for(frame, event['code'], frequency))
    snapshot.update(display_scope='notification_background',formal_point_issued=False,tradable=False)
    # Preserve geometry `points` on centers: those are box corners, not signals.
    for level in snapshot.get('levels',[]):
        level['points'] = []
        level['divergences'] = []
    for key in ('point_exit_plans','native_consolidation_alerts','native_consolidation_previews',
                'operational_risk_alerts','holding_risk_alerts','points','alerts'):
        if key in snapshot:
            snapshot[key] = {} if isinstance(snapshot[key],dict) else []
    return snapshot


def background_evidence(event, runs_root, frequency, primary_attrs, *, anchor_frame=None,
                        fetch=True, max_pages=4, timeout_seconds=20, page_source=None):
    """Prepare independent long history, without changing delivered evidence.

    ``fetch=False`` never asks a provider for history. Normal bounded progress
    raises HistoryPending and must not consume the chart queue's failure budget.
    Only one frequency should receive fetch=True in a notification worker slice.
    """
    import hashlib
    from chanlun.persistence.file_lock import interprocess_file_lock, InterprocessLockTimeout
    from .notification_history import (HistoryArchive, HistoryContractError, HistoryResult,
                                      prepare_notification_history, _basis, _frame_contract)
    scope = _history_scope(event, frequency, primary_attrs)
    base = {'scope':scope, 'frequency':frequency, 'state':'pending', 'reason':'not_prepared',
            'can_resume':True, 'next_retry':2, 'progress_token':scope+':0',
            'requested_to':event['source_closed_at']}
    archive = HistoryArchive(Path(runs_root).parent/'notification_history', scope)
    try:
        with interprocess_file_lock(archive.lock_path, timeout_seconds=.1):
            anchor, previous, saved_snapshot, saved_frame = archive.read()
            context = _history_context(event, frequency)
            fetched_anchor = False
            if anchor is None:
                if anchor_frame is None:
                    if not fetch:
                        raise HistoryPending(base)
                    anchor_frame, _ = context_evidence(event, runs_root, frequency, primary_attrs)
                    fetched_anchor = True
                anchor = anchor_frame.copy(deep=True)
                import pandas as pd
                _frame_contract(anchor, event['code'], frequency,
                                pd.Timestamp(context['cutoff'],unit='s',tz='UTC'))
                _basis(anchor, primary_attrs)
                if int(anchor.date.max().timestamp()) != context['cutoff']:
                    raise HistoryContractError('anchor_cutoff_mismatch')
                archive.write(anchor)
            if saved_snapshot is not None and saved_frame is not None and previous is not None:
                if previous.coverage.get('state') in {'target_reached','source_exhausted'}:
                    return saved_frame.copy(deep=True), saved_snapshot, dict(previous.coverage)
            prepare = prepare_notification_history
            if event['market'] == 'a':
                try:
                    from .notification_qmt_history import prepare_qmt_notification_history
                    prepare = prepare_qmt_notification_history
                except ModuleNotFoundError as exc:
                    if exc.name != __package__+'.notification_qmt_history':
                        raise
                    raise HistoryUnavailable({**base,'state':'unsupported','reason':'local_history_unverified',
                                              'can_resume':False}) from exc
            elif event['market'] == 'currency_spot':
                from chanlun import config
                if (primary_attrs.get('price_basis_provider') == 'binance'
                        and config.EXCHANGE_CURRENCY_SPOT == 'binance_spot'):
                    try:
                        from .notification_binance_history import prepare_binance_notification_history
                        prepare = prepare_binance_notification_history
                    except ModuleNotFoundError as exc:
                        if exc.name != __package__+'.notification_binance_history':
                            raise
                        raise HistoryUnavailable({**base,'state':'unsupported','reason':'spot_history_unverified',
                                                  'can_resume':False}) from exc
            options = dict(anchor_frame=anchor, aligned_cutoff=context['cutoff'], previous=previous,
                           max_pages=max_pages, timeout_seconds=timeout_seconds, fetch=fetch and not fetched_anchor)
            if page_source is not None and event['market'] in {'us','currency_spot'}:
                options['page_source'] = page_source
            result = prepare(event, frequency, primary_attrs, **options)
            coverage = {**base, **result.coverage, 'scope':scope}
            if result.coverage.get('scope') and result.coverage['scope'] != scope:
                coverage.setdefault('source_scope',result.coverage['scope'])
            progress = str(result.state.get('checksum', result.state))
            coverage['progress_token'] = hashlib.sha256((scope+progress).encode()).hexdigest()
            reason = coverage['reason']
            if reason in {'page_budget','deadline'}:
                coverage['state'] = 'pending'
            elif reason == 'source_error':
                coverage['state'] = 'source_error'
            elif reason == 'unsupported_source':
                coverage.update(state='unsupported',can_resume=False)
            elif not coverage.get('compatible_with_anchor'):
                coverage.update(state='invalid',can_resume=False)
            else:
                coverage['state'] = ('source_exhausted' if coverage.get('source_origin')
                                     and not coverage.get('target_reached') else 'target_reached')
            result = HistoryResult(result.frame, coverage, result.state)
            archive.write(anchor, result)
            if coverage['state'] == 'pending':
                raise HistoryPending(coverage)
            if coverage['state'] == 'source_error':
                raise HistorySourceError(coverage)
            if coverage['state'] in {'unsupported','invalid'}:
                raise HistoryUnavailable(coverage)
            try:
                frame = _background_frame(result.frame, context, event, frequency)
                import time
                started = time.monotonic()
                snapshot = _background_snapshot(frame, event, frequency)
                coverage['structure_seconds'] = round(time.monotonic()-started,3)
            except HistoryUnavailable as exc:
                coverage.update(exc.coverage, can_resume=False)
                archive.write(anchor, HistoryResult(result.frame, coverage, result.state))
                raise HistoryUnavailable(coverage) from exc
            coverage.update(available_from=int(frame.date.iloc[0].timestamp()),
                            available_to=int(frame.date.iloc[-1].timestamp()),rows=len(frame))
            archive.write(anchor, HistoryResult(result.frame, coverage, result.state), snapshot, frame)
            return frame, snapshot, coverage
    except HistoryUnavailable:
        raise
    except InterprocessLockTimeout as exc:
        raise HistoryPending({**base,'reason':'already_preparing'}) from exc
    except HistoryContractError as exc:
        raise HistoryUnavailable({**base,'state':'invalid','reason':exc.code,'can_resume':False}) from exc
    except Exception as exc:
        import math
        delay = getattr(exc,'retry_after',None)
        retry = max(2,min(3600,math.ceil(delay))) if type(delay) in (int,float) and math.isfinite(delay) else 2
        raise HistorySourceError({**base,'state':'source_error','reason':type(exc).__name__,
                                  'next_retry':retry}) from exc
