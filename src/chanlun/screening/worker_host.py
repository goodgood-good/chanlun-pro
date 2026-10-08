"""Own one reusable Windows worker pool for a private monitor run directory."""
from pathlib import Path
import os
import re
import time

from chanlun.persistence.file_lock import interprocess_file_lock, InterprocessLockTimeout
from .runner import run_screening, source_revision, write_json
from .worker_pool import ReusableWorkerPool
from .worker_lifetime import process_identity
from .state_io import read_state_json

SCHEMA = 'monitor-worker-runtime-v1'
HOST_FILE = 'worker_host.json'
HOST_LOCK = 'worker_host.lock'
DISPATCH_LOCK = 'worker_dispatch.lock'
STOP_FILE = 'worker_stop.json'
IDLE_SECONDS = 35 * 60


def read_optional(path):
    try:
        return read_state_json(path)
    except FileNotFoundError:
        return None


def pending_run(root):
    latest = read_optional(root/'latest.json')
    if latest is None:
        return None
    identity = latest.get('run_id')
    if not isinstance(identity, str) or not re.fullmatch(r'[0-9a-f]{32}', identity):
        raise ValueError('Invalid monitor run identity')
    directory = root/identity
    if directory.is_symlink() or directory.resolve().parent != root.resolve():
        raise ValueError('Monitor run directory escaped its owner')
    state = read_optional(directory/'status.json')
    if state is None or state.get('run_id') != identity:
        raise ValueError('Monitor run state does not match its directory')
    if state.get('status') != 'starting':
        return None
    request = read_optional(directory/'request.json')
    if request is None or request.get('run_id') != identity:
        raise ValueError('Monitor run request does not match its directory')
    return directory, request


def serve(root, generation, *, idle_seconds=IDLE_SECONDS, poll_seconds=0.5,
          revision_reader=source_revision, pool_factory=ReusableWorkerPool, run=run_screening):
    if os.name != 'nt':
        raise RuntimeError('Reusable monitor workers require Windows process lifetime protection')
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    revision = revision_reader()
    try:
        with interprocess_file_lock(root/HOST_LOCK, timeout_seconds=30):
            identity = process_identity(os.getpid())
            if identity['alive'] is not True or not identity['birth']:
                raise RuntimeError('Cannot establish the monitor coordinator process identity')
            process_birth = identity['birth']
            pool = pool_factory(revision)
            last_work = time.monotonic()
            last_heartbeat = 0.0

            def publish(status, run_id=None, *, force=False):
                nonlocal last_heartbeat
                tick = time.monotonic()
                if not force and tick-last_heartbeat < 1:
                    return
                write_json(root/HOST_FILE, {
                    'schema': SCHEMA, 'pid': os.getpid(), 'process_birth': process_birth, 'generation': generation,
                    'source_revision': revision, 'status': status, 'run_id': run_id,
                    'updated_at': time.time(), 'monotonic_at': tick, **pool.snapshot(),
                })
                last_heartbeat = tick

            try:
                publish('idle', force=True)
                while True:
                    with interprocess_file_lock(root/DISPATCH_LOCK, timeout_seconds=15):
                        candidate = pending_run(root)
                        request_runtime = (candidate[1].get('worker_runtime', {}) if candidate else {})
                        stop = read_optional(root/STOP_FILE)
                        retired = revision_reader() != revision or (
                            candidate is not None and request_runtime != {
                                'schema': SCHEMA, 'generation': generation, 'source_revision': revision})
                        if (retired or stop is not None
                                or candidate is None and time.monotonic()-last_work >= idle_seconds):
                            publish('stopping', force=True)
                            break
                        if candidate is not None:
                            directory, request = candidate
                            # The dispatcher publishes the owner before release
                            # of this same lock. A ready host is the only writer.
                            initial = read_optional(directory/'status.json')
                            write_json(directory/'status.json', {**initial, 'worker_pid': os.getpid(),
                                                                'worker_process_birth': process_birth,
                                                                'updated_at': time.time()})
                            publish('running', directory.name, force=True)
                    if candidate is None:
                        publish('idle')
                        time.sleep(poll_seconds)
                        continue
                    directory, _ = candidate
                    run(directory, worker_pool=pool, pinned_revision=revision,
                        heartbeat=lambda: publish('running', directory.name))
                    last_work = time.monotonic()
                    publish('idle', force=True)
            finally:
                try:
                    pool.close()
                finally:
                    publish('stopped', force=True)
    except InterprocessLockTimeout:
        # An existing coordinator still owns this directory. Never run a
        # second pool or mark its currently owned batch failed from here.
        return


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--generation', required=True)
    args = parser.parse_args()
    serve(args.root, args.generation)
