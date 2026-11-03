from types import SimpleNamespace

import pandas as pd

from chanlun.screening import markets


def test_external_interval_extension_reaches_provider_without_default_horizon_clamp(monkeypatch):
    at=pd.Timestamp('2026-10-14T10:00:00-04:00');calls=[]
    def klines(code,frequency,**kwargs):
        calls.append(kwargs)
        return pd.DataFrame({'date':[at],'close':[10.]})
    monkeypatch.setattr('chanlun.exchange.get_exchange',lambda _:SimpleNamespace(kline_time_label='end',klines=klines))
    context={'market':'us','cutoff':int(at.timestamp()),'history_from':'2026-06-01',
             'session_timezone':'America/New_York','session_periods':[(int(at.timestamp())-1800,int(at.timestamp()))]}
    assert len(markets.external_frame('QQQ.US','1m',context))==1
    assert pd.Timestamp(calls[0]['start_date'])==pd.Timestamp('2026-06-01T00:00:00-04:00')
    assert calls[0]['args']=={'timeout':20,'allow_long_history':True,'_screening_completed':True}
    markets.external_frame('QQQ.US','1m',{**context,'history_from':'2026-09-01'})
    assert 'start_date' not in calls[1] and calls[1]['args']=={'timeout':20,'_screening_completed':True}


def test_default_external_context_uses_same_minimum_session_window_as_a_shares():
    at=pd.Timestamp('2026-10-14T22:00:00+08:00').to_pydatetime()
    context=markets.market_context('us','QQQ.US',at,'1m')
    assert context['history_from']==context['trading_days'][-25]
    extended=markets.market_context('us','QQQ.US',at,'1m',max_anchor_sessions=80)
    assert extended['history_from']==extended['trading_days'][-82]
