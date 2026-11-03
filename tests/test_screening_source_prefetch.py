"""Idle source preparation cannot admit computations or delay a newer owner."""
from copy import deepcopy
from datetime import datetime
import json
import os
from types import SimpleNamespace

import pytest

from chanlun.screening import source_prefetch as prefetch, tail_preparation
from chanlun.screening.preparation_worker import PreparationWorker
from chanlun.screening.runner import write_json
from chanlun.screening.rules import CN
from chanlun.screening.timeframe_policy import POLICY
from chanlun.screening.worker_host import STOP_FILE
from chanlun.screening.worker_lifetime import process_identity
from tests.test_screening_preparation_worker import fixture_server
from tests.web.test_screening_worker_runtime import dispatcher as dispatcher

MAIN = int(datetime(2026, 10, 29, 14, 0, tzinfo=CN).timestamp())
REVISION = 'a' * 64


def symbols(count, first=600000):
    return [{'market': 'a', 'code': f'SH.{first + i}', 'name': 'fixture'} for i in range(count)]


def fixture_values(tmp_path, count=4, owner_count=64):
    root = tmp_path / 'runs';directory = root / ('1' * 32)
    control = {'schema': prefetch.SCHEMA, 'enabled': True, 'generation': 'one', 'version': 1,
               'seed': 'seed', 'scope': 'b' * 64}
    hint = {'schema': prefetch.HINT_SCHEMA, 'admission': {'schema': prefetch.ADMISSION_SCHEMA,
                'kind': 'normal_live', 'count': owner_count, 'limit': owner_count,
                'ordinary_5m_keys': [f"a:{s['code']}" for s in symbols(owner_count)], 'priority_keys': []}, 'control': control, 'cutoff': MAIN, 'expires_at': MAIN + 300,
            'settings': {'recent_sessions': 5, 'max_anchor_sessions': 20, 'timeframe_policy': POLICY},
            'symbols': symbols(count, 601000)}
    request = {'run_id': directory.name, 'settings': {'scope': 'symbols', 'symbols': symbols(owner_count),
               'frequencies': ['5m'], 'timeframe_policy': POLICY},
               'worker_runtime': {'generation': 'one', 'source_revision': REVISION}, 'source_prefetch': hint}
    hint['admission']['members_sha256'] = prefetch.members_digest(request['settings'])
    write_json(root / 'latest.json', {'run_id': directory.name})
    write_json(root / prefetch.CONTROL_FILE, control)
    write_json(directory / 'status.json', {'status': 'completed', 'completed': owner_count, 'total': owner_count})
    return root, directory, request


@pytest.fixture
def idle_owner(tmp_path, monkeypatch):
    root, directory, request = fixture_values(tmp_path)
    monkeypatch.setenv('CHANLUN_PREPARATION_TEST_LOG', str(tmp_path / 'downloads.txt'))
    worker = PreparationWorker(target=fixture_server)
    worker.start([])
    assert worker.recv() == {'done': True}
    worker.finish(successful=True)
    pool = SimpleNamespace(busy=False, closed=False, preparation_worker=worker)
    try:
        yield root, directory, request, pool
    finally:
        worker.close()


def run(owner, **kwargs):
    root, directory, request, pool = owner
    prefetch.run_idle_prefetch(root, directory, request, pool, REVISION, 'one',
                              revision_reader=lambda: REVISION, now=lambda: MAIN + 10, **kwargs)
    return json.loads((root / prefetch.STATUS_FILE).read_text())


def test_warmup_reuses_owned_child_then_normal_inputs_reuse_identical_proofs(idle_owner, tmp_path):
    root, directory, request, pool = idle_owner
    pid = pool.preparation_worker.process.pid
    before = (directory / 'status.json').read_bytes()
    status = run(idle_owner)
    assert status['stop_reason'] == 'completed' and status['worker_released']
    assert status['prepared_symbols'] == 4 and status['receipt_count'] == 12
    assert pool.preparation_worker.process.pid == pid
    assert (directory / 'status.json').read_bytes() == before
    assert not (directory / 'results.jsonl').exists()
    assert json.loads((root / 'latest.json').read_text()) == {'run_id': directory.name}
    before_downloads = (tmp_path / 'downloads.txt').read_bytes()
    stocks = [{**s, '_monitor_periods': ['5m']} for s in request['source_prefetch']['symbols']]
    settings = {**request['source_prefetch']['settings'], 'scope': 'symbols', 'frequencies': ['5m']}
    context = {'5m': {'cutoff': MAIN}}
    list(tail_preparation.prepare_inputs(stocks, settings, context, root / 'analysis_cache', directory,
                                        worker=pool.preparation_worker))
    assert (tmp_path / 'downloads.txt').read_bytes() == before_downloads
    # A cold path still generates the same data-bound proofs. checked_at is a measurement.
    cold = [{**s, '_monitor_periods': ['5m']} for s in request['source_prefetch']['symbols']]
    list(tail_preparation.prepare_inputs(cold, settings, context, tmp_path / 'cold-cache', directory,
                                        worker=pool.preparation_worker))
    def facts(items):
        return [{p: {k: v for k, v in proof.items() if k != 'checked_at'}
                 for p, proof in s['_completed_inputs'].items()} for s in items]
    assert facts(stocks) == facts(cold)


