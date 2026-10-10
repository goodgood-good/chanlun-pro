"""Canonical source preparation is shared, bounded and never relabels high bars."""
from copy import deepcopy
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from chanlun import config
from chanlun.exchange.exchange_usmart import ExchangeUSmart
from chanlun.exchange.usmart_canonical_history import (
    CanonicalUSmartHistory, CanonicalHistoryPending, SOURCE_TIME_CONTRACT, _fingerprint,
)
from chanlun.exchange.errors import USmartAPIError


def minutes(sessions=12):
    import exchange_calendars as xcals
    schedule = xcals.get_calendar('XNYS', start='2025-01-01', end='2026-10-09').schedule.tail(sessions)
    stamps = [pd.date_range(row.open, row.close, freq='1min', inclusive='left') for row in schedule.itertuples()]
    dates = pd.DatetimeIndex(np.concatenate([s.asi8 for s in stamps]), tz='UTC')
    values = np.arange(len(dates), dtype=float) / 1000 + 100
    frame = pd.DataFrame({'date':dates,'code':'NVDA.US','frequency':'1m','open':values,
                          'high':values+.5,'low':values-.5,'close':values+.1,'volume':100.})
    frame.attrs['bar_time_label'] = 'start'
    return frame, schedule.close.iloc[-1]


class Pages:
    def __init__(self, frame):
        self.frame, self.calls = frame, []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.frame.loc[self.frame.date <= kwargs['cursor']].tail(min(kwargs['count'],1000)).copy()


def source_at(tmp_path, frame, cutoff, **kwargs):
    pages = Pages(frame)
    client = SimpleNamespace(quote_host='https://fixture.invalid',channel='fixture')
    source = CanonicalUSmartHistory(client, root=tmp_path, loader=pages,
                                   clock=lambda:cutoff.timestamp(), **kwargs)
    return source, pages


def test_bounded_preparation_progress_survives_new_instance_and_readonly_reads(tmp_path):
    raw, cutoff = minutes()
    source, pages = source_at(tmp_path, raw, cutoff)
    first, state = source.window('NVDA.US','5m',cutoff=cutoff,target_rows=900,max_pages=2)
    assert len(pages.calls)==2 and state['can_resume'] and not state['complete']
    assert state['available_rows']<900
    root_state = deepcopy(state)
    again, same = source.window('NVDA.US','5m',cutoff=cutoff,target_rows=900,fetch=False)
    assert same['progress_token']==state['progress_token'] and len(pages.calls)==2
    pd.testing.assert_frame_equal(first,again)
    other, resumed_pages = source_at(tmp_path, raw, cutoff)
    result, resumed = other.window('NVDA.US','5m',cutoff=cutoff,target_rows=900,max_pages=4)
    assert resumed['complete'] and len(result)==900
    assert resumed['progress_token']!=root_state['progress_token']
    assert all(call['cursor']<pages.calls[-1]['cursor'] for call in resumed_pages.calls)
    assert result.attrs['source_time_contract']==SOURCE_TIME_CONTRACT
    assert result.date.max()==cutoff


def test_default_high_period_contract_is_not_three_thousand_minutes(tmp_path, monkeypatch):
    monkeypatch.setattr(config,'US_HISTORY_KLINE_SOURCE','usmart')
    raw, cutoff = minutes(45)
    source, pages = source_at(tmp_path,raw,cutoff)
    exchange = ExchangeUSmart('us',client=source.client)
    exchange._canonical_history = source
    with pytest.raises(CanonicalHistoryPending) as raised:
        exchange.klines('NVDA.US','5m',end_date=cutoff.isoformat())
    assert raised.value.preparation['required_rows']==3000
    assert raised.value.preparation['available_rows']<1000
    assert len(pages.calls)<=4
    for _ in range(5):
        try:
            frame=exchange.klines('NVDA.US','5m',end_date=cutoff.isoformat())
            break
        except CanonicalHistoryPending:
            pass
    else:
        pytest.fail('bounded slices did not finish available history')
    assert len(frame)==3000 and frame.date.max()==cutoff


