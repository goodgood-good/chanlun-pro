"""Slow read-only dependencies yield without authorizing late notifications."""

from copy import deepcopy
from datetime import timedelta
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from chanlun.screening import markets
from cl_app.services import notification_validation, screening, signal_monitor
from cl_app.services.dingtalk_notifications import DingTalkOutbox
from tests.web.test_dingtalk_notifications import Transport
from tests.web.test_notification_state_freshness import ready_event as ready_event
from tests.web.test_signal_monitor import setup as setup


def test_a_completed_dependency_wakes_dispatch_without_authorizing_the_receipt():
    release,woken=threading.Event(),threading.Event()
    reads=notification_validation.DependencyReads(wake=woken.set)
    def slow():
        assert release.wait(5)
        return 'read-only-proof'
    try:
        assert reads.read('ordinary',slow,time.monotonic()+.01,key='receipt',context_key='epoch') is None
        assert not woken.is_set()
    finally:
        release_lane(reads,'ordinary',release)
    assert woken.wait(1)
    assert reads.read('ordinary',lambda:pytest.fail('reuse the completed read'),time.monotonic()+.1,
                      key='receipt',context_key='epoch')=='read-only-proof'


def release_lane(reads, lane, release):
    release.set()
    future = reads._lanes[lane]
    if future is not None:
        future.result(timeout=5)


@pytest.mark.parametrize('budget_yield,expected_delay', [(True, .5), (False, 5)])
def test_inline_chart_wait_uses_the_same_bounded_dependency_retry(tmp_path, budget_yield, expected_delay):
    from tests.web.test_consolidation_messages import alert
    rows = [alert()]
    validator = lambda events: ([], notification_validation.DeferredEvents(events) if budget_yield else events)
    box = DingTalkOutbox(tmp_path / 'inline.sqlite', now=lambda: 1000, charts=True, signal_validator=validator)
    box.signal_events(rows, since=0)
    assert box.charts.process_one()
    with box.charts._db() as db:
        receipt = db.execute('SELECT status,attempts,next_try FROM jobs').fetchone()
    assert tuple(receipt) == ('pending', 0, 1000 + expected_delay)


def test_dependency_lanes_have_no_queue_and_never_reuse_a_late_result():
    reads = notification_validation.DependencyReads()
    entered, release = threading.Event(), threading.Event()
    calls = []

    def slow():
        calls.append('old')
        entered.set()
        assert release.wait(5)
        return 'late-old-value'

    try:
        assert reads.read('ordinary', slow, time.monotonic() + .03) is None
        assert entered.is_set()
        for _ in range(20):
            assert reads.read('ordinary', slow, time.monotonic() + .03) is None
        assert calls == ['old']
        assert reads.read('holding', lambda: 'fresh-holding', time.monotonic() + .1) == 'fresh-holding'
        assert len(reads._lanes) == 2
    finally:
        release_lane(reads, 'ordinary', release)
    assert reads.read('ordinary', lambda: 'new-value', time.monotonic() + .1) == 'new-value'


@pytest.mark.parametrize('invalidation', ['completed_age', 'observation_age', 'request_key'])
def test_completed_dependency_cannot_escape_its_identity_or_age_bound(invalidation):
    reads = notification_validation.DependencyReads()
    release = threading.Event()

    def slow():
        assert release.wait(5)
        return 'old'

    try:
        assert reads.read('holding', slow, time.monotonic() + .01, key='original') is None
    finally:
        release_lane(reads, 'holding', release)
    previous = reads._lanes['holding']
    if invalidation == 'completed_age':
        previous.completed_at -= 1.01
    elif invalidation == 'observation_age':
        previous.started_at -= 2.01
    key = 'different' if invalidation == 'request_key' else 'original'
    if invalidation == 'request_key':
        assert reads.read('holding', lambda: 'fresh', time.monotonic() + .1, key=key) is None
        assert reads.read('holding', lambda: 'unused', time.monotonic() + .1, key='original') == 'old'
    assert reads.read('holding', lambda: 'fresh', time.monotonic() + .1, key=key) == 'fresh'


