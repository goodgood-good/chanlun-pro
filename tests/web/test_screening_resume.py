"""Resume preserves committed work and its original input identity/cutoff."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from chanlun.screening.period_tasks import period_tasks
from chanlun.screening.worker_transport import store_job
from cl_app.services.screening import ScreeningManager
from cl_app.services import screening_resume as resume


def result(stock):
    return {'market': stock['market'], 'code': stock['code'], 'seconds': 1.25,
            'requested_frequencies': stock['_monitor_periods'],
            'rows': [{'market': stock['market'], 'code': stock['code'],
                      'frequency': stock['_monitor_periods'][0], 'source_closed_at': 123,
                      'selected': [], 'data_errors': []}]}


@pytest.fixture
def stopped(tmp_path, monkeypatch):
    directory = tmp_path / ('a' * 32)
    directory.mkdir()
    universe = [{'market': 'a', 'code': code} for code in ['SH.600000', 'SZ.000001']]
    settings = {'strategy': resume.runner.DIRECT_STRATEGY, 'alert_mode': 'native_center_signals',
                'frequencies': ['5m', '30m'], 'workers': 2}
    request = {'run_id': directory.name, 'settings': settings, 'incremental_periods': True,
               'observed_at': '2026-10-13T11:35:02+08:00', 'deadline_at': 200, 'deadline_kind': 'noon'}
    state = {**request, 'source_revision': 'revision', 'status': 'cancelled', 'phase': 'finished',
             'stop_reason': 'time_budget', 'started_at': 100, 'finished_at': 200,
             'elapsed_seconds': 100, 'completed': 1, 'total': 4, 'completed_symbols': 0,
             'market_cutoffs': {'a': {'5m': 123, '30m': None}}}
    contexts = {'5m': {'cutoff': 123}, '30m': {'cutoff': 123}}
    tasks = period_tasks(universe, settings['frequencies'])
    for filename, value in [('request.json', request), ('status.json', state), ('universe.json', universe)]:
        resume.runner.write_json(directory / filename, value)
    resume.runner.write_json(tmp_path / 'latest.json', {'run_id': directory.name})
    store_job(tasks, settings, contexts, directory, 'revision', request)
    output = directory / 'results.jsonl'
    output.write_text(json.dumps(result(tasks[0])) + '\n', encoding='utf-8')
    monkeypatch.setattr(resume.runner, 'source_revision', lambda: 'revision')
    monkeypatch.setattr(resume, 'process_identity', lambda pid: {'alive': True, 'birth': 'test-owner'})
    monkeypatch.setattr('cl_app.services.screening_lifecycle.pid_alive', lambda pid: True)
    return SimpleNamespace(directory=directory, state=state, request=request, contexts=contexts,
                           tasks=tasks, output=output, prefix=output.read_bytes(),
                           manager=ScreeningManager(tmp_path, warm_results=False))


def test_resume_only_remaining_tasks_at_original_cutoff_and_keeps_prefix(stopped, monkeypatch):
    case = stopped
    launched = Mock()
    start = resume.start_resume(case.manager, popen=launched)
    request = resume.read(case.directory / 'request.json')
    assert case.output.read_bytes() == case.prefix
    assert request['observed_at'] == case.request['observed_at']
    assert request['external_notifications'] is False
    assert 'deadline_at' not in request and 'deadline_at' not in start
    assert start['completed'] == 1 and start['resume_remaining'] == 3
    assert resume.read(next(case.directory.glob('resume_checkpoint.*.json')))['status'] == case.state
    assert 'cl_app.services.screening_resume' in launched.call_args.args[0]

    def scan(tasks, settings, contexts, directory, workers, revision, *, capacity_callback):
        assert tasks == case.tasks[1:]
        assert contexts == case.contexts
        assert directory == case.directory and revision == 'revision'
        capacity_callback(1, {'limit': 1, 'available_mb': 2100})
        for task in tasks:
            yield result(task), []
    monkeypatch.setattr(resume.runner, 'bounded_scan', scan)
    resume.run_resume(case.directory, start['resume_ticket'])
    state = resume.read(case.directory / 'status.json')
    records = [json.loads(line) for line in case.output.read_text(encoding='utf-8').splitlines()]
    assert case.output.read_bytes().startswith(case.prefix)
    assert {resume.identity(row) for row in records} == {resume.identity(row) for row in case.tasks}
    assert len(records) == state['completed'] == state['total'] == 4
    assert state['status'] == 'completed' and state['completed_symbols'] == 2
    assert state['period_progress'] == {'5m': {'completed': 2, 'total': 2}, '30m': {'completed': 2, 'total': 2}}
    assert state['successful_period_count'] == 4 and state['error_count'] == 0
    assert state['market_cutoffs'] == {'a': {'5m': 123, '30m': 123}}
    assert state['effective_workers'] == 1 and state['resource_mode'] == 'limited'
    assert 100 <= state['active_elapsed_seconds'] < 200
    assert state['started_at'] == case.state['started_at']


@pytest.mark.parametrize('damage', ['revision', 'job_checksum', 'duplicate', 'wrong_period', 'missing', 'tail', 'cancel'])
def test_refuses_unverifiable_or_cancelled_prefix_without_modification(stopped, monkeypatch, damage):
    case = stopped
    if damage == 'revision':
        monkeypatch.setattr(resume.runner, 'source_revision', lambda: 'different')
    elif damage == 'job_checksum':
        next(case.directory.glob('worker_inputs.*.json')).write_text('{}', encoding='utf-8')
    elif damage == 'duplicate':
        case.output.write_bytes(case.prefix + case.prefix)
        resume.runner.write_json(case.directory / 'status.json', {**case.state, 'completed': 2})
    elif damage == 'wrong_period':
        record = result(case.tasks[0])
        record['rows'][0]['frequency'] = '1m'
        case.output.write_text(json.dumps(record) + '\n', encoding='utf-8')
    elif damage == 'missing':
        case.output.write_bytes(b'')
    elif damage == 'tail':
        case.output.write_bytes(case.prefix + b'{uncommitted')
    else:
        (case.directory / 'cancel').touch()
    before = case.output.read_bytes()
    state = (case.directory / 'status.json').read_bytes()
    launch = Mock()
    with pytest.raises(ValueError):
        resume.start_resume(case.manager, popen=launch)
    assert case.output.read_bytes() == before
    assert (case.directory / 'status.json').read_bytes() == state
    launch.assert_not_called()


def test_failed_launch_restores_original_stopped_state(stopped):
    case = stopped
    with pytest.raises(OSError, match='cannot launch'):
        resume.start_resume(case.manager, popen=Mock(side_effect=OSError('cannot launch')))
    assert resume.read(case.directory / 'status.json') == case.state
    assert resume.read(case.directory / 'request.json') == case.request
    assert case.output.read_bytes() == case.prefix


def test_resume_authenticates_original_universe_and_retains_preparation_receipts(stopped):
    case=stopped
    for path in case.directory.glob('worker_inputs.*.json'):
        path.unlink()
    annotated=[{**stock,'_completed_inputs':{'5m':{'input_key':'a'*64}}} for stock in case.tasks]
    store_job(annotated,case.request['settings'],case.contexts,case.directory,'revision',case.request)
    job,remaining,_,_,prefix=resume.prepare(case.directory,case.state,case.request,'revision')
    assert len(remaining)==3 and all(stock.get('_completed_inputs') for stock in remaining)
    assert [resume.identity(stock) for stock in remaining]==[resume.identity(stock) for stock in case.tasks[1:]]
    assert job['stocks']==annotated


def test_manual_cancel_during_resume_keeps_progress_and_stops(stopped, monkeypatch):
    case = stopped
    state = resume.start_resume(case.manager, popen=Mock())
    def scan(*args, **kwargs):
        yield result(case.tasks[1]), []
        (case.directory / 'cancel').touch()
    monkeypatch.setattr(resume.runner, 'bounded_scan', scan)
    resume.run_resume(case.directory, state['resume_ticket'])
    final = resume.read(case.directory / 'status.json')
    assert final['status'] == 'cancelled' and final['stop_reason'] == 'user_cancelled'
    assert final['completed'] == 2 and case.output.read_bytes().startswith(case.prefix)
    with pytest.raises(ValueError):
        resume.run_resume(case.directory, state['resume_ticket'])


def test_source_change_after_launch_preserves_prefix_and_marks_failed(stopped, monkeypatch):
    case = stopped
    state = resume.start_resume(case.manager, popen=Mock())
    monkeypatch.setattr(resume.runner, 'source_revision', lambda: 'different')
    resume.run_resume(case.directory, state['resume_ticket'])
    assert resume.read(case.directory / 'status.json')['status'] == 'failed'
    assert case.output.read_bytes() == case.prefix


def test_live_original_worker_or_duplicate_launch_cannot_write(stopped):
    case = stopped
    resume.runner.write_json(case.directory / 'status.json',
                             {**case.state, 'worker_pid': 123, 'worker_process_birth': 'test-owner'})
    with pytest.raises(RuntimeError):
        resume.start_resume(case.manager, popen=Mock())
    resume.runner.write_json(case.directory / 'status.json', case.state)
    resume.start_resume(case.manager, popen=Mock())
    with pytest.raises(ValueError):
        resume.start_resume(case.manager, popen=Mock())
    assert case.output.read_bytes() == case.prefix


def test_interrupted_resume_quarantines_only_unpublished_tail_and_preserves_notifications(stopped,monkeypatch):
    case=stopped
    interrupted={**case.state,'status':'interrupted','stop_reason':None,'worker_pid':123,'worker_process_birth':'owner'}
    monkeypatch.setattr(resume,'process_identity',lambda pid:{'alive':False,'birth':None})
    resume.runner.write_json(case.directory/'status.json',interrupted)
    tail=b'{"partially_written":'
    case.output.write_bytes(case.prefix+tail)
    state=resume.start_resume(case.manager,popen=Mock(),automatic=True)
    assert state['completed']==1 and state['resume_remaining']==3
    assert state['external_notifications'] is True
    assert case.output.read_bytes()==case.prefix
    assert next(case.directory.glob('uncommitted_tail.*.bin')).read_bytes()==tail
    assert resume.read(case.directory/'request.json')['observed_at']==case.request['observed_at']


def test_recovery_resumes_same_interrupted_batch_and_records_a_known_exit(stopped,monkeypatch):
    from cl_app.services.screening_recovery import ScreeningRecovery
    case=stopped
    monkeypatch.setattr(resume,'process_identity',lambda pid:{'alive':False,'birth':None})
    state={**case.state,'status':'interrupted','worker_pid':123,'worker_process_birth':'owner'}
    resume.runner.write_json(case.directory/'status.json',state)
    observer=Mock();observer.code.return_value=0xC0000005
    launch=Mock()
    recovery=ScreeningRecovery(case.manager,now=lambda:1000,observer=lambda *args:observer,
        resume=lambda manager,automatic:resume.start_resume(manager,popen=launch,automatic=automatic))
    recovery.tick()
    assert launch.call_count==1
    assert resume.read(case.directory/'status.json')['resume_from_completed']==1
    assert resume.read(case.directory/'worker_exit.json')['exit_code_hex']=='0xC0000005'
    assert resume.read(case.directory.parent/'latest.json')['run_id']==case.directory.name
    assert case.output.read_bytes()==case.prefix


def test_recovery_does_not_restart_a_manual_cancel_or_loop_after_repeated_crashes(stopped):
    from cl_app.services.screening_recovery import ScreeningRecovery
    case=stopped
    state={**case.state,'status':'interrupted','automatic_resume_times':[970,980,990]}
    resume.runner.write_json(case.directory/'status.json',state)
    callback=Mock()
    recovery=ScreeningRecovery(case.manager,now=lambda:1000,resume=callback)
    recovery.tick()
    callback.assert_not_called()
    assert '三次' in resume.read(case.directory/'status.json')['recovery_error']
    resume.runner.write_json(case.directory/'status.json',{**state,'automatic_resume_times':[]})
    (case.directory/'cancel').touch()
    recovery.retry_after=0
    recovery.tick()
    callback.assert_not_called()


def test_recovery_retries_a_brief_windows_status_read_lock(stopped,monkeypatch):
    from cl_app.services.screening_recovery import ScreeningRecovery
    from pathlib import Path
    case=stopped
    state={**case.state,'status':'interrupted'}
    resume.runner.write_json(case.directory/'status.json',state)
    original=Path.read_text; attempts=[]
    def read(path,*args,**kwargs):
        if path==case.directory/'status.json':
            attempts.append(True)
            if len(attempts)==1:raise PermissionError('brief atomic replacement')
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,'read_text',read)
    monkeypatch.setattr('chanlun.screening.state_io.time.sleep',lambda _:None)
    callback=Mock()
    recovery=ScreeningRecovery(case.manager,now=lambda:1000,resume=callback)
    recovery.tick()
    callback.assert_called_once_with(case.manager,automatic=True)
    assert len(attempts)>=2
