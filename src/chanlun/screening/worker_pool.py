"""Bounded, cancellable worker reuse across independent monitor batches."""
from multiprocessing.connection import wait as wait_connections
from collections import deque, OrderedDict
import multiprocessing
import os
from pathlib import Path
import time
from uuid import uuid4

from .runner import SYMBOL_TIMEOUT_SECONDS, _scan_worker_symbol, _stop_worker, source_revision
from .worker_lifetime import WindowsWorkerJob
from .worker_transport import JobInputReader, bounded_message, store_job, store_reply, read_reply, discard_reply


MAX_AFFINITY_KEYS = 192
MAX_SLOT_AFFINITY_KEYS = 12


def _affinity_key(stock, settings):
    periods = stock.get('_monitor_periods', settings['frequencies'])
    if len(periods) != 1:
        return None
    return stock.get('market', 'a'), stock['code'], periods[0]


def _priority_group(stock, settings):
    periods = stock.get('_monitor_periods', settings['frequencies'])
    # The caller owns ordering (including live/retry waves). Never jump over
    # the first different contiguous group to reach a previously warm key.
    period_group = 'critical' if periods and all(p in {'5m', '30m'} for p in periods) else tuple(periods)
    return stock.get('holding') is True, stock.get('origin') == 'watchlist', period_group


def _failed_result(stock, settings, reason):
    return {'code': stock['code'], 'market': stock.get('market', 'a'), 'name': stock['name'],
            'requested_frequencies':list(stock.get('_monitor_periods',settings['frequencies'])),
            'rows': [{'code': stock['code'], 'market': stock.get('market', 'a'),
                      'name': stock['name'], 'frequency': frequency, 'selected': [],
                      'recent_rejections': [], 'reason_counts': {}, 'data_errors': [reason]}
                     for frequency in stock.get('_monitor_periods', settings['frequencies'])]}


def reusable_worker(connection):
    inputs = JobInputReader()
    try:
        while (message := connection.recv()) is not None:
            task = inputs.load(message)
            result = _scan_worker_symbol(task['stock'], task['settings'], task['contexts'],
                                         task['run_dir'], task['revision'], task['request'])
            connection.send(store_reply(task, result))
    except (EOFError, BrokenPipeError):
        pass
    finally:
        connection.close()


