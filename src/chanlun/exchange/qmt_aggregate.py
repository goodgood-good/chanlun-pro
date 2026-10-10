"""Rebuild a demonstrably inconsistent QMT 30m aggregate from its six real 5m rows."""
import hashlib
import json

import numpy as np
import pandas as pd


_VALUES = ['open', 'high', 'low', 'close', 'volume']
_BASIS = ('structure_price_quantum', 'price_basis_revision',
          'price_basis_provider', 'price_basis_adjustment')
_MAX_RECONSTRUCTIONS = 8  # Bound optional local reads; larger failures remain rejected.


def _valid_source(frame, code, *, geometry=True, frequency=None):
    if (not isinstance(frame, pd.DataFrame) or frame.empty
            or not set(['code', 'date', *_VALUES]).issubset(frame.columns)
            or not isinstance(frame.date.dtype, pd.DatetimeTZDtype)
            or not frame.index.is_unique
            or frame.date.isna().any() or frame.date.duplicated().any() or frame.index.has_duplicates
            or not frame.date.is_monotonic_increasing or not frame.code.eq(code).all()
            or frame.attrs.get('bar_time_label') not in (None, 'end')
            or frequency and 'frequency' in frame and not frame.frequency.eq(frequency).all()
            or frame.attrs.get('qmt_listing_history_conflict') is True):
        return False
    values = frame[_VALUES].to_numpy(dtype=float)
    if (not np.isfinite(values).all() or (values[:, :4] <= 0).any()
            or (values[:, 4] < 0).any()):
        return False
    return not geometry or not _bad_geometry(frame).any()


def _bad_geometry(frame):
    return ((frame.high < frame[['open', 'close', 'low']].max(axis=1))
            | (frame.low > frame[['open', 'close', 'high']].min(axis=1)))


def _record(row):
    return {'time': pd.Timestamp(row.date).isoformat(),
            **{name: float(getattr(row, name)) for name in _VALUES}}


def reconstruct_thirty_minute(frame, code, read_five):
    """Accept only the observed zero-volume extrema omission, never arbitrary repairs.

    The original open, close and volume must equal the full six-row aggregate;
    original high/low must equal the positive-volume subset. A changed basis,
    incomplete grid, invalid source or any unrelated discrepancy keeps the
    original rejected frame. Every replacement retains its original and source
    rows in deterministic metadata for frozen evidence and input fingerprints.
    """
    if not _valid_source(frame, code, geometry=False, frequency='30m'):
        return frame
    invalid = frame.loc[_bad_geometry(frame)]
    if invalid.empty or len(invalid) > _MAX_RECONSTRUCTIONS:
        return frame
    if any(frame.attrs.get(key) in (None, '') for key in _BASIS):
        return frame
    corrections = []
    for index, original in invalid.iterrows():
        closing = pd.Timestamp(original.date).tz_convert('Asia/Shanghai')
        if (closing.second or closing.microsecond or closing.nanosecond
                or closing.hour * 60 + closing.minute not in (600, 630, 660, 690, 810, 840, 870, 900)):
            return frame
        opening = closing - pd.Timedelta(minutes=30)
        try:
            source = read_five(opening, closing)
        except Exception:
            # Optional reconstruction failure cannot authorize changing the
            # original rows or hide their existing INVALID_BARS rejection.
            return frame
        if not _valid_source(source, code, frequency='5m'):
            return frame
        if any(source.attrs.get(key) != frame.attrs[key] for key in _BASIS):
            return frame
        expected = pd.date_range(opening + pd.Timedelta(minutes=5), closing, freq='5min')
        if len(source) != 6 or list(source.date) != list(expected):
            return frame
        traded = source.loc[source.volume > 0]
        if traded.empty or len(traded) == len(source):
            return frame
        derived = {'open': source.open.iloc[0], 'high': source.high.max(),
                   'low': source.low.min(), 'close': source.close.iloc[-1],
                   'volume': source.volume.sum()}
        if (any(float(derived[key]) != float(original[key]) for key in ('open', 'close', 'volume'))
                or float(traded.high.max()) != float(original.high)
                or float(traded.low.min()) != float(original.low)):
            return frame
        source_rows = [_record(row) for row in source.itertuples(index=False)]
        corrections.append((index, derived, {'original': _record(original), 'source_5m': source_rows,
            'source_sha256': hashlib.sha256(json.dumps(source_rows, sort_keys=True,
                                                     allow_nan=False).encode()).hexdigest()}))
    result = frame.copy()
    for index, derived, _proof in corrections:
        for name, value in derived.items():
            result.at[index, name] = value
    result.attrs['qmt_30m_reconstruction'] = {
        'schema': 'qmt-complete-5m-aggregate-v1', 'source_frequency': '5m',
        'bar_time_label': 'end', 'price_basis_revision': frame.attrs['price_basis_revision'],
        'reason': 'native_extrema_omitted_zero_volume_source_rows',
        'rows': [proof for _index, _derived, proof in corrections],
    }
    return result
