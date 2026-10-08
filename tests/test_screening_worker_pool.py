"""Reusable children must preserve isolation, deadlines and process ownership."""
import multiprocessing
import os
from pathlib import Path
import time

import pytest

from chanlun.screening.worker_pool import ReusableWorkerPool
from chanlun.screening.worker_transport import JobInputReader, store_reply


def echo_worker(connection):
    inputs = JobInputReader()
    try:
        while (message := connection.recv()) is not None:
            task = inputs.load(message)
            stock = task['stock']
            if stock.get('blocked'):
                if stock.get('started_file'):
                    Path(stock['started_file']).write_text(str(os.getpid()), encoding='utf-8')
                multiprocessing.Event().wait(120)
            if stock.get('crash'):
                os._exit(3)
            response = store_reply(task, {'code': 'wrong-symbol' if stock.get('wrong_symbol') else stock['code'],
                                        'market': stock.get('market', 'a'), 'rows': [],
                                        'pid': os.getpid(), 'settings': task['settings'],
                                        'contexts': task['contexts'], 'request': task['request'],
                                        'run_dir': task['run_dir'], 'holding': stock.get('holding')})
            if stock.get('wrong_ticket'):
                response['ticket'] = 'old-task'
            connection.send(response)
    except (EOFError, BrokenPipeError):
        pass
    finally:
        connection.close()


@pytest.fixture
def pool():
    value = ReusableWorkerPool('test', worker_target=echo_worker, revision_reader=lambda: 'test')
    yield value
    value.close()


def scan(pool, folder, stocks=None, *, workers=1, **kwargs):
    folder.mkdir(exist_ok=True)
    return pool.scan(stocks or [{'code': 'SH.600000', 'name': 'fixture'}],
                     kwargs.pop('settings', {'frequencies': ['5m']}), kwargs.pop('contexts', {}),
                     folder, workers, 'test', request=kwargs.pop('request', {}), **kwargs)


def results(stream):
    return [result for result, _ in stream if result is not None]


def test_completed_child_reused_with_wholly_new_job_parameters(pool, tmp_path):
    first = results(scan(pool, tmp_path/'first', [{'code': 'SH.600000', 'name': 'one', 'holding': True}],
                         contexts={'5m': {'cutoff': 100}}, request={'force_rebuild': True}))[0]
    second = results(scan(pool, tmp_path/'second', [{'code': 'SZ.000001', 'name': 'two', 'holding': False}],
                          settings={'frequencies': ['30m']}, contexts={'30m': {'cutoff': 200}}))[0]
    assert first['pid'] == second['pid']
    assert second['settings'] == {'frequencies': ['30m']}
    assert second['contexts'] == {'30m': {'cutoff': 200}} and second['request'] == {}
    assert second['holding'] is False and second['run_dir'] == str(tmp_path/'second')
    assert pool.snapshot()['started_processes'] == 1


def test_timeout_recycles_only_failed_child_and_reports_every_requested_period(pool, tmp_path):
    old = results(scan(pool, tmp_path/'warm'))[0]
    value = results(scan(pool, tmp_path/'timeout', [
        {'code': 'SH.600000', 'name': 'blocked', 'blocked': True, '_monitor_periods': ['1m', '5m']},
        {'code': 'SH.600001', 'name': 'good'}], timeout_seconds=3))
    assert [row['frequency'] for row in value[0]['rows']] == ['1m', '5m']
    assert all(row['data_errors'] == ['WORKER_TIMEOUT'] for row in value[0]['rows'])
    assert value[1]['pid'] != old['pid'] and value[1]['code'] == 'SH.600001'


@pytest.mark.parametrize('fault', ['wrong_ticket', 'wrong_symbol', 'crash'])
def test_stale_response_or_crash_cannot_enter_a_later_batch(pool, tmp_path, fault):
    failure = results(scan(pool, tmp_path/'bad', [{'code': 'SH.600000', 'name': 'bad', fault: True}]))[0]
    assert failure['rows'][0]['data_errors'] == ['ENGINE_ERROR']
    recovered = results(scan(pool, tmp_path/'good'))[0]
    assert recovered['code'] == 'SH.600000' and recovered['rows'] == []


