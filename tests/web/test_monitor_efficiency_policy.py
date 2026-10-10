from copy import deepcopy
from datetime import timedelta

from cl_app.services.monitor_view import paged_state, period_check_state
from cl_app.services import signal_monitor
from tests.web.test_notification_state_freshness import ready_event, setup
from tests.web.test_monitor_pagination import sample
from chanlun.screening import runner
from chanlun.screening.rules import POINT_TYPES


def test_monitor_worker_setting_accepts_sixteen_and_rejects_larger_budgets(setup):
    import pytest
    service,*_=setup
    result=service.enable({'monitor_workers':16,'screening_workers':16})
    assert result['settings']['monitor_workers']==16
    assert result['settings']['screening']['workers']==16
    for workers in [0,17,True]:
        with pytest.raises(ValueError):service.enable({'monitor_workers':workers})


def test_holding_types_are_independent_of_scan_types_and_require_current_membership(ready_event,monkeypatch):
    service,_live,event=ready_event
    state=service._load();key=f"{event['market']}:{event['code']}"
    state.update(holding_symbols=[key],holding_source_current=True)
    state['seed']['settings']['point_types']=['1sell']
    event['holding']=True
    monkeypatch.setattr(signal_monitor,'holding_symbols',lambda:[{'market':event['market'],'code':event['code']}])
    assert service._validate_notification_events([event])[0]
    monkeypatch.setattr(signal_monitor,'holding_symbols',lambda:[])
    assert service._validate_notification_events([event])==([],[])
    def unavailable():raise OSError('holding lookup unavailable')
    monkeypatch.setattr(signal_monitor,'holding_symbols',unavailable)
    assert service._validate_notification_events([event])==([],[event])


def test_only_monitor_holdings_expand_the_worker_point_types(tmp_path,monkeypatch):
    seen=[]
    monkeypatch.setattr(runner,'scan_symbol',lambda stock,settings,*args,**kwargs:seen.append(settings) or {'rows':[]})
    settings={'scope':'symbols','frequencies':['5m'],'point_types':['3buy']}
    original=deepcopy(settings)
    for held,request in [(True,{'holding_snapshot':{}}),(False,{'holding_snapshot':{}}),(True,{})]:
        runner._scan_worker_symbol({'market':'a','code':'SH.600000','name':'sample','holding':held},settings,{},tmp_path,'revision',request)
    assert set(seen[0]['point_types'])==set(POINT_TYPES)
    assert seen[1]['point_types']==seen[2]['point_types']==['3buy']
    assert settings==original


def test_a_waiting_message_requests_a_priority_recheck_even_at_the_same_market_cutoff(ready_event,monkeypatch):
    service,live,event=ready_event
    state=service._load();key=f"{event['market']}:{event['code']}"
    check=state['selection_checks'][key+':'+event['frequency']]
    cutoff=check['cutoff'];check['cutoff']-=300
    assert service._validate_notification_events([event])==([],[event])
    assert (key,event['frequency']) in service._notification_rechecks
    state['seed']['symbols'].append({'market':'a','code':'SH.600001','name':'older normal work'})
    state['seed']['codes'].append('a:SH.600001')
    state['attempted_cutoffs'][key]=cutoff;state['attempted_cutoffs']['a:SH.600001']=0
    state.update(force=False,force_pending_symbols=[],pending_symbols_count=0,next_check_at=service.now().timestamp()+600)
    monkeypatch.setattr(signal_monitor,'MONITOR_BATCH_SIZE',1)
    service._schedule_monitor(service.now(),None)
    assert [row['code'] for row in live.started[-1]['symbols']]==[event['code']]
    assert service._notification_retry_after[key]>service.now().timestamp()


def test_current_revision_with_an_old_cutoff_is_updating_not_ready():
    state=sample(2)
    for check in state['selection_checks'].values():check.update(error=False,cutoff=1000)
    clocks={'a:SH.000000:5m':1300,'a:SH.000001:5m':1000}
    result=paged_state(state,{},'current',True,expected_cutoffs=clocks)
    assert result['coverage']=={'total':2,'ready':1,'updating':1,'limited':0,'pending':0,'scheduled':0}
    assert result['signals']['0']['data_freshness']=='updating'
    assert result['signals']['1']['data_freshness']=='ready'
    assert period_check_state({'revision':'current','cutoff':1000},'current',None)=='pending'


def test_notification_recheck_never_bypasses_a_failed_symbol_backoff(ready_event,monkeypatch):
    service,live,event=ready_event
    state=service._load();key=f"{event['market']}:{event['code']}"
    check=state['selection_checks'][key+':'+event['frequency']]
    cutoff=check['cutoff'];check['error']=True
    assert service._validate_notification_events([event])==([],[event])
    state['attempted_cutoffs'][key]=cutoff
    state['retry_symbols'][key]={'cutoff':cutoff,'failures':1,'retry_at':service.now().timestamp()+30}
    state.update(force=False,force_pending_symbols=[],pending_symbols_count=0,next_check_at=service.now().timestamp()+600)
    previous=len(live.started)
    service._schedule_monitor(service.now(),None)
    assert len(live.started)==previous
