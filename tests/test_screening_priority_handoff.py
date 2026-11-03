"""Actual IPC replies, private handoff ownership and unchanged hard-stop paths."""
import json
import os
from pathlib import Path
import threading
import time

import pytest

from chanlun.screening import runner, worker_pool, compute_capacity
from chanlun.screening.priority_handoff import publish_handoff, FILE
from chanlun.screening.worker_pool import ReusableWorkerPool
from chanlun.screening.worker_transport import JobInputReader, store_reply

REV = 'a' * 64


def result_worker(connection):
    reader = JobInputReader()
    count = 0
    try:
        while (message := connection.recv()) is not None:
            task = reader.load(message)
            stock = task['stock']
            count += 1
            if stock.get('started_file'):
                Path(stock['started_file']).write_text(str(os.getpid()))
            while stock.get('release_file') and not Path(stock['release_file']).exists():
                time.sleep(.002)
            time.sleep(stock.get('delay', 0))
            periods = stock.get('_monitor_periods', task['settings']['frequencies'])
            result = {'code': stock['code'], 'market': 'a', 'pid': os.getpid(), 'cache_count': count,
                      'requested_frequencies': periods,
                      'rows': [{'code': stock['code'], 'market': 'a', 'frequency': period,
                                'source_closed_at': task['contexts'][period]['cutoff'],
                                'input_fingerprint': 'f' * 64, 'selected': [],
                                'data_errors': ['ENGINE_ERROR'] if stock.get('result_error') else []}
                               for period in periods]}
            connection.send(store_reply(task, result, prefer_inline=True))
    except (EOFError, BrokenPipeError):
        pass
    finally:
        connection.close()


@pytest.fixture
def environment(tmp_path, monkeypatch):
    monkeypatch.setattr(compute_capacity, 'update_resource_budget', lambda *a, **k:
                        {'limit': 2, 'monitor_capacity': 2, 'available_mb': 8192, 'reserve_mb': 2048})
    monkeypatch.setattr(compute_capacity, 'worker_capacity', lambda count, **kwargs: count)
    monkeypatch.setattr(runner, '_a_calendar_context', lambda *args: {'cutoff': 100})
    revision = [REV]
    pool = ReusableWorkerPool(REV, worker_target=result_worker, revision_reader=lambda: revision[0])
    control = {'schema': 'monitor-source-prefetch-v1', 'enabled': True, 'generation': 'one',
               'version': 1, 'seed': 'seed', 'scope': 'c' * 64}
    runner.write_json(tmp_path / 'source_prefetch_control.json', control)
    stocks = [{'code': f'SH.{600100+i}', 'market': 'a', 'name': 'fixture'} for i in range(3)]
    warm = tmp_path / ('0' * 32)
    warm.mkdir()
    list(pool.scan(stocks[:2], {'frequencies': ['5m']}, {'5m': {'cutoff': 100}}, warm, 2, REV, request={}))

    def request(identity, values=None):
        directory = tmp_path / (identity * 32)
        directory.mkdir(exist_ok=True)
        body = {'run_id': directory.name, 'observed_at': '2026-10-29T14:00:00+08:00',
                'settings': {'scope': 'symbols', 'symbols': values or stocks, 'frequencies': ['5m'],
                             'recent_sessions': 5, 'max_anchor_sessions': 20, 'workers': 2},
                'worker_runtime': {'schema': 'monitor-worker-runtime-v1', 'generation': 'one', 'source_revision': REV},
                'compute_budget': {'limit': 2}, 'holding_snapshot': {'schema': 'monitor-holdings-v1', 'keys': []}}
        runner.write_json(directory / 'request.json', body)
        runner.write_json(tmp_path / 'latest.json', {'run_id': directory.name})
        return directory, body

    try:
        yield tmp_path, pool, control, stocks, request, revision
    finally:
        pool.close()


def pending(environment, *, bad=False):
    root, pool, control, stocks, request, revision = environment
    release = root / 'release'
    values = [{**stock, 'started_file': str(root / f'started-{i}'), 'release_file': str(release),
               'delay': .035 if i == 0 else 5, 'result_error': bad and i == 0}
              for i, stock in enumerate(stocks)]
    directory, body = request('1', values)
    stream = pool.scan(values, body['settings'], {'5m': {'cutoff': 100}}, directory, 2, REV, request=body)
    while not all((root / f'started-{i}').exists() for i in range(2)):
        assert next(stream)[0] is None
    return directory, body, stream, release