def test_idle_broken_pipe_fails_only_its_stock_and_does_not_drop_remaining_work(pool, tmp_path, monkeypatch):
    results(scan(pool, tmp_path/'warm'))
    def broken(*_):
        raise BrokenPipeError('isolated broken channel')
    monkeypatch.setattr(pool.slots[0]['connection'], 'send', broken)
    value = results(scan(pool, tmp_path/'next', [{'code': 'SH.600000', 'name': 'failed'},
                                               {'code': 'SH.600001', 'name': 'good'}]))
    assert len(value) == 2
    assert value[0]['rows'][0]['data_errors'] == ['ENGINE_ERROR']
    assert value[1]['code'] == 'SH.600001' and value[1]['rows'] == []


def test_reply_cleanup_failure_does_not_fail_a_stock_or_replace_a_healthy_child(pool,tmp_path,monkeypatch):
    original=Path.unlink
    def locked(self,*args,**kwargs):
        if self.name.startswith('worker_reply.'):
            raise PermissionError('temporary reply cleanup denial')
        return original(self,*args,**kwargs)
    with monkeypatch.context() as fault:
        fault.setattr(Path,'unlink',locked)
        first=results(scan(pool,tmp_path/'first'))[0]
        second=results(scan(pool,tmp_path/'second'))[0]
    assert first['rows']==second['rows']==[]
    assert first['pid']==second['pid']
    assert pool.snapshot()['started_processes']==1


def test_transient_reply_lock_is_retried_after_the_batch(pool,tmp_path,monkeypatch):
    original=Path.unlink;attempts={}
    def transient(self,*args,**kwargs):
        if self.name.startswith('worker_reply.'):
            attempts[self]=attempts.get(self,0)+1
            if attempts[self]==1:raise PermissionError('short reply lock')
        return original(self,*args,**kwargs)
    monkeypatch.setattr(Path,'unlink',transient)
    result=results(scan(pool,tmp_path/'once'))[0]
    assert result['rows']==[]
    assert list(attempts.values())==[2]
    assert not list((tmp_path/'once').glob('worker_reply.*.json'))


def test_unaccepted_reply_is_retained_when_consumer_stops_before_commit(pool,tmp_path):
    directory=tmp_path/'uncommitted';stream=scan(pool,directory)
    result=None
    while result is None:
        result,_=next(stream)
    assert result['rows']==[]
    assert len(list(directory.glob('worker_reply.*.json')))==1
    stream.close()
    assert len(list(directory.glob('worker_reply.*.json')))==1
    assert pool.busy is False


def test_cancel_keeps_completed_prefix_and_reaps_inflight_calls(pool, tmp_path):
    directory = tmp_path/'cancelled'
    stream = scan(pool, directory, [{'code': 'SH.600000', 'name': 'good'},
                                    {'code': 'SH.600001', 'name': 'blocked', 'blocked': True}])
    first = next(stream)
    while first[0] is None:
        first = next(stream)
    assert first[0]['code'] == 'SH.600000'
    assert next(stream)[0] is None
    assert next(stream)[0] is None  # Blocking stock has now been assigned.
    (directory/'cancel').touch()
    started = time.monotonic()
    assert list(stream) == []
    assert time.monotonic()-started < 5
    assert pool.snapshot()['worker_pids'] == []
    assert results(scan(pool, tmp_path/'next'))[0]['code'] == 'SH.600000'


def test_task_count_bounds_reuse_and_smaller_budget_closes_excess_children(pool, tmp_path):
    pool.max_tasks = 1
    first = results(scan(pool, tmp_path/'one'))[0]['pid']
    second = results(scan(pool, tmp_path/'two'))[0]['pid']
    assert first != second
    pool.max_tasks = 64
    results(scan(pool, tmp_path/'three', [{'code': 'SH.600000', 'name': 'one'},
                                        {'code': 'SH.600001', 'name': 'two'}], workers=2))
    assert len(pool.snapshot()['worker_pids']) == 2
    results(scan(pool, tmp_path/'four'))
    assert len(pool.snapshot()['worker_pids']) == 1


