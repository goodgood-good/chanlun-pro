"""Stable admission age, effective-budget fairness and measured live pressure."""

from copy import deepcopy
from datetime import timedelta

import pytest

from cl_app.services.signal_monitor import SignalMonitor
from chanlun.screening.compute_capacity import role_limits
from cl_app.services.monitor_fairness import (
    remember_pending,
    task_deadline,
    plan_batch,
    record_costs,
    service_pressure,
)
from tests.web.test_monitor_minute_cadence import cold as cold, KEY
from tests.web.test_monitor_incremental_priority import (
    monitor as monitor,
    finish,
    periods,
)


def age_state():
    return {
        "seed": {
            "run_id": "seed",
            "codes": ["a:first", "a:other"],
            "settings": {"frequencies": ["1m", "5m", "30m"]},
        },
        "selection_checks": {},
        "holding_symbols": [],
        "attempted_cutoffs": {},
    }


def test_first_period_deadline_survives_new_targets_and_only_success_releases_age():
    state = age_state()
    plan = {"a:first": ["30m"]}
    assert remember_pending(state, plan, {"a:first:30m": 0}, {}, "v", 0)
    assert task_deadline(state, "a:first", "30m", 1800, {}, 1800) == 60
    assert not remember_pending(state, plan, {"a:first:30m": 1800}, {}, "v", 1800)
    state["selection_checks"]["a:first:30m"] = {
        "revision": "v",
        "cutoff": 1800,
        "error": True,
    }
    assert not remember_pending(state, plan, {"a:first:30m": 3600}, {}, "v", 3600)
    assert state["monitor_pending_deadlines"]["a:first:30m"]["target"] == 0
    state["selection_checks"]["a:first:30m"]["error"] = False
    assert remember_pending(state, {}, {}, {}, "v", 3600)
    assert not state["monitor_pending_deadlines"]


def test_age_is_invalidated_by_seed_revision_or_scope_change():
    state = age_state()
    plan = {"a:first": ["30m"]}
    target = {"a:first:30m": 0}
    remember_pending(state, plan, target, {}, "v", 0)
    state["seed"]["run_id"] = "new"
    remember_pending(state, plan, {"a:first:30m": 1800}, {}, "v", 1800)
    assert state["monitor_pending_deadlines"]["a:first:30m"]["deadline"] == 1860
    remember_pending(state, plan, {"a:first:30m": 3600}, {}, "next", 3600)
    assert state["monitor_pending_deadlines"]["a:first:30m"]["revision"] == "next"
    state["seed"]["settings"]["frequencies"] = ["1m"]
    remember_pending(state, {}, {}, {}, "next", 3600)
    assert not state["monitor_pending_deadlines"]


def test_active_risk_binding_keeps_age_when_its_period_is_not_in_the_selection_scope():
    state = age_state()
    state["seed"]["settings"]["frequencies"] = ["1m"]
    state["holding_risk_plans"] = {
        "risk-plan-identity": {
            "market": "a",
            "code": "first",
            "frequency": "30m",
            "active": True,
        }
    }
    planned = {"a:first": ["30m"]}
    remember_pending(state, planned, {"a:first:30m": 0}, {}, "v", 0)
    age = deepcopy(state["monitor_pending_deadlines"]["a:first:30m"])
    assert not remember_pending(state, planned, {"a:first:30m": 1800}, {}, "v", 1800)
    assert state["monitor_pending_deadlines"]["a:first:30m"] == age
    state["holding_risk_plans"]["risk-plan-identity"]["active"] = False
    remember_pending(state, {}, {}, {}, "v", 1800)
    assert not state["monitor_pending_deadlines"]


def test_equal_close_five_and_thirty_rotate_without_waiting_thirty_minutes():
    state = age_state()
    symbols = [{"market": "a", "code": "first"}, {"market": "a", "code": "other"}]
    plan = {"a:first": ["5m", "30m"], "a:other": ["5m", "30m"]}
    targets = {f"{key}:{f}": 0 for key, fs in plan.items() for f in fs}
    remember_pending(state, plan, targets, {}, "v", 0)
    tasks, cursor = plan_batch(
        symbols,
        plan,
        state=state,
        targets=targets,
        minute_decisions={},
        urgent=set(),
        limit=2,
        cursor=2,
        now=0,
    )
    assert {task["frequency"] for task in tasks} == {"5m", "30m"}
    assert all(task["deadline"] == 60 for task in tasks)
    assert len({task["key"] for task in tasks}) == 2


