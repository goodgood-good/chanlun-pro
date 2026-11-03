import pandas as pd
import pytest

from chanlun.exchange import screening_history_cache as cache
from chanlun.persistence.file_db import FileCacheDB
from chanlun.exchange.errors import SourcePreparationPending


@pytest.fixture
def source(tmp_path):
    storage=object.__new__(FileCacheDB);storage.klines_path=tmp_path
    end=pd.Timestamp('2026-10-14T12:00:00-04:00')
    frame=pd.DataFrame({'date':pd.date_range(end=end-pd.Timedelta(minutes=1),periods=100,freq='min'),
        'code':'QQQ.US','open':10.,'high':11.,'low':9.,'close':10.,'volume':100.})
    frame.attrs={'price_basis_provider':'longbridge','price_basis_adjustment':'forward',
                 'price_basis_revision':'fixture','structure_price_quantum':'0.01'}
    calls=[]
    def read(at=end,observed=None,force=False,loader=None):
        def fetch(overlap):
            calls.append(overlap)
            result=frame.loc[frame.date<at].copy()
            return result if overlap is None else result.loc[result.date>=overlap].copy()
        return cache.load_screening_history(code='QQQ.US',frequency='1m',end=at,
            observed=at+pd.Timedelta(seconds=5) if observed is None else observed,
            loader=loader or fetch,storage=storage,force=force)
    return storage,frame,end,calls,read


def append(frame,at,**values):
    row={**frame.iloc[-1].to_dict(),'date':at,**values}
    frame.loc[len(frame)]=row


def test_completed_input_is_shared_across_reader_restarts(source):
    _,frame,end,calls,read=source
    first=read();cache._locks.clear()
    assert read().equals(first) and len(calls)==1
    assert first.date.iloc[-1]+pd.Timedelta(minutes=1)==end


def test_next_cutoff_refreshes_only_overlap_and_keeps_full_validation_age(source):
    _,frame,end,calls,read=source
    first=read();append(frame,end)
    second=read(end+pd.Timedelta(minutes=1))
    assert len(second)==len(first)+1 and calls==[None,first.date.iloc[-64]]
    assert second.attrs[cache.META]['full_checked_at']==first.attrs[cache.META]['full_checked_at']
    assert second.attrs[cache.META]['mode']=='tail'
    # A fresh tail cannot postpone the five-minute full-history deadline.
    read(end+pd.Timedelta(minutes=1),observed=end+pd.Timedelta(minutes=6))
    assert calls[-1] is None


@pytest.mark.parametrize('field,value',[('close',10.5),('volume',101.)])
def test_any_overlap_correction_requires_a_full_rebuild(source,field,value):
    _,frame,end,calls,read=source
    read();frame.loc[len(frame)-3,field]=value;append(frame,end)
    rebuilt=read(end+pd.Timedelta(minutes=1))
    assert calls[0] is None and calls[1] is not None and calls[2] is None
    assert rebuilt.attrs[cache.META]['mode']=='full'
    assert rebuilt.loc[rebuilt.date==frame.date.iloc[-4],field].iloc[0]==value


def test_missing_current_candle_never_advances_the_cached_cutoff(source):
    _,frame,end,calls,read=source
    read();late=read(end+pd.Timedelta(minutes=1))
    assert late.date.iloc[-1]+pd.Timedelta(minutes=1)==end
    append(frame,end)
    fresh=read(end+pd.Timedelta(minutes=1))
    assert fresh.date.iloc[-1]+pd.Timedelta(minutes=1)==end+pd.Timedelta(minutes=1)


def test_price_basis_change_and_session_change_do_not_merge_old_prices(source):
    _,frame,end,calls,read=source
    read();frame.attrs['price_basis_revision']='changed';append(frame,end)
    assert read(end+pd.Timedelta(minutes=1)).attrs['price_basis_revision']=='changed'
    assert calls[-1] is None
    # Even a clock only one minute later on another local session cannot reuse.
    frame['date']+=pd.Timedelta(days=1)
    read(end+pd.Timedelta(days=1,minutes=1))
    assert calls[-1] is None


def test_old_query_does_not_replace_a_newer_cutoff(source):
    storage,frame,end,calls,read=source
    read();append(frame,end);read(end+pd.Timedelta(minutes=1))
    read(end)
    previous=len(calls)
    read(end+pd.Timedelta(minutes=1))
    assert len(calls)==previous


def test_failed_forced_refresh_cannot_resurrect_an_old_cached_frame(source):
    _,frame,end,calls,read=source
    read()
    empty=pd.DataFrame();empty.attrs['fetch_incomplete']=True
    with pytest.raises(SourcePreparationPending):
        read(force=True,loader=lambda _:empty)
    prior=len(calls);read()
    assert len(calls)==prior+1


@pytest.mark.parametrize('failure', ['incomplete', 'timeout', 'connect'])
def test_failed_tail_preserves_prefix_without_escalation_and_a_later_retry_can_finish(source,failure):
    storage,frame,end,_,read=source
    first=read();path=next(storage.klines_path.rglob('*.parquet'));before=path.read_bytes()
    append(frame,end)
    calls=[]
    class OpenApiException(RuntimeError):
        code=None
    def unavailable(overlap):
        calls.append(overlap)
        assert overlap is not None, 'transport failure must not trigger a full-history retry'
        if failure=='timeout':raise TimeoutError('unavailable')
        if failure=='connect':
            raise OpenApiException('error sending request for url (https://example.invalid/v1/socket/token): client error (Connect)')
        result=pd.DataFrame();result.attrs['fetch_incomplete']=True
        return result
    with pytest.raises(SourcePreparationPending) as caught:
        read(end+pd.Timedelta(minutes=1),loader=unavailable)
    assert caught.value.code=='longbridge_history_refresh_pending'
    assert len(calls)==1 and path.read_bytes()==before
    actual=read(end+pd.Timedelta(minutes=1))
    assert actual.date.iloc[-1]+pd.Timedelta(minutes=1)==end+pd.Timedelta(minutes=1)
    assert actual.attrs[cache.META]['mode']=='tail'
    assert actual.attrs[cache.META]['full_checked_at']==first.attrs[cache.META]['full_checked_at']


