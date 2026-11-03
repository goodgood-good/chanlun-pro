"""Continue a stopped native-period scan at its original market cutoff.

This coordinator is outside the calculation package: changing scheduling must
not give the preserved prefix a different calculation identity. The ordinary
worker pool still validates source revisions and owns resource/cancel limits.
"""
import argparse
from contextlib import ExitStack
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from uuid import uuid4

from chanlun.persistence.file_lock import interprocess_file_lock
from chanlun.screening import runner
from chanlun.screening.period_tasks import PeriodProgress, period_tasks
from chanlun.screening.state_io import read_state_json
from chanlun.screening.worker_lifetime import process_identity
from chanlun.screening.worker_transport import SCHEMA, _read
from .screening_lifecycle import transition_lock


LOG = logging.getLogger(__name__)
_EXECUTION_FIELDS = {'deadline_at', 'deadline_kind', 'external_notifications', 'resume_ticket'}


def read(path):
    return read_state_json(path)


def identity(item):
    return item.get('market', 'a'), item['code'], tuple(item.get('requested_frequencies', item.get('_monitor_periods', ())))


def prepare(directory, state, request, revision, *, allow_uncommitted_tail=False):
    """Authenticate the saved job and check every committed period once."""
    settings = request['settings']
    if (state.get('source_revision') != revision or request.get('run_id') != directory.name
            or state.get('run_id') != directory.name or settings != state.get('settings')
            or request.get('observed_at') != state.get('observed_at')
            or request.get('incremental_periods') is not True
            or settings.get('alert_mode') != 'native_center_signals'
            or settings.get('strategy') != runner.DIRECT_STRATEGY):
        raise ValueError('当前算法或选股条件与原批次不一致，不能混合续算')
    completed, total = state.get('completed'), state.get('total')
    if type(completed) is not int or type(total) is not int or not 0 <= completed <= total:
        raise ValueError('原批次进度无效')
    universe = read(directory/'universe.json')
    tasks = period_tasks(universe, settings['frequencies'])
    pending = {identity(stock): stock for stock in tasks}
    if len(tasks) != total or len(pending) != total:
        raise ValueError('原批次标的周期名单不完整或重复')
    job = None
    semantic = {key: value for key, value in request.items() if key not in _EXECUTION_FIELDS}
    def original_stocks(values):
        # Preparation augments the already saved universe with validated
        # source-window receipts. Keep them for replay, but they do not change
        # which symbol/period the original task requested.
        return [{key: item for key, item in stock.items() if key != '_completed_inputs'} for stock in values]
    for path in directory.glob('worker_inputs.*.json'):
        try:
            value, _ = _read({'file': str(path), 'sha256': path.name.split('.')[1]}, directory, 'worker_inputs')
            if (value.get('schema') == SCHEMA and value.get('run_dir') == str(directory.resolve())
                    and value.get('revision') == revision and original_stocks(value.get('stocks', [])) == original_stocks(tasks)
                    and value.get('settings') == settings
                    and {key: val for key, val in value['request'].items() if key not in _EXECUTION_FIELDS} == semantic):
                job = value
                break
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            continue
    if job is None:
        raise ValueError('缺少校验通过的原始任务上下文，不能重建或更换行情截止')
    pending = {identity(stock): stock for stock in job['stocks']}
    progress = PeriodProgress(universe, settings['frequencies'])
    counts = dict(selected_count=0, error_count=0, successful_period_count=0,
                  reused_combinations=0, reused_selections=0, symbol_seconds_total=0.)
    digest = hashlib.sha256()
    with (directory/'results.jsonl').open('rb') as stream:
        for _ in range(completed):
            line = stream.readline()
            record = json.loads(line)
            key = identity(record)
            if key not in pending:
                raise ValueError('已提交记录不属于原批次或存在重复周期')
            rows = record['rows']
            if (not isinstance(rows, list) or len(rows) > 1
                    or any((row.get('market', 'a'), row['code'], (row['frequency'],)) != key for row in rows)):
                raise ValueError('已提交记录的标的或周期不匹配')
            digest.update(line)
            pending.pop(key)
            progress.accept(record)
            accumulate(counts, record)
        if stream.read(1) and not allow_uncommitted_tail:
            raise ValueError('存在未提交的尾部结果，需要先核对，不能覆盖原结果')
    return job, list(pending.values()), progress, counts, digest.hexdigest()


def accumulate(state, record):
    rows = record['rows']
    seconds = record.get('seconds', 0)
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0:
        raise ValueError('计算项耗时记录无效')
    state['selected_count'] += int(any(row.get('selected') for row in rows))
    state['error_count'] += int(bool(record.get('error')) or any(row.get('data_errors') for row in rows))
    state['successful_period_count'] += runner.successful_period_count(record)
    state['reused_combinations'] += sum(bool(row.get('calculation_reused')) for row in rows)
    state['reused_selections'] += sum(bool(row.get('selection_reused')) for row in rows)
    state['symbol_seconds_total'] = round(state['symbol_seconds_total'] + seconds, 3)