def test_successful_real_reply_reuses_warm_child_in_a_second_run_and_never_admits_the_tail(environment):
    root, pool, control, stocks, request, revision = environment
    directory, body, stream, release = pending(environment)
    initial_pids = set(pool.snapshot()['worker_pids'])
    publish_handoff(directory, body, control)
    release.touch()
    rows = [row for row, _ in stream if row]
    assert len(rows) == 1 and rows[0]['rows'][0]['source_closed_at'] == 100
    assert not (root / 'started-2').exists()
    assert (directory / 'cancel').exists()
    assert pool.snapshot()['worker_pids'] == [rows[0]['pid']]
    assert rows[0]['pid'] in initial_pids
    second, request2 = request('2', [stocks[0]])
    result = [row for row, _ in pool.scan([stocks[0]], request2['settings'], {'5m': {'cutoff': 200}},
               second, 1, REV, request=request2) if row][0]
    assert result['pid'] == rows[0]['pid'] and result['cache_count'] > rows[0]['cache_count']
    assert result['rows'][0]['source_closed_at'] == 200


def test_actual_runner_publishes_only_completed_prefix_and_marks_cancelled(environment, monkeypatch):
    root, pool, control, stocks, request, revision = environment
    release = root / 'runner-release'
    values = [{**stock, 'started_file': str(root / f'runner-started-{i}'), 'release_file': str(release),
               'delay': .035 if i == 0 else 5} for i, stock in enumerate(stocks)]
    directory, body = request('3', values)
    body['verify_completed_inputs'] = True
    runner.write_json(directory / 'request.json', body)
    producer_stopped = threading.Event()
    def prepare(items, *args, on_ready, stop_requested, **kwargs):
        on_ready(items[:2])
        yield {'state': 'running', 'completed': 2, 'total': 3}
        while not stop_requested():
            time.sleep(.002)
        producer_stopped.set()
        # The queued tail was never released and must not become completed.
    monkeypatch.setattr('chanlun.screening.tail_preparation.prepare_inputs', prepare)
    errors = []
    def handoff():
        try:
            deadline = time.monotonic() + 5
            while not all((root / f'runner-started-{i}').exists() for i in range(2)):
                assert time.monotonic() < deadline
                time.sleep(.002)
            publish_handoff(directory, body, control)
            time.sleep(.10)  # Intent precedes the successful IPC reply.
            release.touch()
        except BaseException as exc:
            errors.append(exc)
    thread = threading.Thread(target=handoff)
    thread.start()
    runner.run_screening(directory, worker_pool=pool, pinned_revision=REV)
    thread.join(timeout=5)
    assert not thread.is_alive() and not errors and producer_stopped.is_set()
    status = json.loads((directory / 'status.json').read_text(encoding='utf-8'))
    lines = (directory / 'results.jsonl').read_text(encoding='utf-8').splitlines() if (directory / 'results.jsonl').exists() else []
    assert status['status'] == 'cancelled' and status['completed'] == len(lines) == 1 < status['total']
    assert not (root / 'runner-started-2').exists()
    assert all(json.loads(line)['code'] in {stock['code'] for stock in values[:2]} for line in lines)


@pytest.mark.parametrize('kind', ['cancel', 'stop', 'control', 'deadline'])
def test_hard_requests_never_enter_the_short_wait(environment, monkeypatch, kind):
    root, pool, control, stocks, request, revision = environment
    directory, body, stream, release = pending(environment)
    publish_handoff(directory, body, control)
    if kind == 'cancel': (directory / 'cancel').touch()
    elif kind == 'stop': (root / 'worker_stop.json').touch()
    elif kind == 'control': runner.write_json(root / 'source_prefetch_control.json', {**control, 'enabled': False})
    else: body['deadline_at'] = time.time() - 1
    monkeypatch.setattr(pool, '_drain_priority', lambda *a, **k: pytest.fail('hard stop entered grace'))
    assert not [row for row, _ in stream if row]
    assert not pool.snapshot()['worker_pids']


def test_failed_completed_reply_is_not_published_or_kept_warm(environment):
    root, pool, control, stocks, request, revision = environment
    directory, body, stream, release = pending(environment, bad=True)
    publish_handoff(directory, body, control)
    release.touch()
    assert not [row for row, _ in stream if row]
    assert not pool.snapshot()['worker_pids']


def test_source_change_after_actual_reply_validation_cannot_publish_it(environment, monkeypatch):
    root, pool, control, stocks, request, revision = environment
    directory, body, stream, release = pending(environment)
    publish_handoff(directory, body, control)
    release.touch()
    original = pool._read_slot_reply
    def changed(*args):
        reply = original(*args)
        revision[0] = 'b' * 64
        return reply
    monkeypatch.setattr(pool, '_read_slot_reply', changed)
    with pytest.raises(RuntimeError, match='source changed'):
        list(stream)
    assert not pool.snapshot()['worker_pids']


