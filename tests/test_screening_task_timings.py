"""Complete single-period task costs include linked-period computation."""
from copy import deepcopy
from unittest.mock import Mock

import pytest

from chanlun.screening import worker_pool, worker_transport


def result():
    return {'market': 'a', 'code': 'SH.600088', 'seconds': 12., 'requested_frequencies': ['5m'],
            'rows': [{'market': 'a', 'code': 'SH.600088', 'frequency': '5m', 'selected': [],
                      'timings_ms': {'fetch': 1000., 'structure': 1000., 'validation': 500., 'persistence': 500., 'total': 3000.},
                      'processing_timeline': {'calculation_started_at': 100., 'fetched_at': 101.,
                                              'calculation_finished_at': 103.}}]}


def run_worker(tmp_path, monkeypatch, payload, *, periods=('5m',), finished=112.):
    stock = {'market': 'a', 'code': 'SH.600088', 'name': 'fixture', '_monitor_periods': list(periods)}
    settings = {'frequencies': list(periods)}
    inputs = worker_transport.store_job([stock], settings, {}, tmp_path, 'test', {})
    ticket = 'a' * 32 + '_0'
    channel = Mock()
    channel.recv.side_effect = [{'inputs': inputs, 'ticket': ticket, 'index': 0, 'revision': 'test'}, None]
    monkeypatch.setattr(worker_pool, '_scan_worker_symbol', lambda *args: deepcopy(payload))
    monkeypatch.setattr(worker_pool.time, 'time', lambda: finished)
    worker_pool.reusable_worker(channel)
    return worker_transport.read_reply(channel.send.call_args.args[0], ticket, tmp_path)


def test_transport_publishes_the_whole_period_cost_and_actual_completion_clock(tmp_path, monkeypatch):
    actual = run_worker(tmp_path, monkeypatch, result())['rows'][0]
    assert actual['timings_ms']['total'] == 12000.
    assert actual['timings_ms']['primary_total'] == 3000.
    assert actual['timings_ms']['coordination'] == 9000.
    assert actual['processing_timeline']['calculation_finished_at'] == 112.
    assert actual['processing_timeline']['primary_calculation_finished_at'] == 103.
    assert actual['processing_timeline']['measurement_scope'] == 'screening-period-task-v1'


@pytest.mark.parametrize('damage', ['several_periods', 'several_rows', 'wrong_code', 'wrong_frequency', 'nan', 'negative', 'too_short', 'bad_timings', 'bad_timeline'])
def test_unattributable_or_invalid_measurements_keep_the_original_record(tmp_path, monkeypatch, damage):
    payload = result()
    periods = ('5m',)
    if damage == 'several_periods':
        periods = ('5m', '30m')
        payload['requested_frequencies'] = list(periods)
    elif damage == 'several_rows':
        payload['rows'].append({**deepcopy(payload['rows'][0]), 'frequency': '30m'})
    elif damage == 'wrong_code':
        payload['rows'][0]['code'] = 'SH.600000'
    elif damage == 'wrong_frequency':
        payload['rows'][0]['frequency'] = '1m'
    elif damage == 'bad_timings':
        payload['rows'][0]['timings_ms'] = ['not', 'timings']
    elif damage == 'bad_timeline':
        payload['rows'][0]['processing_timeline'] = 'not a timeline'
    else:
        payload['seconds'] = {'nan': float('nan'), 'negative': -1, 'too_short': 1.}[damage]
    if damage == 'nan':
        # Invalid JSON cannot be transported. Inspect the annotation boundary
        # before the ordinary serializer rejects this malformed worker output.
        actual = deepcopy(payload)
        worker_pool._complete_task_timing(actual, {'stock': {'market': 'a', 'code': 'SH.600088'},
                                                'settings': {'frequencies': ['5m']}}, 112.)
        assert actual['rows'] == payload['rows']
    else:
        actual = run_worker(tmp_path, monkeypatch, payload, periods=periods)
        assert actual == payload


def test_wall_clock_rollback_does_not_rewrite_monotonic_task_duration(tmp_path, monkeypatch):
    actual = run_worker(tmp_path, monkeypatch, result(), finished=99.)['rows'][0]
    assert actual['timings_ms']['total'] == 12000.
    assert actual['processing_timeline']['calculation_finished_at'] == 99.