def test_open_minute_at_cutoff_cannot_enter_any_completed_period(tmp_path):
    raw, cutoff = minutes(2)
    future = raw.tail(1).copy();future['date']=cutoff;future['close']=999.
    raw = pd.concat([raw,future],ignore_index=True)
    source,pages = source_at(tmp_path,raw,cutoff)
    result,coverage = source.window('NVDA.US','1m',cutoff=cutoff,target_rows=300,max_pages=1)
    assert coverage['complete'] and result.date.max()==cutoff
    assert pages.calls[0]['cursor']==cutoff-pd.Timedelta(minutes=1)
    assert result.close.max()<999


def test_terminal_internal_gap_does_not_become_infinite_preparing(tmp_path):
    raw,cutoff=minutes(2)
    raw=raw.drop(raw.index[-10]).reset_index(drop=True)
    source,pages=source_at(tmp_path,raw,cutoff)
    _,coverage=source.window('NVDA.US','5m',cutoff=cutoff,target_rows=100,max_pages=1)
    assert coverage['reason']=='minute_contract_error' and not coverage['can_resume']
    assert coverage['diagnostics']['missing_minute_count']==1


def test_same_clock_basis_across_periods_and_distinct_adjustments(tmp_path):
    raw,cutoff=minutes(2);source,_=source_at(tmp_path,raw,cutoff)
    frames=[source.window('NVDA.US',f,cutoff=cutoff,target_rows=10,max_pages=2)[0] for f in ('1m','5m','30m')]
    assert len({frame.attrs['price_basis_revision'] for frame in frames})==1
    other,_=source.window('NVDA.US','1m',cutoff=cutoff,right=0,target_rows=10,max_pages=1)
    assert other.attrs['price_basis_revision']!=frames[0].attrs['price_basis_revision']


def test_utc_normalization_handles_eastern_seed_and_page_without_object_dates(tmp_path):
    raw,cutoff=minutes(3)
    eastern=raw.copy();eastern['date']=eastern.date.dt.tz_convert('US/Eastern')
    eastern.attrs.update(price_basis_provider='usmart',price_basis_adjustment='forward',
                        _usmart_history_cache={'validated_at':cutoff.timestamp()})
    legacy=SimpleNamespace(_path=lambda _:tmp_path/'old.parquet',_load=lambda _:eastern)
    source,pages=source_at(tmp_path,raw,cutoff,legacy_cache=legacy)
    result,coverage=source.window('NVDA.US','5m',cutoff=cutoff,target_rows=200,max_pages=1)
    assert coverage['complete'] and len(pages.calls)==1
    assert str(result.date.dt.tz)=='UTC'
    stored,_=source._load(source._path('NVDA.US',1),'NVDA.US')
    assert str(stored.date.dt.tz)=='UTC' and stored.date.is_unique


@pytest.mark.parametrize('payload',[{'list':None},{'list':[]}])
def test_explicit_successful_empty_origin_is_terminal_and_readonly(payload,tmp_path):
    payload={'market':'us','symbol':'NVDA','type':1,'right':1,**payload}
    calls=[]
    client=SimpleNamespace(quote_host='https://fixture.invalid',channel='fixture',
                           quote=lambda *a,**kw:calls.append((a,kw)) or payload)
    source=CanonicalUSmartHistory(client,root=tmp_path)
    cutoff=pd.Timestamp('2026-10-09 20:00Z')
    frame,coverage=source.window('NVDA.US','30m',cutoff=cutoff,all_origin=True)
    assert frame.empty and coverage['source_origin'] and not coverage['can_resume']
    assert coverage['reason']=='source_empty'
    source.window('NVDA.US','30m',cutoff=cutoff,all_origin=True)
    assert len(calls)==1
    assert calls[0][0][1]['type']==1


def test_missing_list_field_is_not_a_successful_empty_origin(tmp_path):
    client=SimpleNamespace(quote_host='https://fixture.invalid',channel='fixture',quote=lambda *a,**kw:{})
    source=CanonicalUSmartHistory(client,root=tmp_path)
    with pytest.raises(USmartAPIError,match='identity'):
        source.window('NVDA.US','30m',cutoff=pd.Timestamp('2026-10-09 20:00Z'),all_origin=True)


