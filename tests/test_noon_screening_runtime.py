"""Noon deadlines and Windows pool recovery preserve committed results."""
from copy import deepcopy
import json
import os
import time

import pytest

from chanlun.screening import runner, worker_pool
from tests.test_screening_worker_pool import echo_worker


def test_blocked_worker_releases_its_slot_at_the_run_deadline(tmp_path):
    pool = worker_pool.ReusableWorkerPool('test', worker_target=echo_worker, revision_reader=lambda:'test')
    started = time.monotonic()
    try:
        result = list(pool.scan([{'code':'SH.600000','name':'blocked','blocked':True}],
            {'frequencies':['5m']}, {}, tmp_path, 1, 'test',
            request={'deadline_at':time.time()+1.5}, timeout_seconds=60))
        assert all(row is None for row, _ in result)
        assert time.monotonic()-started < 9
        assert pool.snapshot()['worker_pids'] == [] and pool.busy is False
    finally:
        pool.close()


@pytest.mark.skipif(os.name != 'nt', reason='Windows job retirement recovery')
def test_one_run_recovers_windows_kill_denial_and_does_not_repeat_a_committed_period(tmp_path, monkeypatch):
    original = worker_pool.ReusableWorkerPool
    pools = []
    def create(*args, **kwargs):
        pool = original(*args, **kwargs)
        pools.append(pool)
        return pool
    monkeypatch.setattr(worker_pool, 'ReusableWorkerPool', create)
    marker = tmp_path/'blocked_once'
    stocks = [{'code':'SH.600000','name':'ready'},
              {'code':'SH.600001','name':'blocked once','block_once_marker':str(marker)}]
    stream = runner.bounded_scan(stocks, {'frequencies':['5m']}, {}, tmp_path, 1, 'test',
                                 timeout_seconds=2, worker_target=echo_worker)
    ready = []
    try:
        while not ready:
            row, _ = next(stream)
            if row is not None:
                ready.append(row)
        assert len(ready) == 1
        process = pools[0].slots[0]['process']
        old_pid = process.pid
        monkeypatch.setattr(process, 'terminate', lambda:None)
        def denied():
            raise PermissionError(5, 'isolated termination denial')
        monkeypatch.setattr(process, 'kill', denied)
        ready.extend(row for row, _ in stream if row is not None)
    finally:
        stream.close()
    assert [row['code'] for row in ready] == ['SH.600000','SH.600001']
    assert len(pools) == 2 and all(pool.closed and not pool.busy and not pool.slots for pool in pools)
    from chanlun.screening.worker_lifetime import process_identity
    assert process_identity(old_pid)['alive'] is False
    assert json.loads((tmp_path/'worker_recovery.json').read_text())['completed'] == 1


def test_failed_reap_never_starts_a_replacement_pool(tmp_path, monkeypatch):
    created=[]
    class UnknownPool:
        def __init__(self,*args,**kwargs):
            created.append(self)
        def scan(self,*args,**kwargs):
            raise PermissionError('cannot retire')
            yield
        def close(self):
            raise RuntimeError('cannot confirm children exited')
    monkeypatch.setattr(worker_pool, 'ReusableWorkerPool', UnknownPool)
    with pytest.raises(RuntimeError,match='cannot confirm'):
        list(runner.bounded_scan([{'code':'SH.600000'}],{'frequencies':['5m']},{},tmp_path,1,'test',worker_target=echo_worker))
    assert len(created)==1


def _request(tmp_path, deadline):
    stocks=[{'market':'a','code':code,'name':code} for code in ['SH.600000','SH.600001']]
    request={'run_id':tmp_path.name,'observed_at':'2026-10-12T11:35:00+08:00',
             'settings':{'scope':'symbols','symbols':stocks,'frequencies':['5m','30m'],
                         'recent_sessions':5,'max_anchor_sessions':20,'workers':1,'alert_mode':'native_center_signals'},
             'holding_snapshot':{'schema':'monitor-holdings-v1','keys':[]},
             'incremental_periods':True,'deadline_at':deadline,'deadline_kind':'noon'}
    runner.write_json(tmp_path/'request.json',request)
    return request


def test_deadline_returns_honest_partial_status_without_dropping_committed_output(tmp_path, monkeypatch):
    clock=[1000.]
    _request(tmp_path,1001)
    monkeypatch.setattr(runner.time,'time',lambda:clock[0])
    monkeypatch.setattr(runner,'source_revision',lambda:'test')
    monkeypatch.setattr(runner,'_a_calendar_context',lambda *args:{'cutoff':1000})
    records=[]
    def scan(stocks, settings, *args, **kwargs):
        assert kwargs['timeout_seconds']==30
        first=stocks[0]
        row={'code':first['code'],'requested_frequencies':first['_monitor_periods'],
             'rows':[{'code':first['code'],'frequency':first['_monitor_periods'][0],
                      'selected':[],'source_closed_at':1000,'data_errors':[]}]}
        records.append(deepcopy(row))
        yield row, []
        clock[0]=1002
    monkeypatch.setattr(runner,'bounded_scan',scan)
    runner.run_screening(tmp_path)
    state=json.loads((tmp_path/'status.json').read_text(encoding='utf-8'))
    assert state['status']=='cancelled' and state['stop_reason']=='time_budget'
    assert state['completed']==1 and state['total']==4 and state['successful_period_count']==1
    assert [json.loads(line) for line in (tmp_path/'results.jsonl').read_text().splitlines()]==records


def test_already_expired_budget_never_starts_catalog_or_compute(tmp_path,monkeypatch):
    _request(tmp_path,time.time()-10)
    monkeypatch.setattr(runner,'bounded_catalog',lambda *args:pytest.fail('expired catalog started'))
    monkeypatch.setattr(runner,'bounded_scan',lambda *args,**kw:pytest.fail('expired computation started'))
    runner.run_screening(tmp_path)
    state=json.loads((tmp_path/'status.json').read_text(encoding='utf-8'))
    assert state['stop_reason']=='time_budget' and state['completed']==0
