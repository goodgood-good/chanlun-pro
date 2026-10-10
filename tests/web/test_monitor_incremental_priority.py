"""Committed membership and period scheduling remain independent of slow work."""
from copy import deepcopy
from datetime import datetime, timedelta
import threading

import pytest

from chanlun.screening.rules import CN
from cl_app.services.signal_monitor import SignalMonitor, _brief
from cl_app.services.screening import validate_settings, MonitorResultsReadError
from tests.web.test_signal_monitor import Jobs, signal


@pytest.fixture
def monitor(tmp_path, monkeypatch):
    clock = [datetime(2026, 9, 21, 10, 34, tzinfo=CN)]
    monkeypatch.setattr('cl_app.services.signal_monitor.closed_bar_cutoff',
                        lambda market, code, now, frequency:
                        int(now.timestamp()) // (int(frequency[:-1]) * 60) * int(frequency[:-1]) * 60)
    monkeypatch.setattr('cl_app.services.signal_monitor.watchlist_symbols', lambda: [])
    monkeypatch.setattr('cl_app.services.signal_monitor.holding_symbols', lambda: [])
    manual, live = Jobs(tmp_path / 'manual'), Jobs(tmp_path / 'runs')
    manual.state.update(source_current=True, source_revision='current')
    start = live.start

    def current_start(*args, **kwargs):
        start(*args, **kwargs)
        live.state.update(source_current=True, source_revision='current', current_source_revision='current')
        return live.status()

    live.start = current_start
    service = SignalMonitor(tmp_path, screening=manual, monitor=live, now=lambda: clock[0])
    stock = {'market': 'a', 'code': 'SH.600088', 'name': 'old'}
    settings = validate_settings({'scope': 'symbols', 'symbols': [stock], 'frequencies': ['1m', '5m', '30m']})
    service._load().update(enabled=True, force=True, seed={
        'run_id': 'baseline', 'symbols': [stock], 'codes': ['a:SH.600088'],
        'screening_symbols': [stock], 'settings': settings, 'cutoffs': {}})
    yield service, manual, live, clock
    service._incremental_reader.close()


def finish(service, live, clock, *, cancelled=False, committed=None):
    if committed is not None:
        live.committed = committed
    live.finish(int(clock[0].timestamp()))
    live.state['cutoffs'] = {frequency: int(clock[0].timestamp()) // (int(frequency[:-1]) * 60)
                            * int(frequency[:-1]) * 60 for frequency in ['1m', '5m', '30m']}
    if cancelled:
        live.state.update(status='cancelled', completed=len(committed or []))
    service._consume_monitor()


def periods(live):
    return [symbol.get('_monitor_periods', live.state['settings']['frequencies'])
            for symbol in live.started[-1]['symbols']]


def test_key_periods_get_separate_commits_before_minute_work_and_do_not_repeat(monitor):
    service, _, live, clock = monitor
    for expected in [['30m'], ['5m'], ['1m']]:
        service._schedule_monitor(clock[0], None)
        assert periods(live) == [expected]
        finish(service, live, clock)
    assert len(service._load()['selection_checks']) == 3
    service._schedule_monitor(clock[0], None)
    assert len(live.started) == 3
    clock[0] += timedelta(minutes=1)
    service._schedule_monitor(clock[0], None)
    assert periods(live) == [['5m']]
    finish(service, live, clock)
    service._schedule_monitor(clock[0], None)
    assert periods(live) == [['1m']]


def test_key_boundary_interrupts_minute_work_and_requeues_without_failure(monitor):
    service, _, live, clock = monitor
    for _ in range(2):
        service._schedule_monitor(clock[0], None)
        finish(service, live, clock)
    service._schedule_monitor(clock[0], None)
    assert periods(live) == [['1m']]
    clock[0] += timedelta(minutes=1)
    service._schedule_monitor(clock[0], None)
    assert live.state['cancel_requested']
    finish(service, live, clock, cancelled=True, committed=[])
    assert not service._load()['retry_symbols']
    assert not service._load()['errors']
    service._schedule_monitor(clock[0], None)
    assert periods(live) == [['5m']]
    assert service._load()['deferred_monitor_periods']['a:SH.600088'] == ['1m']


def test_failed_key_period_does_not_hold_other_periods(monitor):
    service, _, live, clock = monitor
    service._schedule_monitor(clock[0], None)
    live.errors = [{'market': 'a', 'code': 'SH.600088', 'frequency': '30m', 'reasons': ['ENGINE_ERROR']}]
    finish(service, live, clock)
    service._schedule_monitor(clock[0], None)
    assert periods(live) == [['5m']]
    live.errors = []
    finish(service, live, clock)
    service._schedule_monitor(clock[0], None)
    assert periods(live) == [['1m']]
    assert service._load()['selection_checks']['a:SH.600088:30m']['error']


@pytest.mark.parametrize('frequency', ['30m', '5m', '1m'])
def test_notification_recheck_does_not_repeat_unrelated_period_or_starve_siblings(monitor, frequency):
    service, _, live, clock = monitor
    target = int(clock[0].timestamp()) // (int(frequency[:-1]) * 60) * int(frequency[:-1]) * 60
    service._notification_rechecks[('a:SH.600088', frequency)] = {
        'cutoff': target, 'revision': 'current', 'expires': clock[0].timestamp() + 1800}
    for expected in ['30m', '5m', '1m']:
        service._schedule_monitor(clock[0], None)
        assert periods(live) == [[expected]]
        finish(service, live, clock)
    service._schedule_monitor(clock[0], None)
    assert len(live.started) == 3
    assert not service._notification_rechecks


@pytest.mark.parametrize('frequency', ['30m', '5m', '1m'])
def test_notification_recheck_of_warm_symbol_runs_only_its_requested_period(monitor, frequency):
    service, _, live, clock = monitor
    for _ in range(3):
        service._schedule_monitor(clock[0], None)
        finish(service, live, clock)
    check = service._load()['selection_checks'][f'a:SH.600088:{frequency}']
    target = check['cutoff']
    check['cutoff'] -= int(frequency[:-1]) * 60
    service._notification_rechecks[('a:SH.600088', frequency)] = {
        'cutoff': target, 'revision': 'current', 'expires': clock[0].timestamp() + 1800}
    service._schedule_monitor(clock[0], None)
    assert periods(live) == [[frequency]] and len(live.started) == 4
    finish(service, live, clock)
    service._schedule_monitor(clock[0], None)
    assert len(live.started) == 4 and not service._notification_rechecks


@pytest.mark.parametrize('frequency,minute', [('30m', 34), ('5m', 36)])
def test_period_retry_survives_faster_minute_clock_and_sibling_success(monitor, frequency, minute):
    service, _, live, clock = monitor
    clock[0] = clock[0].replace(minute=minute)
    key = 'a:SH.600088'
    for _ in range(3):
        service._schedule_monitor(clock[0], None)
        live.errors = ([{'market': 'a', 'code': 'SH.600088', 'frequency': frequency,
                        'reasons': ['ENGINE_ERROR']}] if periods(live) == [[frequency]] else [])
        finish(service, live, clock)
    target = service._load()['retry_symbols'][key]['period_cutoff']
    assert service._load()['retry_symbols'][key]['retry_at'] == clock[0].timestamp() + 60
    # An independent successful minute before the due time cannot erase it.
    clock[0] += timedelta(seconds=30)
    service._schedule_monitor(clock[0], None)
    assert len(live.started) == 3
    clock[0] += timedelta(seconds=30)
    live.errors = []
    service._schedule_monitor(clock[0], None)
    assert periods(live) == [[frequency]]
    assert service._load()['retry_symbols'][key]['period_cutoff'] == target
    finish(service, live, clock)
    assert key not in service._load()['retry_symbols']
    assert not service._load()['selection_checks'][f'{key}:{frequency}']['error']


def test_two_failed_periods_keep_independent_retry_budgets_and_do_not_erase_each_other(monitor):
    service, _, live, clock = monitor
    key = 'a:SH.600088'
    clock[0] = clock[0].replace(minute=36)
    for _ in range(3):
        service._schedule_monitor(clock[0], None)
        frequency = periods(live)[0][0]
        live.errors = ([{'market': 'a', 'code': 'SH.600088', 'frequency': frequency,
                         'reasons': ['ENGINE_ERROR']}] if frequency != '1m' else [])
        finish(service, live, clock)
    state = service._load()
    assert set(state['retry_symbols'][key]['by_period']) == {'30m', '5m'}
    clock[0] += timedelta(minutes=1)
    service._schedule_monitor(clock[0], None)
    assert periods(live) == [['30m']]
    live.errors = [{'market':'a','code':'SH.600088','frequency':'30m','reasons':['ENGINE_ERROR']}]
    finish(service, live, clock)
    records = state['retry_symbols'][key]['by_period']
    assert records['30m']['failures'] == 2 and records['5m']['failures'] == 1
    service._schedule_monitor(clock[0], None)
    assert periods(live) == [['5m']]
    live.errors = []
    finish(service, live, clock)
    assert state['retry_symbols'][key]['periods'] == ['30m']
    assert state['retry_symbols'][key]['failures'] == 2


def test_new_bar_of_failed_period_resets_its_own_exhausted_budget(monitor):
    service, _, live, clock = monitor
    key = 'a:SH.600088'
    for _ in range(3):
        service._schedule_monitor(clock[0], None)
        live.errors = ([{'market':'a','code':'SH.600088','frequency':'30m','reasons':['ENGINE_ERROR']}]
                       if periods(live) == [['30m']] else [])
        finish(service, live, clock)
    service._load()['retry_symbols'][key].update(failures=3, retry_at=None)
    clock[0] = clock[0].replace(hour=11, minute=0)
    live.errors = []
    service._schedule_monitor(clock[0], None)
    assert periods(live) == [['30m']]
    assert key not in service._load()['retry_symbols']


def test_slow_thirty_minute_tail_yields_to_five_minute_sibling(monitor):
    service, _, live, clock = monitor
    service._schedule_monitor(clock[0], None)
    assert periods(live) == [['30m']]
    clock[0] += timedelta(seconds=30)
    service._schedule_monitor(clock[0], None)
    assert live.state['cancel_requested']
    finish(service, live, clock, cancelled=True, committed=[])
    service._schedule_monitor(clock[0], None)
    assert periods(live) == [['5m']]
    finish(service, live, clock)
    service._schedule_monitor(clock[0], None)
    assert periods(live) == [['30m']]
    # The faster symbol clock advances while the same 30m target remains.
    # A second kill would discard the cold work indefinitely on every tick.
    clock[0] += timedelta(minutes=1)
    service._schedule_monitor(clock[0], None)
    assert not live.state.get('cancel_requested')


def test_cancelled_legacy_partial_record_keeps_unfinished_period_unchecked(monitor):
    service, _, live, clock = monitor
    service._schedule_monitor(clock[0], None)
    live.state['settings']['symbols'][0]['_monitor_periods'] = ['30m', '5m']
    state = service._load()
    state['active_periods'] = {'a:SH.600088': ['30m', '5m']}
    state['deferred_monitor_periods'] = {}
    state['yielding_monitor_run'] = live.state['run_id']
    committed = live.committed_results

    def partial(*args, **kwargs):
        result = committed(*args, **kwargs)
        result['verified_periods'] = [p for p in result['verified_periods'] if p['frequency'] == '30m']
        return result

    live.committed_results = partial
    stock = live.state['settings']['symbols'][0]
    finish(service, live, clock, cancelled=True, committed=[stock])
    assert not state['errors'] and not state['retry_symbols']
    assert set(state['selection_checks']) == {'a:SH.600088:30m'}
    assert state['deferred_monitor_periods']['a:SH.600088'] == ['5m']
    service._schedule_monitor(clock[0], None)
    assert periods(live) == [['5m']]


def publish_candidate(manual, *, code='SZ.000001', completed=1):
    stock = {'market': 'a', 'code': code, 'name': 'new'}
    manual.start(validate_settings({'scope': 'symbols', 'symbols': [stock], 'frequencies': ['5m']}))
    manual.state.update(source_current=True, source_revision='current', completed=completed)
    manual.rows = [{**signal(code), 'evidence_verified': True, 'evidence': {'manifest': 'frozen'},
                    'reasons': [], 'audit': {'all_required_checks': True}}]
    return stock


def consume_increment(service, manual, clock):
    service._consume_screening_increment(manual.status(), service._control_version, clock[0])
    if service._incremental_reader._future is not None:
        service._incremental_reader._future.result(timeout=2)
        service._consume_screening_increment(manual.status(), service._control_version, clock[0])
    service._sync_watchlist()


def test_increment_is_additive_idempotent_and_sets_no_history_notification_boundary(monitor):
    service, manual, _, clock = monitor
    original = deepcopy(service._load()['seed']['screening_symbols'])
    publish_candidate(manual)
    consume_increment(service, manual, clock)
    state = service._load()
    assert state['seed']['run_id'] == 'baseline'
    assert state['seed']['codes'] == ['a:SH.600088', 'a:SZ.000001']
    assert state['seed']['screening_symbols'] == original
    assert not state['signals'] and not state['events']
    first_cutoff = state['notification_symbol_since']['a:SZ.000001']
    clock[0] += timedelta(seconds=10)
    consume_increment(service, manual, clock)
    assert state['screening_monitor_cursor']['completed'] == 1
    assert state['notification_symbol_since']['a:SZ.000001'] == first_cutoff


@pytest.mark.parametrize('damage', ['uncommitted', 'unverified', 'audit', 'period_error', 'symbol_error', 'forming'])
def test_partial_or_unaudited_screening_rows_never_enter_pool(monitor, damage):
    service, manual, _, clock = monitor
    publish_candidate(manual)
    if damage == 'uncommitted':
        manual.state['completed'] = 0
    elif damage == 'unverified':
        manual.rows[0]['evidence_verified'] = False
    elif damage == 'audit':
        manual.rows[0]['audit']['all_required_checks'] = False
    elif damage == 'forming':
        manual.rows[0]['point']['status'] = 'forming'
    else:
        manual.errors = [{'market': 'a', 'code': 'SZ.000001', 'reasons': ['ENGINE_ERROR'],
                          **({'frequency': '5m'} if damage == 'period_error' else {})}]
    consume_increment(service, manual, clock)
    assert service._load()['seed']['codes'] == ['a:SH.600088']


@pytest.mark.parametrize('status', ['cancelled', 'failed', 'interrupted'])
def test_failed_increment_retains_baseline_and_independent_holding(monitor, monkeypatch, status):
    service, manual, _, clock = monitor
    stock = publish_candidate(manual)
    consume_increment(service, manual, clock)
    monkeypatch.setattr('cl_app.services.signal_monitor.holding_symbols', lambda: [stock])
    manual.state['status'] = status
    consume_increment(service, manual, clock)
    seed = service._load()['seed']
    assert seed['run_id'] == 'baseline'
    assert seed['screening_symbols'][0]['code'] == 'SH.600088'
    assert seed['codes'] == ['a:SH.600088', 'a:SZ.000001']
    assert not seed.get('incremental_screening_symbols')


def test_repeated_symbol_period_records_advance_cursor_without_duplicate_admission(monitor):
    service, manual, _, clock = monitor
    stock = publish_candidate(manual)
    consume_increment(service, manual, clock)
    since = service._load()['notification_symbol_since']['a:SZ.000001']
    manual.committed = [stock, stock]
    manual.state['completed'] = 2
    clock[0] += timedelta(seconds=10)
    consume_increment(service, manual, clock)
    assert service._load()['screening_monitor_cursor']['completed'] == 2
    assert service._load()['seed']['codes'].count('a:SZ.000001') == 1
    assert service._load()['notification_symbol_since']['a:SZ.000001'] == since


def test_incremental_candidate_historical_signal_is_visible_without_notification(monitor):
    service, manual, live, clock = monitor
    publish_candidate(manual)
    consume_increment(service, manual, clock)
    for _ in range(2):
        service._schedule_monitor(clock[0], None)
        live.rows = [signal('SZ.000001')]
        finish(service, live, clock)
    events = [event for event in service._load()['events'] if event['code'] == 'SZ.000001']
    assert len(events) == 1
    assert events[0]['external_notifications'] is False
    assert events[0]['notification_suppressed_reason'] == 'CONFIRMED_BEFORE_POOL_ADMISSION'
    assert not service._load()['pending_notifications']


def test_blocked_screening_audit_does_not_delay_key_schedule_or_start_more_readers(monitor):
    service, manual, live, clock = monitor
    publish_candidate(manual)
    entered, release = threading.Event(), threading.Event()
    original = manual.committed_results
    calls = []

    def slow(*args, **kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(timeout=3)
        return original(*args, **kwargs)

    manual.committed_results = slow
    try:
        service._consume_screening_increment(manual.status(), service._control_version, clock[0])
        assert entered.wait(timeout=1)
        for _ in range(3):
            service._consume_screening_increment(manual.status(), service._control_version, clock[0])
        service._schedule_monitor(clock[0], None)
        assert periods(live) == [['30m']]
        finish(service, live, clock)
        service._schedule_monitor(clock[0], None)
        assert periods(live) == [['5m']]
        assert calls == [1]
    finally:
        release.set()
        service._incremental_reader._future.result(timeout=2)


def test_cancelled_generation_cannot_admit_late_audited_candidate(monitor):
    service, manual, _, clock = monitor
    publish_candidate(manual)
    entered, release = threading.Event(), threading.Event()
    result = manual.committed_results()

    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=3)
        return result

    manual.committed_results = slow
    try:
        service._consume_screening_increment(manual.status(), service._control_version, clock[0])
        assert entered.wait(timeout=1)
        manual.state['status'] = 'cancelled'
        service._consume_screening_increment(manual.status(), service._control_version, clock[0])
    finally:
        release.set()
        service._incremental_reader._future.result(timeout=2)
    consume_increment(service, manual, clock)
    assert service._load()['seed']['codes'] == ['a:SH.600088']


def test_stop_discards_reader_and_resume_never_overlaps_old_inflight_read(monitor):
    service, manual, _, clock = monitor
    publish_candidate(manual)
    entered, release = threading.Event(), threading.Event()
    original = manual.committed_results
    calls = []

    def slow(*args, **kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(timeout=3)
        return original(*args, **kwargs)

    manual.committed_results = slow
    reader = service._incremental_reader
    try:
        service._consume_screening_increment(manual.status(), service._control_version, clock[0])
        assert entered.wait(timeout=1)
        service.stop_runtime()
        reader.resume()
        service._stop.clear()
        service._consume_screening_increment(manual.status(), service._control_version, clock[0])
        assert calls == [1]
    finally:
        release.set()
        reader._future.result(timeout=2)
    consume_increment(service, manual, clock)
    assert calls == [1, 1]


def test_increment_source_page_uses_only_admitted_period_overlay_until_cancelled(monitor, monkeypatch):
    from cl_app.services.screening_workbench import _live_rows
    service, manual, _, clock = monitor
    publish_candidate(manual)
    consume_increment(service, manual, clock)
    source = manual.state['run_id']
    state = service._load()
    check = {'symbol': 'a:SZ.000001', 'revision': 'current', 'cutoff': int(clock[0].timestamp()),
             'checked_at': int(clock[0].timestamp()), 'identities': [], 'error': False}
    state['selection_checks'] = {'a:SZ.000001:5m': check, 'a:SZ.000001:30m': check,
                                 'a:SH.600088:5m': {**check, 'symbol': 'a:SH.600088'}}
    assert set(service.selection_overlay(source, 'current')) == {'a:SZ.000001:5m'}
    assert service.selection_overlay(source, 'stale') == {}
    assert service.selection_overlay('unrelated', 'current') == {}
    monkeypatch.setattr('cl_app.services.signal_monitor.service', service)
    rows, removed, _, unverified = _live_rows([signal('SZ.000001')], source, 'current')
    assert rows == [] and removed == 1 and unverified == 0
    manual.state['status'] = 'cancelled'
    consume_increment(service, manual, clock)
    assert service.selection_overlay(source, 'current') == {}


def test_empty_prefix_is_retried_when_completed_grows_without_cursor_progress(monitor):
    service, manual, _, clock = monitor
    stock = publish_candidate(manual)
    original = manual.committed_results
    calls = []

    def initially_empty(*args, **kwargs):
        result = original(*args, **kwargs)
        calls.append(1)
        if len(calls) == 1:
            result.update(results_through=0, processed_symbols=[], selected=[])
        return result

    manual.committed_results = initially_empty
    consume_increment(service, manual, clock)
    assert not service._load().get('screening_monitor_cursor')
    manual.committed = [stock, stock]
    manual.state['completed'] = 2
    consume_increment(service, manual, clock)
    assert service._load()['screening_monitor_cursor']['completed'] == 2
    assert calls == [1, 1]


def test_failed_async_reader_is_retryable_at_the_same_cursor(monitor):
    service, manual, _, clock = monitor
    publish_candidate(manual)
    original = manual.committed_results
    calls = []

    def once_failed(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise MonitorResultsReadError(manual.state['run_id'], 'ValueError')
        return original(*args, **kwargs)

    manual.committed_results = once_failed
    service._consume_screening_increment(manual.status(), service._control_version, clock[0])
    with pytest.raises(MonitorResultsReadError):
        service._incremental_reader._future.result(timeout=2)
    service._consume_screening_increment(manual.status(), service._control_version, clock[0])
    consume_increment(service, manual, clock)
    assert calls == [1, 1]
    assert service._load()['screening_monitor_cursor']['completed'] == 1


def test_run_switch_rejects_old_future_then_reads_new_run_without_overlap(monitor):
    service, manual, _, clock = monitor
    publish_candidate(manual)
    original = manual.committed_results
    old_result = original()
    entered, release = threading.Event(), threading.Event()
    calls = []

    def slow_old(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            entered.set()
            assert release.wait(timeout=3)
            return old_result
        return original(*args, **kwargs)

    manual.committed_results = slow_old
    try:
        service._consume_screening_increment(manual.status(), service._control_version, clock[0])
        assert entered.wait(timeout=1)
        publish_candidate(manual, code='SZ.000002')
        service._consume_screening_increment(manual.status(), service._control_version, clock[0])
        assert calls == [1]
    finally:
        release.set()
        service._incremental_reader._future.result(timeout=2)
    consume_increment(service, manual, clock)
    assert service._load()['seed']['codes'] == ['a:SH.600088', 'a:SZ.000002']
    assert calls == [1, 1]


def test_brief_keeps_stock_counts_distinct_from_period_task_counts():
    status = {'run_id': 'run', 'status': 'running', 'completed': 7, 'total': 30,
              'progress_unit': 'symbol_period', 'completed_symbols': 2, 'total_symbols': 10}
    assert _brief(status) == status
