"""The web dispatcher preserves one owned coordinator through fresh requests."""
import json
import os
from unittest.mock import Mock

import pytest

from chanlun.screening.runner import write_json
from chanlun.screening.worker_host import HOST_FILE, STOP_FILE
from cl_app.services import screening
from cl_app.services import screening_worker_runtime as runtime


pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Windows reusable monitor coordinator')
BODY = {'scope': 'symbols', 'symbols': [{'market': 'a', 'code': 'SH.600000', 'name': 'fixture'}]}


@pytest.fixture
def dispatcher(tmp_path, monkeypatch):
    popen = Mock(side_effect=[Mock(pid=1234), Mock(pid=5678), Mock(pid=9012)])
    monkeypatch.setattr(screening.subprocess, 'Popen', popen)
    monkeypatch.setattr(runtime, 'process_identity', lambda pid: {'alive': pid in {1234, 5678, 9012}, 'birth': f'current-{pid}'})
    monkeypatch.setenv('CHANLUN_BUILD_REVISION', 'generation-one')
    manager = screening.ScreeningManager(tmp_path, warm_results=False, reuse_workers=True)
    return manager, popen


def finish(manager, launched):
    directory = manager.root/launched['run_id']
    request = json.loads((directory/'request.json').read_text(encoding='utf-8'))
    write_json(directory/'status.json', {**launched, 'status': 'completed',
                                         'source_revision': request['worker_runtime']['source_revision']})
    write_json(manager.root/HOST_FILE, {**request['worker_runtime'], 'pid': launched['worker_pid'], 'process_birth': launched['worker_process_birth'], 'status': 'idle'})


def test_ready_host_is_reused_after_a_new_web_manager_instance(dispatcher):
    manager, popen = dispatcher
    first = manager.start(BODY, holding_snapshot=[])
    assert first['worker_pid'] == 1234
    assert 'chanlun.screening.worker_host' in popen.call_args.args[0]
    finish(manager, first)
    restored = screening.ScreeningManager(manager.root, warm_results=False, reuse_workers=True)
    second = restored.start({**BODY, 'frequencies': ['30m']}, holding_snapshot=[])
    assert second['worker_pid'] == first['worker_pid'] and popen.call_count == 1
    assert first['run_id'] != second['run_id'] and second['settings']['frequencies'] == ['30m']


@pytest.mark.parametrize('change', ['generation', 'source', 'dead', 'stopping'])
def test_retired_or_dead_host_requires_a_new_owner(dispatcher, monkeypatch, change):
    manager, popen = dispatcher
    first = manager.start(BODY, holding_snapshot=[])
    finish(manager, first)
    if change == 'generation':
        monkeypatch.setenv('CHANLUN_BUILD_REVISION', 'generation-two')
    elif change == 'source':
        monkeypatch.setattr(runtime, 'source_revision', lambda: 'f'*64)
    elif change == 'dead':
        monkeypatch.setattr(runtime, 'process_identity', lambda pid: {'alive': pid != 1234, 'birth': f'current-{pid}'})
    else:
        host = json.loads((manager.root/HOST_FILE).read_text(encoding='utf-8'))
        write_json(manager.root/HOST_FILE, {**host, 'status': 'stopping'})
    second = manager.start(BODY, holding_snapshot=[])
    assert second['worker_pid'] == 5678 and popen.call_count == 2


@pytest.mark.parametrize('content', ['{', '[]', 'null'])
def test_bad_host_cache_does_not_poison_new_dispatch(dispatcher, content):
    manager, popen = dispatcher
    manager.root.mkdir(exist_ok=True)
    (manager.root/HOST_FILE).write_text(content, encoding='utf-8')
    result = manager.start(BODY, holding_snapshot=[])
    assert result['worker_pid'] == 1234 and popen.call_count == 1


def test_pause_cancel_and_resume_use_durable_stop_intent(dispatcher):
    manager, popen = dispatcher
    first = manager.start(BODY, holding_snapshot=[])
    manager.cancel()
    manager.release_workers()
    assert (manager.root/first['run_id']/'cancel').exists()
    assert (manager.root/STOP_FILE).exists()
    finish(manager, first)
    second = manager.start(BODY, holding_snapshot=[])
    assert not (manager.root/STOP_FILE).exists()
    assert second['worker_pid'] == first['worker_pid'] and popen.call_count == 1


def test_reuse_rejects_catalog_enumeration_and_full_page_warmup(dispatcher, tmp_path):
    manager, popen = dispatcher
    with pytest.raises(ValueError, match='explicit symbol'):
        manager.start({'scope': 'all_a'})
    assert popen.call_count == 0
    with pytest.raises(ValueError, match='page-result'):
        screening.ScreeningManager(tmp_path/'other', reuse_workers=True)