def test_fetch_false_does_not_create_storage_or_request_and_has_new_basis(tmp_path):
    path=tmp_path/'absent'
    client=SimpleNamespace(quote_host='https://fixture.invalid',channel='fixture',
                          quote=lambda *a,**kw:pytest.fail('readonly query made a request'))
    source=CanonicalUSmartHistory(client,root=path)
    frame,coverage=source.window('NVDA.US','1m',cutoff=pd.Timestamp('2026-10-09 20:00Z'),fetch=False)
    assert not path.exists() and frame.empty and coverage['can_resume']
    assert frame.attrs['source_time_contract']==SOURCE_TIME_CONTRACT


def test_frozen_older_query_cannot_roll_back_the_shared_newer_source(tmp_path):
    raw,cutoff=minutes(3);source,_=source_at(tmp_path,raw,cutoff)
    source.window('NVDA.US','1m',cutoff=cutoff,target_rows=1000,max_pages=2)
    stored,meta=source._load(source._path('NVDA.US',1),'NVDA.US')
    fingerprint=_fingerprint(stored)
    earlier=cutoff-pd.Timedelta(minutes=30)
    frame,_=source.window('NVDA.US','1m',cutoff=earlier,target_rows=100,max_pages=1)
    assert frame.date.max()==earlier
    after,_=source._load(source._path('NVDA.US',1),'NVDA.US')
    assert _fingerprint(after)==fingerprint and meta['tail_checked']==cutoff.timestamp()


def test_origin_before_requested_history_is_terminal_even_with_newer_cached_rows(tmp_path):
    raw,cutoff=minutes(2);source,_=source_at(tmp_path,raw,cutoff)
    source.window('NVDA.US','30m',cutoff=cutoff,all_origin=True)
    earlier=raw.date.min()-pd.Timedelta(days=1)
    frame,coverage=source.window('NVDA.US','30m',cutoff=earlier,all_origin=True)
    assert frame.empty and coverage['source_origin'] and not coverage['can_resume']
    assert coverage['reason']=='source_empty'


def test_force_performs_fresh_tail_and_busy_lock_cannot_satisfy_force(tmp_path,monkeypatch):
    from contextlib import contextmanager
    from chanlun.exchange import usmart_canonical_history as module
    raw,cutoff=minutes(2);source,pages=source_at(tmp_path,raw,cutoff)
    _,initial=source.window('NVDA.US','5m',cutoff=cutoff,target_rows=100)
    count=len(pages.calls)
    _,refreshed=source.window('NVDA.US','5m',cutoff=cutoff,target_rows=100,force=True)
    assert len(pages.calls)==count+1 and refreshed['complete']
    assert refreshed['progress_token']==initial['progress_token']
    @contextmanager
    def busy(*a,**kw):
        raise module.InterprocessLockTimeout('busy')
        yield
    monkeypatch.setattr(module,'interprocess_file_lock',busy)
    _,waiting=source.window('NVDA.US','5m',cutoff=cutoff,target_rows=100,force=True)
    assert not waiting['complete'] and waiting['can_resume'] and waiting['lock_wait']


def test_timed_out_forced_tail_does_not_publish_cached_complete_window(tmp_path):
    raw,cutoff=minutes(2);source,pages=source_at(tmp_path,raw,cutoff)
    _,original=source.window('NVDA.US','5m',cutoff=cutoff,target_rows=100)
    def timeout(**kwargs): raise TimeoutError('read deadline')
    source.loader=timeout
    _,waiting=source.window('NVDA.US','5m',cutoff=cutoff,target_rows=100,force=True)
    assert waiting['reason']=='deadline' and waiting['can_resume'] and not waiting['complete']
    assert waiting['progress_token']==original['progress_token']


@pytest.mark.parametrize('inside',[False,True])
def test_diagnostics_apply_to_expected_consumption_window_not_entire_shared_history(tmp_path,inside):
    raw,cutoff=minutes(3)
    raw.loc[raw.index[-10 if inside else 0],'high']=1.
    source,_=source_at(tmp_path,raw,cutoff)
    path=source._path('NVDA.US',1)
    source._save(path,raw,{'frontier':raw.date.min().timestamp(),'cursor':(raw.date.min()-pd.Timedelta(milliseconds=1)).isoformat(),
                         'tail_checked':cutoff.timestamp(),'tail_checked_at':cutoff.timestamp()})
    frame,coverage=source.window('NVDA.US','1m',cutoff=cutoff,target_rows=300,fetch=False)
    assert len(frame)==300
    assert (coverage['reason']=='minute_contract_error') is inside
    assert coverage['complete'] is (not inside)


