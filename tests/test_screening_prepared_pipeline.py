from threading import Event, enumerate as threads
import time

import pytest

from chanlun.screening.prepared_scan import pipelined_scan
from chanlun.screening.runner import ScreeningCancelled


def stocks():
    return [{'code': f'SH.60000{i}', 'market': 'a', '_monitor_periods': ['5m']} for i in range(3)]


def run(tmp_path, prepare, scan, values=None):
    return pipelined_scan(values or stocks(), {'frequencies': ['5m']}, {}, tmp_path, 2, 'fixture',
                          request={}, prepare=prepare, scan=scan)


def test_first_batch_runs_while_later_inputs_are_still_preparing_and_pool_is_reused(tmp_path):
    computed, prepared = Event(), Event()
    holders, closed, calls = [], [], []
    class Pool:
        def close(self):
            closed.append(True)
    def prepare(items, *args, on_ready, **kwargs):
        yield {'state': 'running', 'completed': 0, 'total': 3}
        on_ready(items[:1])
        assert computed.wait(3), 'analysis waited for all preparation'
        on_ready(items[1:])
        prepared.set()
        yield {'state': 'completed', 'completed': 3, 'total': 3}
    def scan(items, *args, pool_holder, **kwargs):
        holders.append(id(pool_holder))
        if not pool_holder:
            pool_holder.append(Pool())
        for item in items:
            if not calls:
                assert not prepared.is_set()
                computed.set()
            calls.append(item['code'])
            yield {'code': item['code']}, []
    rows = [row for row, _ in run(tmp_path, prepare, scan) if row]
    assert [row['code'] for row in rows] == [item['code'] for item in stocks()]
    assert len(holders) >= 2 and len(set(holders)) == 1 and closed == [True]


def test_queued_inputs_are_frozen_before_the_producer_changes_its_objects(tmp_path):
    def prepare(items, *args, on_ready, **kwargs):
        items[0]['_completed_inputs'] = {'5m': {'input_key': 'verified'}}
        on_ready(items)
        items[0]['_completed_inputs']['5m']['input_key'] = 'later'
        yield {'state': 'completed'}
    def scan(items, *args, **kwargs):
        assert items[0]['_completed_inputs']['5m']['input_key'] == 'verified'
        for item in items:
            yield {'code': item['code']}, []
    assert len([row for row, _ in run(tmp_path, prepare, scan) if row]) == 3


def test_monitor_reuses_its_owned_pool_and_computes_before_all_inputs_are_ready(tmp_path):
    first_done=Event();prepared_all=Event();calls=[]
    def prepare(items,*args,on_ready,**kwargs):
        on_ready(items[:1])
        assert first_done.wait(3),'monitor blocked on the whole preparation batch'
        on_ready(items[1:]);prepared_all.set()
        yield {'state':'completed','completed':len(items)}
    class Pool:
        def scan(self,items,*args,**kwargs):
            assert 'pool_holder' not in kwargs
            if not calls:
                assert not prepared_all.is_set();first_done.set()
            calls.append([item['code'] for item in items])
            for item in items:yield {'code':item['code']},[]
        def close(self):pytest.fail('pipeline must not close the monitor-owned pool')
    pool=Pool()
    stream=pipelined_scan(stocks(),{'frequencies':['5m']},{},tmp_path,2,'fixture',
        request={},prepare=prepare,worker_pool=pool)
    assert len([row for row,_ in stream if row])==3
    assert len(calls)>=2


def test_newly_prepared_tasks_fill_idle_slots_before_an_earlier_slow_task_finishes(tmp_path):
    from chanlun.screening.worker_pool import ReusableWorkerPool
    from tests.test_screening_worker_pool import echo_worker
    pool=ReusableWorkerPool('test',worker_target=echo_worker,revision_reader=lambda:'test')
    values=[{'code':f'SH.{600000+i}','name':'fixture','delay':.8 if i==0 else .01} for i in range(4)]
    first_done=Event()
    def prepare(items,*args,on_ready,**kwargs):
        on_ready(items[:2]);assert first_done.wait(5)
        on_ready(items[2:]);yield {'state':'completed'}
    try:
        warm=tmp_path/'warm';warm.mkdir()
        list(pool.scan(values[1:3],{'frequencies':['5m']},{},warm,2,'test',request={}))
        run=tmp_path/'run';run.mkdir();order=[]
        for row,_ in pipelined_scan(values,{'frequencies':['5m']},{},run,2,'test',request={},prepare=prepare,worker_pool=pool):
            if row:
                order.append(row['code'])
                if row['code']==values[1]['code']:first_done.set()
        assert order.index(values[2]['code'])<order.index(values[0]['code'])
        assert len(order)==4 and len(set(order))==4
    finally:pool.close()


