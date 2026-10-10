from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
import time

import pytest

from chanlun.screening import compute_capacity as capacity


def test_idle_monitor_returns_all_capacity_to_screening(monkeypatch):
    monkeypatch.setattr(capacity.os,'cpu_count',lambda:16)
    assert capacity.worker_capacity(16,monitor=True)==16
    assert capacity.worker_capacity(16)==16
    monkeypatch.setattr(capacity.os,'cpu_count',lambda:4)
    assert capacity.worker_capacity(16,monitor=True)==4
    assert capacity.worker_capacity(16)==4
    assert capacity.role_limits(16, demand=16, screening_active=True) == {
        'monitor_capacity':12,'screening_capacity':4,'monitor_reserved':12}


def test_manual_and_monitor_work_share_slots_and_release_them_after_errors(tmp_path,monkeypatch):
    from chanlun import config
    monkeypatch.setattr(config,'get_data_path',lambda:tmp_path)
    monkeypatch.setattr(capacity.os,'cpu_count',lambda:4)
    budget=capacity.budget_request()
    gate=threading.Barrier(8);lock=threading.Lock();active=0;maximum=0
    def task(index):
        nonlocal active,maximum
        request={'compute_budget':budget}
        if index%2:request['worker_runtime']={}
        gate.wait(timeout=5)
        with capacity.compute_slot(request,tmp_path):
            with lock:active+=1;maximum=max(active,maximum)
            time.sleep(.04)
            with lock:active-=1
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(task,range(8)))
    assert 2<=maximum<=4 and active==0
    with pytest.raises(RuntimeError,match='body failure'):
        with capacity.compute_slot({'compute_budget':budget},tmp_path):
            raise RuntimeError('body failure')
    with capacity.compute_slot({'compute_budget':budget},tmp_path):pass
    Path(tmp_path,'cancel').touch()
    with pytest.raises(RuntimeError,match='cancelled'):
        with capacity.compute_slot({'compute_budget':budget},tmp_path):pass


def test_memory_budget_stops_new_work_and_recovers_gradually():
    gib=capacity.GIB
    assert capacity.pressure_limit(None,{'available':gib//4,'reserve':2*gib},16,100)==0
    assert capacity.pressure_limit(None,{'available':3*gib,'reserve':2*gib},16,100)==2
    old={'limit':8,'changed_at':100}
    assert capacity.pressure_limit(old,{'available':gib,'reserve':2*gib},16,103)==6
    assert capacity.pressure_limit(old,{'available':8*gib,'reserve':2*gib},16,101)==8
    assert capacity.pressure_limit(old,{'available':8*gib,'reserve':2*gib},16,103)==9
    # A two-core machine keeps both roles runnable; the old static reservation
    # assigned zero to screening at this limit even with an idle monitor.
    assert capacity.role_capacity(2)==2 and capacity.role_capacity(2,monitor=True)==2
    pressure=capacity.role_limits(2, demand=2, screening_active=True)
    assert capacity.role_capacity(2,pressure=pressure)==1
    assert capacity.role_capacity(2,monitor=True,pressure=pressure)==1


@pytest.fixture
def resource_budget(tmp_path,monkeypatch):
    from chanlun import config
    monkeypatch.setattr(config,'get_data_path',lambda:tmp_path)
    monkeypatch.setattr(capacity.os,'cpu_count',lambda:16)
    monkeypatch.setattr(capacity,'memory_status',lambda:{'available':64*capacity.GIB,'reserve':2*capacity.GIB})
    return capacity.budget_request()


@pytest.mark.parametrize('limit,live,scan', [(0,0,0),(1,1,1),(2,1,1),(3,2,1),(4,3,1),(8,6,2),(16,12,4)])
def test_live_lease_split_keeps_low_cpu_roles_runnable(limit,live,scan):
    limits=capacity.role_limits(limit,demand=800,screening_active=True)
    assert (limits['monitor_capacity'],limits['screening_capacity'])==(live,scan)
    assert capacity.role_limits(limit,demand=0)['screening_capacity']==limit
    assert capacity.role_limits(limit,demand=800,screening_active=False)['monitor_capacity']==limit


def test_live_demand_renews_independent_owners_then_expires_and_returns_slots(resource_budget,monkeypatch):
    now=[1000.]
    monkeypatch.setattr(capacity.time,'time',lambda:now[0])
    capacity.update_resource_budget(resource_budget)
    active=capacity.publish_monitor_demand(resource_budget,demand=800,owner='old',expires_in=12)
    assert active['monitor_capacity']==12 and active['screening_capacity']==4
    capacity.publish_monitor_demand(resource_budget,demand=3,owner='new',expires_in=12)
    remaining=capacity.publish_monitor_demand(resource_budget,demand=0,owner='old')
    assert remaining['monitor_capacity']==3 and remaining['screening_capacity']==13
    now[0]+=13
    expired=capacity.update_resource_budget(resource_budget)
    assert expired['monitor_demand']==0 and expired['screening_capacity']==16
    assert expired['monitor_capacity']==16


@pytest.mark.parametrize('kwargs', [{'demand':True},{'demand':-1},{'demand':1,'expires_in':0},
                                  {'demand':1,'expires_in':61},{'demand':1,'expires_in':float('nan')},
                                  {'demand':1,'owner':''}])
def test_invalid_demand_is_rejected_before_writing_lease(resource_budget,kwargs):
    with pytest.raises(ValueError):
        capacity.publish_monitor_demand(resource_budget,**kwargs)
    assert not (Path(resource_budget['root'])/capacity.DEMAND_FILE).exists()


def test_corrupt_demand_cannot_strand_either_role(resource_budget):
    capacity.update_resource_budget(resource_budget)
    (Path(resource_budget['root'])/capacity.DEMAND_FILE).write_text('partial')
    value=capacity.update_resource_budget(resource_budget,monitor=True)
    assert value['monitor_capacity']==value['screening_capacity']==16


def test_slot_body_lock_timeout_is_not_swallowed_or_retried(resource_budget,tmp_path):
    calls=[]
    with pytest.raises(capacity.InterprocessLockTimeout,match='body failed'):
        with capacity.compute_slot({'compute_budget':resource_budget},tmp_path):
            calls.append(True)
            raise capacity.InterprocessLockTimeout('body failed')
    assert calls==[True]


def test_memory_budget_is_shared_and_invalid_destinations_cannot_be_written(tmp_path,monkeypatch):
    from chanlun import config
    monkeypatch.setattr(config,'get_data_path',lambda:tmp_path)
    monkeypatch.setattr(capacity.os,'cpu_count',lambda:16)
    monkeypatch.setattr(capacity,'memory_status',lambda:{'available':3*capacity.GIB,'reserve':2*capacity.GIB})
    budget=capacity.budget_request()
    assert capacity.update_resource_budget(budget)['limit']==2
    assert capacity._resource_read(tmp_path/'compute_budget',16)['limit']==2
    with pytest.raises(ValueError):
        capacity.update_resource_budget({**budget,'root':str(tmp_path/'elsewhere')})
    assert not (tmp_path/'elsewhere').exists()
