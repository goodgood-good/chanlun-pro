from copy import deepcopy
from datetime import timedelta
import json
import pytest

from tests.web.test_monitor_incremental_priority import monitor as monitor, periods, finish


def protected_tail(monitor, *, holding):
    service, _, live, clock = monitor
    state = service._load()
    slow = "a:SH.600088"
    priority = "a:SH.600000"
    state.update(force=False, monitor_fair_cursor=2)
    state["selection_checks"][slow + ":5m"] = {
        "symbol": slow,
        "revision": "current",
        "error": False,
        "identities": [],
        "cutoff": int(clock[0].timestamp()) // 300 * 300,
        "input_fingerprint": "a" * 64,
    }
    service._schedule_monitor(clock[0], None)
    assert periods(live) == [["30m"]]
    state["seed"]["symbols"].append(
        {"market": "a", "code": "SH.600000", "name": "priority"}
    )
    state["seed"]["codes"].append(priority)
    state["holding_source_current"] = True
    state["holding_symbols"] = [priority] if holding else []
    frequency = "1m" if holding else "5m"
    target = (
        int(clock[0].timestamp())
        // (int(frequency[:-1]) * 60)
        * (int(frequency[:-1]) * 60)
    )
    state["selection_checks"][priority + ":" + frequency] = {
        "symbol": priority,
        "revision": "current",
        "error": False,
        "cutoff": target - 300,
        "identities": [],
    }
    state["active_scheduled_at"] = clock[0].timestamp() - 10
    state["live_slice_yields"] = {
        slow: json.dumps([("30m", state["active_period_targets"][slow + ":30m"])])
    }
    if not holding:
        service._notification_rechecks[priority, frequency] = {
            "cutoff": target,
            "revision": "current",
            "expires": clock[0].timestamp() + 1800,
        }
    return service, live, clock, state, priority, frequency, target


@pytest.mark.parametrize("holding", [True, False])
def test_priority_rescues_a_protected_tail_once_without_faking_a_check_or_starving_it(
    monitor, holding
):
    service, live, clock, state, key, frequency, target = protected_tail(
        monitor, holding=holding
    )
    before = deepcopy(state["selection_checks"])

    def request():
        service._yield_live_for_priority(
            clock[0],
            live.status(),
            service._control_version,
            state["seed"]["run_id"],
            state["active_monitor_run"],
        )

    request()
    assert live.state.get("cancel_requested") is True
    assert state["priority_slice_yields"][key + ":" + frequency] == ["current", target]
    live.state["cancel_requested"] = False
    state["yielding_monitor_run"] = None
    request()
    assert not live.state["cancel_requested"], (
        "the same priority target must not repeatedly cancel a slow task"
    )
    for _ in range(3):
        clock[0] += timedelta(minutes=1 if holding else 5)
        if not holding:
            service._notification_rechecks[key, frequency]["cutoff"] += 300
        request()
        assert not live.state["cancel_requested"], (
            "new priority clocks must not repeatedly cancel the rescued slow target"
        )
    # A different slow target has its own bounded rescue allowance.
    slow = "a:SH.600088"
    state["active_period_targets"][slow + ":30m"] += 1800
    state["live_slice_yields"][slow] = json.dumps(
        [("30m", state["active_period_targets"][slow + ":30m"])])
    request()
    assert live.state["cancel_requested"]
    assert state["selection_checks"] == before


def test_priority_handoff_keeps_the_existing_same_clock_failure_budget(monitor):
    service, live, clock, state, key, frequency, target = protected_tail(
        monitor, holding=False
    )
    state["retry_symbols"][key] = {
        "cutoff": target,
        "period_cutoff": target,
        "periods": [frequency],
        "failures": 3,
        "retry_at": None,
    }
    service._yield_live_for_priority(
        clock[0],
        live.status(),
        service._control_version,
        state["seed"]["run_id"],
        state["active_monitor_run"],
    )
    assert not live.state.get("cancel_requested")
    assert state["retry_symbols"][key]["failures"] == 3


def test_notification_backoff_does_not_spend_priority_rescue(monitor):
    service, live, clock, state, key, frequency, target = protected_tail(monitor, holding=False)
    service._notification_retry_after[key] = clock[0].timestamp() + 10
    service._yield_live_for_priority(clock[0], live.status(), service._control_version,
                                    state["seed"]["run_id"], state["active_monitor_run"])
    assert not live.state.get("cancel_requested")
    assert not state.get("live_priority_rescues")
    assert not state.get("priority_slice_yields")
    clock[0] += timedelta(seconds=10)
    service._yield_live_for_priority(clock[0], live.status(), service._control_version,
                                    state["seed"]["run_id"], state["active_monitor_run"])
    assert live.state["cancel_requested"]


def test_priority_bookkeeping_is_pruned_when_symbols_leave_pool():
    from cl_app.services.monitor_maintenance import prune_diagnostics
    state = {"seed": {"codes": ["us:QQQ.US"]},
             "priority_slice_yields": {"us:QQQ.US:1m": ["rev", 1], "a:SH.600000:5m": ["rev", 2]},
             "live_priority_rescues": {"us:QQQ.US": "kept", "a:SH.600000": "removed"}}
    assert prune_diagnostics(state)
    assert state["priority_slice_yields"] == {"us:QQQ.US:1m": ["rev", 1]}
    assert state["live_priority_rescues"] == {"us:QQQ.US": "kept"}
    assert not prune_diagnostics(state)


@pytest.mark.parametrize("holding", [True, False])
def test_rescued_priority_gets_next_batch_without_failure_or_false_completion(monitor, holding):
    service, live, clock, state, key, frequency, target = protected_tail(monitor, holding=holding)
    service._yield_live_for_priority(clock[0], live.status(), service._control_version,
                                    state["seed"]["run_id"], state["active_monitor_run"])
    assert live.state["cancel_requested"]
    finish(service, live, clock, cancelled=True, committed=[])
    service._schedule_monitor(clock[0], None)
    assert live.started[-1]["symbols"][0]["code"] == "SH.600000"
    assert periods(live)[0] == [frequency]
    assert not state["retry_symbols"]
    assert state["selection_checks"][key + ":" + frequency]["cutoff"] < target


def test_source_revision_has_an_independent_rescue_allowance(monitor):
    service, live, clock, state, key, frequency, target = protected_tail(monitor, holding=False)
    for revision in ("current", "replacement"):
        live.state.update(current_source_revision=revision, source_revision=revision, cancel_requested=False)
        service._notification_rechecks[key, frequency]["revision"] = revision
        service._yield_live_for_priority(clock[0], live.status(), service._control_version,
                                        state["seed"]["run_id"], state["active_monitor_run"])
        assert live.state["cancel_requested"]
        assert state["live_priority_rescues"]["a:SH.600088"][0] == revision
