"""Dispatch private monitor jobs to an owned reusable coordinator."""
import os
from pathlib import Path
import subprocess
import sys
import time

from chanlun.screening.runner import source_revision, write_json
from chanlun.screening.worker_host import SCHEMA, HOST_FILE, STOP_FILE, read_optional
from chanlun.screening.worker_lifetime import process_identity


def runtime_request():
    revision = source_revision()
    return {'schema': SCHEMA, 'generation': os.environ.get('CHANLUN_BUILD_REVISION') or revision,
            'source_revision': revision}


def dispatch(root, directory, request, *, project, env, popen, creationflags):
    """Caller holds the directory's cross-process dispatch lock throughout."""
    root, directory = Path(root), Path(directory)
    (root/STOP_FILE).unlink(missing_ok=True)
    runtime = request['worker_runtime']
    try:
        host = read_optional(root/HOST_FILE)
    except (ValueError, OSError):
        host = None  # A cache hint cannot prevent a fresh, single-owner launch.
    if not isinstance(host, dict):
        host = {}
    process = None
    identity = process_identity(host.get('pid'))
    reusable = (all(host.get(key) == runtime[key] for key in ('schema', 'generation', 'source_revision'))
                and host.get('status') in {'idle', 'running'} and identity['alive'] is True
                and bool(host.get('process_birth')) and identity['birth'] == host['process_birth'])
    if reusable:
        pid = host['pid']
    else:
        with (root/'worker_host.log').open('ab') as log:
            process = popen([sys.executable, '-m', 'chanlun.screening.worker_host',
                             '--root', str(root.resolve()), '--generation', runtime['generation']],
                            cwd=project, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                            creationflags=creationflags)
        pid = process.pid
        identity = process_identity(pid)
    if type(pid) is not int or pid <= 0:
        raise RuntimeError('Monitor coordinator did not provide a valid process owner')
    if identity['alive'] is not True or not identity['birth']:
        raise RuntimeError('Monitor coordinator process identity could not be verified')
    state = read_optional(directory/'status.json')
    if state is None or state.get('run_id') != request['run_id'] or state.get('status') != 'starting':
        raise RuntimeError('Monitor start lost ownership before dispatch')
    write_json(directory/'status.json', {**state, 'worker_pid': pid,
                                        'worker_process_birth': identity['birth'], 'updated_at': time.time()})
    return process


def release(root):
    write_json(Path(root)/STOP_FILE, {'schema': SCHEMA, 'requested_at': time.time()})
