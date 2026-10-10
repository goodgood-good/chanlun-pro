"""One process-safe compute budget shared by screening and live monitoring."""
from contextlib import contextmanager, ExitStack
import ctypes
import json
import math
import os
from pathlib import Path
import time
from uuid import uuid4

from chanlun.persistence.file_lock import interprocess_file_lock, InterprocessLockTimeout
from chanlun.persistence import file_lock as _file_locks

MAX_WORKERS = 16
GIB = 1024 ** 3
RESOURCE_FILE = 'resource_budget.json'
DEMAND_FILE = 'monitor_demand.json'
MAX_DEMAND_TTL_SECONDS = 60.0
_DEMAND_OWNER = f'{os.getpid()}:{uuid4().hex}'


def role_limits(limit, demand=0, screening_active=False):
    """Pure, work-conserving allocation; actual slots are shared OS locks."""
    if type(limit) is not int or not 0 <= limit <= MAX_WORKERS or type(demand) is not int or demand < 0:
        raise ValueError('invalid role capacity')
    reserved = min(demand, (3 * limit + 3) // 4, max(0, limit - 1))
    return {'monitor_capacity': reserved if reserved and screening_active else limit,
            'screening_capacity': limit - reserved,
            'monitor_reserved': reserved}


def _demand_leases(root, now):
    try:
        data = json.loads((root / DEMAND_FILE).read_text(encoding='utf-8'))
        if data.get('schema') != 'monitor-compute-demand-v1' or not isinstance(data.get('leases'), dict):
            return {}
        return {owner: lease for owner, lease in data['leases'].items()
                if isinstance(owner, str) and 0 < len(owner) <= 128 and isinstance(lease, dict)
                and type(lease.get('demand')) is int and 0 < lease['demand'] <= MAX_WORKERS
                and all(type(lease.get(key)) in (int, float) and math.isfinite(lease[key])
                        for key in ('updated_at', 'expires_at'))
                and 0 <= now - lease['updated_at']
                and 0 < lease['expires_at'] - lease['updated_at'] <= MAX_DEMAND_TTL_SECONDS
                and now < lease['expires_at']}
    except (OSError, ValueError, AttributeError):
        return {}


def _with_roles(state, root, maximum, now):
    leases = _demand_leases(root, now)
    demand = max((item['demand'] for item in leases.values()), default=0)
    return {**state, **role_limits(state['limit'], demand, state.get('background_until', 0) > now),
            'monitor_demand': demand,
            'demand_expires_at': max((item['expires_at'] for item in leases.values()), default=0),
            'maximum': maximum}


def publish_monitor_demand(budget, *, demand, expires_in=12.0, owner=None):
    """Renew one scheduler's short lease; zero releases only that owner.

    A failed/exited scheduler automatically loses its reservation after expiry.
    This advertises demand and never starts a worker or acquires a compute slot.
    """
    root = _budget_root(budget)
    if (type(demand) is not int or demand < 0
            or type(expires_in) not in (int, float) or not math.isfinite(expires_in)
            or not 0 < expires_in <= MAX_DEMAND_TTL_SECONDS):
        raise ValueError('invalid monitor demand lease')
    owner = _DEMAND_OWNER if owner is None else owner
    if not isinstance(owner, str) or not 0 < len(owner) <= 128:
        raise ValueError('invalid monitor demand owner')
    now = time.time()
    with interprocess_file_lock(root / 'monitor_demand.lock', timeout_seconds=.25, poll_seconds=.01):
        leases = _demand_leases(root, now)
        leases.pop(owner, None)
        if demand:
            if len(leases) >= 32:
                raise ValueError('too many active monitor demand owners')
            leases[owner] = {'demand': min(demand, budget['limit']), 'updated_at': now,
                             'expires_at': now + expires_in}
        temporary = root / f'.monitor-demand.{os.getpid()}.{uuid4().hex}.tmp'
        try:
            temporary.write_text(json.dumps({'schema': 'monitor-compute-demand-v1', 'leases': leases}), encoding='utf-8')
            os.replace(temporary, root / DEMAND_FILE)
        finally:
            temporary.unlink(missing_ok=True)
    return update_resource_budget(budget, monitor=True)


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
                return _with_roles(previous, root, maximum, now)
            limit=pressure_limit(previous,memory,maximum,now)
            state={'schema':'screening-memory-budget-v1','limit':limit,'updated_at':now,
                   'changed_at':now if not previous or previous['limit']!=limit else previous['changed_at'],
                   'background_until':(previous or {}).get('background_until',0) if monitor else now+10,
                   'available_mb':None if not memory else round(memory['available']/1024**2),
                   'reserve_mb':None if not memory else round(memory['reserve']/1024**2)}
            temporary=root/(RESOURCE_FILE+'.tmp')
            temporary.write_text(json.dumps(state),encoding='utf-8')
            os.replace(temporary,root/RESOURCE_FILE)
            return _with_roles(state, root, maximum, now)
    except (OSError,InterprocessLockTimeout):
        state = {'limit':pressure_limit(_resource_read(root,maximum),memory,maximum,now),
                'available_mb':None if not memory else round(memory['available']/1024**2),
                'background_until':(_resource_read(root,maximum) or {}).get('background_until',0) if monitor else now+10}
        return _with_roles(state, root, maximum, now)


def role_capacity(limit, *, monitor=False, pressure=None):
    field = 'monitor_capacity' if monitor else 'screening_capacity'
    return min(limit, (pressure or {}).get(field, limit))


def cpu_capacity():
    return max(1, min(MAX_WORKERS, os.cpu_count() or 1))


def worker_capacity(requested, *, monitor=False):
    # The advertised demand lease, not a permanent two-slot deduction, owns
    # the live/screening split. This remains the configured process ceiling.
    return max(1, min(requested, cpu_capacity()))


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
def _try_slot_lock(path):
    """One nonblocking probe, using the same OS byte-lock protocol.

    Waiting separately on every occupied slot while holding admission would
    serialize a whole wave, especially with Windows' coarse monotonic clock.
    """
    handle = path.open('a+b')
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b'\0')
            handle.flush()
            os.fsync(handle.fileno())
        locked = _file_locks._try_lock(handle)
    except BaseException:
        handle.close()
        raise
    if not locked:
        handle.close()
        yield False
        return
    try:
        yield True
    finally:
        try:
            _file_locks._unlock(handle)
        finally:
            handle.close()


