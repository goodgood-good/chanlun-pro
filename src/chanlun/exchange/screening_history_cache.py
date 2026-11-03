"""Shared completed US inputs with bounded intraday prefix reuse.

Only the explicit screening path opts in. Each newer cutoff refreshes an
overlap and the tail. A session change, any correction, a changed source
contract, or five minutes since the last full read requires a full history
verification. A failed refresh never returns the old frame as current.
"""
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path
import threading
import weakref

import pandas as pd

from chanlun.exchange._lookback import get_lookback_timedelta
from chanlun.persistence.file_lock import interprocess_file_lock
from chanlun.tools.log_util import LogUtil
from chanlun.exchange.errors import SourcePreparationPending

FULL_RECHECK_SECONDS = 300
META = '_screening_history_cache'
NAMESPACE = 'longbridge_screening_history'
_guard = threading.Lock()
_locks = weakref.WeakValueDictionary()


def _transport_failure(error):
    if isinstance(error, (TimeoutError, ConnectionError)):
        return True
    # Do not import the optional SDK merely to inspect its public error. The
    # observed socket-token Connect failure has no API/business error code.
    message = str(getattr(error, 'message', None) or error)
    return (type(error).__name__ == 'OpenApiException' and getattr(error, 'code', None) is None
            and 'error sending request for url' in message and 'client error (Connect)' in message)


def _refresh_pending(code, frequency, end, reason):
    identity = {'provider':'longbridge', 'code':code, 'frequency':frequency,
                'cutoff':int(end.timestamp()), 'reason':reason}
    token = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return SourcePreparationPending({'progress_token':token, 'retry_after':3, 'reason':reason},
                                    code='longbridge_history_refresh_pending')


@lru_cache(maxsize=1)
def source_revision():
    root=Path(__file__).parent
    digest=hashlib.sha256()
    for name in ('screening_history_cache.py','exchange_cq.py','_lookback.py','kline_completion.py',
                 'price_basis.py','kline_precision.py'):
        digest.update(name.encode());digest.update((root/name).read_bytes())
    return digest.hexdigest()


def _clean(frame):
    result=frame.copy()
    result.attrs={k:v for k,v in frame.attrs.items() if k!=META}
    return result


def _fingerprint(frame):
    digest=hashlib.sha256(pd.util.hash_pandas_object(frame,index=False).values.tobytes())
    digest.update(json.dumps(_clean(frame).attrs,sort_keys=True,default=str,allow_nan=False).encode())
    return digest.hexdigest()


def _completed(frame,frequency,end):
    if frame is None or frame.empty or frame.attrs.get('fetch_incomplete'):
        return None
    if (not {'date','open','high','low','close','volume'}.issubset(frame.columns)
            or not isinstance(frame.date.dtype,pd.DatetimeTZDtype) or frame.date.duplicated().any()
            or not frame.date.is_monotonic_increasing
            or frame.attrs.get('price_basis_provider')!='longbridge'
            or frame.attrs.get('price_basis_adjustment')!='forward'):
        return None
    # Longbridge emits opening labels. The screening adapter normalizes them
    # exactly once after this source cache returns its completed raw candles.
    value=_clean(frame.loc[frame.date+pd.Timedelta(minutes=int(frequency[:-1]))<=end])
    # Match the provider's existing timestamp contract across Parquet readers
    # (fixed-offset tz implementations otherwise compare as different dtypes).
    value['date']=value.date.dt.tz_convert('Asia/Shanghai')
    return (None if value.empty or value.date.iloc[-1]+pd.Timedelta(minutes=int(frequency[:-1]))!=end
            else value)


def _window(frame,start,end,frequency,meta=None):
    result=_clean(frame.loc[(frame.date>=start)
        & (frame.date+pd.Timedelta(minutes=int(frequency[:-1]))<=end)]).reset_index(drop=True)
    if meta:
        result.attrs[META]={key:meta[key] for key in ('full_checked_at','checked_at','through','mode')}
    return result


