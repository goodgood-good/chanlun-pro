"""Batch loading matches the existing causal incremental calculator exactly."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import pickle
from types import SimpleNamespace

import pytest

from chanlun.core.macd_htf import CausalPartialHigherMACDCalculator as Calculator
from chanlun.core.types import Kline


def bars(count=97):
    start = datetime(2024, 12, 31, 23, 45, tzinfo=timezone.utc)
    return [Kline(i, start + timedelta(minutes=i*13), 120., 80., 100.,
                  100. + (i % 17) * .013 - i * .003, 100.) for i in range(count)]


def incremental(klines, **options):
    calc = Calculator('1m', **options)
    result = None
    for end in range(1, len(klines)+1):
        result = calc.update(klines[:end], validated_incremental_prefix=True)
    return calc, result


@pytest.mark.parametrize('market', ['a', 'us', 'currency_spot'])
@pytest.mark.parametrize('target', ['5m', '30m', 'd', 'w', 'm', 'y'])
@pytest.mark.parametrize('china', [False, True])
def test_full_load_is_exactly_equal_to_incremental_values_and_tail_state(market, target, china):
    source = bars()
    for periods in ({}, {'fast':2, 'slow':3, 'signal':2}):
        options = {'market':market, 'target_frequency':target, 'china_mode':china, **periods}
        reference, expected = incremental(source, **options)
        calc = Calculator('1m', **options)
        assert calc.update(source) == expected
        assert calc._bucket_base == reference._bucket_base
        assert calc._last_partial == reference._last_partial
        assert all(a is b.date for a,b in zip(calc.dates, source))


@pytest.mark.parametrize('target', ['5m', '30m', 'd', 'w', 'm', 'y'])
def test_appends_revisions_and_shorter_rebuilds_preserve_causal_prefix(target):
    source = bars(150)
    options = {'market':'us', 'target_frequency':target, 'fast':2, 'slow':3, 'signal':2}
    calc = Calculator('1m', **options)
    first = deepcopy(calc.update(source[:65]))
    extended = deepcopy(calc.update(source[:120], validated_incremental_prefix=True))
    assert extended == incremental(source[:120], **options)[1]
    for name in ('dif','dea','hist','dates','known_at','bucket_keys'):
        assert list(extended[name][:65]) == list(first[name])
    source[119] = Kline(119, source[119].date, 130., 70., 100., 109., 100.)
    revised = deepcopy(calc.update(source[:120], validated_incremental_prefix=True))
    assert revised == incremental(source[:120], **options)[1]
    for name in ('dif','dea','hist'):
        assert revised[name][:119] == extended[name][:119]
    assert calc.update(source[:71]) == incremental(source[:71], **options)[1]


@pytest.mark.parametrize('target', ['5m', '30m', 'd', 'w', 'm', 'y'])
def test_batch_advances_scalar_state_once_per_higher_period(target, monkeypatch):
    source = bars(600)
    expected = incremental(source, market='a', target_frequency=target)[1]
    original = Calculator._partial
    calls = []
    def partial(self, close, base):
        calls.append(1)
        return original(self, close, base)
    monkeypatch.setattr(Calculator, '_partial', partial)
    result = Calculator('1m', 'a', target_frequency=target).update(source)
    assert result == expected
    assert len(calls) <= len(set(result['bucket_keys']))


@pytest.mark.parametrize('change', ['nan','infinity','bad_close','missing_date','nan_date','infinite_date','duplicate','backward','naive'])
def test_invalid_full_history_is_rejected_and_clears_previous_state(change):
    source = bars(64)
    calc = Calculator('1m', 'a')
    assert calc.update(source[:10]) is not None
    source[0].c += .1  # A changed historical prefix requires a full rebuild.
    bad = source[32]
    if change in {'nan','infinity','bad_close'}:
        bad.c = {'nan':float('nan'), 'infinity':float('inf'), 'bad_close':'invalid'}[change]
    elif change == 'missing_date':
        bad.date = None
    elif change in {'nan_date','infinite_date'}:
        bad.date = SimpleNamespace(timestamp=lambda:float('nan') if change=='nan_date' else float('inf'))
    elif change == 'duplicate':
        bad.date = source[31].date
    elif change == 'backward':
        bad.date = source[31].date-timedelta(minutes=1)
    else:
        bad.date = bad.date.replace(tzinfo=None)
    assert calc.update(source) is None
    assert calc.keys == calc.dates == calc.closes == calc.dif == calc.dea == calc.hist == []
    assert calc._bucket_base is calc._last_partial is None


@pytest.mark.parametrize('count', [1, 2, 8, 31, 32, 33])
def test_small_histories_and_threshold_boundaries_match_incremental(count):
    source = bars(count)
    assert Calculator('1m', 'a').update(source) == incremental(source, market='a')[1]


def test_serialized_batch_state_can_continue_with_the_scalar_incremental_path():
    source = bars(110)
    calc = Calculator('1m', 'a', target_frequency='30m')
    calc.update(source[:75])
    restored = pickle.loads(pickle.dumps(calc))
    assert restored.update(source, validated_incremental_prefix=True) == incremental(source, market='a', target_frequency='30m')[1]


def test_subclass_partial_formula_keeps_the_scalar_extension_contract():
    class Custom(Calculator):
        def _partial(self, close, base):
            fast, slow, dea = super()._partial(close, base)
            return fast + .25, slow, dea
    source = bars(75)
    reference = Custom('1m', 'a')
    for end in range(1, len(source)+1):
        expected = reference.update(source[:end], validated_incremental_prefix=True)
    assert Custom('1m', 'a').update(source) == expected