def test_formal_holding_run_revokes_warmup_and_preserves_committed_partial_receipts(idle_owner):
    root, directory, request, pool = idle_owner
    old_pid = pool.preparation_worker.process.pid
    new = root / ('2' * 32)
    def prepare(*args, **kwargs):
        stream = tail_preparation.prepare_inputs(*args, **kwargs)
        try:
            for progress in stream:
                yield progress
                if progress.get('completed', 0) > 0:
                    write_json(new / 'status.json', {'status': 'starting', 'holding': True})
                    write_json(root / 'latest.json', {'run_id': new.name})
        finally:
            stream.close()
    status = run(idle_owner, prepare=prepare)
    assert status['stop_reason'] == 'new_run' and status['worker_released']
    assert status['receipt_count'] >= 4
    assert pool.preparation_worker.process is None
    assert process_identity(old_pid)['alive'] is False
    assert json.loads((new / 'status.json').read_text()) == {'status': 'starting', 'holding': True}
    assert all(tail_preparation.saved_preparation(root / 'analysis_cache', s['code'], '1m', MAIN)
               for s in request['source_prefetch']['symbols'])


@pytest.mark.parametrize('change,reason', [('stop', 'stop_requested'), ('control', 'control_changed'),
    ('generation', 'control_changed'), ('pool', 'control_changed'), ('missing_control', 'control_changed'),
    ('new_run', 'new_run'), ('source', 'source_changed'), ('expired', 'cutoff_expired'),
    ('future', 'cutoff_expired'), ('unknown_context', 'unknown_context'), ('missing_context', 'unknown_context')])
def test_stale_or_revoked_hint_never_starts_preparation(tmp_path, change, reason):
    owner = list(fixture_values(tmp_path))
    root, directory, request = owner
    worker = SimpleNamespace(closed=False, identity=None, process=SimpleNamespace(is_alive=lambda: True))
    owner.append(SimpleNamespace(busy=False, closed=False, preparation_worker=worker))
    kwargs = {'revision_reader': lambda: REVISION, 'now': lambda: MAIN + 10}
    if change == 'stop': write_json(root / STOP_FILE, {'stop': True})
    elif change in ('control', 'generation', 'pool'):
        control = dict(request['source_prefetch']['control'])
        control[{'control': 'version', 'generation': 'generation', 'pool': 'scope'}[change]] = {'control': 2, 'generation': 'two', 'pool': 'c' * 64}[change]
        write_json(root / prefetch.CONTROL_FILE, control)
    elif change == 'missing_control': (root / prefetch.CONTROL_FILE).unlink()
    elif change == 'new_run': write_json(root / 'latest.json', {'run_id': '2' * 32})
    elif change == 'source': kwargs['revision_reader'] = lambda: 'd' * 64
    elif change == 'expired': kwargs['now'] = lambda: MAIN + 300
    elif change == 'future': kwargs['now'] = lambda: MAIN - 1
    elif change == 'unknown_context': kwargs['context_reader'] = lambda *args: {'calendar_error': 'unavailable'}
    elif change == 'missing_context': kwargs['context_reader'] = lambda *args: None
    def forbidden(*args, **kwargs): pytest.fail('stale hint reached preparation')
    prefetch.run_idle_prefetch(*owner, REVISION, 'one', prepare=forbidden, **kwargs)
    status = json.loads((root / prefetch.STATUS_FILE).read_text())
    assert status['stop_reason'] == reason and status['receipt_count'] == 0


