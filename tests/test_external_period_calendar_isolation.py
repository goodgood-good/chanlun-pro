"""An external period's clock failure stays local to that period."""
from datetime import datetime

import pytest

from chanlun.screening import runner, markets


@pytest.mark.parametrize('market,code',[('us','TSLA.US'),('hk','00700.HK'),('currency_spot','BTC/USDT')])
def test_external_calendar_failure_does_not_skip_other_requested_periods(tmp_path,monkeypatch,market,code):
    original=markets.market_context
    def contexts(market,code,observed,period,*args,**kwargs):
        if period=='1m':raise OSError('isolated short-period calendar read failure')
        return original(market,code,observed,period,*args,**kwargs)
    monkeypatch.setattr(markets,'market_context',contexts)
    attempted=[]
    monkeypatch.setattr(runner,'fetch_frame',lambda code,period,context:attempted.append(period))
    monkeypatch.setattr(runner,'frame_quality',lambda *_:{'errors':['NO_DATA']})
    result=runner._scan_worker_symbol({'market':market,'code':code,'name':'fixture'},
        {'frequencies':['1m','5m','30m'],'recent_sessions':5,'max_anchor_sessions':20},{},tmp_path,'test',
        {'observed_at':'2026-09-17T23:14:00+08:00'})
    assert attempted==['5m','30m']
    assert len(result['rows'])==3
    assert result['rows'][0]['data_errors']==['SESSION_CALENDAR_UNAVAILABLE']
    assert all(row['data_errors']==['NO_DATA'] for row in result['rows'][1:])


def test_external_holding_extra_period_does_not_block_the_selected_clock(tmp_path,monkeypatch):
    original=markets.market_context
    def contexts(market,code,observed,period,*args,**kwargs):
        if period=='1m':raise ValueError('isolated additional period unavailable')
        return original(market,code,observed,period,*args,**kwargs)
    monkeypatch.setattr(markets,'market_context',contexts)
    attempted=[]
    monkeypatch.setattr(runner,'fetch_frame',lambda code,period,context:attempted.append(period))
    monkeypatch.setattr(runner,'frame_quality',lambda *_:{'errors':['NO_DATA']})
    result=runner._scan_worker_symbol({'market':'us','code':'TSLA.US','name':'fixture',
                                      'holding':True,'_monitor_periods':['1m','5m']},
        {'frequencies':['5m'],'recent_sessions':5,'max_anchor_sessions':20},{},tmp_path,'test',
        {'observed_at':'2026-09-17T23:14:00+08:00'})
    assert attempted==['5m'] and len(result['rows'])==2
    assert result['rows'][0]['data_errors']==['SESSION_CALENDAR_UNAVAILABLE']


def test_missing_main_clock_cannot_authorize_a_lower_confirmation_clock(tmp_path,monkeypatch):
    calls=[]
    def missing(market,code,at,period,*args,**kwargs):
        calls.append(period)
        raise OSError('main clock unavailable')
    monkeypatch.setattr(markets,'market_context',missing)
    monkeypatch.setattr(runner,'fetch_frame',lambda *_:pytest.fail('unverified clocks cannot fetch confirmation input'))
    result=runner._scan_worker_symbol({'market':'us','code':'TSLA.US','name':'fixture'},
        {'strategy':runner.STRATEGY,'scope':'symbols','frequencies':['5m'],'recent_sessions':5,'max_anchor_sessions':20},
        {},tmp_path,'test',{'observed_at':'2026-09-17T23:14:00+08:00'})
    assert calls==['5m']
    assert result['rows'][0]['data_errors']==['SESSION_CALENDAR_UNAVAILABLE']


def test_lower_calendar_failure_still_uses_the_verified_main_cutoff(tmp_path,monkeypatch):
    observed=datetime.fromisoformat('2026-09-17T23:14:00+08:00')
    cutoff=int(datetime.fromisoformat('2026-09-17T23:10:00+08:00').timestamp())
    calls=[];captured={}
    def context(market,code,at,period,*args,**kwargs):
        calls.append((period,at))
        if period=='1m':raise ValueError('lower clock unavailable')
        return {'frequency':'5m','cutoff':cutoff}
    def scan(stock,settings,contexts,*args,**kwargs):
        captured.update(contexts)
        return {'code':stock['code'],'market':stock['market'],'rows':[]}
    monkeypatch.setattr(markets,'market_context',context)
    monkeypatch.setattr(runner,'scan_symbol',scan)
    runner._scan_worker_symbol({'market':'us','code':'TSLA.US','name':'fixture'},
        {'strategy':runner.STRATEGY,'scope':'symbols','frequencies':['5m'],'recent_sessions':5,'max_anchor_sessions':20},
        {},tmp_path,'test',{'observed_at':observed.isoformat()})
    assert calls[0]==('5m',observed) and int(calls[1][1].timestamp())==cutoff
    assert captured['5m']['cutoff']==cutoff and captured['1m']['calendar_error']
