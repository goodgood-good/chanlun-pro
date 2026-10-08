"""Real coordinator lifecycle with isolated echo workers and durable requests."""
from functools import partial
import json
import multiprocessing
import os
from pathlib import Path
import time

import pytest

from chanlun.persistence.file_lock import interprocess_file_lock
from chanlun.screening.runner import write_json
from chanlun.screening.worker_host import DISPATCH_LOCK, HOST_FILE, SCHEMA, STOP_FILE, serve, read_optional
from chanlun.screening.worker_pool import ReusableWorkerPool
from tests.test_screening_worker_pool import echo_worker


pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Windows worker lifetime contract')


def fake_run(directory, *, worker_pool, pinned_revision, heartbeat):
    request = json.loads((directory/'request.json').read_text(encoding='utf-8'))
    settings = request['settings']
    state = {**request, 'worker_pid': os.getpid(), 'status': 'running', 'completed': 0,
             'source_revision': pinned_revision}
    write_json(directory/'status.json', state)
    for result, _ in worker_pool.scan(settings['symbols'], settings, {}, directory, 1,
                                      pinned_revision, request=request, timeout_seconds=60):
        if result is not None:
            with (directory/'results.jsonl').open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(result)+'\n')
            state['completed'] += 1
            write_json(directory/'status.json', state)
        heartbeat()
    write_json(directory/'status.json', {**state, 'status': 'cancelled' if (directory/'cancel').exists() else 'completed'})


def fixture_host(root, generation, idle=30):
    factory = partial(ReusableWorkerPool, worker_target=echo_worker, revision_reader=lambda: 'test')
    serve(root, generation, idle_seconds=idle, poll_seconds=0.05,
          revision_reader=lambda: 'test', pool_factory=factory, run=fake_run)


def wait_until(check, timeout=12):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError('isolated coordinator condition did not become true')


def read(path):
    return read_optional(path) or {}


def enqueue(root, ordinal, *, generation='one', blocked=False):
    identity = f'{ordinal:032x}'
    directory = root/identity
    request = {'run_id': identity, 'worker_runtime': {'schema': SCHEMA, 'generation': generation, 'source_revision': 'test'},
               'settings': {'frequencies': ['5m'], 'symbols': [{'code': f'SH.{600000+ordinal}', 'market': 'a',
                                                             'name': 'fixture', 'blocked': blocked,
                                                             'started_file': str(directory/'blocked.worker')}]}}
    with interprocess_file_lock(root/DISPATCH_LOCK, timeout_seconds=5):
        write_json(directory/'request.json', request)
        write_json(directory/'status.json', {**request, 'status': 'starting'})
        write_json(root/'latest.json', {'run_id': identity})
    return directory


@pytest.fixture
def host(tmp_path):
    process = multiprocessing.get_context('spawn').Process(target=fixture_host, args=(tmp_path, 'one'))
    process.start()
    wait_until(lambda: read(tmp_path/HOST_FILE).get('status') == 'idle')
    yield tmp_path, process
    write_json(tmp_path/STOP_FILE, {'stop': True})
    process.join(timeout=5)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
    process.close()


def finish(directory):
    wait_until(lambda: read(directory/'status.json').get('status') == 'completed')
    return json.loads((directory/'results.jsonl').read_text(encoding='utf-8'))


def test_completed_batches_use_same_owned_child_and_different_requests(host):
    root, process = host
    first = finish(enqueue(root, 1))
    second = finish(enqueue(root, 2))
    assert first['pid'] == second['pid'] and first['pid'] != process.pid
    assert first['code'] != second['code'] and first['request']['run_id'] != second['request']['run_id']
    assert read(root/HOST_FILE)['started_processes'] == 1
    from chanlun.screening.worker_lifetime import process_identity
    assert read(root/HOST_FILE)['process_birth'] == process_identity(process.pid)['birth']


def test_new_service_generation_retires_the_old_host_without_claiming_new_job(host):
    root, process = host
    finish(enqueue(root, 1))
    waiting = enqueue(root, 2, generation='two')
    process.join(timeout=8)
    assert not process.is_alive()
    assert read(waiting/'status.json')['status'] == 'starting'
    assert read(root/HOST_FILE)['status'] == 'stopped'


def test_pause_releases_idle_workers(host):
    root, process = host
    finish(enqueue(root, 1))
    write_json(root/STOP_FILE, {'stop': True})
    process.join(timeout=8)
    assert not process.is_alive()
    assert read(root/HOST_FILE)['worker_pids'] == []


def test_idle_lease_expires_after_the_last_completed_batch(tmp_path):
    directory = enqueue(tmp_path, 1)
    process = multiprocessing.get_context('spawn').Process(target=fixture_host, args=(tmp_path, 'one', 0.2))
    try:
        process.start()
        finish(directory)
        process.join(timeout=8)
        assert not process.is_alive()
        assert read(tmp_path/HOST_FILE)['worker_pids'] == []
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        process.close()


def test_second_coordinator_cannot_duplicate_a_live_owners_work(host):
    root, first = host
    earlier = finish(enqueue(root, 1))
    second = multiprocessing.get_context('spawn').Process(target=fixture_host, args=(root, 'one'))
    try:
        second.start()
        directory = enqueue(root, 2)
        later = finish(directory)
        assert later['pid'] == earlier['pid']
        assert len((directory/'results.jsonl').read_text(encoding='utf-8').splitlines()) == 1
    finally:
        write_json(root/STOP_FILE, {'stop': True})
        first.join(timeout=8)
        second.join(timeout=8)
        if second.is_alive():
            second.terminate()
            second.join(timeout=5)
        second.close()


def test_host_death_reaps_a_blocked_child_and_new_host_recovers(host):
    # Import only in the parent, after pytest's database-isolation fixture.
    from cl_app.services.screening_lifecycle import pid_alive
    root, process = host
    first = finish(enqueue(root, 1))
    child = first['pid']
    directory = enqueue(root, 2, blocked=True)
    wait_until(lambda: read(directory/'status.json').get('status') == 'running')
    wait_until(lambda: (directory/'blocked.worker').is_file())
    assert int((directory/'blocked.worker').read_text(encoding='utf-8')) == child
    process.terminate()
    process.join(timeout=5)
    wait_until(lambda: pid_alive(child) is False)
    replacement = multiprocessing.get_context('spawn').Process(target=fixture_host, args=(root, 'one'))
    try:
        # Interrupted work is retried as a new immutable run, as in the app.
        next_run = enqueue(root, 3)
        replacement.start()
        assert finish(next_run)['pid'] != child
    finally:
        write_json(root/STOP_FILE, {'stop': True})
        replacement.join(timeout=5)
        if replacement.is_alive():
            replacement.terminate()
            replacement.join(timeout=5)
        replacement.close()