@pytest.mark.parametrize('action,reason', [('stop', 'stop_requested'), ('control', 'control_changed'), ('source', 'source_changed')])
def test_revocation_during_preparation_reaps_child_before_return(idle_owner, action, reason):
    root, directory, request, pool = idle_owner
    revision = [REVISION];old_pid = pool.preparation_worker.process.pid
    def prepare(*args, **kwargs):
        stream = tail_preparation.prepare_inputs(*args, **kwargs)
        try:
            for progress in stream:
                yield progress
                if progress.get('worker_pid'):
                    if action == 'stop': write_json(root / STOP_FILE, {'stop': True})
                    elif action == 'control': write_json(root / prefetch.CONTROL_FILE, {})
                    else: revision[0] = 'd' * 64
        finally: stream.close()
    prefetch.run_idle_prefetch(root, directory, request, pool, REVISION, 'one',
        revision_reader=lambda: revision[0], now=lambda: MAIN + 10, prepare=prepare)
    status = json.loads((root / prefetch.STATUS_FILE).read_text())
    assert status['stop_reason'] == reason and status['worker_released']
    assert pool.preparation_worker.process is None
    assert process_identity(old_pid)['alive'] is False


def test_an_unreaped_preparation_cannot_allow_next_formal_run(idle_owner):
    root, directory, request, pool = idle_owner
    worker = pool.preparation_worker
    def broken(*args, **kwargs):
        worker.identity = 'unreleased'
        yield {'state': 'running'}
    try:
        with pytest.raises(RuntimeError, match='did not release'):
            run(idle_owner, prepare=broken)
    finally:
        worker.identity = None
    assert json.loads((root / prefetch.STATUS_FILE).read_text())['worker_released'] is False


@pytest.mark.parametrize('failure_at', ['iteration', 'close'])
@pytest.mark.parametrize('remaining', [True, None])
def test_teardown_error_with_live_or_unknown_owner_is_fatal(tmp_path, failure_at, remaining):
    root, directory, request = fixture_values(tmp_path)
    worker = SimpleNamespace(closed=False, identity=None, started_processes=1,
                             process=SimpleNamespace(pid=42, is_alive=lambda: True))
    pool = SimpleNamespace(busy=False, closed=False, preparation_worker=worker)
    calls = [0]
    def identity(pid):
        calls[0] += 1
        return {'alive': True, 'birth': 'original'} if calls[0] == 1 else {'alive': remaining, 'birth': 'original' if remaining else None}
    class BrokenStream:
        def __iter__(self): return self
        def __next__(self):
            if failure_at == 'iteration':
                worker.process = None
                raise PermissionError('teardown failed')
            raise StopIteration
        def close(self):
            worker.process = None
            if failure_at == 'close': raise PermissionError('teardown failed')
    with pytest.raises(RuntimeError, match='did not release'):
        prefetch.run_idle_prefetch(root, directory, request, pool, REVISION, 'one',
            revision_reader=lambda: REVISION, now=lambda: MAIN+10,
            identity_reader=identity, prepare=lambda *a, **k: BrokenStream())
    assert json.loads((root / prefetch.STATUS_FILE).read_text())['worker_released'] is False


def test_control_file_changes_only_with_control_or_pool_inputs(tmp_path, monkeypatch):
    calls=[]
    monkeypatch.setattr('chanlun.screening.runner.write_json', lambda *a, **k: calls.append((a, k)))
    state={'enabled': True, 'seed': {'run_id': 'seed', 'codes': ['a:SH.600000'], 'settings': {}}}
    previous=None
    for signal_count in range(4):
        state['events']=[{}] * signal_count
        value=prefetch.control_value(state, 1, 'one')
        previous=prefetch.publish_control(tmp_path, value, previous)
    assert len(calls)==1
    state['seed']['codes'].append('a:SH.600001')
    changed=prefetch.control_value(state, 1, 'one')
    prefetch.publish_control(tmp_path, changed, previous)
    assert len(calls)==2 and changed!=previous


def test_hint_is_bounded_and_never_advances_normal_plan_or_includes_urgent_members():
    all_symbols=symbols(140);keys=[f"a:{s['code']}" for s in all_symbols]
    state={'enabled':True,'holding_symbols':[keys[65]],'seed':{'run_id':'seed','codes':keys,'settings':{}}}
    plan={'symbols':all_symbols,'periods':{k:['5m'] for k in keys},'targets':{f'{k}:5m':MAIN for k in keys},
          'recent':set(keys),'urgent':{keys[66]},'now':MAIN+10,'state':{},'cursor':1}
    launch={'mode':'live','settings':{'symbols':all_symbols[:64],'workers':16,'recent_sessions':5,
            'max_anchor_sessions':20,'timeframe_policy':POLICY,'frequencies':['5m']},
            'options':{},'fair_plan':plan,'fair_cursor':2,'critical_cursor':1}
    before=deepcopy(launch)
    def select(plan, capacity):
        return ([{'key':f"a:{s['code']}",'symbol':s,'frequency':'5m'} for s in plan['symbols']],99,99)
    from cl_app.services.signal_monitor import SignalMonitor
    hint=prefetch.make_hint(launch,state,prefetch.control_value(state,1,'one'),select, batch_limit=SignalMonitor._batch_limit)
    assert len(hint['symbols'])==64 and launch==before
    assert not ({s['code'] for s in hint['symbols']} & {s['code'] for s in all_symbols[:64]})
    assert all(f"a:{s['code']}" not in {keys[65],keys[66]} for s in hint['symbols'])
    launch['settings'].pop('timeframe_policy')
    assert prefetch.make_hint(launch,state,prefetch.control_value(state,1,'one'),select, batch_limit=SignalMonitor._batch_limit) is None