@pytest.mark.parametrize('change', ['control', 'scan_types', 'holding_removed'])
def test_completed_result_is_invalidated_by_control_and_holding_epoch(ready_event, monkeypatch, change):
    service, live, original_event = ready_event
    monkeypatch.setattr(notification_validation, 'VALIDATION_BUDGET_SECONDS', .03)
    event = {**original_event, 'signal_type': 'holding_risk', 'point_type': 'risk_stop',
             'holding': True, 'binding_id': 'binding'}
    state = service._load()
    state.update(holding_source_current=True, holding_symbols=['a:SH.600088'])
    state['holding_risk_plans']['a:SH.600088:5m'] = {
        'active': True, 'binding_id': 'binding', 'stop_triggered_at': event['available_at']}
    monkeypatch.setattr(signal_monitor, 'holding_symbols', lambda: [{'market': 'a', 'code': 'SH.600088'}])
    release = threading.Event()
    original_status = live.status
    calls = []

    def source():
        calls.append(1)
        if len(calls) == 1:
            assert release.wait(5)
        return original_status()

    monkeypatch.setattr(live, 'status', source)
    try:
        assert service._validate_notification_events([event]) == ([], [event])
    finally:
        release_lane(service._notification_dependency_reads, 'holding', release)
    if change == 'control':
        service._control_version += 1
    elif change == 'scan_types':
        state['seed']['settings']['point_types'] = ['1sell']
    else:
        # Even a lagging holdings dependency must not resurrect membership
        # which the current monitor state has already removed.
        state['holding_symbols'] = []
    valid, pending = service._validate_notification_events([event])
    assert calls == [1, 1], 'a prior epoch cannot supply the resumed dependency result'
    assert not pending
    assert valid == ([] if change == 'holding_removed' else [event])


def test_state_reset_during_dependencies_does_not_trigger_cold_recovery(ready_event, monkeypatch):
    service, live, event = ready_event
    original = live.status

    def reset():
        service._state = None
        return original()

    monkeypatch.setattr(live, 'status', reset)
    loader = Mock(side_effect=AssertionError('cold state I/O is outside the notification budget'))
    monkeypatch.setattr(service, '_load', loader)
    assert service._validate_notification_events([event]) == ([], [event])
    loader.assert_not_called()


def test_notification_source_does_not_read_or_reconcile_a_run(tmp_path, monkeypatch):
    manager = screening.ScreeningManager(tmp_path)
    revision = Mock(return_value='current-revision')
    monkeypatch.setattr(screening, 'source_revision', revision)
    monkeypatch.setattr(manager, '_directory', Mock(side_effect=AssertionError('run I/O forbidden')))
    monkeypatch.setattr(manager, '_freshness', Mock(side_effect=AssertionError('unrelated calendars forbidden')))
    assert manager.notification_source_status() == {'current_source_revision': 'current-revision'}
    assert manager.notification_source_status() == {'current_source_revision': 'current-revision'}
    assert revision.call_count == 1
    manager._revision_checked -= 6
    revision.return_value = 'new-revision'
    assert manager.notification_source_status() == {'current_source_revision': 'new-revision'}


