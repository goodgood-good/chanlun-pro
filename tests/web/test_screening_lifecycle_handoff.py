"""A late interruption probe must never replace newer task ownership."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock

import pytest

from chanlun.screening.runner import write_json
from chanlun.screening.state_io import read_state_json
from cl_app.services import screening_lifecycle as lifecycle


@pytest.mark.parametrize('replacement', ['completed', 'resuming'])
def test_state_published_during_second_owner_probe_wins(tmp_path, replacement):
    original = {'run_id': tmp_path.name, 'status': 'running', 'worker_pid': 1234,
                'updated_at': 100, 'completed': 5, 'total': 10}
    newer = {**original, 'updated_at': 200, 'completed': 10, 'status': 'completed'}
    if replacement == 'resuming':
        newer = {**original, 'updated_at': 200, 'status': 'starting',
                 'worker_pid': 5678, 'resume_ticket': 'new-owner'}
    write_json(tmp_path / 'status.json', original)
    calls = []
    def alive(_pid):
        calls.append(_pid)
        if len(calls) == 2:
            write_json(tmp_path / 'status.json', newer)
        return False
    result = lifecycle.reconcile_interrupted(tmp_path, deepcopy(original), now=200, alive=alive)
    assert result == newer
    assert read_state_json(tmp_path / 'status.json') == newer


@pytest.mark.parametrize('reader', ['interruption', 'resume'])
def test_transition_read_retries_a_transient_windows_file_lock(tmp_path, monkeypatch, reader):
    original = {'run_id': tmp_path.name, 'status': 'running', 'worker_pid': 1234, 'updated_at': 100}
    write_json(tmp_path / 'status.json', original)
    read = Path.read_text
    denied = []
    def sometimes_locked(path, *args, **kwargs):
        if path.name == 'status.json' and len(denied) < 2:
            denied.append(path)
            raise PermissionError('temporary sharing violation')
        return read(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', sometimes_locked)
    if reader == 'interruption':
        result = lifecycle.reconcile_interrupted(tmp_path, original, now=200, alive=lambda _: False)
        assert result['status'] == 'interrupted'
    else:
        from cl_app.services.screening_resume import read as resume_read
        assert resume_read(tmp_path / 'status.json') == original
    assert len(denied) == 2


def test_new_scan_can_reconcile_a_dead_previous_owner_while_holding_admission_lock(tmp_path, monkeypatch):
    from cl_app.services import screening
    source = 'a' * 32
    write_json(tmp_path / 'latest.json', {'run_id': source})
    write_json(tmp_path / source / 'status.json',
               {'run_id': source, 'status': 'running', 'worker_pid': 1234, 'updated_at': 100})
    monkeypatch.setattr(screening, '_pid_alive', lambda _: False)
    launch = Mock()
    monkeypatch.setattr(screening.subprocess, 'Popen', launch)
    manager = screening.ScreeningManager(tmp_path, warm_results=False)
    result = manager.start({'scope': 'codes', 'codes': ['SH.600088']})
    assert result['run_id'] != source and result['status'] == 'starting'
    assert read_state_json(tmp_path / source / 'status.json')['status'] == 'interrupted'
    launch.assert_called_once()


def test_another_process_transition_cannot_be_overwritten_by_status_polling(tmp_path):
    original = {'run_id': tmp_path.name, 'status': 'running', 'worker_pid': 1234,
                'updated_at': 100, 'completed': 5, 'total': 10}
    newer = {**original, 'status': 'starting', 'updated_at': 200, 'resume_ticket': 'replacement'}
    write_json(tmp_path / 'status.json', original)
    root = Path(lifecycle.__file__).resolve().parents[4]
    script = '''
import json,sys
from pathlib import Path
from chanlun.persistence.file_lock import interprocess_file_lock
from chanlun.screening.runner import write_json
directory=Path(sys.argv[1])
with interprocess_file_lock(directory/'lifecycle.lock'):
    print('locked',flush=True)
    state=json.loads(sys.stdin.readline())
    write_json(directory/'status.json',state,durable=True)
'''
    owner = subprocess.Popen([sys.executable, '-u', '-c', script, str(tmp_path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**os.environ, 'PYTHONPATH': str(root / 'src')},
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    try:
        assert owner.stdout.readline().strip() == 'locked'
        assert owner.poll() is None
        assert lifecycle.reconcile_interrupted(tmp_path, original, now=200, alive=lambda _: False) == original
        assert read_state_json(tmp_path / 'status.json') == original
    finally:
        _, error = owner.communicate(json.dumps(newer) + '\n', timeout=10)
    assert owner.returncode == 0, error
    assert read_state_json(tmp_path / 'status.json') == newer
