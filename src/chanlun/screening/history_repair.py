"""Reconstruct missing coarse candles only from complete local minute inputs."""
from datetime import datetime, timedelta
from copy import deepcopy
import hashlib
import json
import math

import pandas as pd

from .interval_clock import CN


def snapshot_provenance(frame):
    """Keep derivation evidence only for candles visible in this replay."""
    report = frame.attrs.get('history_repair') or {}
    if not report.get('filled_bars') or frame.empty:
        return None
    cutoff = int(frame.date.iloc[-1].timestamp())
    bars = [bar for bar in report.get('bars', []) if bar['closed_at'] <= cutoff]
    if not bars:
        return None
    return deepcopy({**{key: report[key] for key in (
        'method', 'source_frequency', 'aggregation', 'native_equivalence') if key in report},
        'filled_bars': len(bars), 'bars': bars})


def fill_closed_gaps(frame, finer, missing, *, code, frequency):
    """Keep existing bars; never interpolate prices or invent missing minutes."""
    if (frame is None or frame.empty or frequency not in {'5m', '30m'}
            or not code.startswith(('SH.', 'SZ.', 'BJ.'))):
        return frame
    targets = sorted(set(int(stamp) for stamp in missing))
    if not targets:
        return frame
    result = frame.copy()
    report = {'method': 'complete_local_1m', 'source_frequency': '1m',
              'aggregation': 'reported_minute_ohlcv', 'native_equivalence': 'not_assumed',
              'requested_bars': len(targets), 'filled_bars': 0, 'remaining_bars': len(targets), 'bars': []}
    result.attrs['history_repair'] = report
    if finer is None or finer.empty:
        report['reason'] = 'MINUTE_HISTORY_UNAVAILABLE'
        return result
    for field in ('price_basis_revision', 'structure_price_quantum'):
        if not frame.attrs.get(field) or str(frame.attrs[field]) != str(finer.attrs.get(field)):
            report['reason'] = 'MINUTE_PRICE_BASIS_MISMATCH'
            return result
    if ('code' not in finer or set(finer.code) != {code}
            or finer.date.duplicated().any() or not finer.date.is_monotonic_increasing):
        report['reason'] = 'MINUTE_IDENTITY_OR_CLOCK_INVALID'
        return result
    step = int(frequency[:-1])
    available = {int(row.date.timestamp()): row for row in finer.itertuples(index=False)}
    existing = {int(t.timestamp()) for t in frame.date}
    additions = []
    for end in targets:
        if end in existing:
            continue
        closed = datetime.fromtimestamp(end, CN)
        if not frame.date.iloc[0] <= closed <= frame.date.iloc[-1]:
            continue
        left = closed - timedelta(minutes=step)
        # Only genuine session-aligned A-share bars can be synthesized.
        base = 570 if 570 < closed.hour * 60 + closed.minute <= 690 else 780
        offset = closed.hour * 60 + closed.minute - base
        if not 0 < offset <= 120 or offset % step or closed.second:
            continue
        clocks = list(range(end - step * 60 + 60, end + 1, 60))
        if (left.hour, left.minute) == (9, 30):
            clocks.insert(0, end - step * 60)
        if any(stamp not in available for stamp in clocks):
            continue
        rows = [available[stamp] for stamp in clocks]
        if any(not all(math.isfinite(float(getattr(row, name))) for name in ('open', 'high', 'low', 'close', 'volume'))
               or row.low <= 0 or row.volume < 0 or not row.low <= min(row.open, row.close) <= max(row.open, row.close) <= row.high
               for row in rows):
            continue
        candle = {'code': code, 'date': closed, 'open': rows[0].open,
                  'high': max(row.high for row in rows), 'low': min(row.low for row in rows),
                  'close': rows[-1].close, 'volume': sum(row.volume for row in rows)}
        if 'amount' in frame:
            if 'amount' not in finer:
                continue
            candle['amount'] = sum(row.amount for row in rows)
        if set(frame.columns) - set(candle):
            continue
        additions.append(candle)
        source = [[stamp, *(float(getattr(row, name)) for name in ('open', 'high', 'low', 'close', 'volume'))]
                  for stamp, row in zip(clocks, rows)]
        report['bars'].append({'closed_at': end, 'source_from': clocks[0], 'source_bars': len(rows),
                               'source_sha256': hashlib.sha256(json.dumps(source, separators=(',', ':')).encode()).hexdigest()})
    if additions:
        attrs = dict(result.attrs)
        inserted = pd.DataFrame(additions)[frame.columns]
        inserted['date'] = inserted.date.astype(frame.date.dtype)
        result = pd.concat([result, inserted], ignore_index=True).sort_values('date').reset_index(drop=True)
        result.attrs = attrs
    report.update(filled_bars=len(additions), remaining_bars=len(targets) - len(additions),
                  reason='COMPLETE' if len(additions) == len(targets) else 'MINUTE_HISTORY_INCOMPLETE')
    return result
