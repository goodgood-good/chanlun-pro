from datetime import datetime, timedelta

import pandas as pd
import pytest

from chanlun.screening.history_repair import fill_closed_gaps, snapshot_provenance
from chanlun.screening.rules import CN


def inputs():
    opening = datetime(2025, 9, 30, 9, 30, tzinfo=CN)
    minute = pd.DataFrame([{'code': 'BJ.920001', 'date': opening + timedelta(minutes=i),
                           'open': 5 if i == 0 else 6, 'high': 5 if i == 0 else 7,
                           'low': 5 if i == 0 else 6, 'close': 5 if i == 0 else 6.5, 'volume': 10.}
                          for i in range(31)])
    minute.attrs.update(price_basis_revision='same-adjustment', structure_price_quantum='0.01')
    coarse = minute.iloc[[1, -1]].copy()
    coarse['date'] = [opening - timedelta(days=1), opening + timedelta(days=1)]
    coarse.attrs = dict(minute.attrs)
    return coarse, minute, int((opening + timedelta(minutes=30)).timestamp())


def test_complete_minutes_restore_the_real_opening_price_without_replacing_existing_candles():
    coarse, fine, cutoff = inputs()
    before = coarse.copy()
    result = fill_closed_gaps(coarse, fine, [cutoff], code='BJ.920001', frequency='30m')
    assert len(result) == 3 and result.attrs['history_repair']['filled_bars'] == 1
    row = result.iloc[1]
    assert (row.open, row.high, row.low, row.close, row.volume) == (5, 7, 5, 6.5, 310)
    assert result.attrs['history_repair']['bars'][0]['source_bars'] == 31
    pd.testing.assert_frame_equal(coarse, before)
    assert not coarse.attrs.get('history_repair')
    assert result.iloc[[0, 2]].reset_index(drop=True).equals(coarse.reset_index(drop=True))


@pytest.mark.parametrize('damage', ['missing_minute', 'missing_opening', 'duplicate', 'wrong_symbol', 'price_basis', 'negative_volume', 'invalid_ohlc'])
def test_missing_or_incompatible_sources_never_fill_a_gap(damage):
    coarse, fine, cutoff = inputs()
    if damage == 'missing_minute':
        fine = fine.drop(12)
    elif damage == 'missing_opening':
        fine = fine.iloc[1:]
    elif damage == 'duplicate':
        fine = pd.concat([fine, fine.iloc[:1]])
    elif damage == 'wrong_symbol':
        fine['code'] = 'BJ.920002'
    elif damage == 'price_basis':
        fine.attrs['price_basis_revision'] = 'different'
    elif damage == 'negative_volume':
        fine.loc[15, 'volume'] = -1
    else:
        fine.loc[15, 'high'] = 1
    result = fill_closed_gaps(coarse, fine, [cutoff], code='BJ.920001', frequency='30m')
    assert result.equals(coarse)
    assert result.attrs['history_repair']['filled_bars'] == 0


def test_repair_only_addresses_missing_completed_session_aligned_candles():
    coarse, fine, cutoff = inputs()
    for target in [cutoff + 60, cutoff + 2 * 86400, int(coarse.date.iloc[0].timestamp())]:
        result = fill_closed_gaps(coarse, fine, [target], code='BJ.920001', frequency='30m')
        assert result.equals(coarse)


def test_reconstruction_preserves_the_vendors_timezone_dtype():
    import pytz
    coarse, fine, cutoff = inputs()
    coarse['date'] = coarse.date.dt.tz_convert(pytz.timezone('Asia/Shanghai'))
    result = fill_closed_gaps(coarse, fine, [cutoff], code='BJ.920001', frequency='30m')
    assert result.date.dtype == coarse.date.dtype
    assert isinstance(result.date.dtype, pd.DatetimeTZDtype)
    assert len(result) == 3


def test_replay_provenance_never_claims_native_equivalence_or_leaks_future_repairs():
    coarse, fine, cutoff = inputs()
    result = fill_closed_gaps(coarse, fine, [cutoff], code='BJ.920001', frequency='30m')
    note = snapshot_provenance(result)
    assert note['native_equivalence'] == 'not_assumed'
    assert note['aggregation'] == 'reported_minute_ohlcv'
    assert snapshot_provenance(result.iloc[:1]) is None
    note['bars'][0]['source_bars'] = -1
    assert result.attrs['history_repair']['bars'][0]['source_bars'] == 31