def start_resume(manager, *, popen=None, automatic=False):
    """Continue the latest interrupted run at its original verified cutoff."""
    from .screening_lifecycle import pid_alive
    root = manager.root.resolve()
    with manager._lock, interprocess_file_lock(root/'worker_dispatch.lock', timeout_seconds=15), ExitStack() as transitions:
        run_id = read(root/'latest.json')['run_id']
        if not re.fullmatch(r'[0-9a-f]{32}', run_id):
            raise ValueError('选股批次编号无效')
        directory = root/run_id
        if directory.is_symlink():
            raise ValueError('选股批次目录不合法')
        transitions.enter_context(transition_lock(directory))
        state, request = read(directory/'status.json'), read(directory/'request.json')
        interrupted = state.get('status') == 'interrupted'
        legacy_noon = (state.get('status') == 'cancelled' and state.get('stop_reason') == 'time_budget'
                       and state.get('deadline_kind') == 'noon')
        if not (interrupted or legacy_noon) or (directory/'cancel').exists():
            raise ValueError('仅可接续意外中断或旧午间时间上限停止的任务')
        if automatic and not interrupted:
            raise ValueError('自动恢复不接续已取消的任务')
        pid = state.get('worker_pid')
        if automatic and (type(pid) is not int or pid <= 0):
            raise ValueError('缺少原工作进程身份，保留结果并等待核对')
        if pid is not None:
            owner = process_identity(pid) if os.name == 'nt' else {'alive': pid_alive(pid)}
            same = owner.get('birth') is None or owner.get('birth') == state.get('worker_process_birth')
            if owner['alive'] is None or owner['alive'] and same:
                raise RuntimeError('尚未确认原计算进程已退出')
        revision = runner.source_revision()
        _, remaining, _, _, prefix = prepare(directory, state, request, revision,
                                             allow_uncommitted_tail=interrupted)
        ticket, now = uuid4().hex, time.time()
        runner.write_json(directory/f'resume_checkpoint.{ticket}.json',
                          {'status': state, 'request': request, 'prefix_sha256': prefix}, durable=True)
        if interrupted:
            quarantine_uncommitted_tail(directory, state['completed'], prefix, ticket)
        updated_request = {key: value for key, value in request.items() if key not in _EXECUTION_FIELDS}
        notifications = False if legacy_noon else request.get('external_notifications', True)
        updated_request.update(resume_ticket=ticket, external_notifications=notifications)
        launch = {key: value for key, value in state.items() if key not in {
            'deadline_at', 'deadline_kind', 'stop_reason', 'error', 'finished_at', 'worker_pid', 'worker_process_birth',
            'interruption_detected_at', 'recovery_error'}}
        launch.update(status='starting', phase='resuming', updated_at=now, resumed_at=now,
                      resume_count=state.get('resume_count', 0)+1, resume_ticket=ticket,
                      resume_from_completed=state['completed'], resume_remaining=len(remaining),
                      resume_prefix_sha256=prefix, external_notifications=notifications,
                      active_elapsed_seconds=state.get('active_elapsed_seconds', state.get('elapsed_seconds', 0)))
        if automatic:
            launch['automatic_resume_times'] = [*state.get('automatic_resume_times', []), now][-3:]
        runner.write_json(directory/'request.json', updated_request, durable=True)
        runner.write_json(directory/'status.json', launch, durable=True)
        project = Path(__file__).resolve().parents[4]
        env = {**os.environ, 'PYTHONPATH': os.pathsep.join((str(project/'src'), str(project/'web/chanlun_chart'))),
               'PYTHONIOENCODING': 'utf-8', 'OMP_NUM_THREADS': '1', 'OPENBLAS_NUM_THREADS': '1', 'MKL_NUM_THREADS': '1'}
        try:
            with (directory/'worker.log').open('ab') as log:
                process = (popen or subprocess.Popen)(
                    [sys.executable, '-m', 'cl_app.services.screening_resume', '--run-dir', str(directory), '--ticket', ticket],
                    cwd=project, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                    creationflags=(subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS) if os.name == 'nt' else 0)
        except Exception:
            runner.write_json(directory/'request.json', request, durable=True)
            runner.write_json(directory/'status.json', state, durable=True)
            raise
        manager._process = process
    if manager._warm_results:
        try:
            threading.Thread(target=manager._warm_after_run, args=(directory, process), daemon=True,
                             name='screening-resume-results').start()
        except Exception:
            LOG.exception('Result warmup could not start for resumed screening')
    return launch


