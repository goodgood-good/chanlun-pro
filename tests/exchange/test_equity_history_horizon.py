from types import SimpleNamespace

import pandas as pd
import pytest

from chanlun import config
from chanlun.exchange._lookback import DEFAULT_LOOKBACK_DAYS, get_lookback_days, get_lookback_timedelta
from chanlun.exchange.exchange_usmart import ExchangeUSmart


@pytest.mark.parametrize('frequency', list(DEFAULT_LOOKBACK_DAYS))
def test_us_and_a_share_horizons_are_identical(frequency):
    assert get_lookback_days(frequency, market='us') == get_lookback_days(frequency, market='a')


def test_equity_extension_does_not_change_other_markets():
    assert get_lookback_days('1m', market='us') == 60
    assert get_lookback_days('5m', market='us') == 365
    assert get_lookback_days('1m', market='hk') == get_lookback_days('1m') == 30
    assert get_lookback_days('5m', market='hk') == 90


@pytest.mark.parametrize('frequency', ['1m', '5m', '30m'])
def test_default_native_us_history_targets_dates_without_a_three_thousand_bar_cap(monkeypatch, frequency):
    monkeypatch.setattr(config, 'US_HISTORY_KLINE_SOURCE', 'usmart')
    end = pd.Timestamp('2026-10-09T20:00:00Z')
    monkeypatch.setattr(pd.Timestamp, 'now', lambda **_kwargs: end)
    calls = []
    frame = pd.DataFrame({'value': [1]})
    def window(*_args, **kwargs):
        calls.append(kwargs)
        return frame, {'reason': 'complete', 'complete': True}
    exchange = ExchangeUSmart('us', client=SimpleNamespace())
    monkeypatch.setattr(exchange, '_canonical_source', lambda: SimpleNamespace(window=window))
    assert exchange.klines('AAPL.US', frequency, end_date=end.isoformat()) is frame
    assert calls[-1]['target_from'] == end-get_lookback_timedelta(frequency, market='a')
    assert calls[-1]['target_rows'] is None
    assert calls[-1]['max_pages'] == 4  # Longer history remains resumable and bounded per request.
    exchange.klines('AAPL.US', frequency, end_date=end.isoformat(), args={'pages': 2})
    assert calls[-1]['target_rows'] == 600