def test_durable_first_age_survives_resource_wait_and_restart(cold, monkeypatch):
    service, manual, live, clock, cutoff = cold
    state = service._load()
    state["seed"]["settings"]["frequencies"] = ["30m"]
    state["selection_checks"] = {}
    service._publish_compute_budget = True
    service._compute_budget = {"isolated": True}
    monkeypatch.setattr(
        "cl_app.services.signal_monitor.publish_monitor_demand",
        lambda *a, **k: {"monitor_capacity": 0},
    )
    service._schedule_monitor(clock[0], None)
    age = deepcopy(state["monitor_pending_deadlines"][KEY + ":30m"])
    assert not live.started
    restored = SignalMonitor(
        service.root, screening=manual, monitor=live, now=lambda: clock[0]
    )
    try:
        clock[0] += timedelta(minutes=30)
        restored._publish_compute_budget = True
        restored._compute_budget = {"isolated": True}
        restored._schedule_monitor(clock[0], None)
        assert restored._load()["monitor_pending_deadlines"][KEY + ":30m"] == age
        restored.pause()
        restored._schedule_monitor(clock[0], None)
        assert not live.started
        assert restored._load()["monitor_pending_deadlines"][KEY + ":30m"] == age
    finally:
        restored._incremental_reader.close()


def test_effective_memory_budget_replans_live_and_history_shares(cold, monkeypatch):
    service, _, live, clock, cutoff = cold
    state = service._load()
    stocks = [
        {"market": "a", "code": f"SH.{600000 + i}", "name": "live"} for i in range(24)
    ]
    stocks += [
        {"market": "a", "code": f"SH.{600100 + i}", "name": "history"} for i in range(8)
    ]
    state["seed"].update(
        symbols=stocks,
        screening_symbols=deepcopy(stocks),
        codes=["a:" + s["code"] for s in stocks],
    )
    state.update(
        force=True,
        selection_checks={},
        attempted_cutoffs={},
        minute_cadence_checkpoints={},
    )
    clock[0] += timedelta(minutes=1)
    monkeypatch.setattr(
        "cl_app.services.signal_monitor.closed_bar_cutoff",
        lambda m, c, n, f: (
            int(n.timestamp()) // 60 * 60 if int(c[3:]) < 600100 else cutoff - 86400
        ),
    )
    monkeypatch.setattr("cl_app.services.signal_monitor.cpu_capacity", lambda: 16)
    service._publish_compute_budget = True
    service._compute_budget = {"isolated": True}
    monkeypatch.setattr(
        "cl_app.services.signal_monitor.publish_monitor_demand",
        lambda *a, **k: {"monitor_capacity": 2},
    )
    service._schedule_monitor(clock[0], None)
    selected = live.started[-1]["symbols"]
    assert sum(s["name"] == "live" for s in selected) == 6
    assert sum(s["name"] == "history" for s in selected) == 2
    assert state["pending_background_tasks"] == 6
    assert len(state["active_minute_override_tokens"]) == 8
    assert len(state["minute_force_pending"]) == 32


def pressure_state(cost):
    state = age_state()
    keys = [f"a:{i}" for i in range(800)]
    state["seed"]["codes"] = keys
    decisions = {
        key: {"cold": i >= 484, "next_due_at": 0} for i, key in enumerate(keys)
    }
    planned = {key: ["1m", "5m", "30m"] for key in keys}
    targets = {f"{key}:{f}": 0 for key in keys for f in ("1m", "5m", "30m")}
    for f in ("1m", "5m", "30m"):
        record_costs(
            state,
            [
                {
                    "frequency": f,
                    "processing_timeline": {
                        "calculation_started_at": 100,
                        "calculation_finished_at": 100 + cost,
                    },
                }
            ],
        )
    remember_pending(state, planned, targets, decisions, "v", 0)
    return state, keys, decisions, planned, targets


@pytest.mark.parametrize("cost,boost", [(0.25, False), (1.0, True)])
def test_measured_workload_adjusts_pressure_without_boosting_closed_markets(
    cost, boost
):
    state, keys, decisions, planned, targets = pressure_state(cost)
    pressure = service_pressure(state, planned, targets, decisions, set(keys), 0, 16)
    assert pressure["overdue_pressure"] is boost
    assert (
        service_pressure(state, planned, targets, decisions, set(), 0, 16)[
            "overdue_pressure"
        ]
        is False
    )
    if boost:
        assert pressure["required_workers"] > 12


def test_untrusted_or_missing_timing_does_not_create_a_measurement():
    state = {}
    for finish_at in (float("nan"), 99, 100, 1000):
        record_costs(
            state,
            [
                {
                    "frequency": "1m",
                    "processing_timeline": {
                        "calculation_started_at": 100,
                        "calculation_finished_at": finish_at,
                    },
                }
            ],
        )
    assert not state["monitor_period_costs"]


