"""Scalar time lookup preserves every supported boundary and incremental value."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pytest

from chanlun.core.macd_htf import CausalPartialHigherMACDCalculator, _causal_bucket_keys
from chanlun.core.types import Kline


@pytest.mark.parametrize('market', [None, 'a', 'hk', 'us', 'fx', 'ny_futures', 'currency_spot'])
@pytest.mark.parametrize('higher', ['5m', '30m', 'd', 'w', 'm', 'y'])
def test_scalar_matches_batch_buckets_at_epoch_and_calendar_boundaries(market, higher):
    dates = [datetime.fromisoformat(value).replace(tzinfo=timezone.utc) for value in (
        '0001-01-01T00:00:00', '1969-12-31T23:59:59', '1970-01-01T00:00:00',
        '2000-02-29T23:59:59', '2024-12-31T23:59:59', '2025-01-01T00:00:00',
        '2026-03-08T06:59:59', '2026-03-08T07:00:00',
        '2026-03-09T00:00:00', '2026-09-30T15:59:59', '2026-09-30T16:00:00',
        '2026-10-01T00:00:00', '2026-10-01T04:59:59', '2026-10-01T05:00:00',
        '2026-10-01T09:34:59', '2026-10-01T09:35:00', '2026-10-01T09:35:01',
        '2026-10-01T09:59:59', '2026-10-01T10:00:00', '2026-10-01T10:00:01',
        '9999-12-31T23:59:59')]
    stamps = np.array([int(value.timestamp()) for value in dates], dtype=np.int64)
    expected = _causal_bucket_keys(stamps, higher, market).tolist()
    calculator = CausalPartialHigherMACDCalculator('1m', market, target_frequency=higher)
    assert [calculator._key_for(SimpleNamespace(date=value)) for value in dates] == expected


@pytest.mark.parametrize('higher', ['5m', '30m', 'd', 'w', 'm', 'y'])
def test_append_and_last_close_revision_match_cold_calculation_without_rewriting_prefix(higher):
    started = datetime(2024, 12, 31, 23, 45, tzinfo=timezone.utc)
    bars = [Kline(i, started + timedelta(minutes=i), 120., 80., 100., 100. + (i % 7) - i / 10, 100.)
            for i in range(40)]
    calculator = CausalPartialHigherMACDCalculator('1m', 'us', fast=2, slow=3, signal=2, target_frequency=higher)
    prefix = deepcopy(calculator.update(bars[:17]))
    extended = deepcopy(calculator.update(bars[:30]))
    cold = CausalPartialHigherMACDCalculator('1m', 'us', fast=2, slow=3, signal=2, target_frequency=higher)
    assert extended == cold.update(bars[:30])
    for name in ('dif', 'dea', 'hist', 'dates', 'known_at', 'bucket_keys'):
        assert list(extended[name][:17]) == list(prefix[name])
    bars[29] = Kline(29, bars[29].date, 130., 70., 100., 115., 100.)
    revised = calculator.update(bars, validated_incremental_prefix=True)
    cold.reset()
    assert revised == cold.update(bars)
    for name in ('dif', 'dea', 'hist'):
        assert revised[name][:29] == extended[name][:29]


@pytest.mark.parametrize('date', [None, '2026-10-01', SimpleNamespace(timestamp=lambda: float('nan')),
                                  SimpleNamespace(timestamp=lambda: float('inf'))])
def test_invalid_timestamp_remains_rejected(date):
    calculator = CausalPartialHigherMACDCalculator('1m', 'a')
    with pytest.raises(ValueError, match='invalid kline date'):
        calculator._key_for(SimpleNamespace(date=date))
