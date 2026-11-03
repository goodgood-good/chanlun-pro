"""Bounded, cancellable worker reuse across independent monitor batches."""
from multiprocessing.connection import wait as wait_connections
from collections import OrderedDict
import multiprocessing
import math
import os
from pathlib import Path
from threading import Lock
import time
from uuid import uuid4

from .runner import SYMBOL_TIMEOUT_SECONDS, _scan_worker_symbol, _stop_worker, source_revision, time_budget_expired
from .worker_lifetime import WindowsWorkerJob
from .worker_transport import JobInputReader, bounded_message, store_job, store_reply, read_reply, discard_reply, TIMING_SCOPE
from .pending_tasks import PendingTasks


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


def _complete_task_timing(result, task, finished_at):
    """Attribute the scan call's full duration to exactly one physical period."""
    stock, settings = task['stock'], task['settings']
    periods = stock.get('_monitor_periods', settings['frequencies'])
    rows = result.get('rows', [])
    seconds = result.get('seconds')
    if (len(periods) != 1 or result.get('requested_frequencies') != list(periods)
            or len(rows) != 1 or not isinstance(rows[0], dict)
            or rows[0].get('frequency') != periods[0] or rows[0].get('code') != stock['code']
            or rows[0].get('market', 'a') != stock.get('market', 'a')
            or type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0
            or type(finished_at) not in (int, float) or not math.isfinite(finished_at) or finished_at <= 0):
        return
    row = rows[0]
    raw_timings, raw_timeline = row.get('timings_ms'), row.get('processing_timeline')
    if not isinstance(raw_timings, dict) or raw_timeline is not None and not isinstance(raw_timeline, dict):
        return
    timings = dict(raw_timings)
    primary = timings.get('total')
    if (type(primary) not in (int, float) or not math.isfinite(primary) or primary < 0
            or seconds * 1000 + 1 < primary):
        return
    # The task duration is rounded to milliseconds; retain the more precise
    # primary duration if that rounding alone would make the total smaller.
    total = max(seconds * 1000, primary)
    timings.update(total=round(total, 2), primary_total=primary)
    components = [timings.get(key) for key in ('fetch', 'structure', 'validation', 'persistence')]
    if (all(type(value) in (int, float) and math.isfinite(value) and value >= 0 for value in components)
            and sum(components) <= total + 1):
        timings['coordination'] = round(max(0, total - sum(components)), 2)
    timeline = dict(raw_timeline or {})
    if 'calculation_finished_at' in timeline:
        timeline['primary_calculation_finished_at'] = timeline['calculation_finished_at']
    timeline.update(calculation_finished_at=finished_at, measurement_scope=TIMING_SCOPE)
    row.update(timings_ms=timings, processing_timeline=timeline)