@contextmanager
def compute_slot(request, run_dir):
    budget = request.get('compute_budget')
    if budget is None:  # Recorded/offline jobs retain their existing execution contract.
        yield
        return
    expected = _budget_root(budget)
    maximum = min(budget['limit'], cpu_capacity())
    monitor = request.get('worker_runtime') is not None
    # File locks are released by the OS if a timed-out worker is terminated.
    # Only active computations hold slots; idle reusable workers hold none.
    while True:
        if (Path(run_dir) / 'cancel').exists():
            raise RuntimeError('Computation cancelled while waiting for capacity')
        acquired = ExitStack()
        admitted = False
        try:
            # Serialize only admission, never a calculation. During a memory
            # downgrade, older occupied slots still count toward the new limit;
            # they finish naturally while new admissions wait for headroom.
            with interprocess_file_lock(expected / 'slot_admission.lock', timeout_seconds=.025, poll_seconds=.001):
                state = _resource_read(expected, maximum) or {'limit': maximum}
                pressure = _with_roles(state, expected, maximum, time.time())
                limit, reserved = pressure['limit'], pressure['monitor_reserved']
                ordinary = limit - reserved
                # These are namespace-wide identities, including on a small
                # CPU/budget. A stale worker with another maximum cannot own
                # an uncounted extra lane during a downgrade or role switch.
                protected = list(range(MAX_WORKERS - reserved, MAX_WORKERS))
                shared = list(range(ordinary))
                if monitor:
                    slots = (protected + shared if pressure['monitor_capacity'] == limit else protected)
                else:
                    slots = shared
                occupied = 0
                if limit < MAX_WORKERS:
                    for index in range(MAX_WORKERS):
                        with _try_slot_lock(expected / f'slot_{index}.lock') as free:
                            occupied += not free
                if occupied < limit:
                    for index in slots:
                        if acquired.enter_context(_try_slot_lock(expected / f'slot_{index}.lock')):
                            admitted = True
                            break
        except InterprocessLockTimeout:
            acquired.close()
            time.sleep(.01)
            continue
        except BaseException:
            acquired.close()
            raise
        try:
            if admitted:
                yield index
                return
        finally:
            acquired.close()
        time.sleep(.01)
