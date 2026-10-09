"""Verified tail reuse must agree with a cold provider window."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
import multiprocessing
import time

import numpy as np
import pandas as pd
import pytest

from chanlun.exchange.errors import USmartAPIError
from chanlun.exchange.usmart_history_cache import USmartHistoryCache,FETCH,META
from chanlun.screening.cache import input_fingerprint


def prices(end, count=100):
    date=pd.date_range(end=pd.Timestamp(end).tz_convert('America/New_York'),periods=count,freq='min')
    values=np.arange(count,dtype=float)/100+100
    frame=pd.DataFrame({'date':date,'frequency':'1m','code':'MSFT.US','open':values,
                        'high':values+1,'low':values-1,'close':values+.5,'volume':100.0})
    frame.attrs.update(price_basis_provider='usmart',price_basis_adjustment='forward',
                       price_basis_revision='fixture-basis',structure_price_quantum='0.001')
    return frame


@pytest.fixture
def history(tmp_path):
    end=pd.Timestamp('2026-10-09T12:00:00-04:00')
    value=SimpleNamespace(end=end,clock=end.timestamp()+.1,frame=prices(end),calls=[],fail=False)
    value.cache=USmartHistoryCache(tmp_path,clock=lambda:value.clock)
    def loader(start):
        value.calls.append(start)
        if value.fail:
            raise USmartAPIError('upstream unavailable',status_code=503)
        frame=value.frame.tail(100 if start is None else 20).copy(deep=True)
        frame.attrs[FETCH]={'page_rows':20,'pages':5 if start is None else 1}
        return frame
    value.loader=loader
    def read(**kwargs):
        return value.cache.read(code='MSFT.US',frequency='1m',end=value.end,
            host='https://quote.example',channel='fixture',right=1,count=20,pages=5,
            loader=kwargs.pop('loader',value.loader),**kwargs)
    value.read=read
    def advance(seconds=60):
        value.end+=pd.Timedelta(seconds=seconds)
        value.clock+=seconds
        if seconds>=60:
            added=prices(value.end,1)
            # Preserve the exact previously observed prefix.
            added.loc[:,['open','high','low','close']]+=2
            value.frame=pd.concat([value.frame,added],ignore_index=True)
    value.advance=advance
    return value


def test_new_closed_bar_uses_tail_and_matches_cold_frame_and_fingerprint(history):
    first=history.read()
    history.advance()
    updated=history.read()
    cold=history.frame.tail(100).reset_index(drop=True)
    pd.testing.assert_frame_equal(updated,cold)
    assert input_fingerprint(updated,'MSFT.US','1m')==input_fingerprint(cold,'MSFT.US','1m')
    assert history.calls[0] is None and history.calls[1] is not None
    assert len(history.calls)==2
    assert META not in updated.attrs and FETCH not in updated.attrs
    assert len(first)==len(updated)==100


def test_same_cutoff_is_coalesced_across_new_cache_instances(history,tmp_path):
    first=history.read()
    history.cache=USmartHistoryCache(tmp_path,clock=lambda:history.clock)
    pd.testing.assert_frame_equal(history.read(),first)
    assert history.calls==[None]


@pytest.mark.parametrize('fault',['revised_close','revised_volume','missing_overlap','changed_basis','invalid_ohlc'])
def test_revisions_and_invalid_overlap_force_complete_revalidation(history,fault):
    history.read();history.advance()
    if fault=='revised_close':history.frame.loc[len(history.frame)-8,'close']+=.1
    elif fault=='revised_volume':history.frame.loc[len(history.frame)-8,'volume']+=10
    elif fault=='missing_overlap':history.frame=history.frame.drop(history.frame.index[-8]).reset_index(drop=True)
    elif fault=='changed_basis':history.frame.attrs['price_basis_revision']='new-adjustment'
    else:history.frame.loc[len(history.frame)-8,'low']=1000
    result=history.read()
    assert history.calls[0] is None and history.calls[1] is not None and history.calls[2] is None
    pd.testing.assert_frame_equal(result,history.frame.tail(100).reset_index(drop=True))


def test_quarter_hour_validation_catches_older_revision_outside_tail(history):
    history.read()
    history.frame.loc[0,'close']+=.1
    history.advance(seconds=900)
    result=history.read()
    assert history.calls==[None,None]
    pd.testing.assert_frame_equal(result,history.frame.tail(100).reset_index(drop=True))


def test_failed_tail_does_not_return_stale_data_or_replace_good_cache(history,tmp_path):
    history.read();path=next(tmp_path.glob('*.parquet'));before=path.read_bytes()
    history.advance();history.fail=True
    with pytest.raises(USmartAPIError):history.read()
    assert path.read_bytes()==before


def test_failed_force_invalidates_previous_source_until_full_success(history):
    history.read();history.advance();history.fail=True
    with pytest.raises(USmartAPIError):history.read(force=True)
    history.fail=False
    history.read()
    assert history.calls==[None,None,None]


def test_damaged_cache_and_clock_rollback_do_not_reuse(history,tmp_path):
    history.read();path=next(tmp_path.glob('*.parquet'))
    frame=pd.read_parquet(path);frame.loc[0,'close']+=.1;frame.to_parquet(path,index=False)
    history.read()
    assert history.calls==[None,None]
    history.clock-=.05
    history.read()
    assert history.calls==[None,None,None]


def test_damaged_cache_window_metadata_cannot_shrink_the_history(history,tmp_path):
    history.read();path=next(tmp_path.glob('*.parquet'))
    frame=pd.read_parquet(path);frame.attrs[META]['row_limit']=0;frame.to_parquet(path,index=False)
    history.advance()
    result=history.read()
    assert len(result)==100 and history.calls==[None,None]


def test_historical_request_does_not_overwrite_newer_cache(history,tmp_path):
    history.read();history.advance();history.read()
    path=next(tmp_path.glob('*.parquet'));before=path.read_bytes()
    history.end-=pd.Timedelta(minutes=1)
    history.read()
    assert history.calls[-1] is None
    assert path.read_bytes()==before


def test_phase_change_forces_full_history_even_within_validation_deadline(history):
    history.end=pd.Timestamp('2026-10-09T09:19:00-04:00')
    history.clock=history.end.timestamp()+.1;history.frame=prices(history.end)
    history.read();history.advance()
    history.read()
    assert history.calls==[None,None]


def test_concurrent_consumers_coalesce_full_history_download(history):
    entered,release=Event(),Event()
    def loader(start):
        entered.set();assert release.wait(3)
        return history.loader(start)
    with ThreadPoolExecutor(2) as pool:
        first=pool.submit(history.read,loader=loader)
        assert entered.wait(1)
        second=pool.submit(history.read)
        release.set()
        pd.testing.assert_frame_equal(first.result(),second.result())
    assert history.calls==[None]


def test_invalid_source_is_never_cached(history,tmp_path):
    history.frame.loc[10,'low']=1000
    history.read();history.read()
    assert history.calls==[None,None]
    assert not list(tmp_path.glob('*.parquet'))


def test_capped_window_does_not_gain_an_extra_bar_when_provider_returns_a_future_bar(history):
    def loader(start):
        value=history.loader(start)
        if start is None:value=value.tail(99).reset_index(drop=True)
        value.attrs[FETCH]={**value.attrs[FETCH],'future_rows':1,'window_capped':True}
        return value
    first=history.read(loader=loader);assert len(first)==99
    history.advance()
    second=history.read(loader=loader)
    assert len(second)==99
    pd.testing.assert_frame_equal(second,history.frame.tail(99).reset_index(drop=True))
    assert history.calls[-1] is not None


def test_adapter_reuses_bounded_history_and_fetches_only_one_overlap_page(tmp_path,monkeypatch):
    from chanlun import config
    from chanlun.exchange.exchange_usmart import ExchangeUSmart,_timestamp_to_api_time,_parse_api_time
    import pytz
    monkeypatch.setattr(config,'US_HISTORY_KLINE_SOURCE','usmart')
    end=pd.Timestamp('2026-10-09T12:00:00-04:00')
    source={'frame':prices(end,100),'clock':end.timestamp()+.1}
    calls=[]
    def quote(endpoint,payload):
        assert endpoint=='kline'
        calls.append(dict(payload))
        cursor=_parse_api_time(payload['start'],pytz.timezone('America/New_York'))
        frame=source['frame']
        page=frame.loc[frame.date<=cursor].tail(20)
        return {'list':[{'latestTime':_timestamp_to_api_time(row.date),
                        **{name:getattr(row,name) for name in ('open','high','low','close','volume')}}
                       for row in page.itertuples(index=False)]}
    client=SimpleNamespace(quote=quote,quote_host='https://quote.example',channel='fixture')
    adapter=ExchangeUSmart('us',client=client,
                           history_cache=USmartHistoryCache(tmp_path,clock=lambda:source['clock']))
    options={'count':100,'pages':3}
    first=adapter.klines('MSFT.US','1m',end_date=end.isoformat(),args=options)
    assert len(first)==60 and len(calls)==3
    end+=pd.Timedelta(minutes=1);source['clock']+=60
    added=prices(end,1)
    source['frame']=pd.concat([source['frame'],added],ignore_index=True)
    cached=adapter.klines('MSFT.US','1m',end_date=end.isoformat(),args=options)
    assert len(calls)==4 and len(cached)==60
    cold=ExchangeUSmart('us',client=client).klines('MSFT.US','1m',end_date=end.isoformat(),args=options)
    assert len(calls)==7
    pd.testing.assert_frame_equal(cached,cold)
    assert input_fingerprint(cached,'MSFT.US','1m')==input_fingerprint(cold,'MSFT.US','1m')


def test_explicit_history_query_keeps_its_range_and_bypasses_source_cache(tmp_path,monkeypatch):
    from chanlun import config
    from chanlun.exchange.exchange_usmart import ExchangeUSmart
    monkeypatch.setattr(config,'US_HISTORY_KLINE_SOURCE','usmart')
    cache=SimpleNamespace(read=lambda **_:pytest.fail('explicit historical ranges cannot reuse the live source cache'))
    adapter=ExchangeUSmart('us',client=SimpleNamespace(quote=lambda *_:{'list':[]}),history_cache=cache)
    assert adapter.klines('MSFT.US','1m',start_date='2025-01-01',end_date='2025-01-02').empty


@pytest.mark.parametrize('fail',[False,True])
def test_older_forced_query_preserves_newer_shared_snapshot(history,tmp_path,fail):
    history.read();history.advance();history.read()
    path=next(tmp_path.glob('*.parquet'));before=path.read_bytes()
    history.end-=pd.Timedelta(minutes=1);history.fail=fail
    if fail:
        with pytest.raises(USmartAPIError):history.read(force=True)
    else:
        history.read(force=True)
    assert path.read_bytes()==before


@pytest.mark.parametrize('flag',['invalid_rows','stalled'])
def test_incomplete_full_response_cannot_be_returned_as_usable_history(history,tmp_path,flag):
    def incomplete(start):
        frame=history.loader(start)
        frame.attrs[FETCH][flag]=1
        return frame
    with pytest.raises(USmartAPIError,match='分页未完整'):
        history.read(loader=incomplete)
    assert not list(tmp_path.glob('*.parquet'))


def test_explicit_range_also_rejects_malformed_source_rows(monkeypatch):
    from chanlun import config
    from chanlun.exchange.exchange_usmart import ExchangeUSmart
    monkeypatch.setattr(config,'US_HISTORY_KLINE_SOURCE','usmart')
    client=SimpleNamespace(quote=lambda *_:{'list':[{'latestTime':'invalid','open':10,'high':11,'low':9,'close':10}]})
    # No usable row at all must also preserve the malformed-source diagnosis.
    with pytest.raises(USmartAPIError):
        ExchangeUSmart('us',client=client).klines('MSFT.US','1m',start_date='2026-10-08',end_date='2026-10-09')


def test_same_source_thread_wait_has_a_real_deadline(tmp_path):
    import threading
    from chanlun.exchange.usmart_history_cache import _flight
    from chanlun.exchange.errors import USmartHistoryPending
    lock=threading.RLock();entered=Event();release=Event()
    def owner():
        with lock:
            entered.set();release.wait(2)
    with ThreadPoolExecutor(1) as pool:
        job=pool.submit(owner);assert entered.wait(1)
        before=time.monotonic()
        with pytest.raises(USmartHistoryPending):
            with _flight(lock,tmp_path/'history.parquet',.03):
                pytest.fail('a blocked source cannot pretend to acquire the transaction')
        assert time.monotonic()-before<.5
        release.set();job.result()


def _history_process(root,barrier,queue):
    end=pd.Timestamp('2026-10-09T12:00:00-04:00')
    cache=USmartHistoryCache(root,clock=lambda:end.timestamp()+.1)
    def loader(start):
        queue.put('fetch');time.sleep(.2)
        result=prices(end);result.attrs[FETCH]={'page_rows':20,'pages':5}
        return result
    barrier.wait(15)
    frame=cache.read(code='MSFT.US',frequency='1m',end=end,host='https://quote.example',
                     channel='fixture',right=1,count=20,pages=5,loader=loader)
    queue.put(('done',len(frame)))


def test_independent_processes_share_one_source_download(tmp_path):
    context=multiprocessing.get_context('spawn');barrier=context.Barrier(2);queue=context.Queue()
    processes=[context.Process(target=_history_process,args=(str(tmp_path),barrier,queue)) for _ in range(2)]
    try:
        for process in processes:process.start()
        messages=[]
        while sum(isinstance(row,tuple) for row in messages)<2:
            messages.append(queue.get(timeout=20))
        for process in processes:
            process.join(10);assert process.exitcode==0
        assert messages.count('fetch')==1
        assert [row for row in messages if isinstance(row,tuple)]==[('done',100),('done',100)]
    finally:
        for process in processes:
            if process.is_alive():process.terminate()
            process.join(2)
        queue.close()
