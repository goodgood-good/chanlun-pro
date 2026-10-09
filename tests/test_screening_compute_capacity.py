from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
import time

import pytest

from chanlun.screening import compute_capacity as capacity


def test_budget_uses_sixteen_logical_processors_and_reserves_monitor_capacity(monkeypatch):
    monkeypatch.setattr(capacity.os,'cpu_count',lambda:16)
    assert capacity.worker_capacity(16,monitor=True)==16
    assert capacity.worker_capacity(16)==14
    monkeypatch.setattr(capacity.os,'cpu_count',lambda:4)
    assert capacity.worker_capacity(16,monitor=True)==4
    assert capacity.worker_capacity(16)==2


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
