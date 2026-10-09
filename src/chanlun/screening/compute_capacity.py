"""One process-safe compute budget shared by screening and live monitoring."""
from contextlib import contextmanager, ExitStack
import ctypes
import json
import math
import os
from pathlib import Path
import time

from chanlun.persistence.file_lock import interprocess_file_lock, InterprocessLockTimeout

MAX_WORKERS = 16
MONITOR_RESERVE = 2
GIB = 1024 ** 3
RESOURCE_FILE = 'resource_budget.json'


def memory_status():
    """Available physical and commit memory, without spawning a helper."""
    if os.name != 'nt':
        return None
    class Status(ctypes.Structure):
        _fields_ = [('length',ctypes.c_ulong),('load',ctypes.c_ulong),
                    *[(name,ctypes.c_ulonglong) for name in ('total','physical','commit_total','commit','virtual_total','virtual','extended')]]
    value=Status();value.length=ctypes.sizeof(value)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(value)):
        return None
    return {'available':min(value.physical,value.commit),'physical':value.physical,
            'commit':value.commit,'reserve':min(2*GIB,max(GIB//2,value.total//8))}


def pressure_limit(previous, memory, maximum, now):
    """Reduce admission promptly, then recover one worker at a time."""
    if not memory:
        return maximum
    available,reserve=memory['available'],memory['reserve']
    if available < GIB//2:
        return 0
    if not previous:
        return max(0,min(maximum,int((available-reserve)//(GIB//2))))
    limit=min(maximum,previous['limit'])
    if now-previous.get('changed_at',0)<2:
        return limit
    if available<reserve:
        return max(0,limit-max(1,math.ceil((reserve-available)/(GIB//2))))
    if available>=reserve+GIB//2:
        return min(maximum,limit+1)
    return limit


def _resource_read(root, maximum):
    try:
        state=json.loads((root/RESOURCE_FILE).read_text(encoding='utf-8'))
        if (state.get('schema')=='screening-memory-budget-v1' and type(state.get('limit')) is int
                and 0<=state['limit']<=maximum
                and all(type(state.get(key)) in (int,float) for key in ('updated_at','changed_at'))):
            return state
    except (OSError,ValueError,AttributeError):
        pass
    return None


def update_resource_budget(budget, *, monitor=False):
    root=_budget_root(budget)
    maximum=min(budget['limit'],cpu_capacity())
    memory=memory_status();now=time.time()
    try:
        with interprocess_file_lock(root/'resource_budget.lock',timeout_seconds=.25,poll_seconds=.01):
            previous=_resource_read(root,maximum)
            if previous and 0<=now-previous.get('updated_at',0)<1 and (monitor or previous.get('background_until',0)>now+5):
                return previous
            limit=pressure_limit(previous,memory,maximum,now)
            state={'schema':'screening-memory-budget-v1','limit':limit,'updated_at':now,
                   'changed_at':now if not previous or previous['limit']!=limit else previous['changed_at'],
                   'background_until':(previous or {}).get('background_until',0) if monitor else now+10,
                   'available_mb':None if not memory else round(memory['available']/1024**2),
                   'reserve_mb':None if not memory else round(memory['reserve']/1024**2)}
            temporary=root/(RESOURCE_FILE+'.tmp')
            temporary.write_text(json.dumps(state),encoding='utf-8')
            os.replace(temporary,root/RESOURCE_FILE)
            return state
    except (OSError,InterprocessLockTimeout):
        return {'limit':pressure_limit(_resource_read(root,maximum),memory,maximum,now),
                'available_mb':None if not memory else round(memory['available']/1024**2),
                'background_until':(_resource_read(root,maximum) or {}).get('background_until',0) if monitor else now+10}


def role_capacity(limit, *, monitor=False):
    return limit if monitor else max(0,limit-min(MONITOR_RESERVE,cpu_capacity()-1))


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


def _budget_root(budget):
    from chanlun import config
    expected=(config.get_data_path()/'compute_budget').resolve()
    if (not isinstance(budget,dict) or budget.get('schema')!='screening-compute-budget-v1'
            or Path(budget.get('root','')).resolve()!=expected
            or type(budget.get('limit')) is not int or not 1<=budget['limit']<=MAX_WORKERS):
        raise ValueError('Invalid shared computation budget')
    return expected


@contextmanager
def compute_slot(request, run_dir):
    budget = request.get('compute_budget')
    if budget is None:  # Recorded/offline jobs retain their existing execution contract.
        yield
        return
    expected = _budget_root(budget)
    maximum = min(budget['limit'], cpu_capacity())
    monitor = request.get('worker_runtime') is not None
    reserve = min(MONITOR_RESERVE,maximum-1)
    # File locks are released by the OS if a timed-out worker is terminated.
    # Only active computations hold slots; idle reusable workers hold none.
    while True:
        if (Path(run_dir) / 'cancel').exists():
            raise RuntimeError('Computation cancelled while waiting for capacity')
        state=_resource_read(expected,maximum)
        limit=state['limit'] if state else maximum
        ordinary=max(0,limit-reserve)
        # Reserved slot identities stay fixed when the memory limit changes.
        slots=(list(range(maximum-reserve,maximum-reserve+min(reserve,limit)))
               +list(range(ordinary))) if monitor else list(range(ordinary))
        for index in slots:
            with ExitStack() as stack:
                try:
                    stack.enter_context(interprocess_file_lock(expected / f'slot_{index}.lock',
                                        timeout_seconds=0.001, poll_seconds=0.001))
                except InterprocessLockTimeout:
                    continue
                yield
                return
        time.sleep(0.05 if not slots else 0.01)