def reusable_worker(connection):
    inputs = JobInputReader()
    try:
        while (message := connection.recv()) is not None:
            task = inputs.load(message)
            result = _scan_worker_symbol(task['stock'], task['settings'], task['contexts'],
                                         task['run_dir'], task['revision'], task['request'])
            _complete_task_timing(result, task, time.time())
            connection.send(store_reply(task, result, prefer_inline=True))
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
        self._batch_lock = Lock()
        self.job = WindowsWorkerJob() if os.name == 'nt' else None
        self.started_processes = 0
        self.completed_tasks = 0
        self._affinity = OrderedDict()
        self._affinity_sequence = 0
        self.affinity_hits = 0
        self.preparation_worker = None

    def get_preparation_worker(self):
        if self.preparation_worker is None:
            from .preparation_worker import PreparationWorker
            self.preparation_worker=PreparationWorker()
        return self.preparation_worker

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
                slot['power_policy'] = self.job.assign(process)
        except BaseException:
            child.close()
            if process.pid is not None:
                try:
                    _stop_worker(slot)
                except BaseException:
                    self.closed = True
                    self.slots.append(slot)
                    raise
            else:
                parent.close()
            raise
        self.started_processes += 1
        return slot

    def _discard(self, slot):
        try:
            _stop_worker(slot)
        except BaseException:
            # Unknown child liveness invalidates the whole reusable pool.
            # Its Windows job is retired before any replacement can start.
            self.closed = True
            raise
        self._forget(slot)

    def _forget(self, slot):
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
        value, reused = pending.take(slot['affinity_id'], self._affinity)
        if reused:
            self.affinity_hits += 1
        return value

    def snapshot(self):
        return {'started_processes': self.started_processes, 'completed_tasks': self.completed_tasks,
                'pool_closed': self.closed, 'pool_busy': self.busy,
                'affinity_hits': self.affinity_hits, 'affinity_keys': len(self._affinity),
                'worker_power_policies': {str(s['process'].pid):s.get('power_policy')
                                          for s in self.slots if s['process'].is_alive()},
                'worker_pids': [s['process'].pid for s in self.slots if s['process'].is_alive()]}

    def scan(self, stocks, settings, contexts, run_dir, workers, revision, *, request,
             timeout_seconds=SYMBOL_TIMEOUT_SECONDS, capacity_callback=None, ready_source=None, stop_source=None):
        if not self._batch_lock.acquire(blocking=False):
            raise RuntimeError('Worker pool is already serving a batch')
        try:
            if self.closed:
                raise RuntimeError('Worker pool is closed')
            self.busy = True
            yield from self._scan(stocks, settings, contexts, run_dir, workers, revision,
                                 request=request, timeout_seconds=timeout_seconds,
                                 capacity_callback=capacity_callback,ready_source=ready_source,stop_source=stop_source)
        finally:
            try:
                if self.closed:
                    self._close_pool()
            finally:
                self.busy = False
                self._batch_lock.release()

    def _scan(self, stocks, settings, contexts, run_dir, workers, revision, *, request,
              timeout_seconds, capacity_callback,ready_source=None,stop_source=None):
        if revision != self.revision or self.revision_reader() != self.revision:
            raise RuntimeError('Worker pool source revision is stale')
        if type(workers) is not int or workers < 1:
            raise ValueError('worker count must be positive')
        run_dir = Path(run_dir)
        if (run_dir / 'cancel').exists() or time_budget_expired(request) or not stocks:
            return
        inputs = None if ready_source is not None else store_job(stocks, settings, contexts, run_dir, revision, request)
        pending = PendingTasks([] if ready_source is not None else stocks, settings, group_key=_priority_group, affinity_key=_affinity_key)
        references={};published=0;source_done=ready_source is None
        cleanup_pending = []
        priority_draining = False
        try:
            for slot in list(self.slots):
                if not slot['process'].is_alive() or slot['tasks'] >= self.max_tasks:
                    self._discard(slot)
            while len(self.slots) > workers:
                self._discard(self.slots[-1])
            while True:
                if (run_dir.parent / 'worker_stop.json').exists():
                    (run_dir / 'cancel').touch()
                    return
                if (run_dir / 'cancel').exists() or time_budget_expired(request):
                    return
                from .priority_handoff import read_handoff
                try:
                    handoff = read_handoff(run_dir, request, revision)
                except (OSError, ValueError, TypeError, KeyError):
                    (run_dir / 'cancel').touch()
                    return
                if handoff is not None:
                    priority_draining = True
                    if stop_source is not None:
                        stop_source()
                    yield from self._drain_priority(handoff, settings, contexts, run_dir,
                                                    request, cleanup_pending)
                    return
                if ready_source is not None and not source_done:
                    arrivals,source_done=ready_source()
                    for group in arrivals:
                        reference=store_job(group,settings,contexts,run_dir,revision,request)
                        additions=[]
                        for offset,stock in enumerate(group):
                            references[published]=(reference,offset)
                            additions.append((published,stock));published+=1
                        pending.extend(additions)
                if self.revision_reader() != self.revision:
                    raise RuntimeError('Screening source changed during a reusable batch')
                if (run_dir / 'cancel').exists() or time_budget_expired(request):
                    return
                target = workers
                pressure = {'limit': workers}
                maintenance_paused = False
                if request.get('compute_budget'):
                    from .compute_capacity import update_resource_budget,role_capacity
                    monitor=request.get('worker_runtime') is not None
                    maintenance = request.get('background_maintenance') is True
                    pressure=update_resource_budget(request['compute_budget'],monitor=monitor or maintenance)
                    target=min(workers,role_capacity(pressure['limit'],monitor=monitor,pressure=pressure))
                    maintenance_paused = maintenance and pressure['limit'] > 0 and pressure.get('background_until',0) > time.time()
                    if maintenance_paused:
                        target = 0
                if target==0 and not maintenance_paused:
                    # Waiting children still consume RAM. Requeue unpublished
                    # work before retiring them, so a zero budget can recover.
                    for slot in list(reversed(self.slots)):
                        if slot['stock'] is not None:
                            pending.appendleft((slot['ordinal'],slot['stock']))
                        self._discard(slot)
                # A short monitor reservation changes admission, not memory
                # pressure. Keep already-started idle children while there is
                # headroom, so every minute boundary does not discard their
                # imports, provider connection setup and bounded CL caches.
                available,reserve=pressure.get('available_mb'),pressure.get('reserve_mb')
                memory_safe=(type(available) in (int,float) and type(reserve) in (int,float)
                             and available>=reserve+512)
                idle_limit=min(workers,pressure['limit']) if memory_safe and target else target
                for slot in list(reversed(self.slots)):
                    if slot['stock'] is None and len(self.slots)>idle_limit:
                        self._discard(slot)
                busy=sum(slot['stock'] is not None for slot in self.slots)
                if capacity_callback:
                    capacity_callback(max(target,busy),{**pressure,'waiting':target==0})
                for index in range(target):
                    if (busy>=target or not pending or (run_dir / 'priority_handoff.json').exists()
                            or (run_dir / 'cancel').exists() or (run_dir.parent / 'worker_stop.json').exists()
                            or time_budget_expired(request)):
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
                    if ((run_dir / 'priority_handoff.json').exists() or (run_dir / 'cancel').exists()
                            or (run_dir.parent / 'worker_stop.json').exists() or time_budget_expired(request)):
                        break
                    ordinal, stock = self._take_pending(pending, slot, settings)
                    task_inputs,task_index=references[ordinal] if ready_source is not None else (inputs,ordinal)
                    ticket = f'{uuid4().hex}_{ordinal}'
                    task = bounded_message({'ticket': ticket, 'index': task_index,
                                            'inputs': task_inputs, 'revision': revision})
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
                if ((run_dir / 'priority_handoff.json').exists() or (run_dir / 'cancel').exists()
                        or (run_dir.parent / 'worker_stop.json').exists() or time_budget_expired(request)):
                    continue
                active = [slot for slot in self.slots if slot['stock'] is not None]
                if not active:
                    if not pending and source_done:
                        return
                    time.sleep(1 if maintenance_paused else .2)
                    yield None, []
                    continue
                ready = wait_connections([slot['connection'] for slot in active],
                    timeout=min(.2 if request.get('compute_budget') else 2, timeout_seconds))
                if self.revision_reader() != self.revision:
                    raise RuntimeError('Screening source changed before committing a worker result')
                if (run_dir / 'priority_handoff.json').exists():
                    # An intent can arrive while this ordinary IPC wait was
                    # active. Re-enter the strict handoff path before accepting
                    # replies or refilling a slot; failed replies stay retired.
                    continue
                now = time.monotonic()
                for slot in active:
                    result, failed = None, False
                    accepted_reply = None
                    stock = slot['stock']
                    if slot['connection'] in ready:
                        try:
                            result, accepted_reply = self._read_slot_reply(slot, settings, run_dir)
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
            if priority_draining:
                (run_dir / 'cancel').touch()
                self._retire_parallel([slot for slot in self.slots if slot['stock'] is not None])
            else:
                for slot in list(self.slots):
                    if slot['stock'] is not None:
                        self._discard(slot)
            for reference in cleanup_pending:
                discard_reply(*reference, run_dir)

    @staticmethod
    def _read_slot_reply(slot, settings, run_dir):
        stock = slot['stock']
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
        return result, (message, slot['ticket'])

    def _drain_priority(self, handoff, settings, contexts, run_dir, request, cleanup_pending):
        from .priority_handoff import MAX_GRACE_SECONDS, read_handoff
        deadline = time.monotonic() + max(0, min(MAX_GRACE_SECONDS,
            handoff['requested_at'] + MAX_GRACE_SECONDS - time.time()))

        def allowed():
            if ((run_dir / 'cancel').exists() or (run_dir.parent / 'worker_stop.json').exists()
                    or time_budget_expired(request)):
                return False
            if self.revision_reader() != self.revision:
                raise RuntimeError('Screening source changed during priority handoff')
            try:
                return read_handoff(run_dir, request, self.revision) == handoff
            except (OSError, ValueError, TypeError, KeyError):
                return False

        active = [slot for slot in self.slots if slot['stock'] is not None]
        if not active:
            return
        ready = []
        while allowed():
            remaining = max(0, deadline - time.monotonic())
            ready = wait_connections([slot['connection'] for slot in active], timeout=min(.025, remaining))
            if ready or remaining <= 0:
                break
        # No new assignment or preparation arrival is consumed in this path.
        for slot in active:
            if slot['connection'] not in ready or not allowed():
                continue
            stock = slot['stock']
            try:
                result, accepted = self._read_slot_reply(slot, settings, run_dir)
            except (EOFError, OSError, KeyError, TypeError, ValueError, AttributeError):
                continue  # Invalid/incomplete work stays uncommitted and is retired.
            rows = result['rows']
            periods = stock.get('_monitor_periods', settings['frequencies'])
            complete = (not result.get('error') and len(rows) == len(periods)
                and {row['frequency'] for row in rows} == set(periods)
                and all(not row.get('error') and not row.get('data_errors')
                    and not row.get('consolidation_error') and not row.get('consolidation_rejections')
                    and type(row.get('source_closed_at')) is int
                    and (stock.get('market', 'a') != 'a'
                         or row['source_closed_at'] == contexts.get(row['frequency'], {}).get('cutoff'))
                    for row in rows))
            if not complete or not allowed():
                continue
            slot['stock'] = None
            slot['tasks'] += 1
            self._remember(slot, stock, settings)
            self.completed_tasks += 1
            yield result, [s['stock']['code'] for s in self.slots if s['stock'] is not None]
            if not discard_reply(*accepted, run_dir):
                cleanup_pending.append(accepted)

    def _retire_parallel(self, slots):
        """Terminate only owned busy children, with shared bounded exit checks."""
        failure = None
        try:
            for slot in slots:
                try:
                    slot['connection'].close()
                    if slot['process'].is_alive():
                        slot['process'].terminate()
                except (OSError, ValueError) as exc:
                    failure = exc
            deadline = time.monotonic() + 2
            for slot in slots:
                slot['process'].join(timeout=max(0, deadline - time.monotonic()))
            remaining = [slot for slot in slots if slot['process'].is_alive()]
            for slot in remaining:
                try:
                    slot['process'].kill()
                except (OSError, ValueError) as exc:
                    failure = exc
            deadline = time.monotonic() + 1
            for slot in remaining:
                slot['process'].join(timeout=max(0, deadline - time.monotonic()))
            for slot in slots:
                if slot['process'].is_alive():
                    raise RuntimeError('Priority handoff could not confirm child exit') from failure
                slot['process'].close()
                self._forget(slot)
            if failure is not None:
                raise RuntimeError('Priority handoff child cleanup failed') from failure
        except BaseException:
            self.closed = True
            raise

    def close(self):
        if not self._batch_lock.acquire(blocking=False):
            raise RuntimeError('Worker pool is already serving a batch')
        try:
            self._close_pool()
        finally:
            self._batch_lock.release()

    def _close_pool(self):
        self.closed = True
        if self.preparation_worker is not None:
            self.preparation_worker.close()
        # Closing this non-inherited job handle also terminates children whose
        # individual process handle refused terminate/kill. Still reap and
        # verify every child below before allowing the host to create a pool.
        if self.job is not None:
            self.job.close()
        failure = None
        for slot in list(self.slots):
            try:
                _stop_worker(slot)
            except Exception as exc:
                failure = exc
            else:
                self._forget(slot)
        if self.slots:
            raise RuntimeError('Worker pool shutdown could not confirm every child exited') from failure