def test_fresh_overlap_missing_minute_cannot_be_silently_filled_from_cache(tmp_path):
    raw,cutoff=minutes(2);source,pages=source_at(tmp_path,raw,cutoff)
    _,state=source.window('NVDA.US','5m',cutoff=cutoff,target_rows=100)
    assert state['complete']
    pages.frame=raw.drop(raw.index[-10]).reset_index(drop=True)
    _,changed=source.window('NVDA.US','5m',cutoff=cutoff,target_rows=100,force=True)
    assert changed['reason']=='minute_contract_error' and not changed['can_resume']
    assert changed['diagnostics']['missing_minute_count']==1


@pytest.mark.parametrize('frequency,count',[('1m',3000),('5m',300),('30m',100)])
def test_missing_first_expected_minute_is_not_a_partial_left_page(tmp_path,frequency,count):
    from chanlun.exchange.usmart_session_aggregation import expected_closed_window_start
    raw,cutoff=minutes(12)
    first_close=expected_closed_window_start(frequency,cutoff,count)
    missing=first_close-pd.Timedelta(minutes=int(frequency[:-1]))
    raw=raw.loc[raw.date!=missing].reset_index(drop=True)
    source,_=source_at(tmp_path,raw,cutoff)
    source._save(source._path('NVDA.US',1),raw,{'frontier':raw.date.min().timestamp(),'origin':True,
        'cursor':(raw.date.min()-pd.Timedelta(milliseconds=1)).isoformat(),
        'tail_checked':cutoff.timestamp(),'tail_checked_at':cutoff.timestamp()})
    frame,coverage=source.window('NVDA.US',frequency,cutoff=cutoff,target_rows=count,fetch=False)
    assert len(frame)==count-1
    assert coverage['diagnostics']['missing_minute_count']==1
    assert coverage['reason']=='minute_contract_error' and not coverage['complete']


def test_warmed_30m_keeps_three_thousand_expected_high_period_bars(tmp_path,monkeypatch):
    monkeypatch.setattr(config,'US_HISTORY_KLINE_SOURCE','usmart')
    raw,cutoff=minutes(240);source,pages=source_at(tmp_path,raw,cutoff)
    source._save(source._path('NVDA.US',1),raw,{'frontier':raw.date.min().timestamp(),
        'cursor':(raw.date.min()-pd.Timedelta(milliseconds=1)).isoformat(),
        'tail_checked':cutoff.timestamp(),'tail_checked_at':cutoff.timestamp()})
    exchange=ExchangeUSmart('us',client=source.client);exchange._canonical_history=source
    frame=exchange.klines('NVDA.US','30m',end_date=cutoff.isoformat())
    assert len(frame)==3000 and frame.date.max()==cutoff and pages.calls==[]


@pytest.mark.parametrize('field',['market','symbol','type','right'])
def test_missing_explicit_response_identity_cannot_become_origin(tmp_path,field):
    payload={'market':'us','symbol':'NVDA','type':1,'right':1,'list':None};del payload[field]
    client=SimpleNamespace(quote_host='https://fixture.invalid',channel='fixture',quote=lambda *a,**kw:payload)
    source=CanonicalUSmartHistory(client,root=tmp_path)
    with pytest.raises(USmartAPIError,match='identity'):
        source.window('NVDA.US','30m',cutoff=pd.Timestamp('2026-10-09 20:00Z'),all_origin=True)


def test_quote_deadline_bounds_http_and_restores_normal_request_context(monkeypatch):
    from tests.exchange.test_usmart_retry import client_for,response
    client,calls,_=client_for(monkeypatch,[response(),response()])
    monkeypatch.setattr('chanlun.exchange.exchange_usmart.time.monotonic',lambda:500.)
    assert client.quote('kline',{},deadline=505.)=={'value':1}
    assert calls[0]['timeout']<=2.5
    with pytest.raises(TimeoutError):client.quote('kline',{},deadline=500.)
    assert len(calls)==1
    assert client.quote('kline',{})=={'value':1}
    assert calls[-1]['timeout']==client.timeout
