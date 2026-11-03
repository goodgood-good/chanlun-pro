"""Bounded preparation/analysis pipeline with one reusable calculation pool."""
from collections import deque
from copy import deepcopy
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread
import time


def pipelined_scan(stocks, settings, contexts, run_dir, workers, revision, *, request,
                   capacity_callback=None, preparation_callback=None,
                   prepare=None, scan=None, worker_pool=None):
    from .runner import bounded_scan, ScreeningCancelled, ScreeningTimeBudget, time_budget_expired
    from .tail_preparation import prepare_inputs
    prepare = prepare or prepare_inputs
    if scan is not None and worker_pool is not None:
        raise ValueError('A pipeline accepts one calculation pool')
    scan = scan or (worker_pool.scan if worker_pool is not None else bounded_scan)
    ready = Queue(maxsize=8)
    stopped, finished = Event(), Event()
    lock = Lock()
    progress = {'state': 'starting', 'completed': 0, 'total': 0, 'failed': 0, 'reused': 0}
    errors = []
    priority_stopped = False
    pool_holder = []
    began = time.monotonic()

    def enqueue(items):
        # Published inputs are immutable even while later batches add proofs.
        for offset in range(0, len(items), 100):
            batch = deepcopy(items[offset:offset + 100])
            while not stopped.is_set():
                try:
                    ready.put(batch, timeout=.1)
                    break
                except Full:
                    pass
            else:
                raise ScreeningCancelled()

    def produce():
        stream = None
        try:
            stream = prepare(stocks, settings, contexts, Path(run_dir).parent / 'analysis_cache', run_dir,
                             on_ready=enqueue, first_batch_size=min(100, max(1, workers)),
                             stop_requested=stopped.is_set,
                             **({'worker':worker_pool.get_preparation_worker()} if prepare is prepare_inputs
                                and callable(getattr(worker_pool,'get_preparation_worker',None)) else {}))
            for value in stream:
                with lock:
                    progress.update(value, elapsed_seconds=round(time.monotonic() - began, 1))
                if stopped.is_set():
                    break
        except BaseException as exc:
            if not (stopped.is_set() and isinstance(exc, ScreeningCancelled)):
                errors.append(exc)
        finally:
            try:
                if stream is not None:
                    stream.close()
            except BaseException as exc:
                errors.append(exc)
            finished.set()

    def stop_for_priority():
        nonlocal priority_stopped
        priority_stopped = True
        stopped.set()

    def publish():
        if preparation_callback:
            with lock:
                value = dict(progress)
            preparation_callback(value)

    def identity(stock):
        return stock.get('market', 'a'), stock['code'], tuple(stock.get('_monitor_periods', settings['frequencies']))
    required = {identity(stock) for stock in stocks}
    if len(required) != len(stocks):
        raise ValueError('Screening tasks must be unique before preparation')
    submitted = set()
    producer = Thread(target=produce, name='ScreeningInputPreparation', daemon=True)
    producer.start()
    deferred = deque()
    def ready_source():
        publish()
        if errors:raise errors[0]
        arrivals=[]
        while True:
            try:group=ready.get_nowait()
            except Empty:break
            for stock in group:
                key=identity(stock)
                if key not in required or key in submitted:
                    raise ValueError('Preparation published a foreign or duplicate task')
                submitted.add(key)
            arrivals.append(group)
        done=finished.is_set() and ready.empty()
        if done and submitted!=required:
            raise RuntimeError('Preparation finished without releasing every requested task')
        return arrivals,done
    try:
        from .worker_pool import ReusableWorkerPool
        if isinstance(worker_pool,ReusableWorkerPool):
            stream=worker_pool.scan(stocks,settings,contexts,run_dir,workers,revision,
                timeout_seconds=30 if request.get('deadline_at') is not None else 180,
                capacity_callback=capacity_callback,request=request,ready_source=ready_source,
                stop_source=stop_for_priority)
            try:
                for result in stream:
                    publish()
                    if errors:raise errors[0]
                    yield result
            finally:stream.close()
            return
        while True:
            publish()
            if (Path(run_dir) / 'cancel').exists():
                raise ScreeningCancelled()
            if time_budget_expired(request):
                raise ScreeningTimeBudget()
            if errors:
                raise errors[0]
            if not deferred:
                try:
                    deferred.extend(ready.get(timeout=.2))
                except Empty:
                    if finished.is_set():
                        if errors:
                            raise errors[0]
                        if not ready.empty():
                            continue
                        break
                    yield None, []
                    continue
            # Merge prepared batches to avoid a worker restart or a small-batch
            # barrier for every native request. Memory remains bounded.
            while len(deferred) < 512:
                try:
                    deferred.extend(ready.get_nowait())
                except Empty:
                    break
            batch = [deferred.popleft() for _ in range(min(512, len(deferred)))]
            for stock in batch:
                key = identity(stock)
                if key not in required or key in submitted:
                    raise ValueError('Preparation published a foreign or duplicate task')
                submitted.add(key)
            stream = scan(batch, settings, contexts, run_dir, workers, revision,
                          timeout_seconds=30 if request.get('deadline_at') is not None else 180,
                          capacity_callback=capacity_callback, request=request,
                          **({'pool_holder':pool_holder} if worker_pool is None else {}))
            try:
                for result in stream:
                    publish()
                    if errors:
                        raise errors[0]
                    yield result
            finally:
                stream.close()
        if submitted != required:
            raise RuntimeError('Preparation finished without releasing every requested task')
        publish()
    finally:
        stopped.set()
        producer.join(timeout=10)
        if pool_holder:
            pool_holder[0].close()
        if producer.is_alive():
            if worker_pool is not None:
                worker_pool.closed = True
            raise RuntimeError('Input preparation did not stop after cancellation')
        # The producer may fail after the last calculation yield. Do not let a
        # stopped flag or an already-closed generator hide teardown failure.
        preparation = getattr(worker_pool, 'preparation_worker', None)
        if errors and (priority_stopped or getattr(preparation, 'closed', False)):
            if worker_pool is not None:
                worker_pool.closed = True
            raise RuntimeError('Input preparation cleanup failed during handoff') from errors[0]
