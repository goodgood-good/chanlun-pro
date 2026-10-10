"""Native minute labels and every aggregate component stay causal and exact."""
from copy import deepcopy

import numpy as np
import pandas as pd
import pytest

from chanlun.exchange.usmart_session_aggregation import aggregate_closed_us_minutes, expected_closed_window_start


def raw(stamps):
    dates = pd.to_datetime(stamps,utc=True).tz_convert('America/New_York')
    count = len(dates)
    return pd.DataFrame({'date':dates,'frequency':'1m','code':'NVDA.US',
        'open':np.arange(count,dtype=float)+100, 'high':np.arange(count,dtype=float)+102,
        'low':np.arange(count,dtype=float)+99, 'close':np.arange(count,dtype=float)+101,
        'volume':np.arange(count,dtype=float)+1})


def minutes(start,count):
    return raw(pd.date_range(start,periods=count,freq='min'))


def test_source_open_becomes_close_only_after_its_minute_has_completed():
    source=minutes('2026-10-09 13:30Z',7)
    source.attrs={'structure_price_quantum':'0.001','price_basis_revision':'frozen-fixture','bar_time_label':'start'}
    saved=deepcopy(source)
    frame,coverage=aggregate_closed_us_minutes(source,'1m','2026-10-09 13:35:59Z')
    assert frame.date.tolist()==list(pd.date_range('2026-10-09 13:31Z',periods=5,freq='min'))
    for name in ('open','high','low','close','volume'):
        assert frame[name].tolist()==source[name].iloc[:5].tolist()
    assert coverage['unclosed_or_future_minute_count']==2
    assert coverage['canonical_cutoff']==int(pd.Timestamp('2026-10-09 13:35Z').timestamp())
    assert frame.attrs['bar_time_label']=='end' and frame.attrs['source_minute_count']==5
    assert frame.attrs['price_basis_revision']==source.attrs['price_basis_revision']
    pd.testing.assert_frame_equal(source,saved)
    assert source.attrs==saved.attrs