def test_complete_task_cost_replaces_legacy_primary_cost_and_survives_wall_clock_rollback():
    from chanlun.screening.worker_transport import TIMING_SCOPE
    state = {'monitor_period_costs': {'5m': {'seconds': 3., 'samples': 50}}}
    period = {'frequency': '5m', 'timings_ms': {'total': 12000},
              'processing_timeline': {'measurement_scope': TIMING_SCOPE,
                  'calculation_started_at': 100, 'calculation_finished_at': 99}}
    record_costs(state, [period])
    assert state['monitor_period_costs']['5m'] == {'seconds': 12., 'samples': 1, 'measurement_scope': TIMING_SCOPE}
    record_costs(state, [{'frequency': '5m', 'processing_timeline': {
        'calculation_started_at': 100, 'calculation_finished_at': 103}}])
    assert state['monitor_period_costs']['5m']['seconds'] == 12.
    assert state['monitor_period_costs']['5m']['samples'] == 1
    record_costs(state, [{**period, 'timings_ms': {'total': 10000}}])
    assert state['monitor_period_costs']['5m']['seconds'] == 11.6


@pytest.mark.parametrize('duration', [None, float('nan'), float('inf'), -1, 0, True, 180001])
def test_invalid_complete_task_cost_does_not_fall_back_to_partial_time(duration):
    from chanlun.screening.worker_transport import TIMING_SCOPE
    state = {}
    record_costs(state, [{'frequency': '5m', 'timings_ms': {'total': duration},
        'processing_timeline': {'measurement_scope': TIMING_SCOPE,
                               'calculation_started_at': 100, 'calculation_finished_at': 103}}])
    assert not state['monitor_period_costs']


def test_linked_period_cost_changes_the_live_capacity_estimate():
    from chanlun.screening.worker_transport import TIMING_SCOPE
    state, keys, decisions, planned, targets = pressure_state(.25)
    before = service_pressure(state, planned, targets, decisions, set(keys), 0, 16)
    record_costs(state, [{'frequency': '5m', 'timings_ms': {'total': 12000},
        'processing_timeline': {'measurement_scope': TIMING_SCOPE,
                               'calculation_started_at': 100, 'calculation_finished_at': 112}}])
    after = service_pressure(state, planned, targets, decisions, set(keys), 0, 16)
    assert after['required_workers'] > before['required_workers'] and after['overdue_pressure'] is True
    assert service_pressure(state, planned, targets, decisions, set(), 0, 16)['overdue_pressure'] is False


def test_measured_demand_uses_the_effective_memory_limit_for_escalation(
    cold, monkeypatch
):
    service, _, _, _, _ = cold
    service._publish_compute_budget = True
    service._compute_budget = {"isolated": True}
    monkeypatch.setattr("cl_app.services.signal_monitor.cpu_capacity", lambda: 16)
    calls = []

    def publish(*args, **kwargs):
        calls.append((kwargs["demand"], kwargs["overdue_pressure"]))
        return {
            "limit": 8,
            **role_limits(
                8, kwargs["demand"], True, overdue_pressure=kwargs["overdue_pressure"]
            ),
        }

    monkeypatch.setattr(
        "cl_app.services.signal_monitor.publish_monitor_demand", publish
    )
    # Measured pressure is compared with the effective memory-limited pool.
    # The expanded lease still leaves two of its eight slots to screening.
    assert (
        service._monitor_capacity(
            16, True, service_estimate={"required_workers": 7, "boost_ready": True}
        )
        == 6
    )
    assert calls == [(8, False), (7, True)]
    assert service._last_compute_limits["screening_capacity"] == 2


def test_reported_wait_is_admission_age_not_old_market_clock_age():
    state = age_state()
    planned = {"a:first": ["5m"]}
    targets = {"a:first:5m": 0}
    remember_pending(state, planned, targets, {}, "v", 86400)
    report = service_pressure(state, planned, targets, {}, {"a:first"}, 86400, 16)
    assert report["oldest_wait_seconds"] == 0
    assert not report["boost_ready"]


def test_consumed_verified_timing_is_recorded_once_and_releases_age(cold):
    service, _, live, clock, _ = cold
    clock[0] += timedelta(minutes=5)
    original = live.committed_results

    def timed(*a, **k):
        result = original(*a, **k)
        for p in result["verified_periods"]:
            p["processing_timeline"] = {
                "calculation_started_at": 100,
                "calculation_finished_at": 101,
            }
        return result

    live.committed_results = timed
    service._schedule_monitor(clock[0], None)
    assert periods(live) == [["1m"]]
    assert KEY + ":1m" in service._load()["monitor_pending_deadlines"]
    finish(service, live, clock)
    assert KEY + ":1m" not in service._load()["monitor_pending_deadlines"]
    assert service._load()["monitor_period_costs"]["1m"] == {"seconds": 1, "samples": 1}
    service._consume_monitor()
    assert service._load()["monitor_period_costs"]["1m"]["samples"] == 1