@pytest.mark.parametrize('failure', ['raises', 'missing', 'duplicate'])
def test_incomplete_or_invalid_preparation_cannot_report_a_completed_scan(tmp_path, failure):
    def prepare(items, *args, on_ready, **kwargs):
        if failure == 'raises':
            raise RuntimeError('fixture preparation failure')
        on_ready(items[:1] if failure == 'missing' else [items[0], items[0]])
        yield {'state': 'completed'}
    def scan(items, *args, **kwargs):
        for item in items:
            yield {'code': item['code']}, []
    with pytest.raises((RuntimeError, ValueError)):
        list(run(tmp_path, prepare, scan))
    assert not any(thread.name == 'ScreeningInputPreparation' for thread in threads())


def test_cancel_stops_preparation_and_keeps_already_published_results(tmp_path):
    cleaned = Event()
    def prepare(items, *args, on_ready, stop_requested, **kwargs):
        try:
            on_ready(items[:1])
            while not stop_requested():
                time.sleep(.01)
                yield {'state': 'running'}
        finally:
            cleaned.set()
    def scan(items, *args, **kwargs):
        yield {'code': items[0]['code']}, []
    stream = run(tmp_path, prepare, scan)
    while True:
        row, _ = next(stream)
        if row:
            break
    (tmp_path / 'cancel').touch()
    with pytest.raises(ScreeningCancelled):
        list(stream)
    assert cleaned.is_set()


def test_fallback_dispatches_unverified_inputs_without_fabricating_receipts(tmp_path):
    def prepare(items, *args, on_ready, **kwargs):
        on_ready(items)
        yield {'state': 'fallback', 'failed': 3}
    def scan(items, *args, **kwargs):
        assert all('_completed_inputs' not in item for item in items)
        for item in items:
            yield {'code': item['code']}, []
    assert len([row for row, _ in run(tmp_path, prepare, scan) if row]) == 3


def test_preparation_batches_share_a_real_spawned_worker_and_execution_request(tmp_path):
    from functools import partial
    import multiprocessing
    from chanlun.screening.runner import bounded_scan
    from tests.test_screening_worker_pool import echo_worker
    computed = Event()
    children_before = {child.pid for child in multiprocessing.active_children()}
    request = {'rolling_window_targets': {'5m': '2026-01-01'}}
    def prepare(items, *args, on_ready, **kwargs):
        on_ready(items[:1])
        assert computed.wait(15)
        on_ready(items[1:])
        yield {'state': 'completed'}
    stream = pipelined_scan(stocks(), {'frequencies': ['5m']}, {}, tmp_path, 1, 'test',
        request=request, prepare=prepare, scan=partial(bounded_scan, worker_target=echo_worker))
    rows = []
    for row, _ in stream:
        if row:
            rows.append(row)
            computed.set()
    assert len(rows) == 3
    assert len({row['pid'] for row in rows}) == 1
    assert all(row['request'] == request for row in rows)
    assert {child.pid for child in multiprocessing.active_children()} <= children_before


def test_cancellation_releases_a_producer_blocked_by_a_full_queue(tmp_path):
    cleaned = Event()
    values = [{'code': f'SH.{i:06}', '_monitor_periods': ['5m']} for i in range(2000)]
    def prepare(items, *args, on_ready, **kwargs):
        try:
            on_ready(items)
            yield {'state': 'completed'}
        finally:
            cleaned.set()
    def scan(items, *args, **kwargs):
        (tmp_path / 'cancel').touch()
        yield {'code': items[0]['code']}, []
    with pytest.raises(ScreeningCancelled):
        list(run(tmp_path, prepare, scan, values))
    assert cleaned.is_set()
    assert not any(thread.name == 'ScreeningInputPreparation' for thread in threads())
