"""Scheduling cannot enumerate a full-market evidence directory each poll."""
from datetime import datetime
import json
from unittest.mock import Mock
from types import SimpleNamespace

import pandas as pd
import pytest

from chanlun.screening import runner
from cl_app.services import screening
from cl_app.services.signal_monitor import SignalMonitor


def completed_manager(tmp_path, monkeypatch):
    monkeypatch.setattr(screening, 'source_revision', lambda: 'a' * 64)
    manager = screening.ScreeningManager(tmp_path / 'screening', warm_results=False)
    run_id = 'b' * 32
    directory = manager.root / run_id
    screening.write_json(manager.root / 'latest.json', {'run_id': run_id})
    screening.write_json(directory / 'status.json', {
        'run_id': run_id, 'status': 'completed', 'completed': 1, 'total': 1,
        'source_revision': 'a' * 64, 'cutoffs': {'5m': 1000},
        'settings': {'frequencies': ['5m']},
    })
    return manager, directory


@pytest.mark.parametrize('enabled', [False, True])
def test_tick_reads_lifecycle_without_polling_every_evidence_file(tmp_path, monkeypatch, enabled):
    manager, directory = completed_manager(tmp_path, monkeypatch)
    state = manager.scheduler_status()
    service = SignalMonitor(tmp_path / 'monitor', screening=manager,
                            now=lambda: datetime(2026, 10, 28, 10, tzinfo=screening.CN))
    service._load().update(enabled=enabled, seed={
        'run_id': state['run_id'], 'codes': [], 'symbols': [], 'settings': state['settings'],
        'selection_policy_version': state['selection_policy_version'],
        'result_reader_revision': state['result_reader_revision'],
    })
    forbidden = Mock(side_effect=AssertionError('scheduler enumerated full-market evidence'))
    monkeypatch.setattr(screening, 'evidence_version', forbidden)
    monkeypatch.setattr(manager, '_freshness', Mock(side_effect=AssertionError('scheduler built UI freshness')))
    published = Mock()
    monkeypatch.setattr(service, '_publish_notifications', published)
    monkeypatch.setattr(service, '_sync_watchlist', lambda: None)
    monkeypatch.setattr(service, '_consume_monitor', lambda: None)
    monkeypatch.setattr(service, '_schedule_daily', lambda *args: None)
    monkeypatch.setattr(service, '_schedule_monitor', lambda *args: None)
    monkeypatch.setattr('cl_app.services.signal_monitor.closed_bar_cutoff', lambda *args: 1000)
    try:
        service.tick()
        service.tick()
        assert published.call_count == 2
        for call in published.call_args_list:
            observed = call.args[0]
            assert observed['source_current'] is True
            assert observed['current_source_revision'] == 'a' * 64
            assert observed['run_id'] == state['run_id']
            assert observed['result_reader_revision'] == state['result_reader_revision']
        forbidden.assert_not_called()
    finally:
        service._incremental_reader.close()
        service._monitor_result_reader.close()


def test_lightweight_status_does_not_authorize_corrupt_committed_evidence(tmp_path, monkeypatch):
    manager, directory = completed_manager(tmp_path, monkeypatch)
    dates = pd.date_range('2026-10-28 14:55', periods=2, freq='5min', tz=screening.CN)
    frame = pd.DataFrame({'date': dates, 'code': 'SH.600000', 'open': 12.,
                          'high': 13., 'low': 11., 'close': 12., 'volume': 100})
    cutoff = int(dates[-1].timestamp())
    point = {'point_id': 'P', 'available_at': cutoff}
    snapshot = {'symbol': 'SH.600000', 'source_frequency': '5m', 'source_closed_at': cutoff,
                'levels': [{'structural_level': 0, 'points': [point]}]}
    manifest = runner._save_evidence(directory, 'SH.600000', '5m', frame, snapshot)
    record = {'code': 'SH.600000', 'requested_frequencies': ['5m'], 'rows': [{
        'code': 'SH.600000', 'frequency': '5m', 'source_closed_at': cutoff,
        'selected': [{'point': point}], 'evidence': manifest, 'recent_rejections': [], 'reason_counts': {},
    }]}
    (directory / 'results.jsonl').write_text(json.dumps(record) + '\n', encoding='utf-8')
    monkeypatch.setattr(screening, 'evidence_version', Mock(side_effect=AssertionError('full-directory poll')))
    assert manager.scheduler_status()['source_current']
    path = directory / 'evidence/SH.600000_5m.json.gz'
    damaged = bytearray(path.read_bytes())
    damaged[len(damaged) // 2] ^= 1
    path.write_bytes(damaged)
    result = manager.committed_results()
    assert not result['selected']
    assert result['errors'][0]['reasons'] == ['EVIDENCE_MISSING']


def test_lightweight_input_keeps_repaired_evidence_retry_recovery(tmp_path, monkeypatch):
    from cl_app.services import signal_monitor
    manager, directory = completed_manager(tmp_path, monkeypatch)
    service = SignalMonitor(tmp_path / 'monitor', screening=manager)
    token, timer = ['broken'], [100.]
    monkeypatch.setattr(screening, 'evidence_version', lambda _: token[0])
    monkeypatch.setattr(manager, '_freshness', lambda _: {})
    monkeypatch.setattr(signal_monitor, 'time', SimpleNamespace(monotonic=lambda: timer[0]))
    read = Mock(side_effect=ValueError('corrupt saved result'))
    monkeypatch.setattr(manager, 'results', read)
    for _ in range(6):
        light = manager.scheduler_status()
        assert 'evidence_version' not in light
        assert service._screening_results_for_monitor(light)['status'] == 'unavailable'
        timer[0] += 61
    assert read.call_count == 3
    assert service._screening_read_retry_at == float('inf')
    token[0] = 'repaired'
    read.side_effect = None
    read.return_value = {'status': 'completed', 'results_loading': False}
    assert service._screening_results_for_monitor(manager.scheduler_status())['status'] == 'completed'
    assert read.call_count == 4
    assert 'screening_result_error' not in service._load()


def test_full_status_for_consumption_cannot_switch_to_a_new_run(tmp_path, monkeypatch):
    manager, directory = completed_manager(tmp_path, monkeypatch)
    service = SignalMonitor(tmp_path / 'monitor', screening=manager)
    light = manager.scheduler_status()
    monkeypatch.setattr(manager, 'status', lambda: {**light, 'run_id': 'c' * 32, 'evidence_version': 'changed'})
    read = Mock(side_effect=AssertionError('must not consume another run'))
    monkeypatch.setattr(manager, 'results', read)
    assert service._screening_results_for_monitor(light)['status'] == 'unavailable'
    read.assert_not_called()
