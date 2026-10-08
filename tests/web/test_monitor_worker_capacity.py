"""Small urgent batches preserve workers needed by the waiting backlog."""
from datetime import timedelta

import pytest

from tests.web.test_monitor_live_priority import mixed, finish
from tests.test_screening_worker_pool import pool, scan, results


@pytest.mark.parametrize('full_scan,expected', [(False, 4), (True, 2)])
def test_urgent_batch_keeps_backlog_capacity_within_the_current_resource_budget(mixed, full_scan, expected):
    service, manual, live, clock, _closed = mixed
    if full_scan:
        manual.state['status'] = 'running'
    service._schedule_monitor(clock[0], None)
    assert [item['code'] for item in live.started[-1]['symbols']] == ['LIVE.US']
    assert live.started[-1]['workers'] == expected
    assert service._load()['pending_symbols_count'] == 20


def test_a_small_total_queue_still_reduces_the_process_budget(mixed):
    service, _manual, live, clock, _closed = mixed
    state = service._load()
    state['seed']['symbols'] = state['seed']['symbols'][-1:]
    state['seed']['codes'] = ['us:LIVE.US']
    service._schedule_monitor(clock[0], None)
    assert live.started[-1]['workers'] == 1


def test_real_worker_processes_survive_a_small_urgent_batch_between_background_batches(mixed, pool, tmp_path):
    service, _manual, live, clock, closed = mixed

    def execute(name):
        request = live.started[-1]
        rows = results(scan(pool, tmp_path/name, request['symbols'],
                            workers=request['workers'], settings=request))
        assert {row['code'] for row in rows} == {stock['code'] for stock in request['symbols']}
        return set(pool.snapshot()['worker_pids'])

    service._schedule_monitor(clock[0], None)
    assert len(execute('first-live')) == 1  # Capacity alone never starts idle children.
    finish(service, live, closed['us'])
    service._schedule_monitor(clock[0], None)
    warmed = execute('background')
    assert len(warmed) == 4
    finish(service, live, closed['a'])
    clock[0] += timedelta(minutes=5)
    closed['us'] += 300
    service._schedule_monitor(clock[0], None)
    assert [stock['code'] for stock in live.started[-1]['symbols']] == ['LIVE.US']
    assert execute('urgent') == warmed
    finish(service, live, closed['us'])
    service._schedule_monitor(clock[0], None)
    assert execute('background-again') == warmed
    assert pool.snapshot()['started_processes'] == 4
