"""Canonical closed US bars built only from unique, closed native minutes.

Input ``date`` is the native minute's OPEN. Output ``date`` is the bucket's
CLOSE. No gap is filled, no provider price is repaired, and no bucket crosses
an XNYS session. Coverage separates a paginated left edge from internal holes
and the unfinished current bucket; consumers must inspect diagnostics.
"""
from __future__ import annotations

import math
from functools import lru_cache
from numbers import Real

import numpy as np
import pandas as pd

MINUTE = 60_000_000_000
VALUES = ['open', 'high', 'low', 'close', 'volume']


def _clock(value):
    if isinstance(value, bool) or isinstance(value, Real) and not math.isfinite(value):
        raise ValueError('clock must be timezone-aware or Unix seconds')
    stamp = (pd.Timestamp(value, unit='s', tz='UTC') if isinstance(value, Real)
             else pd.Timestamp(value))
    if pd.isna(stamp) or stamp.tzinfo is None:
        raise ValueError('clock must be timezone-aware or Unix seconds')
    return stamp.tz_convert('UTC')


def expected_closed_window_start(frequency, cutoff, count):
    """First close in the last N expected buckets, independent of source rows.

    Select this boundary before diagnosing/aggregating input. Counting only
    successfully built bars would incorrectly replace a bad current bucket with
    an older good one. A caller may further narrow it with ``target_from``.
    """
    if frequency not in {'1m','5m','30m'} or type(count) is not int or count < 1:
        raise ValueError('expected window requires a supported frequency and positive count')
    stamp = _clock(cutoff).floor('min')
    return _expected_window_start(frequency, stamp.value, count)