def quarantine_uncommitted_tail(directory, completed, prefix, ticket):
    """Preserve bytes never covered by published progress before recomputing them."""
    output = directory/'results.jsonl'
    with output.open('r+b') as stream:
        digest = hashlib.sha256()
        for _ in range(completed):
            line = stream.readline()
            if not line.endswith(b'\n'):
                raise ValueError('已提交结果不完整，不能截断')
            digest.update(line)
        boundary = stream.tell()
        tail = stream.read()
        if digest.hexdigest() != prefix:
            raise ValueError('已提交结果在恢复前发生变化')
        if not tail:
            return
        archive = directory/f'uncommitted_tail.{ticket}.bin'
        with archive.open('xb') as backup:
            backup.write(tail)
            backup.flush()
            os.fsync(backup.fileno())
        runner.write_json(directory/f'uncommitted_tail.{ticket}.json',
            {'bytes': len(tail), 'sha256': hashlib.sha256(tail).hexdigest(), 'committed_bytes': boundary,
             'committed_sha256': prefix, 'completed': completed}, durable=True)
        stream.truncate(boundary)
        stream.flush()
        os.fsync(stream.fileno())


def run_resume(directory, ticket):
    with interprocess_file_lock(directory/'resume.lock', timeout_seconds=.1):
        with transition_lock(directory):
            state, request = read(directory/'status.json'), read(directory/'request.json')
            if (state.get('status') != 'starting' or state.get('resume_ticket') != ticket
                    or request.get('resume_ticket') != ticket):
                raise ValueError('续算进程已失去当前任务的所有权')
            began = time.time()
            active_before = state.get('active_elapsed_seconds', 0)
            state.update(status='running', worker_pid=os.getpid(), current_codes=[])
            if os.name == 'nt':
                owner = process_identity(os.getpid())
                if owner['alive'] is not True or not owner['birth']:
                    raise RuntimeError('无法核对续算进程身份')
                state['worker_process_birth'] = owner['birth']
            def publish():
                now = time.time()
                state.update(updated_at=now, elapsed_seconds=round(now-state['started_at'], 1),
                             active_elapsed_seconds=round(active_before+now-began, 1))
                runner.write_json(directory/'status.json', state)
            publish()
        stream = None
        try:
            revision = runner.source_revision()
            job, remaining, progress, counts, prefix = prepare(directory, state, request, revision)
            if prefix != state['resume_prefix_sha256']:
                raise ValueError('已完成结果在续算启动前发生变化')
            state.update(counts, phase='screening')
            from chanlun.screening.compute_capacity import worker_capacity
            workers = worker_capacity(request['settings'].get('workers', 16))
            def capacity(effective, pressure):
                state.update(effective_workers=effective, resource_limit=pressure['limit'],
                             resource_mode='waiting' if pressure.get('waiting') else 'limited' if pressure['limit']<workers else 'normal',
                             available_memory_mb=pressure.get('available_mb'))
            stream = runner.bounded_scan(remaining, request['settings'], job['contexts'], directory, workers,
                                         revision, capacity_callback=capacity)
            for result, current in stream:
                if result is not None:
                    finished = progress.accept(result)
                    with (directory/'results.jsonl').open('a', encoding='utf-8') as target:
                        target.write(json.dumps(result, ensure_ascii=False, allow_nan=False)+'\n')
                    state['completed'] += 1
                    accumulate(state, result)
                    state.update(completed_symbols=finished, period_progress=progress.snapshot())
                    for row in result['rows']:
                        cutoff = row.get('source_closed_at')
                        periods = state.get('market_cutoffs', {}).get(row.get('market', result.get('market', 'a')), {})
                        if row.get('frequency') in periods and type(cutoff) is int:
                            periods[row['frequency']] = max(cutoff, periods[row['frequency']] or 0)
                state['current_codes'] = current
                publish()
            if (directory/'cancel').exists():
                state.update(status='cancelled', stop_reason='user_cancelled')
            elif state['completed'] == state['total']:
                state['status'] = 'completed'
            else:
                raise RuntimeError('续算进程退出但尚有未提交的周期')
        except Exception as error:
            LOG.exception('Screening resume failed')
            state.update(status='failed', error=f'{type(error).__name__}: {error}')
        finally:
            if stream is not None and hasattr(stream, 'close'):
                try:
                    stream.close()
                except Exception as error:
                    LOG.exception('Screening resume worker cleanup failed')
                    state.update(status='failed', error=f'{type(error).__name__}: {error}')
        state.update(phase='finished', current_codes=[], finished_at=time.time())
        publish()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--ticket', required=True)
    options = parser.parse_args()
    run_resume(options.run_dir.resolve(), options.ticket)
