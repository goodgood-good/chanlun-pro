"""Bounded native Longbridge pages for frozen notification display context."""
from functools import lru_cache
import threading
import time

import pandas as pd

from .notification_history import HistoryContractError, _digest, BASIS, VALUES


class LongbridgePageSource:
    provider = 'longbridge'

    def __init__(self, *, exchange_factory=None, monotonic=time.monotonic):
        self.identity = _digest({'provider':'longbridge','contract':'completed-native-pages-v1'})
        self.factory, self.monotonic = exchange_factory, monotonic
        self.exchange = None
        self.lock = threading.Lock()

    def __call__(self, *, code, frequency, cursor, right, remaining):
        from chanlun.exchange.exchange_cq import (
            ExchangeChangQiao, Period, AdjustType, TradeSessions,
            _candlestick_frame, normalize_history_frame, _is_connection_limit, is_history_retention_boundary,
        )
        from chanlun.exchange.kline_completion import drop_unclosed_last_bar, normalize_completed_bar_labels
        if right != 1 or frequency not in {'1m','5m','30m'}:
            raise HistoryContractError('unsupported_adjustment')
        deadline = self.monotonic()+remaining
        if not self.lock.acquire(timeout=max(0,remaining)):
            raise TimeoutError('Longbridge history page busy')
        try:
            if self.exchange is None:
                self.exchange = (self.factory or ExchangeChangQiao)()
            payload = {'provider':self.provider,'code':code,'frequency':frequency,'right':right,'rows':[]}
            try:
                budget = deadline-self.monotonic()
                if budget <= 0:
                    raise TimeoutError('Longbridge history deadline')
                candles = self.exchange._fetch_candlesticks_api(code,
                    {'1m':Period.Min_1,'5m':Period.Min_5,'30m':Period.Min_30}[frequency],
                    AdjustType.ForwardAdjust,1000,cursor.to_pydatetime(),TradeSessions.Intraday,
                    priority='prewarm',timeout_seconds=budget)
            except Exception as exc:
                if is_history_retention_boundary(exc):
                    payload['source_boundary'] = 'provider_retention_limit'
                    return payload
                if _is_connection_limit(exc):
                    raise self.exchange._remember_connection_limit(exc) from exc
                raise
            if not candles:
                payload['source_boundary'] = 'provider_empty_before_cursor'
                return payload
            frame = _candlestick_frame(candles)
            if len(candles)>1000 or len(frame)!=len(candles) or frame.date.isna().any() or frame.date.max()>cursor:
                raise HistoryContractError('invalid_longbridge_page')
            oldest_open = frame.date.min()
            frame = normalize_history_frame(frame,code,frequency,'us')
            frame = drop_unclosed_last_bar(frame,frequency,time_label='start',as_of=cursor)
            frame = normalize_completed_bar_labels(frame,frequency,time_label='start')
            if frame.empty:
                # The SDK cursor is an opening label. A response can contain
                # only the already-read boundary candle. Walk past its opening
                # in another bounded page; this is not proof of source origin.
                payload.update(source_boundary='opening_overlap_only',
                               next_cursor=(oldest_open-pd.Timedelta(milliseconds=1)).isoformat())
                return payload
            frame.attrs['bar_time_label']='end'
            payload['basis'] = {key:frame.attrs.get(key) for key in (*BASIS,'bar_time_label')}
            payload['rows'] = [{**{key:row[key] for key in VALUES},'time':int(row['date'].timestamp())}
                               for row in frame.to_dict('records')]
            return payload
        finally:
            self.lock.release()


def decode_page(data, *, code, frequency, cursor, right):
    if (not isinstance(data,dict) or data.get('provider')!='longbridge' or data.get('code')!=code
            or data.get('frequency')!=frequency or data.get('right')!=right or not isinstance(data.get('rows'),list)):
        raise HistoryContractError('response_identity_conflict')
    if not data['rows']:
        boundary = data.get('source_boundary')
        if boundary == 'opening_overlap_only':
            try:
                before = pd.Timestamp(data['next_cursor'])
                if before.tzinfo is None or not before < cursor:
                    raise ValueError('cursor did not advance')
            except (KeyError,TypeError,ValueError) as exc:
                raise HistoryContractError('page_ignored_cursor') from exc
        elif boundary not in {'provider_retention_limit','provider_empty_before_cursor'}:
            raise HistoryContractError('unverified_source_boundary')
        return None
    try:
        frame=pd.DataFrame(data['rows'])
        frame['date']=pd.to_datetime(frame.pop('time'),unit='s',utc=True).dt.tz_convert('Asia/Shanghai')
        frame['code'],frame['frequency']=code,frequency
        frame=frame[['date','frequency','code',*VALUES]].sort_values('date').reset_index(drop=True)
        frame.attrs=dict(data['basis'])
    except (KeyError,TypeError,ValueError) as exc:
        raise HistoryContractError('invalid_longbridge_page') from exc
    return frame


@lru_cache(maxsize=1)
def default_page_source():
    return LongbridgePageSource()