@lru_cache(maxsize=256)
def _expected_window_start(frequency, cutoff_ns, count):
    import exchange_calendars as xcals
    cutoff = pd.Timestamp(cutoff_ns,unit='ns',tz='UTC')
    step = int(frequency[:-1])*MINUTE
    days = max(14, math.ceil(count*int(frequency[:-1])/390)*2+14)
    while True:
        calendar = xcals.get_calendar('XNYS',start=(cutoff-pd.Timedelta(days=days)).date(),
                                     end=(cutoff+pd.Timedelta(days=1)).date())
        schedule = calendar.schedule.loc[:cutoff.tz_convert(calendar.tz).date()]
        remaining = count
        for row in schedule.iloc[::-1].itertuples():
            opening = row.open.value
            available = max(0,(min(row.close.value,cutoff_ns)-opening)//step)
            if available >= remaining:
                return pd.Timestamp(opening+(available-remaining+1)*step,unit='ns',tz='UTC')
            remaining -= available
        # The heuristic is only a fetch bound, never a fabricated calendar.
        # Unusually long historical closures require a larger verified range.
        days *= 2


def aggregate_closed_us_minutes(raw, frequency, as_of, start=None):
    """Return ``(frame, coverage)`` without consulting any quote provider.

    ``start`` is an inclusive OUTPUT close-time boundary. Closed input rows
    preceding it remain available to complete its first bucket. Invalid 1m
    prices remain visible for the strict validator; higher buckets containing
    invalid or duplicated minutes are withheld rather than hiding bad inputs
    behind max/min. Input frames and their metadata are never mutated.
    """
    if frequency not in {'1m', '5m', '30m'}:
        raise ValueError('unsupported canonical US frequency')
    cutoff, beginning = _clock(as_of), None if start is None else _clock(start)
    if beginning is not None and beginning > cutoff:
        raise ValueError('start must not follow as_of')
    if not isinstance(raw, pd.DataFrame) or not {'date', *VALUES} <= set(raw):
        raise ValueError('native minute columns are missing')
    if raw.empty and not isinstance(raw.date.dtype, pd.DatetimeTZDtype):
        raw = raw.copy(deep=True)
        raw['date'] = pd.Series([], dtype='datetime64[ns, UTC]')
    if not isinstance(raw.date.dtype, pd.DatetimeTZDtype) or raw.date.isna().any():
        raise ValueError('native minute timestamps must be known and timezone-aware')
    if raw.attrs.get('bar_time_label') not in (None, 'start'):
        raise ValueError('aggregation requires native opening labels')
    if 'frequency' in raw and not raw.frequency.eq('1m').all():
        raise ValueError('aggregation requires minute input')
    if 'code' in raw and (raw.code.isna().any() or raw.code.nunique() > 1):
        raise ValueError('aggregation cannot combine symbols')

    step = int(frequency[:-1]) * MINUTE
    stamps = raw.date.dt.tz_convert('UTC').astype('datetime64[ns, UTC]').astype('int64').to_numpy()
    known = stamps + MINUTE <= cutoff.value
    if np.any(stamps[known] % MINUTE):
        raise ValueError('native opening labels must be whole minutes')
    numbers = raw[VALUES].apply(pd.to_numeric, errors='coerce').to_numpy(dtype=float)
    coverage = {'frequency':frequency, 'as_of':int(cutoff.timestamp()),
        'input_minute_count':len(raw), 'unclosed_or_future_minute_count':int((~known).sum()),
        'outside_session_minute_count':0, 'regular_minute_count':0, 'source_minute_count':0,
        'missing_minute_opens':[], 'duplicate_minute_opens':[], 'invalid_minute_opens':[],
        'leading_partial_bucket':None, 'trailing_partial_bucket':None,
        'incomplete_buckets':[], 'unfinished_bucket':None,
        'expected_cutoff':None, 'observed_minute_cutoff':None, 'canonical_cutoff':None,
        'complete':False, 'stale_source':False}
    output = []
    zone = raw.date.dt.tz
    columns = ['date', 'frequency', *(['code'] if 'code' in raw else []), *VALUES]

    def finish():
        frame = pd.DataFrame(output, columns=columns)
        frame['date'] = pd.to_datetime(frame.date, utc=True).dt.tz_convert(zone)
        for name in VALUES:
            frame[name] = pd.to_numeric(frame[name], errors='coerce').astype(float)
        frame.attrs = dict(raw.attrs)
        frame.attrs.update(bar_time_label='end', canonical_minute_cutoff=coverage['canonical_cutoff'],
                           source_minute_count=coverage['source_minute_count'])
        for category in ('missing','duplicate','invalid'):
            coverage[category+'_minute_opens'] = sorted(set(coverage[category+'_minute_opens']))
            coverage[category+'_minute_count'] = len(coverage[category+'_minute_opens'])
        coverage['complete'] = bool(output) and not (coverage['missing_minute_count']
            or coverage['duplicate_minute_count'] or coverage['invalid_minute_count']
            or coverage['trailing_partial_bucket'] or coverage['stale_source'])
        return frame, coverage

    if not known.any():
        return finish()
    import exchange_calendars as xcals
    first = pd.Timestamp(int(stamps[known].min()), unit='ns', tz='UTC').tz_convert('America/New_York')
    calendar = xcals.get_calendar('XNYS', start=(first-pd.Timedelta(days=7)).date(),
                                 end=(cutoff+pd.Timedelta(days=1)).date())
    schedule = calendar.schedule.loc[first.date():cutoff.tz_convert(calendar.tz).date()]
    opens = pd.DatetimeIndex(schedule.open).as_unit('ns').asi8
    closes = pd.DatetimeIndex(schedule.close).as_unit('ns').asi8
    if not len(opens):
        coverage['outside_session_minute_count'] = int(known.sum())
        return finish()
    positions = np.searchsorted(opens, stamps, side='right') - 1
    regular = known & (positions >= 0) & (stamps < closes[np.maximum(positions, 0)])
    coverage['outside_session_minute_count'] = int((known & ~regular).sum())
    coverage['regular_minute_count'] = int(regular.sum())
    for opening, closing in zip(opens, closes):
        completed = int(opening + (min(closing, cutoff.value)-opening)//step*step)
        if completed > opening:
            coverage['expected_cutoff'] = completed // 1_000_000_000
    if not regular.any():
        coverage['stale_source'] = coverage['expected_cutoff'] is not None
        return finish()

    lookup = {}
    for index in np.flatnonzero(regular):
        lookup.setdefault(int(stamps[index]), []).append(int(index))
    first_open, last_open = min(lookup), max(lookup)
    coverage['observed_minute_cutoff'] = (last_open + MINUTE)//1_000_000_000
    finite = np.isfinite(numbers).all(axis=1)
    op, high, low, close, volume = numbers.T
    valid = finite & (low > 0) & (volume >= 0) & (low <= np.minimum(op,close)) & (np.maximum(op,close) <= high)
    code = raw.code.iloc[0] if 'code' in raw else None
    for opening, closing in zip(opens, closes):
        if closing <= first_open or opening > last_open:
            continue
        first_bucket = max(0, int((first_open-opening)//step))
        for bucket_open in range(int(opening+first_bucket*step), int(closing), step):
            bucket_close = bucket_open+step
            if bucket_close > closing:
                break
            if bucket_open > last_open:
                break
            if bucket_close > cutoff.value:
                coverage['unfinished_bucket'] = {'open':bucket_open//1_000_000_000,
                                                  'close':bucket_close//1_000_000_000}
                break
            if beginning is not None and bucket_close < beginning.value:
                continue
            expected = range(bucket_open,bucket_close,MINUTE)
            missing = [stamp for stamp in expected if stamp not in lookup]
            duplicates = [stamp for stamp in expected if len(lookup.get(stamp,())) > 1]
            invalid = [stamp for stamp in expected if stamp in lookup
                       and any(not valid[index] for index in lookup[stamp])]
            internal = [stamp for stamp in missing if first_open <= stamp <= last_open]
            coverage['missing_minute_opens'].extend(stamp//1_000_000_000 for stamp in internal)
            coverage['duplicate_minute_opens'].extend(stamp//1_000_000_000 for stamp in duplicates)
            coverage['invalid_minute_opens'].extend(stamp//1_000_000_000 for stamp in invalid)
            if missing or duplicates or invalid:
                detail = {'open':bucket_open//1_000_000_000,'close':bucket_close//1_000_000_000,
                    'missing_opens':[stamp//1_000_000_000 for stamp in missing],
                    'duplicate_opens':[stamp//1_000_000_000 for stamp in duplicates],
                    'invalid_opens':[stamp//1_000_000_000 for stamp in invalid]}
                coverage['incomplete_buckets'].append(detail)
                if any(stamp < first_open for stamp in missing):
                    coverage['leading_partial_bucket'] = detail
                if any(stamp > last_open for stamp in missing):
                    coverage['trailing_partial_bucket'] = detail
                if missing or duplicates or frequency != '1m':
                    continue
            indices = [lookup[stamp][0] for stamp in expected]
            block = numbers[indices]
            row = {'date':pd.Timestamp(bucket_close,unit='ns',tz='UTC'), 'frequency':frequency,
                   'open':block[0,0], 'high':np.max(block[:,1]), 'low':np.min(block[:,2]),
                   'close':block[-1,3], 'volume':np.sum(block[:,4])}
            if 'code' in raw:
                row['code'] = code
            output.append(row)
            coverage['source_minute_count'] += len(indices)
            coverage['canonical_cutoff'] = bucket_close//1_000_000_000
    coverage['stale_source'] = (coverage['expected_cutoff'] is not None
                               and coverage['canonical_cutoff'] != coverage['expected_cutoff'])
    return finish()