@pytest.mark.parametrize('frequency,size',[('5m',5),('30m',30)])
def test_aggregate_uses_exact_complete_unique_minute_components(frequency,size):
    source=minutes('2026-10-09 13:30Z',size+3)
    cutoff=source.date.iloc[size-1]+pd.Timedelta(minutes=1)
    frame,coverage=aggregate_closed_us_minutes(source,frequency,cutoff)
    assert len(frame)==1 and frame.date.iloc[0]==cutoff
    assert frame.iloc[0].to_dict() | {} == {'date':cutoff,'frequency':frequency,'code':'NVDA.US',
        'open':100.,'high':float(101+size),'low':99.,'close':float(100+size),
        'volume':float(size*(size+1)//2)}
    assert coverage['complete'] and coverage['source_minute_count']==size


def test_internal_missing_minute_and_entire_bucket_are_never_filled():
    source=minutes('2026-10-09 13:30Z',20).drop(index=[6,10,11,12,13,14])
    frame,coverage=aggregate_closed_us_minutes(source,'5m','2026-10-09 13:50Z')
    assert frame.date.tolist()==[pd.Timestamp('2026-10-09 13:35Z'),pd.Timestamp('2026-10-09 13:50Z')]
    assert coverage['missing_minute_count']==6 and not coverage['complete']
    assert not coverage['leading_partial_bucket'] and not coverage['trailing_partial_bucket']
    assert len(coverage['incomplete_buckets'])==2


def test_partial_left_page_is_distinct_from_internal_holes():
    source=minutes('2026-10-09 13:32Z',8)
    frame,coverage=aggregate_closed_us_minutes(source,'5m','2026-10-09 13:40Z')
    assert frame.date.tolist()==[pd.Timestamp('2026-10-09 13:40Z')]
    assert coverage['missing_minute_count']==0 and coverage['complete']
    assert len(coverage['leading_partial_bucket']['missing_opens'])==2
    # Having a partial page at the left cannot hide a different hole inside it.
    _,broken=aggregate_closed_us_minutes(source.drop(index=1),'5m','2026-10-09 13:40Z')
    assert broken['missing_minute_count']==1 and not broken['complete']


def test_unfinished_right_bucket_is_not_an_internal_gap_or_stale_bar():
    source=minutes('2026-10-09 13:30Z',9)
    frame,coverage=aggregate_closed_us_minutes(source,'5m','2026-10-09 13:37:30Z')
    assert frame.date.tolist()==[pd.Timestamp('2026-10-09 13:35Z')]
    assert coverage['unfinished_bucket']['close']==int(pd.Timestamp('2026-10-09 13:40Z').timestamp())
    assert coverage['missing_minute_count']==0 and coverage['complete']
    assert not coverage['stale_source'] and coverage['unclosed_or_future_minute_count']==2
    assert frame.attrs['canonical_minute_cutoff']==int(pd.Timestamp('2026-10-09 13:35Z').timestamp())


def test_closed_tail_missing_minutes_are_distinct_from_unfinished_tail():
    source=minutes('2026-10-09 13:30Z',7)
    frame,coverage=aggregate_closed_us_minutes(source,'5m','2026-10-09 13:40Z')
    assert coverage['trailing_partial_bucket'] and not coverage['unfinished_bucket']
    assert coverage['stale_source'] and not coverage['complete']
    assert coverage['missing_minute_count']==0  # No missing slot within the covered prefix.
    assert len(frame)==1


@pytest.mark.parametrize('frequency',['1m','5m','30m'])
def test_duplicate_minutes_do_not_choose_an_arbitrary_winner(frequency):
    source=minutes('2026-10-09 13:30Z',30)
    source=pd.concat([source,source.iloc[[2]]],ignore_index=True)
    frame,coverage=aggregate_closed_us_minutes(source,frequency,'2026-10-09 14:00Z')
    assert coverage['duplicate_minute_count']==1 and not coverage['complete']
    assert pd.Timestamp('2026-10-09 13:33Z') not in set(frame.date)
    if frequency=='30m':assert frame.empty


def test_bad_source_minute_is_not_repaired_or_hidden_inside_aggregate_extrema():
    source=minutes('2026-10-09 13:30Z',5)
    source.loc[0,'low']=100.5  # Above its unchanged 100 opening; later lows could mask it.
    native,native_health=aggregate_closed_us_minutes(source,'1m','2026-10-09 13:35Z')
    higher,health=aggregate_closed_us_minutes(source,'5m','2026-10-09 13:35Z')
    assert native.iloc[0].low==100.5 and native.iloc[0].open==100
    assert native_health['invalid_minute_count']==health['invalid_minute_count']==1
    assert higher.empty and not health['complete']


def test_start_is_output_close_bound_and_retains_earlier_components():
    source=minutes('2026-10-09 13:30Z',10)
    frame,coverage=aggregate_closed_us_minutes(source,'5m','2026-10-09 13:40Z',start='2026-10-09 13:40Z')
    assert frame.date.tolist()==[pd.Timestamp('2026-10-09 13:40Z')]
    assert frame.iloc[0].open==105 and frame.iloc[0].volume==40
    assert coverage['complete'] and coverage['source_minute_count']==5


def test_overnight_premarket_and_regular_closing_stamp_cannot_enter_a_bucket():
    source=raw([pd.Timestamp('2026-10-08 19:59Z'),pd.Timestamp('2026-10-08 20:00Z'),
                pd.Timestamp('2026-10-09 12:00Z'),*pd.date_range('2026-10-09 13:30Z',periods=5,freq='min')])
    frame,coverage=aggregate_closed_us_minutes(source,'5m','2026-10-09 13:35Z')
    assert frame.date.tolist()==[pd.Timestamp('2026-10-09 13:35Z')]
    assert coverage['outside_session_minute_count']==2
    assert coverage['leading_partial_bucket']['close']==int(pd.Timestamp('2026-10-08 20:00Z').timestamp())


def test_dst_changes_utc_open_but_preserves_session_local_buckets():
    source=raw([*pd.date_range('2026-03-06 14:30Z',periods=5,freq='min'),
                *pd.date_range('2026-03-09 13:30Z',periods=5,freq='min')])
    frame,coverage=aggregate_closed_us_minutes(source,'5m','2026-03-09 13:35Z')
    assert frame.date.tolist()==[pd.Timestamp('2026-03-06 14:35Z'),pd.Timestamp('2026-03-09 13:35Z')]
    assert frame.date.dt.hour.tolist()==[9,9]
    # The rest of Friday is truly missing, not filled across the weekend.
    assert coverage['missing_minute_count']==385


def test_holiday_and_half_day_calendar_have_no_fabricated_afternoon_buckets():
    source=raw([pd.Timestamp('2026-11-26 15:00Z'),
                *pd.date_range('2026-11-27 14:30Z',periods=210,freq='min'),
                pd.Timestamp('2026-11-27 18:00Z')])
    frame,coverage=aggregate_closed_us_minutes(source,'30m','2026-11-27 22:00Z')
    assert len(frame)==7 and frame.date.max()==pd.Timestamp('2026-11-27 18:00Z')
    assert coverage['outside_session_minute_count']==2
    assert coverage['expected_cutoff']==int(pd.Timestamp('2026-11-27 18:00Z').timestamp())
    assert coverage['complete']


def test_history_older_than_730_days_is_preserved_and_leading_page_not_fabricated():
    source=raw([*pd.date_range('2023-01-03 14:30Z',periods=30,freq='min'),
                *pd.date_range('2026-10-09 13:30Z',periods=30,freq='min')])
    frame,coverage=aggregate_closed_us_minutes(source,'30m','2026-10-09 14:00Z')
    assert frame.date.min()==pd.Timestamp('2023-01-03 15:00Z') and len(frame)==2
    assert coverage['missing_minute_count']>100000


def test_unsorted_and_microsecond_dtype_input_still_uses_absolute_nanosecond_clock():
    source=minutes('2026-10-09 13:30Z',5).iloc[::-1].copy()
    source['date']=source.date.astype('datetime64[us, America/New_York]')
    frame,coverage=aggregate_closed_us_minutes(source,'5m',pd.Timestamp('2026-10-09 13:35Z').timestamp())
    assert len(frame)==1 and frame.iloc[0].open==100 and coverage['complete']


def test_empty_and_fully_unclosed_inputs_remain_empty_with_explicit_incomplete_status():
    source=minutes('2026-10-09 13:30Z',1)
    for frame in (source.iloc[:0],source,pd.DataFrame(columns=['date',*('open','high','low','close','volume')])):
        output,coverage=aggregate_closed_us_minutes(frame,'1m','2026-10-09 13:30:30Z')
        assert output.empty and not coverage['complete']
        assert isinstance(output.date.dtype,pd.DatetimeTZDtype)


@pytest.mark.parametrize('bad',['labels','frequency','mixed_codes','naive','nan_clock','subminute'])
def test_ambiguous_input_contracts_are_rejected(bad):
    source=minutes('2026-10-09 13:30Z',5)
    if bad=='labels':source.attrs['bar_time_label']='end'
    if bad=='frequency':source['frequency']='5m'
    if bad=='mixed_codes':source.loc[0,'code']='OTHER.US'
    if bad=='naive':source['date']=source.date.dt.tz_localize(None)
    if bad=='nan_clock':source.loc[0,'date']=pd.NaT
    if bad=='subminute':source.loc[0,'date']+=pd.Timedelta(seconds=1)
    with pytest.raises(ValueError):aggregate_closed_us_minutes(source,'5m','2026-10-09 13:35Z')


@pytest.mark.parametrize('frequency,cutoff,count,expected',[
    ('5m','2026-03-09 13:35Z',3,'2026-03-06 20:55Z'),
    ('30m','2026-11-27 22:00Z',8,'2026-11-25 21:00Z'),
    ('30m','2026-11-27 22:00Z',7,'2026-11-27 15:00Z'),
])
def test_expected_window_counts_session_grid_across_dst_holiday_and_half_day(frequency,cutoff,count,expected):
    assert expected_closed_window_start(frequency,cutoff,count)==pd.Timestamp(expected)


@pytest.mark.parametrize('fault',['invalid','missing'])
def test_fixed_expected_window_ignores_only_older_faults_without_replacing_current_bad_buckets(fault):
    source=minutes('2026-10-09 13:30Z',30)
    cutoff=pd.Timestamp('2026-10-09 14:00Z')
    left=expected_closed_window_start('5m',cutoff,3)
    assert left==pd.Timestamp('2026-10-09 13:50Z')
    old=source.copy(deep=True)
    if fault=='invalid':old.loc[2,'low']=999
    else:old=old.drop(index=2)
    frame,healthy=aggregate_closed_us_minutes(old,'5m',cutoff,start=left)
    assert len(frame)==3 and healthy['complete']
    current=old.copy(deep=True)
    if fault=='invalid':current.loc[17,'low']=999
    else:current=current.drop(index=17)
    frame,broken=aggregate_closed_us_minutes(current,'5m',cutoff,start=left)
    assert frame.date.tolist()==[pd.Timestamp('2026-10-09 13:55Z'),cutoff]
    assert broken[('invalid' if fault=='invalid' else 'missing')+'_minute_count']==1
    assert not broken['complete']  # Never reach backward to substitute a third good bar.
