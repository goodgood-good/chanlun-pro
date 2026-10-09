"""One process-safe compute budget shared by screening and live monitoring."""
from contextlib import contextmanager, ExitStack
import os
from pathlib import Path
import time

from chanlun.persistence.file_lock import interprocess_file_lock, InterprocessLockTimeout

MAX_WORKERS = 16
MONITOR_RESERVE = 2


def cpu_capacity():
    return max(1, min(MAX_WORKERS, os.cpu_count() or 1))


def worker_capacity(requested, *, monitor=False):
    capacity = cpu_capacity()
    available = capacity if monitor else max(1, capacity - MONITOR_RESERVE)
    return max(1, min(requested, available))


def budget_request():
    from chanlun import config
    return {'schema':'screening-compute-budget-v1',
            'root':str(config.get_data_path() / 'compute_budget'), 'limit':cpu_capacity()}


@contextmanager
def compute_slot(request, run_dir):
    budget = request.get('compute_budget')
    if budget is None:  # Recorded/offline jobs retain their existing execution contract.
        yield
        return
    from chanlun import config
    expected = (config.get_data_path() / 'compute_budget').resolve()
    if (not isinstance(budget, dict) or budget.get('schema') != 'screening-compute-budget-v1'
            or Path(budget.get('root', '')).resolve() != expected
            or type(budget.get('limit')) is not int or not 1 <= budget['limit'] <= MAX_WORKERS):
        raise ValueError('Invalid shared computation budget')
    limit = min(budget['limit'], cpu_capacity())
    monitor = request.get('worker_runtime') is not None
    ordinary = max(1, limit - MONITOR_RESERVE)
    slots = list(range(ordinary, limit)) + list(range(ordinary)) if monitor else list(range(ordinary))
    # File locks are released by the OS if a timed-out worker is terminated.
    # Only active computations hold slots; idle reusable workers hold none.
    while True:
        if (Path(run_dir) / 'cancel').exists():
            raise RuntimeError('Computation cancelled while waiting for capacity')
        for index in slots:
            with ExitStack() as stack:
                try:
                    stack.enter_context(interprocess_file_lock(expected / f'slot_{index}.lock',
                                        timeout_seconds=0.001, poll_seconds=0.001))
                except InterprocessLockTimeout:
                    continue
                yield
                return
        time.sleep(0.01)
