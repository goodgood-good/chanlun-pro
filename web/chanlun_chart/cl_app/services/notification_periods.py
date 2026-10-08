"""Additional chart periods, bounded by the delivered signal's market clock."""
from datetime import datetime
from pathlib import Path


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
