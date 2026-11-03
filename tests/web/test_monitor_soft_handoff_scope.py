from datetime import timedelta

import pytest

from tests.web.test_monitor_incremental_priority import monitor as monitor
from tests.web.test_monitor_priority_handoff_epochs import protected_tail


@pytest.mark.parametrize('holding', [True, False])
def test_only_actual_due_holding_or_notification_uses_private_soft_handoff(monitor, holding):
    service, live, clock, state, key, frequency, target = protected_tail(monitor, holding=holding)
    calls = []
    service._source_prefetch_control = {'enabled': True, 'version': service._control_version}
    live.handoff_for_priority = lambda run, control: calls.append((run, control))
    service._yield_live_for_priority(clock[0], live.status(), service._control_version,
                                    state['seed']['run_id'], state['active_monitor_run'])
    assert calls == [(state['active_monitor_run'], service._source_prefetch_control)]
    assert not live.state.get('cancel_requested')


def test_ordinary_rotation_remains_hard_even_with_a_soft_capable_manager(monitor):
    service, _, live, clock = monitor
    calls = []
    live.handoff_for_priority = lambda *args: calls.append(args)
    service._schedule_monitor(clock[0], None)
    clock[0] += timedelta(minutes=1)
    service._schedule_monitor(clock[0], None)
    assert live.state['cancel_requested']
    assert calls == []


def test_notification_backoff_does_not_authorize_a_soft_handoff(monitor):
    service, live, clock, state, key, frequency, target = protected_tail(monitor, holding=False)
    service._notification_retry_after[key] = clock[0].timestamp() + 10
    calls = []
    live.handoff_for_priority = lambda *args: calls.append(args)
    service._yield_live_for_priority(clock[0], live.status(), service._control_version,
                                    state['seed']['run_id'], state['active_monitor_run'])
    assert calls == [] and not live.state.get('cancel_requested')
