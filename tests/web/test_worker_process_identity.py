"""An unrelated process reusing a PID cannot own a monitor batch."""
import json
import os
from unittest.mock import Mock

import pytest

from chanlun.screening.runner import write_json
from chanlun.screening.worker_host import HOST_FILE
from cl_app.services import screening, screening_lifecycle, screening_worker_runtime


pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Windows process creation identity')


def test_recycled_host_pid_starts_a_new_coordinator(tmp_path, monkeypatch):
    popen = Mock(side_effect=[Mock(pid=1234), Mock(pid=5678)])
    monkeypatch.setattr(screening.subprocess, 'Popen', popen)
    monkeypatch.setattr(screening_worker_runtime, 'process_identity',
                        lambda pid: {'alive': True, 'birth': f'current-{pid}'}, raising=False)
    manager = screening.ScreeningManager(tmp_path, warm_results=False, reuse_workers=True)
    body = {'scope': 'symbols', 'symbols': [{'market': 'a', 'code': 'SH.600000', 'name': 'fixture'}]}
    first = manager.start(body, holding_snapshot=[])
    write_json(tmp_path/first['run_id']/'status.json', {**first, 'status': 'completed'})
    write_json(tmp_path/HOST_FILE, {**first['worker_runtime'], 'pid': 1234,
                                    'process_birth': 'earlier-process', 'status': 'idle'})
    second = manager.start(body, holding_snapshot=[])
    assert second['worker_pid'] == 5678 and popen.call_count == 2


def test_running_record_loses_ownership_when_its_pid_has_been_recycled(tmp_path, monkeypatch):
    state = {'run_id': tmp_path.name, 'status': 'running', 'worker_pid': 1234,
             'worker_process_birth': 'earlier-process', 'updated_at': 100}
    write_json(tmp_path/'status.json', state)
    monkeypatch.setattr(screening_lifecycle, 'process_identity',
                        lambda _: {'alive': True, 'birth': 'later-process'}, raising=False)
    result = screening_lifecycle.reconcile_interrupted(tmp_path, state, now=200, alive=lambda _: True)
    assert result['status'] == 'interrupted'
    assert json.loads((tmp_path/'status.json').read_text(encoding='utf-8')) == result


@pytest.mark.parametrize('identity', [{'alive': None, 'birth': None}, {'alive': True, 'birth': None},
                                    {'alive': True, 'birth': 'earlier-process'}])
def test_unknown_or_matching_identity_does_not_declare_interruption(tmp_path, monkeypatch, identity):
    state = {'run_id': tmp_path.name, 'status': 'running', 'worker_pid': 1234,
             'worker_process_birth': 'earlier-process', 'updated_at': 100}
    write_json(tmp_path/'status.json', state)
    monkeypatch.setattr(screening_lifecycle, 'process_identity', lambda _: identity, raising=False)
    assert screening_lifecycle.reconcile_interrupted(tmp_path, state, now=200, alive=lambda _: True) == state


def test_current_process_has_a_stable_exact_creation_identity():
    from chanlun.screening.worker_lifetime import process_identity
    first = process_identity(os.getpid())
    assert first['alive'] is True and len(first['birth']) == 16
    assert int(first['birth'], 16) > 0
    assert process_identity(os.getpid()) == first
    assert process_identity(2147483647) == {'alive': False, 'birth': None}
    assert process_identity(None) == {'alive': None, 'birth': None}


def test_completed_record_wins_a_racing_creation_identity_check(tmp_path, monkeypatch):
    state = {'run_id': tmp_path.name, 'status': 'running', 'worker_pid': 1234,
             'worker_process_birth': 'earlier-process', 'updated_at': 100}
    completed = {**state, 'status': 'completed'}
    write_json(tmp_path/'status.json', state)
    def identity(_):
        write_json(tmp_path/'status.json', completed)
        return {'alive': True, 'birth': 'later-process'}
    monkeypatch.setattr(screening_lifecycle, 'process_identity', identity)
    assert screening_lifecycle.reconcile_interrupted(tmp_path, state, now=200) == completed
