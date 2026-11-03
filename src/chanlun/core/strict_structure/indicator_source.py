"""The user's single causal MACD HTF source for strength and measurements.

The smooth chart overlay is presentation only. All measurements use the raw
values known at each source-bar close; missing HTF never falls back to MACD.
"""
import numpy as np

from chanlun.core.macd_htf import CAUSAL_PARTIAL_HTF_ALGORITHM, level_plus_one

STRENGTH_SOURCE = 'macd_htf'
STRENGTH_BASIS = 'causal_macd_htf_v1'


def strength_macd(cd):
    frequency = cd.get_frequency()
    values = getattr(cd, '_strict_htf_macd_by_level', {}).get(0)
    target = level_plus_one(frequency)
    if not target or not isinstance(values, dict):
        raise ValueError('MACD HTF raw evidence unavailable')
    if values.get('algorithm') != CAUSAL_PARTIAL_HTF_ALGORITHM:
        raise ValueError('MACD HTF requires the causal raw algorithm')
    if values.get('source_frequency') != frequency or values.get('target_frequency') != target:
        raise ValueError('MACD HTF target differs from the source period')
    dates=tuple(bar.date for bar in cd.get_src_klines())
    size = len(dates)
    if tuple(values.get('dates',())) != dates or tuple(values.get('known_at',())) != dates:
        raise ValueError('MACD HTF raw source clocks differ from the source bars')
    for key in ('dif','dea','hist'):
        series = np.asarray(values.get(key, ()), dtype=float)
        if series.ndim != 1 or len(series) != size:
            raise ValueError('MACD HTF raw values must align with source bars')
        if not np.isfinite(series).all():
            raise ValueError('MACD HTF raw values must be finite')
    return values


def strength_metadata(cd):
    values = strength_macd(cd)
    return {'strength_source':STRENGTH_SOURCE, 'strength_basis':STRENGTH_BASIS,
            'strength_frequency':values['target_frequency'], 'strength_algorithm':values['algorithm']}