@pytest.mark.parametrize('owner_count', [36, 40, 44, 48, 60, 64])
def test_dispatch_transports_hint_without_growing_the_formal_batch(dispatcher, tmp_path, owner_count):
    manager, popen = dispatcher
    _, _, request = fixture_values(tmp_path / 'fixture', count=64, owner_count=owner_count)
    hint = request['source_prefetch']
    hint['control']['generation'] = 'generation-one'
    launched = manager.start({'scope': 'symbols', 'symbols': symbols(owner_count),
                              'timeframe_policy': POLICY, 'frequencies':['1m','5m','30m']},
                             due_periods={f"a:{s['code']}":['5m'] for s in symbols(owner_count)},
                             holding_snapshot=[], source_prefetch=hint)
    saved = json.loads((manager.root / launched['run_id'] / 'request.json').read_text())
    assert len(saved['settings']['symbols']) == owner_count
    assert saved['source_prefetch'] == hint
    assert popen.call_count == 1


def test_new_control_revokes_hint_even_if_following_state_commit_fails(tmp_path, monkeypatch):
    from cl_app.services import signal_monitor
    monkeypatch.setenv('CHANLUN_BUILD_REVISION', 'one')
    monitor = signal_monitor.SignalMonitor(tmp_path)
    state = {'enabled': True, 'seed': {'run_id': 'seed', 'codes': ['a:SH.600000'], 'settings': {}}}
    previous = prefetch.control_value(state, 1, 'one')
    write_json(tmp_path / 'runs' / prefetch.CONTROL_FILE, previous)
    monitor._source_prefetch_control = previous
    monitor._control_version = 2
    monitor._state = {**state, 'enabled': False}
    monkeypatch.setattr(signal_monitor, 'prune_diagnostics', lambda state: False)
    def denied(*args, **kwargs): raise OSError('state storage failed')
    monkeypatch.setattr(signal_monitor, 'write_json', denied)
    with pytest.raises(OSError): monitor._save()
    changed = json.loads((tmp_path / 'runs' / prefetch.CONTROL_FILE).read_text())
    assert changed['enabled'] is False and changed['version'] == 2
    assert changed != previous and monitor._state is None


def test_host_cannot_dispatch_new_run_before_prefetch_releases_owner(tmp_path, monkeypatch):
    from chanlun.screening import worker_host
    order=[]
    class Pool:
        busy=False
        closed=False
        def snapshot(self): return {'worker_pids': []}
        def close(self): order.append('close')
    def enqueue(number):
        directory=tmp_path / (str(number) * 32)
        request={'run_id':directory.name,'settings':{'symbols':symbols(64)},
                 'worker_runtime':{'schema':worker_host.SCHEMA,'generation':'one','source_revision':REVISION}}
        write_json(directory/'request.json',request)
        write_json(directory/'status.json',{**request,'status':'starting'})
        write_json(tmp_path/'latest.json',{'run_id':directory.name})
        return directory
    first=enqueue(1)
    def actual_run(directory, **kwargs):
        if directory.name.startswith('2'): assert order[-1]=='released'
        order.append('run'+directory.name[0])
        write_json(directory/'status.json',{'run_id':directory.name,'status':'completed','completed':64,'total':64})
        if directory.name.startswith('2'): write_json(tmp_path/STOP_FILE,{'stop':True})
    def warmup(root, directory, *args, **kwargs):
        if directory==first:
            order.append('prefetch')
            enqueue(2)
            order.append('released')
    monkeypatch.setattr(prefetch,'run_idle_prefetch',warmup)
    worker_host.serve(tmp_path,'one',revision_reader=lambda:REVISION,pool_factory=lambda _:Pool(),run=actual_run)
    assert order==['run1','prefetch','released','run2','close']
