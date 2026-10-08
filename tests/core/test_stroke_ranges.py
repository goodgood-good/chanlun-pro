"""Extrema indexes must agree with direct queries through rebuilds and edits."""
from decimal import Decimal
from math import copysign
from random import Random
from types import SimpleNamespace

import pytest

from chanlun.core.stroke_ranges import StrokeRanges


def bars(count):
    random = Random(783)
    return [SimpleNamespace(index=i, h=random.randrange(10, 500), l=random.randrange(-500, -10))
            for i in range(count)]


def assert_queries(index, values):
    assert index.count == len(values)
    for start in range(len(values)):
        for end in range(start, len(values)):
            sample = values[start:end + 1]
            assert index.query(start, end) == (max(k.h for k in sample), min(k.l for k in sample))
    assert index.query(-1, 0) is None
    assert index.query(0, len(values)) is None
    assert index.query(1, 0) is None


@pytest.mark.parametrize('count', [0, 1, 2, 31, 32, 33, 129])
def test_batch_ranges_match_direct_extrema(count):
    values = bars(count)
    index = StrokeRanges()
    index.update(values)
    assert_queries(index, values)


def test_batch_then_append_tail_correction_and_shorter_rebuild():
    values = bars(96)
    index = StrokeRanges()
    index.update(values[:32])
    index.update(values[:65], start=32)
    assert_queries(index, values[:65])
    values[31].h = 2000
    values[40].l = -3000
    index.update(values, start=31)
    assert_queries(index, values)
    index.update(values[:3])
    assert all(len(row) == 3 for row in [*index.highs, *index.lows])
    assert_queries(index, values[:3])
    index.update(values[:33], start=3)
    assert_queries(index, values[:33])
    index.update([])
    index.update(values[:9], start=0)
    assert_queries(index, values[:9])


def test_prices_keep_integer_and_decimal_precision():
    values = [SimpleNamespace(index=i, h=2**100+i, l=Decimal('-0.0000000000000000000001')*i)
              for i in range(19)]
    index = StrokeRanges()
    index.update(values)
    assert_queries(index, values)
    assert type(index.query(0, 18)[0]) is int
    assert type(index.query(0, 18)[1]) is Decimal


def test_equal_prices_keep_incremental_pair_order():
    values = [SimpleNamespace(index=0, h=-0.0, l=0.0), SimpleNamespace(index=1, h=0.0, l=-0.0)]
    batch, incremental = StrokeRanges(), StrokeRanges()
    batch.update(values)
    incremental.update(values[:1])
    incremental.update(values, start=1)
    assert [copysign(1, x) for x in batch.query(0, 1)] == [1, -1]
    assert [copysign(1, x) for x in batch.query(0, 1)] == [copysign(1, x) for x in incremental.query(0, 1)]


def test_invalid_indices_preserve_the_existing_recovery_boundary():
    index = StrokeRanges()
    values = bars(8)
    index.update(values)
    values[5].index = 9
    with pytest.raises(ValueError, match='consecutive'):
        index.update(values)
    assert index.count == 5
    values[5].index = 5
    index.update(values, start=5)
    assert_queries(index, values)
