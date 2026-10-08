"""A missing A-share calendar must not block independent markets."""
from datetime import datetime
import json

import pytest

from chanlun.screening import runner, markets
from cl_app.services.screening import validate_settings


@pytest.fixture
def isolated_run(tmp_path,monkeypatch):
    real_scan=runner.scan_symbol
    def calendar_failure(*_args,**_kwargs):
        raise OSError('isolated A-share calendar is unavailable')
    monkeypatch.setattr(runner,'trading_context',calendar_failure)
    def calculate(stock,settings,contexts,*args,**kwargs):
        if stock.get('market','a')=='a':
            return real_scan(stock,settings,contexts,*args,**kwargs)
        return {**stock,'rows':[{'code':stock['code'],'market':stock['market'],'name':stock['name'],
                                'frequency':frequency,'source_closed_at':contexts[frequency]['cutoff'],
                                'selected':[],'data_errors':[]} for frequency in settings['frequencies']]}
    monkeypatch.setattr(runner,'scan_symbol',calculate)
    monkeypatch.setattr(runner,'fetch_frame',lambda *_args,**_kwargs:pytest.fail('Unavailable A calendar must not fetch prices'))
    def run(stocks):
        settings=validate_settings({'scope':'symbols','symbols':stocks,'frequencies':['5m']})
        request={'run_id':tmp_path.name,'settings':settings,'observed_at':'2026-09-17T23:14:00+08:00',
                 'holding_snapshot':{'schema':'monitor-holdings-v1','keys':[]}}
        runner.write_json(tmp_path/'request.json',request)
        def worker(items,options,contexts,directory,_workers,revision):
            for stock in items:
                yield runner._scan_worker_symbol(stock,options,contexts,directory,revision,request),[]
        monkeypatch.setattr(runner,'bounded_scan',worker)
        runner.run_screening(tmp_path)
        state=json.loads((tmp_path/'status.json').read_text(encoding='utf-8'))
        path=tmp_path/'results.jsonl'
        rows=[json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()] if path.is_file() else []
        return state,rows
    return run


@pytest.mark.parametrize('market,code',[('us','TSLA.US'),('hk','00700.HK'),('currency_spot','BTC/USDT')])
def test_foreign_only_scan_needs_only_its_own_calendar(isolated_run,market,code):
    state,rows=isolated_run([{'market':market,'code':code,'name':'fixture'}])
    assert state['status']=='completed' and state['successful_period_count']==1
    expected=markets.market_context(market,code,datetime.fromisoformat('2026-09-17T23:14:00+08:00'),'5m')['cutoff']
    assert rows[0]['rows'][0]['source_closed_at']==expected
    assert state['market_cutoffs'][market]['5m']==expected
    assert state['cutoffs']=={} and state['calendar_source'] is None


def test_mixed_scan_records_a_calendar_failure_and_finishes_other_market(isolated_run):
    state,rows=isolated_run([{'market':'a','code':'SH.600000','name':'A fixture'},
                             {'market':'us','code':'TSLA.US','name':'US fixture'}])
    assert state['status']=='completed' and state['completed']==2
    assert state['error_count']==1 and state['successful_period_count']==1
    by_market={row['market']:row for row in rows}
    assert by_market['a']['rows'][0]['data_errors']==['SESSION_CALENDAR_UNAVAILABLE']
    assert by_market['us']['rows'][0]['data_errors']==[]
    assert state['market_cutoffs']['a']['5m'] is None


def test_a_only_failure_stays_visible_without_a_fabricated_cutoff(isolated_run):
    state,rows=isolated_run([{'market':'a','code':'SH.600000','name':'fixture'}])
    assert state['status']=='completed' and state['successful_period_count']==0
    assert state['error_count']==1 and state['market_cutoffs']['a']['5m'] is None
    assert rows[0]['rows'][0]['data_errors']==['SESSION_CALENDAR_UNAVAILABLE']
    assert state['cutoffs']=={}


def test_one_missing_period_does_not_prevent_fetching_another_period(tmp_path,monkeypatch):
    calls=[]
    monkeypatch.setattr(runner,'fetch_frame',lambda code,period,context:calls.append(period))
    monkeypatch.setattr(runner,'frame_quality',lambda *_:{'errors':['NO_DATA']})
    result=runner.scan_symbol({'market':'a','code':'SH.600000','name':'fixture'},
        {'frequencies':['1m','5m']},{'1m':{'calendar_error':'missing 1m clock'},'5m':{'cutoff':100}},tmp_path)
    assert calls==['5m']
    assert result['rows'][0]['data_errors']==['SESSION_CALENDAR_UNAVAILABLE']
    assert result['rows'][1]['data_errors']==['NO_DATA']


def test_missing_additional_holding_period_does_not_block_selected_period(tmp_path,monkeypatch):
    calls=[]
    def unavailable(*_):raise OSError('additional period calendar unavailable')
    monkeypatch.setattr(runner,'trading_context',unavailable)
    monkeypatch.setattr(runner,'fetch_frame',lambda code,period,context:calls.append(period))
    monkeypatch.setattr(runner,'frame_quality',lambda *_:{'errors':['NO_DATA']})
    result=runner._scan_worker_symbol(
        {'market':'a','code':'SH.600000','name':'fixture','_monitor_periods':['1m','5m']},
        {'frequencies':['5m'],'recent_sessions':5,'max_anchor_sessions':20},
        {'5m':{'cutoff':100}},tmp_path,'test',{'observed_at':'2026-09-17T10:05:00+08:00'})
    assert calls==['5m'] and len(result['rows'])==2
    assert result['rows'][0]['data_errors']==['SESSION_CALENDAR_UNAVAILABLE']
    assert result['rows'][1]['data_errors']==['NO_DATA']