class ReusableWorkerPool:
    """Sequential batches, independent task parameters, bounded process ages."""
    def __init__(self, revision, *, max_tasks=256, worker_target=reusable_worker, revision_reader=source_revision):
        if type(max_tasks) is not int or max_tasks < 1:
            raise ValueError('worker task limit must be positive')
        self.revision = revision
        self.max_tasks = max_tasks
        self.worker_target = worker_target
        self.revision_reader = revision_reader
        self.slots = []
        self.busy = False
        self.closed = False
        self.job = WindowsWorkerJob() if os.name == 'nt' else None
        self.started_processes = 0
        self.completed_tasks = 0
        self._affinity = OrderedDict()
        self._affinity_sequence = 0
        self.affinity_hits = 0

    def _start(self):
        if self.revision_reader() != self.revision:
            raise RuntimeError('Screening source changed before worker initialization')
        context = multiprocessing.get_context('spawn')
        parent, child = context.Pipe()
        process = context.Process(target=self.worker_target, args=(child,))
        slot = {'process': process, 'connection': parent, 'stock': None,
                'started': 0.0, 'stopped': False, 'tasks': 0, 'affinity_id': uuid4().hex}
        try:
            process.start()
            child.close()
            if self.job is not None:
                self.job.assign(process)
        except BaseException:
            child.close()
            if process.pid is not None:
                _stop_worker(slot)
            else:
                parent.close()
            raise
        self.started_processes += 1
        return slot

    def _discard(self, slot):
        _stop_worker(slot)
        self.slots.remove(slot)
        for key, value in list(self._affinity.items()):
            if value[0] == slot['affinity_id']:
                del self._affinity[key]

    def _remember(self, slot, stock, settings):
        key = _affinity_key(stock, settings)
        if key is None or slot not in self.slots:
            return
        self._affinity_sequence += 1
        protected = stock.get('holding') is True and key[2] in {'1m', '5m', '30m'}
        self._affinity.pop(key, None)
        self._affinity[key] = (slot['affinity_id'], protected, self._affinity_sequence)
        local = [k for k, value in self._affinity.items() if value[0] == slot['affinity_id']]
        while len(local) > MAX_SLOT_AFFINITY_KEYS:
            victim = next((k for k in local if not self._affinity[k][1]), local[0])
            del self._affinity[victim]
            local.remove(victim)
        while len(self._affinity) > MAX_AFFINITY_KEYS:
            victim = next((k for k, value in self._affinity.items() if not value[1]), next(iter(self._affinity)))
            del self._affinity[victim]

    def _take_pending(self, pending, slot, settings):
        group = _priority_group(pending[0][1], settings)
        selected, recent = 0, -1
        for index, (_, stock) in enumerate(pending):
            if _priority_group(stock, settings) != group:
                break
            owner = self._affinity.get(_affinity_key(stock, settings))
            if owner is not None and owner[0] == slot['affinity_id'] and owner[2] > recent:
                selected, recent = index, owner[2]
        if recent >= 0:
            self.affinity_hits += 1
        value = pending[selected]
        del pending[selected]
        return value

    def snapshot(self):
        return {'started_processes': self.started_processes, 'completed_tasks': self.completed_tasks,
                'affinity_hits': self.affinity_hits, 'affinity_keys': len(self._affinity),
                'worker_pids': [s['process'].pid for s in self.slots if s['process'].is_alive()]}

    def scan(self, stocks, settings, contexts, run_dir, workers, revision, *, request,
             timeout_seconds=SYMBOL_TIMEOUT_SECONDS, capacity_callback=None):
        if self.closed or self.busy:
            raise RuntimeError('Worker pool is closed or already serving a batch')
        if revision != self.revision or self.revision_reader() != self.revision:
            raise RuntimeError('Worker pool source revision is stale')
        if type(workers) is not int or workers < 1:
            raise ValueError('worker count must be positive')
        run_dir = Path(run_dir)
        if (run_dir / 'cancel').exists() or not stocks:
            return
        inputs = store_job(stocks, settings, contexts, run_dir, revision, request)
        self.busy = True
        pending = deque(enumerate(stocks))
        cleanup_pending = []
        try:
            for slot in list(self.slots):
                if not slot['process'].is_alive() or slot['tasks'] >= self.max_tasks:
                    self._discard(slot)
            while len(self.slots) > workers:
                self._discard(self.slots[-1])
            while True:
                if self.revision_reader() != self.revision:
                    raise RuntimeError('Screening source changed during a reusable batch')
                if (run_dir / 'cancel').exists():
                    return
                target=workers;pressure={'limit':workers}
                if request.get('compute_budget'):
                    from .compute_capacity import update_resource_budget,role_capacity
                    monitor=request.get('worker_runtime') is not None
                    pressure=update_resource_budget(request['compute_budget'],monitor=monitor)
                    target=min(workers,role_capacity(pressure['limit'],monitor=monitor,pressure=pressure))
                if target==0:
                    # Waiting children still consume RAM. Requeue unpublished
                    # work before retiring them, so a zero budget can recover.
                    for slot in list(reversed(self.slots)):
                        if slot['stock'] is not None:
                            pending.appendleft((slot['ordinal'],slot['stock']))
                        self._discard(slot)
                for slot in list(reversed(self.slots)):
                    if slot['stock'] is None and len(self.slots)>target:
                        self._discard(slot)
                busy=sum(slot['stock'] is not None for slot in self.slots)
                if capacity_callback:
                    capacity_callback(max(target,busy),{**pressure,'waiting':target==0})
                for index in range(target):
                    if busy>=target or not pending:
                        break
                    slot = self.slots[index] if index < len(self.slots) else None
                    if slot is not None and slot['stock'] is not None:
                        continue
                    if slot is not None and (not slot['process'].is_alive() or slot['tasks'] >= self.max_tasks):
                        self._discard(slot)
                        slot = self._start()
                        self.slots.insert(index, slot)
                    if slot is None:
                        slot = self._start()
                        self.slots.append(slot)
                    ordinal, stock = self._take_pending(pending, slot, settings)
                    ticket = f'{uuid4().hex}_{ordinal}'
                    task = bounded_message({'ticket': ticket, 'index': ordinal,
                                            'inputs': inputs, 'revision': revision})
                    slot.update(stock=stock, ordinal=ordinal, ticket=ticket, started=time.monotonic())
                    busy+=1
                    try:
                        slot['connection'].send(task)
                    except (OSError, EOFError, ValueError):
                        self._discard(slot)
                        self.completed_tasks += 1
                        yield _failed_result(stock, settings, 'ENGINE_ERROR'), [
                            s['stock']['code'] for s in self.slots if s['stock'] is not None]
                        continue
                active = [slot for slot in self.slots if slot['stock'] is not None]
                if not active:
                    if not pending:
                        return
                    time.sleep(.2)
                    yield None, []
                    continue
                ready = wait_connections([slot['connection'] for slot in active],
                    timeout=min(.2 if request.get('compute_budget') else 2, timeout_seconds))
                if self.revision_reader() != self.revision:
                    raise RuntimeError('Screening source changed before committing a worker result')
                now = time.monotonic()
                for slot in active:
                    result, failed = None, False
                    accepted_reply = None
                    stock = slot['stock']
                    if slot['connection'] in ready:
                        try:
                            message = slot['connection'].recv()
                            result = read_reply(message, slot['ticket'], run_dir)
                            if result.get('code') != stock['code'] or result.get('market', 'a') != stock.get('market', 'a'):
                                raise ValueError('Worker response belongs to another symbol')
                            if not isinstance(result.get('rows'), list):
                                raise ValueError('Worker response has no period records')
                            periods = stock.get('_monitor_periods', settings['frequencies'])
                            if any(row.get('code') != stock['code']
                                   or row.get('market', 'a') != stock.get('market', 'a')
                                   or row.get('frequency') not in periods for row in result['rows']):
                                raise ValueError('Worker period record belongs to another request')
                            accepted_reply = (message, slot['ticket'])
                        except (EOFError, OSError, KeyError, TypeError, ValueError, AttributeError):
                            result, failed = None, True
                    timed_out = now - slot['started'] >= timeout_seconds
                    if result is None and (failed or timed_out or not slot['process'].is_alive()):
                        reason = 'WORKER_TIMEOUT' if timed_out else 'ENGINE_ERROR'
                        result = _failed_result(stock, settings, reason)
                        self._discard(slot)
                    if result is not None:
                        slot['stock'] = None
                        slot['tasks'] += 1
                        if accepted_reply is not None:
                            self._remember(slot, stock, settings)
                        self.completed_tasks += 1
                        yield result, [s['stock']['code'] for s in self.slots if s['stock'] is not None]
                        if accepted_reply is not None and not discard_reply(*accepted_reply, run_dir):
                            cleanup_pending.append(accepted_reply)
                yield None, [s['stock']['code'] for s in self.slots if s['stock'] is not None]
        finally:
            # A cancelled or abandoned generator may still have native calls
            # in flight. Only completed/idle processes can enter the next batch.
            for slot in list(self.slots):
                if slot['stock'] is not None:
                    self._discard(slot)
            for reference in cleanup_pending:
                discard_reply(*reference, run_dir)
            self.busy = False

    def close(self):
        if self.closed:
            return
        try:
            for slot in list(self.slots):
                self._discard(slot)
        finally:
            if self.job is not None:
                self.job.close()
            self.closed = True
