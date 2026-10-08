"""Transient state-file locks must not stop a healthy monitor coordinator."""
from pathlib import Path
from unittest.mock import Mock
import json

import pytest

from chanlun.screening import worker_host
from chanlun.screening.runner import write_json


def test_optional_state_retries_brief_locks_and_returns_the_complete_document(tmp_path, monkeypatch):
    path = tmp_path/'state.json'
    document = {'status': 'running', 'completed': 7}
    write_json(path, document)
    original = Path.read_text
    attempts = []

    def temporarily_locked(current, *args, **kwargs):
        if current == path:
            attempts.append(True)
            if len(attempts) < 3:
                raise PermissionError('isolated transient sharing violation')
        return original(current, *args, **kwargs)

    delays = []
    monkeypatch.setattr(Path, 'read_text', temporarily_locked)
    monkeypatch.setattr(worker_host.time, 'sleep', delays.append)
    assert worker_host.read_optional(path) == document
    assert len(attempts) == 3 and delays == [0.05, 0.1]


def test_permanent_denial_is_bounded_and_never_treated_as_absent(tmp_path, monkeypatch):
    denied = Mock(side_effect=PermissionError('isolated persistent denial'))
    delays = []
    monkeypatch.setattr(Path, 'read_text', denied)
    monkeypatch.setattr(worker_host.time, 'sleep', delays.append)
    with pytest.raises(PermissionError, match='persistent denial'):
        worker_host.read_optional(tmp_path/'state.json')
    assert denied.call_count == 15 and len(delays) == 14
    assert max(delays) == 0.5 and sum(delays) < 6


def test_absent_and_corrupt_documents_keep_distinct_meanings(tmp_path, monkeypatch):
    delays = []
    monkeypatch.setattr(worker_host.time, 'sleep', delays.append)
    assert worker_host.read_optional(tmp_path/'missing.json') is None
    path = tmp_path/'broken.json'
    path.write_text('{', encoding='utf-8')
    with pytest.raises(json.JSONDecodeError):
        worker_host.read_optional(path)
    assert not delays


def test_waiting_job_is_not_lost_when_its_status_is_briefly_locked(tmp_path, monkeypatch):
    identity = 'a'*32
    directory = tmp_path/identity
    request = {'run_id': identity, 'settings': {'workers': 2}}
    write_json(directory/'request.json', request)
    write_json(directory/'status.json', {'run_id': identity, 'status': 'starting'})
    write_json(tmp_path/'latest.json', {'run_id': identity})
    original = Path.read_text
    attempted = []

    def briefly_locked(path, *args, **kwargs):
        if path.name == 'status.json' and not attempted:
            attempted.append(True)
            raise PermissionError('isolated state replacement lock')
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', briefly_locked)
    monkeypatch.setattr(worker_host.time, 'sleep', lambda _: None)
    assert worker_host.pending_run(tmp_path) == (directory, request)
