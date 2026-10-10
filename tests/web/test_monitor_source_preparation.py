"""Progressive history loading must continue without becoming a false signal."""
from datetime import datetime, timedelta

from chanlun.screening.rules import CN
from tests.web.test_monitor_clock_wake import waiting_monitor


def prepared_service(tmp_path):
    service, live, clock, key = waiting_monitor(
        tmp_path, 'us', 'AMD.US', '5m', datetime(2026, 9, 21, 22, 4, tzinfo=CN))
    state = service._load()
    state['active_cutoffs'] = {key: state['attempted_cutoffs'][key]}
    return service, live, clock, key


def error(token):
    return {'market':'us', 'code':'AMD.US', 'frequency':'5m',
            'reasons':['DATA_SOURCE_PREPARING'], 'source_preparation':{'progress_token':token}}


def test_more_than_three_progress_slices_still_schedule_at_same_closed_clock(tmp_path):
    service, live, clock, key = prepared_service(tmp_path)
    for iteration in range(8):
        service._record_source_preparation({key}, [error(str(iteration))],
                                           clock[0].timestamp(), str(iteration))
        retry = service._load()['retry_symbols'][key]
        assert retry['failures'] == 0
        assert retry['retry_at'] == clock[0].timestamp() + 3
    clock[0] += timedelta(seconds=3)
    service._schedule_monitor(clock[0], None)
    assert len(live.started) == 1


def test_no_progress_backs_off_and_real_error_retains_existing_failure_policy(tmp_path):
    service, _, clock, key = prepared_service(tmp_path)
    for iteration in range(9):
        service._record_source_preparation({key}, [error('unchanged')],
                                           clock[0].timestamp(), str(iteration))
    retry = service._load()['retry_symbols'][key]
    assert retry['failures'] == 0
    assert retry['retry_at'] == clock[0].timestamp() + 240
    service._record_failures({key}, clock[0].timestamp(), 'network-failed')
    retry = service._load()['retry_symbols'][key]
    assert retry['failures'] == 1
    assert retry['retry_at'] == clock[0].timestamp() + 60


def test_same_run_is_idempotent_and_new_progress_resets_stall_delay(tmp_path):
    service, _, clock, key = prepared_service(tmp_path)
    service._record_source_preparation({key}, [error('one')], clock[0].timestamp(), 'one')
    original = dict(service._load()['retry_symbols'][key])
    service._record_source_preparation({key}, [error('two')], clock[0].timestamp()+1, 'one')
    assert service._load()['retry_symbols'][key] == original
    service._record_source_preparation({key}, [error('one')], clock[0].timestamp(), 'two')
    assert service._load()['retry_symbols'][key]['preparation_stalls'] == 1
    service._record_source_preparation({key}, [error('two')], clock[0].timestamp(), 'three')
    assert service._load()['retry_symbols'][key]['preparation_stalls'] == 0