def test_slow_revision_read_eventually_delivers_through_real_outbox_retries(ready_event, monkeypatch, tmp_path):
    service, _live, event = ready_event
    monkeypatch.setattr(notification_validation, 'VALIDATION_BUDGET_SECONDS', .03)
    clock = [service.now().timestamp()]
    monkeypatch.setattr(screening, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    manager = screening.ScreeningManager(tmp_path / 'read-only-source')
    monkeypatch.setattr(service, '_monitor', manager)
    state = service._load()
    state['selection_checks']['a:SH.600088:5m']['revision'] = 'current-revision'
    entered, release = threading.Event(), threading.Event()
    hashes = []

    def slow_revision():
        hashes.append(clock[0])
        entered.set()
        assert release.wait(5)
        return 'current-revision'

    monkeypatch.setattr(screening, 'source_revision', slow_revision)
    transport = Transport()
    box = DingTalkOutbox(tmp_path / 'slow-source.sqlite', transport=transport, now=lambda: clock[0],
                        signal_validator=service._validate_notification_events)
    box.signal_events([event], since=0)
    try:
        box.deliver_one()
        assert entered.is_set() and not transport.messages
        # The slow reader outlives the old five-second hash cache window.
        # Multiple real receipt retries retain one reader, not a thread queue.
        for _ in range(3):
            with box._read_connect() as db:
                receipt = db.execute('SELECT next_try,attempts FROM outbox').fetchone()
            assert receipt['next_try'] == clock[0] + .5 and receipt['attempts'] == 0
            clock[0] += 2
            box.deliver_one()
        assert len(hashes) == 1 and not transport.messages
    finally:
        release_lane(service._notification_dependency_reads, 'ordinary', release)
    assert manager._revision_checked == clock[0], 'cache age begins only when its hash is complete'
    clock[0] += 1
    box.deliver_one()
    assert len(transport.messages) == 1 and len(hashes) == 1
    with box._read_connect() as db:
        assert db.execute('SELECT status,attempts FROM outbox').fetchone()[:] == ('sent', 1)


@pytest.mark.parametrize('read_seconds', [.25, 1.25])
def test_stably_slow_holding_read_eventually_sends_once_via_outbox(ready_event, monkeypatch, tmp_path, read_seconds):
    service, _live, original = ready_event
    event = {**original, 'signal_type': 'holding_risk', 'point_type': 'risk_stop',
             'holding': True, 'binding_id': 'binding'}
    state = service._load()
    state.update(holding_source_current=True, holding_symbols=['a:SH.600088'])
    state['holding_risk_plans']['a:SH.600088:5m'] = {
        'active': True, 'binding_id': 'binding', 'stop_triggered_at': event['available_at']}
    calls = []
    release = threading.Event()
    clock = [0.]

    def slow_holdings():
        calls.append(1)
        assert release.wait(5)
        return [{'market': 'a', 'code': 'SH.600088'}]

    monkeypatch.setattr(signal_monitor, 'holding_symbols', slow_holdings)
    # Model dependency age and outbox time together. Durable SQLite writes on
    # a busy host must not spend the read's one-second handoff window. The
    # real wall-budget tests separately verify that Future waits are bounded.
    monkeypatch.setattr(notification_validation, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    base = service.now().timestamp()
    now = lambda: base + clock[0]
    transport = Transport()
    box = DingTalkOutbox(tmp_path / 'slow-holding.sqlite', transport=transport, now=now,
                        signal_validator=service._validate_notification_events)
    box.signal_events([event], since=0)
    try:
        box.deliver_one()
        assert not transport.messages
        clock[0] = read_seconds
        release_lane(service._notification_dependency_reads, 'holding', release)
        clock[0] += .25
        box.deliver_one()
        assert len(transport.messages) == 1 and calls == [1]
        box.signal_events([event], since=0)
        box.deliver_one()
        assert len(transport.messages) == 1
    finally:
        release_lane(service._notification_dependency_reads, 'holding', release)


def test_two_slow_holding_receipts_cannot_evict_each_others_completed_reads(ready_event, monkeypatch, tmp_path):
    service, _live, original = ready_event
    monkeypatch.setattr(notification_validation, 'VALIDATION_BUDGET_SECONDS', .03)
    state = service._load()
    events = [{**original, 'id': f'risk-{index}', 'code': code, 'signal_type': 'holding_risk',
               'point_type': 'risk_stop', 'holding': True, 'binding_id': f'binding-{index}', 'name': f'held_{index}'}
              for index, code in enumerate(('SH.600088', 'SH.600089'))]
    holdings = [{'market': event['market'], 'code': event['code']} for event in events]
    state.update(holding_source_current=True, holding_symbols=[f"a:{event['code']}" for event in events])
    for event in events:
        state['holding_risk_plans'][f"a:{event['code']}:5m"] = {
            'active': True, 'binding_id': event['binding_id'], 'stop_triggered_at': event['available_at']}
    releases = [threading.Event(), threading.Event()]
    reads = []

    def gated_holdings():
        index = len(reads)
        reads.append(index)
        assert index < 2 and releases[index].wait(5)
        return holdings

    monkeypatch.setattr(signal_monitor, 'holding_symbols', gated_holdings)
    base = service.now().timestamp()
    clock = [base]
    # This case models the .25/.3/.5 second handoff, including the bounded
    # completed-result ages. Host SQLite latency must not advance only the
    # monotonic half of that scenario while its outbox clock remains frozen.
    # Future.result still uses its real bounded wait; wall-budget tests above
    # independently cover slow reads against the real monotonic clock.
    monkeypatch.setattr(notification_validation, 'time', SimpleNamespace(monotonic=lambda: clock[0] - base))
    transport = Transport()
    box = DingTalkOutbox(tmp_path / 'two-holdings.sqlite', transport=transport, now=lambda: clock[0],
                        signal_validator=service._validate_notification_events)
    try:
        box.signal_events([events[0]], since=0)
        box.deliver_one()  # C yields; its receipt is due at +0.5 seconds.
        clock[0] = base + .25
        release_lane(service._notification_dependency_reads, 'holding', releases[0])
        clock[0] = base + .3
        box.signal_events([events[1]], since=0)
        box.deliver_one()  # D cannot discard C's completed dependency snapshot.
        assert reads == [0] and not transport.messages
        clock[0] = base + .5
        box.deliver_one()
        assert len(transport.messages) == 1 and 'held_0' in transport.messages[0]
        clock[0] = base + 4.5  # Preserve the HTTP pacing before trying D.
        box.deliver_one()
        assert reads == [0, 1] and len(transport.messages) == 1
        release_lane(service._notification_dependency_reads, 'holding', releases[1])
        clock[0] = base + 5
        box.deliver_one()
        assert len(transport.messages) == 2 and 'held_1' in transport.messages[1]
        with box._read_connect() as db:
            assert [tuple(row) for row in db.execute('SELECT status,attempts FROM outbox')] == [('sent', 1), ('sent', 1)]
    finally:
        for release in releases:
            release.set()
        future = service._notification_dependency_reads._lanes['holding']
        if future is not None:
            future.result(timeout=5)


def test_holding_read_is_pure_and_preserves_first_row_order_and_projection(monkeypatch):
    rows = [SimpleNamespace(market=market, stock_code=code, stock_name=name)
            for market, code, name in [('a', 'SH.600088', 'first'), ('us', 'AAPL.US', 'second'),
                                       ('a', 'SH.600088', 'duplicate')]]
    reader = Mock(return_value=rows)
    create = Mock(side_effect=AssertionError('read must not create groups'))
    monkeypatch.setattr('chanlun.persistence.db.db.zx_get_global_group_stocks', reader)
    monkeypatch.setattr('chanlun.persistence.db.db.zx_add_global_group', create)
    assert markets.holding_symbols() == [
        {'market': 'a', 'code': 'SH.600088', 'name': 'first', 'origin': 'watchlist'},
        {'market': 'us', 'code': 'AAPL.US', 'name': 'second', 'origin': 'watchlist'},
    ]
    reader.return_value = []
    assert markets.holding_symbols() == []
    create.assert_not_called()
    reader.return_value = [SimpleNamespace(market='a', stock_code='../bad', stock_name='bad')]
    with pytest.raises(ValueError, match='持仓分组'):
        markets.holding_symbols()


def test_monitor_lock_wait_is_bounded_and_does_not_start_dependency_work(ready_event, monkeypatch):
    service, live, event = ready_event
    monkeypatch.setattr(notification_validation, 'VALIDATION_BUDGET_SECONDS', .04)
    entered, release = threading.Event(), threading.Event()
    status = Mock(wraps=live.status)
    monkeypatch.setattr(live, 'status', status)

    def hold():
        with service._lock:
            entered.set()
            assert release.wait(5)

    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert entered.wait(5)
        started = time.monotonic()
        assert service._validate_notification_events([event]) == ([], [event])
        assert time.monotonic() - started < .4
        status.assert_not_called()
    finally:
        release.set()
        thread.join(5)
    assert service._validate_notification_events([event])[0]


def test_minute_boundary_during_dependency_reads_cannot_authorize_old_cutoff(ready_event, monkeypatch):
    service, _live, event = ready_event
    initial = service.now()
    clock = [initial]
    monkeypatch.setattr(service, 'now', lambda: clock[0])
    original = signal_monitor.closed_bar_cutoff

    def crossing(*args, **kwargs):
        result = original(*args, **kwargs)
        clock[0] = initial + timedelta(minutes=1)
        return result

    monkeypatch.setattr(signal_monitor, 'closed_bar_cutoff', crossing)
    assert service._validate_notification_events([event]) == ([], [event])


def test_final_state_lock_also_obeys_the_original_budget(ready_event, monkeypatch):
    service, live, event = ready_event
    monkeypatch.setattr(notification_validation, 'VALIDATION_BUDGET_SECONDS', .04)
    entered, release = threading.Event(), threading.Event()
    original = live.status

    def hold():
        with service._lock:
            entered.set()
            assert release.wait(5)

    thread = threading.Thread(target=hold)

    def source():
        thread.start()
        assert entered.wait(5)
        return original()

    monkeypatch.setattr(live, 'status', source)
    try:
        started = time.monotonic()
        assert service._validate_notification_events([event]) == ([], [event])
        assert time.monotonic() - started < .4
    finally:
        release.set()
        thread.join(5)


@pytest.mark.parametrize('late_change', ['exit', 'risk_removed'])
def test_late_read_never_mutates_recheck_or_validates_superseded_state(ready_event, monkeypatch, late_change):
    service, live, event = ready_event
    monkeypatch.setattr(notification_validation, 'VALIDATION_BUDGET_SECONDS', .04)
    original = live.status
    entered, release = threading.Event(), threading.Event()

    def slow():
        result = original()
        entered.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(live, 'status', slow)
    state = service._load()
    if late_change == 'risk_removed':
        event = {**event, 'signal_type': 'holding_risk', 'point_type': 'risk_stop',
                 'holding': True, 'binding_id': 'binding'}
        state['holding_source_current'] = True
        state['holding_symbols'] = ['a:SH.600088']
        state['holding_risk_plans']['a:SH.600088:5m'] = {
            'active': True, 'binding_id': 'binding', 'stop_triggered_at': event['available_at']}
        monkeypatch.setattr(signal_monitor, 'holding_symbols', lambda: [{'market': 'a', 'code': 'SH.600088'}])
    lane = 'holding' if late_change == 'risk_removed' else 'ordinary'
    try:
        assert service._validate_notification_events([event]) == ([], [event])
        assert entered.is_set()
        if late_change == 'exit':
            state['signals'].clear()
            state['selection_checks']['a:SH.600088:5m']['identities'] = []
        else:
            state['holding_risk_plans']['a:SH.600088:5m']['active'] = False
        rechecks = deepcopy(service._notification_rechecks)
    finally:
        release_lane(service._notification_dependency_reads, lane, release)
    assert service._notification_rechecks == rechecks
    monkeypatch.setattr(live, 'status', original)
    assert service._validate_notification_events([event]) == ([], [])


def test_slow_ordinary_dependency_yields_dispatch_to_new_holding_without_http_cooldown(
    ready_event, monkeypatch, tmp_path,
):
    service, live, ordinary = ready_event
    monkeypatch.setattr(notification_validation, 'VALIDATION_BUDGET_SECONDS', .04)
    original = live.status
    entered, release = threading.Event(), threading.Event()
    original_calls = []

    def source():
        if threading.current_thread().name == 'NotificationRead-ordinary':
            original_calls.append(1)
            entered.set()
            assert release.wait(5)
        return original()

    monkeypatch.setattr(live, 'status', source)
    monkeypatch.setattr(signal_monitor, 'holding_symbols', lambda: [{'market': 'a', 'code': 'SH.600088'}])
    state = service._load()
    state['holding_source_current'] = True
    held = {**ordinary, 'id': 'held-point', 'holding': True, 'name': 'holding_probe'}
    state['signals'][held['id']] = deepcopy(held)
    state['selection_checks']['a:SH.600088:5m']['identities'].append(held['id'])
    clock = [service.now().timestamp()]
    transport = Transport()
    box = DingTalkOutbox(tmp_path / 'budget.sqlite', transport=transport, now=lambda: clock[0],
                        signal_validator=service._validate_notification_events)
    box.signal_events([ordinary], since=0)
    try:
        started = time.monotonic()
        box.deliver_one()
        assert entered.is_set() and time.monotonic() - started < .5
        assert not transport.messages
        peer = DingTalkOutbox(box.path, transport=transport, now=lambda: clock[0],
                             signal_validator=service._validate_notification_events)
        peer.signal_events([held], since=0)
        peer.deliver_one()
        assert len(transport.messages) == 1 and 'holding_probe' in transport.messages[0]
        assert original_calls == [1]
        with peer._read_connect() as db:
            rows = list(db.execute('SELECT status,attempts FROM outbox ORDER BY rowid'))
        assert [(row['status'], row['attempts']) for row in rows] == [('retry', 0), ('sent', 1)]
    finally:
        release_lane(service._notification_dependency_reads, 'ordinary', release)