def test_cancelled_tail_keeps_cancellation_identity_and_no_full_retry(source):
    from chanlun.exchange.quote_capacity import QuoteConnectionCancelled
    storage,frame,end,_,read=source
    read();path=next(storage.klines_path.rglob('*.parquet'));before=path.read_bytes();append(frame,end)
    error=QuoteConnectionCancelled('request cancelled');calls=[]
    def cancelled(overlap):
        calls.append(overlap)
        raise error
    with pytest.raises(QuoteConnectionCancelled) as caught:
        read(end+pd.Timedelta(minutes=1),loader=cancelled)
    assert caught.value is error and len(calls)==1 and calls[0] is not None
    assert path.read_bytes()==before


def test_failed_full_recheck_preserves_bytes_but_cannot_return_them_as_current(source):
    storage,frame,end,_,read=source
    read();path=next(storage.klines_path.rglob('*.parquet'));before=path.read_bytes();calls=[]
    def unavailable(overlap):
        calls.append(overlap)
        result=pd.DataFrame();result.attrs['fetch_incomplete']=True
        return result
    with pytest.raises(SourcePreparationPending):
        read(observed=end+pd.Timedelta(minutes=6),loader=unavailable)
    assert calls==[None] and path.read_bytes()==before
    next_calls=[]
    def restored(overlap):
        next_calls.append(overlap);return frame.copy()
    result=read(observed=end+pd.Timedelta(minutes=6),loader=restored)
    assert next_calls==[None] and result.attrs[cache.META]['mode']=='full'


def test_business_failure_is_not_misclassified_as_transport_pending(source):
    _,frame,end,_,read=source
    read();append(frame,end)
    class OpenApiException(RuntimeError):
        code=301607
    error=OpenApiException('monthly quota exhausted')
    def denied(_):raise error
    with pytest.raises(OpenApiException) as caught:
        read(end+pd.Timedelta(minutes=1),loader=denied)
    assert caught.value is error


def test_real_correction_followed_by_transport_failure_cannot_restore_the_rejected_prefix(source):
    _,frame,end,default_calls,read=source
    read();frame.loc[len(frame)-3,'close']=10.5;append(frame,end)
    calls=[]
    def corrected_then_unavailable(overlap):
        calls.append(overlap)
        if overlap is None:
            incomplete=pd.DataFrame();incomplete.attrs['fetch_incomplete']=True
            return incomplete
        return frame.loc[frame.date>=overlap].copy()
    with pytest.raises(SourcePreparationPending):
        read(end+pd.Timedelta(minutes=1),loader=corrected_then_unavailable)
    assert len(calls)==2 and calls[0] is not None and calls[1] is None
    previous=len(default_calls)
    result=read(end+pd.Timedelta(minutes=1))
    assert default_calls[previous:]==[None]
    assert result.attrs[cache.META]['mode']=='full'
    assert result.loc[result.date==frame.date.iloc[-4],'close'].iloc[0]==10.5


def test_corrupt_persisted_content_and_source_change_are_revalidated(source,monkeypatch):
    storage,frame,end,calls,read=source
    read();path=next(storage.klines_path.rglob('*.parquet'))
    corrupt=pd.read_parquet(path);corrupt.loc[0,'close']=123;corrupt.to_parquet(path,index=False)
    assert read().close.iloc[0]==10 and len(calls)==2
    monkeypatch.setattr(cache,'source_revision',lambda:'updated')
    read();assert len(calls)==3


def _child_shared_read(root,messages):
    from pathlib import Path
    storage=object.__new__(FileCacheDB);storage.klines_path=Path(root)
    end=pd.Timestamp('2026-10-14T12:00:00-04:00')
    def forbidden(_):
        raise AssertionError('The second process must consume the committed shared input')
    messages.put('started')
    try:
        value=cache.load_screening_history(code='QQQ.US',frequency='1m',end=end,
            observed=end+pd.Timedelta(seconds=5),loader=forbidden,storage=storage)
        messages.put(('complete',len(value)))
    except Exception as exc:
        messages.put(('error',str(exc)))


def test_concurrent_process_waits_for_the_single_complete_publication(source):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    import multiprocessing
    from queue import Empty
    storage,frame,end,_,read=source
    entered,release=Event(),Event()
    def loader(_):
        entered.set()
        assert release.wait(20)
        return frame.copy()
    ctx=multiprocessing.get_context('spawn');messages=ctx.Queue()
    child=ctx.Process(target=_child_shared_read,args=(str(storage.klines_path),messages))
    with ThreadPoolExecutor(max_workers=1) as pool:
        first=pool.submit(read,loader=loader)
        try:
            assert entered.wait(2)
            child.start()
            assert messages.get(timeout=15)=='started'
            with pytest.raises(Empty):messages.get(timeout=.15)
            release.set()
            assert len(first.result(timeout=5))==100
            assert messages.get(timeout=10)==('complete',100)
            child.join(5)
            assert child.exitcode==0
        finally:
            release.set()
            if child.pid and child.is_alive():child.terminate();child.join(5)
            messages.close()