@pytest.mark.skipif(os.name != 'nt', reason='Windows owned job fallback')
def test_failed_parallel_exit_poisoning_blocks_a_second_run(environment, monkeypatch):
    root, pool, control, stocks, request, revision = environment
    directory, body, stream, release = pending(environment)
    publish_handoff(directory, body, control)
    process = pool.slots[0]['process']
    monkeypatch.setattr(process, 'terminate', lambda: None)
    monkeypatch.setattr(process, 'kill', lambda: None)
    with pytest.raises(RuntimeError, match='confirm child exit'):
        list(stream)
    assert pool.closed and not pool.snapshot()['worker_pids']
    second, request2 = request('2', [stocks[0]])
    with pytest.raises(RuntimeError, match='closed'):
        list(pool.scan([stocks[0]], request2['settings'], {'5m': {'cutoff': 100}}, second, 1, REV, request=request2))


def test_repeated_priority_intent_cannot_renew_its_grace(environment):
    root, pool, control, stocks, request, revision = environment
    directory, body = request('1')
    first = publish_handoff(directory, body, control)
    time.sleep(.01)
    assert publish_handoff(directory, body, control) == first


def test_priority_arriving_during_slot_refill_stops_the_next_assignment(environment, monkeypatch):
    root, pool, control, stocks, request, revision = environment
    values = [{**stock, 'started_file': str(root / f'refill-{i}')}
              for i, stock in enumerate(stocks)]
    directory, body = request('1', values)
    send = pool.slots[0]['connection'].send
    def handoff_after_first_send(message):
        send(message)
        publish_handoff(directory, body, control)
    monkeypatch.setattr(pool.slots[0]['connection'], 'send', handoff_after_first_send)
    rows = [row for row, _ in pool.scan(values, body['settings'], {'5m': {'cutoff': 100}},
                                       directory, 2, REV, request=body) if row]
    assert len(rows) <= 1
    assert not (root / 'refill-1').exists() and not (root / 'refill-2').exists()


def test_producer_cleanup_error_after_the_last_pool_yield_still_poisoning_the_pool(tmp_path):
    from chanlun.screening.prepared_scan import pipelined_scan
    stopped = threading.Event()
    allow_failure = threading.Event()
    class FakePool(ReusableWorkerPool):
        def scan(self, *args, stop_source, **kwargs):
            stop_source()
            stopped.set()
            yield None, []
            allow_failure.set()
            # No later result poll can observe the producer's delayed failure.
    def prepare(stocks, *args, stop_requested, **kwargs):
        yield {'state': 'running'}
        assert stopped.wait(timeout=2) and stop_requested()
        assert allow_failure.wait(timeout=2)
        raise PermissionError('late preparation owner teardown failure')
    pool = FakePool(REV)
    try:
        with pytest.raises(RuntimeError, match='preparation cleanup failed') as caught:
            list(pipelined_scan([{'code': 'SH.600000', 'name': 'fixture'}],
                 {'frequencies': ['5m']}, {}, tmp_path, 1, REV,
                 request={}, prepare=prepare, worker_pool=pool))
        assert isinstance(caught.value.__cause__, PermissionError)
        assert pool.closed
    finally:
        allow_failure.set()
        pool.close()


@pytest.mark.skipif(os.name != 'nt', reason='Windows process identity and job ownership')
@pytest.mark.parametrize('failure_at', ['before_process_close', 'after_process_close'])
def test_failed_preparation_retirement_keeps_owner_and_closed_close_retries_it(tmp_path, monkeypatch, failure_at):
    from chanlun.screening.preparation_worker import PreparationWorker
    from chanlun.screening.worker_lifetime import process_identity
    from tests.test_screening_preparation_worker import echo
    worker = PreparationWorker(target=echo)
    worker.start(['warm'])
    worker.recv()
    assert worker.recv() == {'done': True}
    worker.finish(successful=True)
    old = worker.process
    old_pid = old.pid
    birth = process_identity(old_pid)['birth']
    original_stop = runner._stop_worker
    try:
        with monkeypatch.context() as fault:
            fault.setattr(worker.job, 'close', lambda: None)
            def denied(slot):
                if failure_at == 'after_process_close':
                    original_stop(slot)
                raise PermissionError('owned child teardown denied')
            fault.setattr(runner, '_stop_worker', denied)
            with pytest.raises(PermissionError, match='teardown denied'):
                worker.close()
            assert worker.closed and worker.process is old
            if failure_at == 'before_process_close':
                assert old.is_alive()
            else:
                with pytest.raises(ValueError, match='closed'):
                    _ = old.pid
            assert worker._retiring_pid == old_pid and worker._retiring_birth == birth
            with pytest.raises(RuntimeError, match='unavailable'):
                worker.start(['forbidden'])
        worker.close()  # closed=True must not skip reaping the retained owner.
        assert worker.process is None and worker.connection is None and worker.job is None
        assert process_identity(old_pid)['alive'] is False
    finally:
        worker.close()
