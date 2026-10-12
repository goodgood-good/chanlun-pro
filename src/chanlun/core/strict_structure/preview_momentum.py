"""Closed-bar evidence for the first contraction of same-direction MACD.

D:/缠论/chanlun_lesson_corpus/L024_*:313-319 permits a provisional area
estimate before price turns. One strict contraction is our explicit engineering
trigger; it is not a claim that every shorter histogram bar proves divergence.
"""
import math


MOMENTUM_RULE = 'first_closed_contraction_v1'


def contraction_witness(bars, histogram, direction, start):
    sign = 1 if direction == 'up' else -1
    momentum = [max(0., sign * float(value)) for value in histogram]
    index = next((i for i in range(len(momentum)-1, 0, -1)
                  if bars[i-1].date >= start and momentum[i-1] > momentum[i]), None)
    if index is None:
        return None, 'MOMENTUM_CONTRACTION_LOST'
    if any(value > momentum[index] for value in momentum[index+1:]):
        return None, 'MOMENTUM_RECOVERED'
    return {'momentum_rule':MOMENTUM_RULE,
            'momentum_times':[int(bar.date.timestamp()) for bar in bars[index-1:index+1]],
            'momentum_values':momentum[index-1:index+1],
            'momentum_checked_at':int(bars[-1].date.timestamp()),
            'momentum_latest':momentum[-1]}, None


def contraction_source_valid(witness, start, cutoff):
    try:
        times, values = witness['momentum_times'], witness['momentum_values']
        latest = witness['momentum_latest']
        return (witness.get('momentum_rule') == MOMENTUM_RULE
                and len(times) == len(values) == 2
                and all(type(t) is int for t in times)
                and start <= times[0] < times[1] <= cutoff
                and witness['momentum_checked_at'] == cutoff
                and all(type(v) in (int,float) and math.isfinite(v) for v in (*values,latest))
                and values[0] > values[1] >= latest >= 0)
    except (KeyError, TypeError, ValueError):
        return False