def load_screening_history(*,code,frequency,end,observed,loader,start=None,storage=None,force=False):
    end,observed=pd.Timestamp(end),pd.Timestamp(observed)
    start=end-get_lookback_timedelta(frequency,market='us') if start is None else pd.Timestamp(start)
    if end>observed or start>=end:
        return loader(None)
    def read(overlap):
        try:
            result = loader(overlap)
        except Exception as exc:
            # Capacity waits and cancellation already have a precise contract;
            # retain those exception types instead of reclassifying them.
            from chanlun.exchange.quote_capacity import QuoteConnectionBusy, QuoteConnectionCancelled
            from chanlun.exchange.longbridge_history_limit import LongbridgeHistoryBusy
            if isinstance(exc, (QuoteConnectionBusy, QuoteConnectionCancelled, LongbridgeHistoryBusy,
                                SourcePreparationPending)):
                raise
            if _transport_failure(exc):
                raise _refresh_pending(code, frequency, end, 'transport_unavailable') from exc
            raise
        if (getattr(result, 'attrs', {}) or {}).get('fetch_incomplete'):
            # Incomplete transport is not evidence that a verified old prefix
            # changed. Never escalate a failed tail into a same-call full read,
            # and never publish the old prefix as the requested current window.
            raise _refresh_pending(code, frequency, end, 'incomplete_response')
        return result
    if storage is None:
        from chanlun.persistence.file_db import fdb
        storage=fdb
    key=(hashlib.sha256(code.encode()).hexdigest()[:32],frequency)
    path=storage._kline_parquet_path(NAMESPACE,*key).with_suffix('.parquet.lock').resolve()
    with _guard:
        lock=_locks.get(path)
        if lock is None:
            lock=threading.RLock();_locks[path]=lock
    with lock,interprocess_file_lock(path):
        cached=storage.load_klines_parquet(NAMESPACE,*key)
        meta={}
        valid=False
        if cached is not None and not cached.empty:
            meta=cached.attrs.get(META) or {}
            if not isinstance(meta,dict):meta={}
            try:
                valid=(meta['schema']==1 and meta['code']==code and meta['frequency']==frequency
                    and meta['revision']==source_revision() and meta['fingerprint']==_fingerprint(cached)
                    and 0<=observed.timestamp()-meta['full_checked_at']<FULL_RECHECK_SECONDS
                    and meta['requested_start']<=start.timestamp()
                    and pd.Timestamp(meta['through'],unit='s',tz='UTC').tz_convert('America/New_York').date()
                        ==end.tz_convert('America/New_York').date())
            except (KeyError,ValueError,TypeError,OverflowError):
                valid=False
        previous_through=meta.get('through',0)
        if type(previous_through) not in (int,float) or not math.isfinite(previous_through):
            previous_through=0
        def invalidate():
            if storage.save_klines_parquet(NAMESPACE,*key,pd.DataFrame()) is not True:
                raise OSError('Cannot invalidate corrected screening history')
        if force and cached is not None and previous_through<=end.timestamp():
            invalidate()
        if valid and not force and meta['through']==end.timestamp():
            LogUtil.info(f'[screening_history] {code} {frequency} mode=shared cutoff={int(end.timestamp())}')
            return _window(cached,start,end,frequency,meta)
        value=None;mode='full';full_checked=observed.timestamp()
        if valid and not force and meta['through']<end.timestamp():
            overlap=cached.date.iloc[max(0,len(cached)-64)]
            tail=_completed(read(overlap),frequency,end)
            if tail is not None:
                old=_clean(cached.loc[cached.date>=overlap]).reset_index(drop=True)
                same=tail.loc[tail.date<=cached.date.iloc[-1]].reset_index(drop=True)
                if (old.attrs==same.attrs and list(old.columns)==list(same.columns)
                        and old.equals(same)):
                    appended=tail.loc[tail.date>cached.date.iloc[-1]]
                    value=pd.concat([_clean(cached),appended],ignore_index=True)
                    value.attrs=_clean(cached).attrs
                    mode='tail';full_checked=meta['full_checked_at']
                else:
                    invalidate()
        if value is None:
            raw=read(None)
            value=_completed(raw,frequency,end)
            if value is None:
                # Invalidate a rejected refresh so later callers cannot revive
                # the prefix that just failed its source verification.
                if cached is not None and previous_through<=end.timestamp():
                    storage.save_klines_parquet(NAMESPACE,*key,pd.DataFrame())
                return raw
        value=_window(value,start,end,frequency)
        if value.empty:
            return value
        current={'schema':1,'code':code,'frequency':frequency,'revision':source_revision(),
                 'requested_start':start.timestamp(),'full_checked_at':full_checked,
                 'checked_at':observed.timestamp(),'through':end.timestamp(),'mode':mode,
                 'fingerprint':_fingerprint(value)}
        value.attrs[META]=current
        # A historical caller must never replace a newer committed cutoff.
        if end.timestamp()>=previous_through:
            storage.save_klines_parquet(NAMESPACE,*key,value)
        LogUtil.info(f'[screening_history] {code} {frequency} mode={mode} rows={len(value)}')
        return _window(value,start,end,frequency,current)