def test_stale_revision_or_closed_pool_cannot_accept_work(pool, tmp_path):
    results(scan(pool, tmp_path/'one'))
    pool.revision_reader = lambda: 'changed'
    with pytest.raises(RuntimeError, match='stale'):
        results(scan(pool, tmp_path/'changed'))
    pool.close()
    with pytest.raises(RuntimeError, match='closed'):
        results(scan(pool, tmp_path/'closed'))


def test_source_change_during_a_call_discards_uncommitted_work(pool, tmp_path):
    results(scan(pool, tmp_path/'warm'))
    stream = scan(pool, tmp_path/'changing', [{'code': 'SH.600000', 'name': 'blocked', 'blocked': True}])
    assert next(stream)[0] is None
    pool.revision_reader = lambda: 'changed'
    with pytest.raises(RuntimeError, match='source changed'):
        list(stream)
    assert pool.snapshot()['worker_pids'] == []


def test_per_symbol_calculation_does_not_retain_force_holdings_or_shorter_periods(monkeypatch):
    from chanlun.screening import runner
    captured = []
    def compute(stock, settings, contexts, directory, **kwargs):
        captured.append((stock, settings, contexts, kwargs))
        return {'code': stock['code'], 'rows': []}
    monkeypatch.setattr(runner, 'scan_symbol', compute)
    monkeypatch.setattr(runner, 'trading_context', lambda stamp, *args: {'cutoff': stamp.timestamp()})
    settings = {'frequencies': ['5m'], 'recent_sessions': 5, 'max_anchor_sessions': 20}
    stock = {'code': 'SH.600000', 'name': 'one', 'holding': True, '_monitor_periods': ['1m', '5m']}
    runner._scan_worker_symbol(stock, settings, {'5m': {'cutoff': 100}}, 'first', 'r',
                               {'force_rebuild': True, 'observed_at': '2026-10-07T10:05:00+08:00'})
    runner._scan_worker_symbol({'code': 'SZ.000001', 'name': 'two', 'holding': False},
                               {'frequencies': ['30m']}, {'30m': {'cutoff': 200}}, 'second', 'r', {})
    assert captured[0][1]['frequencies'] == ['1m', '5m'] and captured[0][3]['force_rebuild'] is True
    assert captured[1][1]['frequencies'] == ['30m'] and captured[1][2] == {'30m': {'cutoff': 200}}
    assert captured[1][0]['holding'] is False and 'force_rebuild' not in captured[1][3]


def test_failed_result_write_releases_borrowed_iterator_and_keeps_committed_prefix(tmp_path):
    import json
    from chanlun.screening.runner import run_screening, write_json
    stock = {'market': 'a', 'code': 'SH.600000', 'name': 'fixture'}
    request = {'run_id': tmp_path.name, 'observed_at': '2026-09-21T10:05:00+08:00',
               'settings': {'scope': 'symbols', 'symbols': [stock], 'frequencies': ['5m'],
                            'recent_sessions': 5, 'max_anchor_sessions': 20, 'workers': 1},
               'holding_snapshot': {'schema': 'monitor-holdings-v1', 'keys': []}}
    write_json(tmp_path/'request.json', request)
    class Borrowed:
        def __init__(self):
            self.items = iter([({'code': stock['code'], 'rows': []}, []),
                               ({'code': stock['code'], 'rows': [], 'invalid': object()}, [])])
            self.closed = False
        def __iter__(self):
            return self
        def __next__(self):
            return next(self.items)
        def close(self):
            self.closed = True
    class Pool:
        revision = 'test'
        def __init__(self):
            self.iterator = Borrowed()
        def scan(self, *_args, **_kwargs):
            return self.iterator
    pool = Pool()
    run_screening(tmp_path, worker_pool=pool, pinned_revision='test')
    assert pool.iterator.closed
    assert json.loads((tmp_path/'status.json').read_text(encoding='utf-8'))['status'] == 'failed'
    assert len((tmp_path/'results.jsonl').read_text(encoding='utf-8').splitlines()) == 1
