from copy import deepcopy
from datetime import datetime

import pytest

from chanlun.screening.rules import CN
from chanlun.screening.timeframe_policy import POLICY,LIVE_PERIODS
from cl_app.services.monitor_scan_priority import defer_to_screening
from cl_app.services.monitor_scope import build_minute_scope_index
from cl_app.services import monitor_requests
from tests.web.test_signal_monitor import setup as setup


def inputs():
    symbol={'market':'a','code':'SH.600088','name':'sample'}
    key='a:SH.600088'
    state={'seed':{'codes':[key],'symbols':[symbol],'screening_symbols':[symbol]},'force':False}
    manual={'run_id':'a'*32,'status':'running','source_current':True,'source_revision':'r',
            'settings':{'scope':'all_a_watchlist','alert_mode':'native_center_signals',
                        'timeframe_policy':POLICY,'frequencies':['5m','30m']}}
    return state,manual,symbol


def test_only_expired_automatic_closed_market_work_waits_without_acknowledgment():
    state,manual,symbol=inputs();before=deepcopy(state)
    assert defer_to_screening(state,manual,symbol,1000,3000,'r',build_minute_scope_index(state))
    assert state==before
    assert not defer_to_screening(state,{**manual,'status':'completed'},symbol,1000,3000,'r',build_minute_scope_index(state))
    assert not defer_to_screening(state,manual,symbol,1000,2000,'r',build_minute_scope_index(state))


@pytest.mark.parametrize('protection',['holding','watchlist','manual','urgent','unknown_request','source_changed','custom_scope'])
def test_explicit_membership_controls_and_current_notifications_keep_priority(protection):
    state,manual,symbol=inputs();key='a:SH.600088'
    if protection=='holding':state['holding_symbols']=[key]
    if protection=='watchlist':symbol['origin']='watchlist'
    if protection=='manual':monitor_requests.add(state,key,'manual',1)
    if protection=='unknown_request':state['force_pending_symbols']=[key]
    if protection=='source_changed':manual['source_current']=False
    if protection=='custom_scope':manual['settings']['scope']='symbols'
    assert not defer_to_screening(state,manual,symbol,1000,3000,'r',build_minute_scope_index(state),urgent=protection=='urgent')


def test_screening_owned_handoff_is_deferred_but_a_new_manual_owner_is_preserved():
    state,manual,symbol=inputs();key='a:SH.600088'
    state['force_pending_symbols']=[key]
    monitor_requests.add(state,key,'screening:'+manual['run_id'],{'source':'screening'})
    assert defer_to_screening(state,manual,symbol,1000,3000,'r',build_minute_scope_index(state))
    monitor_requests.add(state,key,'manual',2)
    assert not defer_to_screening(state,manual,symbol,1000,3000,'r',build_minute_scope_index(state))


def test_scheduler_keeps_watchlist_and_resumes_ordinary_work_after_full_scan(setup,monkeypatch):
    service,manual,live,clock=setup
    clock[0]=datetime(2026,9,17,22,0,tzinfo=CN)
    _,source,ordinary=inputs()
    watch={'market':'a','code':'SH.600089','name':'watch','origin':'watchlist'}
    actual=service._load();settings={**actual['settings']['screening'],'frequencies':LIVE_PERIODS}
    actual.update(enabled=True,force=False,force_pending_symbols=[],minute_force_pending={},
                  seed={'run_id':'old','codes':['a:SH.600088','a:SH.600089'],
                        'symbols':[ordinary,watch],'screening_symbols':[ordinary],'settings':settings})
    manual.state.update(source,current_source_revision='r')
    plans=[]
    monkeypatch.setattr(service,'_dispatch_monitor',plans.append)
    service._plan_monitor(clock[0],None)
    launch=plans[-1]
    assert {s['code'] for s in launch['settings']['symbols']}=={'SH.600089'}
    assert actual['closed_market_deferred_symbols']==1
    assert 'a:SH.600088' not in actual['attempted_cutoffs']
    manual.state['status']='completed'
    service._plan_monitor(clock[0],None)
    resumed=plans[-1]
    assert {s['code'] for s in resumed['settings']['symbols']}=={'SH.600088','SH.600089'}
    assert actual['closed_market_deferred_symbols']==0


def test_native_preparation_does_not_reserve_the_whole_compute_pool(setup,monkeypatch):
    service,manual,live,_=setup
    actual=service._load();symbols=[{'market':'a','code':f'SH.{600000+i}','name':'fixture'} for i in range(18)]
    keys=[f"a:{s['code']}" for s in symbols]
    actual.update(enabled=True,seed={'run_id':'seed','codes':keys,'symbols':symbols},
                  active_monitor_run='live',processed_run=None,active_symbols=keys,
                  active_live_symbols=keys,active_consumed_symbols=[],pending_live_tasks=4)
    manual.state.update(status='running')
    live.state.update(status='running',run_id='live',phase='source_preparation')
    reservations=[]
    monkeypatch.setattr(service,'_monitor_capacity',lambda demand,*a,**k:reservations.append(demand))
    monkeypatch.setattr(service,'_yield_live_for_priority',lambda *a:None)
    monkeypatch.setattr(service,'_yield_background_for_live',lambda *a:None)
    service._plan_monitor(service.now(),None)
    assert reservations==[2]


@pytest.mark.parametrize('stable,screening,expected',[(True,True,8),(False,True,16),(True,False,16)])
def test_known_workload_limits_reservation_without_hiding_unknown_or_overdue_demand(setup,monkeypatch,stable,screening,expected):
    from cl_app.services import signal_monitor
    service,*_=setup
    monkeypatch.setattr(signal_monitor,'cpu_capacity',lambda:16)
    service._load()['settings']['monitor_workers']=16
    service._monitor_capacity(16,screening,service_estimate={'boost_ready':stable,'required_workers':7})
    assert service._last_compute_limits['monitor_demand']==expected
    service._monitor_capacity(16,True,overdue_pressure=True,service_estimate={'boost_ready':True,'required_workers':16})
    assert service._last_compute_limits['monitor_demand']==16
