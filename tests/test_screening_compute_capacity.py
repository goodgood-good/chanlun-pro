from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, ExitStack
import json
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
        'monitor_capacity':4,'screening_capacity':12,'monitor_reserved':4}


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


def test_future_maintenance_yields_to_an_active_full_scan_without_leaking_locks(tmp_path,monkeypatch):
    from chanlun import config
    monkeypatch.setattr(config,'get_data_path',lambda:tmp_path)
    monkeypatch.setattr(capacity.os,'cpu_count',lambda:2)
    budget=capacity.budget_request()
    capacity.update_resource_budget(budget)
    with pytest.raises(capacity.MaintenanceDeferred):
        with capacity.compute_slot({'compute_budget':budget,'background_maintenance':True},tmp_path):
            pytest.fail('maintenance was admitted during a full scan')
    with capacity.compute_slot({'compute_budget':budget},tmp_path):
        pass


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


def test_fresh_advisory_budget_does_not_contend_for_the_writer_lock(resource_budget,monkeypatch):
    monkeypatch.setattr(capacity.time,'time',lambda:1000.)
    first=capacity.update_resource_budget(resource_budget,monitor=True)
    def forbidden(*args,**kwargs):pytest.fail('a fresh atomic snapshot needs no writer lock')
    monkeypatch.setattr(capacity,'interprocess_file_lock',forbidden)
    second=capacity.update_resource_budget(resource_budget,monitor=True)
    assert second==first


@pytest.mark.parametrize('limit,live,scan', [(0,0,0),(1,1,1),(2,1,1),(3,1,2),(4,1,3),(8,2,6),(16,4,12)])
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
    assert active['monitor_capacity']==4 and active['screening_capacity']==12
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


def test_late_old_owner_release_preserves_a_newer_owner_lease(resource_budget, monkeypatch):
    capacity.update_resource_budget(resource_budget)
    original = capacity.interprocess_file_lock
    entered, resume = threading.Event(), threading.Event()

    @contextmanager
    def delayed(path, **kwargs):
        if Path(path).name == 'monitor_demand.lock' and threading.current_thread().name.startswith('old-owner'):
            entered.set()
            assert resume.wait(5)
        with original(path, **kwargs):
            yield

    monkeypatch.setattr(capacity, 'interprocess_file_lock', delayed)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix='old-owner') as pool:
        old = pool.submit(capacity.publish_monitor_demand, resource_budget, demand=0, owner='old')
        try:
            assert entered.wait(2)
            capacity.publish_monitor_demand(resource_budget, demand=16, owner='new')
            path = Path(resource_budget['root']) / capacity.DEMAND_FILE
            before = json.loads(path.read_text())['leases']['new']
        finally:
            resume.set()
        released = old.result(timeout=3)
    assert json.loads(path.read_text())['leases'] == {'new': before}
    assert released['monitor_capacity'] == 4 and released['screening_capacity'] == 12


def test_demand_reader_does_not_discard_lease_newer_than_callers_timestamp(resource_budget):
    capacity.update_resource_budget(resource_budget)
    stale_now = time.time() - 1
    capacity.publish_monitor_demand(resource_budget, demand=16, owner='new')
    observed = capacity._with_roles({'limit':16, 'background_until':time.time()+10},
                                    Path(resource_budget['root']), 16, stale_now)
    assert observed['monitor_demand'] == 16 and observed['monitor_capacity'] == 4


@pytest.mark.parametrize('limit,live,scan', [(0,0,0),(1,1,1),(2,1,1),(4,3,1),(8,6,2),(16,12,4)])
def test_overdue_pressure_borrows_capacity_without_starving_screening(limit, live, scan):
    roles = capacity.role_limits(limit, demand=16, screening_active=True, overdue_pressure=True)
    assert (roles['monitor_capacity'], roles['screening_capacity']) == (live, scan)
    assert capacity.role_limits(limit, demand=16, overdue_pressure=True)['monitor_capacity'] == limit


def test_soft_memory_pause_recovers_one_worker_without_waiting_for_an_extra_half_gib():
    gib=capacity.GIB
    paused={'limit':0,'changed_at':90}
    memory={'available':int(2.4*gib),'reserve':2*gib}
    assert capacity.pressure_limit(paused,memory,16,100)==1
    active={'limit':1,'changed_at':90}
    assert capacity.pressure_limit(active,{'available':int(1.8*gib),'reserve':2*gib},16,100)==1
    assert capacity.pressure_limit(active,{'available':gib//4,'reserve':2*gib},16,100)==0


def test_boost_uses_each_owners_actual_demand_and_releases_to_normal_share(resource_budget):
    capacity.update_resource_budget(resource_budget)
    capacity.publish_monitor_demand(resource_budget, demand=16, owner='normal')
    small = capacity.publish_monitor_demand(resource_budget, demand=2, owner='urgent', overdue_pressure=True, holding_demand=2)
    assert small['monitor_capacity'] == 4
    thirteen = capacity.publish_monitor_demand(resource_budget, demand=13, owner='urgent', overdue_pressure=True, holding_demand=7)
    assert (thirteen['monitor_capacity'], thirteen['screening_capacity']) == (12, 4)
    high = capacity.publish_monitor_demand(resource_budget, demand=16, owner='urgent', overdue_pressure=True, holding_demand=16)
    assert (high['monitor_capacity'], high['screening_capacity']) == (12, 4)
    normal = capacity.publish_monitor_demand(resource_budget, demand=16, owner='urgent', overdue_pressure=False)
    assert (normal['monitor_capacity'], normal['screening_capacity']) == (4, 12)
    with pytest.raises(ValueError):
        capacity.publish_monitor_demand(resource_budget, demand=16, overdue_pressure='yes')


def test_owner_publication_failure_releases_acquired_os_slot(resource_budget, tmp_path, monkeypatch):
    def fail(*args):
        raise OSError('owner marker publication failed')
    with monkeypatch.context() as fault:
        fault.setattr(capacity, '_record_slot_owner', fail)
        with pytest.raises(OSError, match='owner marker publication failed'):
            with capacity.compute_slot({'compute_budget':resource_budget}, tmp_path):
                pytest.fail('unregistered owner entered calculation')
    with capacity.compute_slot({'compute_budget':resource_budget}, tmp_path) as slot:
        assert slot == 0
    assert not list(Path(resource_budget['root']).glob('*.owner.json'))


def test_unknown_preupgrade_slot_counts_conservatively_until_it_releases(resource_budget, tmp_path):
    root = Path(resource_budget['root'])
    root.mkdir(parents=True, exist_ok=True)
    stamp = time.time()
    (root / capacity.RESOURCE_FILE).write_text(json.dumps({'schema':'screening-memory-budget-v1',
        'limit':4, 'updated_at':stamp, 'changed_at':stamp, 'background_until':stamp+60}))
    capacity.publish_monitor_demand(resource_budget, demand=16, owner='scheduler')
    entered = threading.Event()
    def scanning_task():
        with capacity.compute_slot({'compute_budget':resource_budget}, tmp_path):
            entered.set()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with ExitStack() as locks:
            for index in range(3):
                locks.enter_context(capacity.interprocess_file_lock(root/f'slot_{index}.lock'))
            task = pool.submit(scanning_task)
            assert not entered.wait(.1)
        task.result(timeout=3)
    assert entered.is_set()
