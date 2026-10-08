"""The screening validator preserves calendar boundaries and gap diagnostics."""
from datetime import datetime

import pandas as pd
import pytest

from chanlun.screening.markets import market_context
from chanlun.screening.rules import CN, frame_quality, validate_frame


@pytest.fixture(params=[('us', 'TSLA.US'), ('hk', '00700.HK'), ('currency_spot', 'BTC/USDT')])
def session_frame(request):
    market, code = request.param
    context = market_context(market, code, datetime(2026, 3, 10, 23, 0, tzinfo=CN), '5m')
    stamps = [stamp for opening, closing in context['session_periods'][-10:]
              for stamp in range(opening + 300, closing + 1, 300)]
    frame = pd.DataFrame({'date': pd.to_datetime(stamps, unit='s', utc=True),
                          'code': code, 'open': 10., 'high': 11., 'low': 9.,
                          'close': 10., 'volume': 100.},
                         index=[f'bar-{i}' for i in range(len(stamps))])
    frame.attrs['source_identity'] = 'preserved'
    return frame, context


def test_real_sessions_preserve_valid_closes_and_input(session_frame):
    frame, context = session_frame
    original = frame.copy(deep=True)
    assert validate_frame(frame, context) == []
    assert frame_quality(frame, context)['status'] == 'complete'
    pd.testing.assert_frame_equal(frame, original)
    assert frame.attrs == original.attrs


def test_missing_candle_remains_a_gap_not_an_invalid_session(session_frame):
    frame, context = session_frame
    removed = frame.iloc[200].date
    frame = frame.drop(frame.index[200])
    assert validate_frame(frame, context) == []
    quality = frame_quality(frame, context)
    assert quality['errors'] == ['DATA_GAPS']
    assert quality['missing_bars'] == 1
    assert quality['first_missing_at'] == int(removed.timestamp())


@pytest.mark.parametrize('offset', [pd.Timedelta(minutes=1), pd.Timedelta(seconds=1),
                                  pd.Timedelta(microseconds=1), pd.Timedelta(nanoseconds=1)])
def test_off_grid_or_fractional_closes_are_rejected(session_frame, offset):
    frame, context = session_frame
    frame.loc[frame.index[200], 'date'] += offset
    assert validate_frame(frame, context) == ['INVALID_SESSION']


def test_intraday_break_and_opening_are_not_completed_candles():
    context = market_context('hk', '00700.HK', datetime(2026, 3, 10, 18, 0, tzinfo=CN), '5m')
    periods = context['session_periods'][-10:]
    stamps = [stamp for opening, closing in periods for stamp in range(opening + 300, closing + 1, 300)]
    base = pd.DataFrame({'date': pd.to_datetime(stamps, unit='s', utc=True),
                         'open': 10., 'high': 11., 'low': 9., 'close': 10., 'volume': 100.})
    assert validate_frame(base, context) == []
    for stamp in (periods[-1][0], periods[-2][1] + 300):
        extra = base.iloc[[0]].copy()
        extra['date'] = pd.to_datetime([stamp], unit='s', utc=True)
        frame = pd.concat([base, extra]).sort_values('date').reset_index(drop=True)
        assert validate_frame(frame, context) == ['INVALID_SESSION']


def test_adjacent_daily_sessions_keep_the_previous_days_final_candle():
    context = market_context('currency_spot', 'BTC/USDT', datetime(2026, 3, 10, 23, 0, tzinfo=CN), '5m')
    stamps = [stamp for opening, closing in context['session_periods'][-2:]
              for stamp in range(opening + 300, closing + 1, 300)]
    frame = pd.DataFrame({'date': pd.to_datetime(stamps, unit='s', utc=True),
                          'open': 10., 'high': 11., 'low': 9., 'close': 10., 'volume': 100.})
    opening = context['session_periods'][-1][0]
    assert opening in (frame.date.astype('int64') // 1_000_000_000).values
    assert validate_frame(frame, context) == []
