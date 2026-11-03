"""Private, non-renewable short handoff intent; generic cancellation stays hard."""
from copy import deepcopy
import math
from pathlib import Path
import time

from .state_io import read_state_json

FILE = 'priority_handoff.json'
SCHEMA = 'monitor-priority-handoff-v1'
MAX_GRACE_SECONDS = .25


def read_handoff(directory, request, revision):
    directory = Path(directory)
    path = directory / FILE
    if not path.exists():
        return None
    value = read_state_json(path)
    runtime = request.get('worker_runtime') or {}
    control = value.get('control') if isinstance(value, dict) else None
    if (not isinstance(value, dict)
            or set(value) != {'schema', 'run_id', 'source_revision', 'generation', 'requested_at', 'control'}
            or value['schema'] != SCHEMA or value['run_id'] != directory.name
            or value['run_id'] != request.get('run_id')
            or value['source_revision'] != revision or runtime.get('source_revision') != revision
            or not value['generation'] or value['generation'] != runtime.get('generation')
            or runtime.get('schema') != 'monitor-worker-runtime-v1'
            or type(value['requested_at']) not in (int, float) or not math.isfinite(value['requested_at'])
            or not 0 < value['requested_at'] <= time.time()
            or not isinstance(control, dict) or control.get('enabled') is not True
            or control.get('schema') != 'monitor-source-prefetch-v1'
            or control.get('generation') != value['generation']
            or read_state_json(directory.parent / 'source_prefetch_control.json') != control
            or read_state_json(directory.parent / 'latest.json') != {'run_id': directory.name}):
        raise ValueError('Priority handoff does not belong to the current monitor owner')
    return value


def publish_handoff(directory, request, control):
    from .runner import write_json
    directory = Path(directory)
    runtime = request.get('worker_runtime') or {}
    if (not isinstance(control, dict) or control.get('enabled') is not True
            or control.get('generation') != runtime.get('generation')
            or read_state_json(directory.parent / 'source_prefetch_control.json') != control):
        raise ValueError('Priority handoff control is not durably published')
    existing = read_handoff(directory, request, runtime.get('source_revision'))
    if existing is not None:
        return existing  # Repeated polling cannot extend the short grace.
    value = {'schema': SCHEMA, 'run_id': directory.name,
             'source_revision': runtime['source_revision'], 'generation': runtime['generation'],
             'requested_at': time.time(), 'control': deepcopy(control)}
    write_json(directory / FILE, value, durable=True)
    return read_handoff(directory, request, runtime['source_revision'])
