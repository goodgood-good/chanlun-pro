"""An overdue sibling keeps its critical slot before a minute claims its symbol."""

from collections import Counter
from copy import deepcopy
from datetime import timedelta

import pytest

from cl_app.services.monitor_fairness import plan_batch
from tests.web.test_monitor_minute_cadence import cold as cold, KEY
from tests.web.test_monitor_incremental_priority import monitor as monitor


def inputs(size=4):
    symbols = [{"market": "a", "code": f"SH.{600088 + i}"} for i in range(size)]
    keys = ["a:" + s["code"] for s in symbols]
    periods = {key: ["1m", "5m", "30m"] for key in keys}
    state = {
        "holding_symbols": [keys[0]],
        "selection_checks": {},
        "attempted_cutoffs": {},
        "attempted_monitor_periods": {
            key + ":1m": {"revision": "current", "cutoff": 60, "scheduled_at": 90}
            for key in keys
        },
        "monitor_pending_deadlines": {
            f"{key}:{f}": {
                "deadline": 120 if f == "1m" else 60,
                "revision": "current",
                "first_pending_at": 0,
            }
            for key in keys
            for f in periods[key]
        },
    }
    targets = {f"{key}:{f}": 120 for key in keys for f in periods[key]}
    return symbols, keys, periods, state, targets


@pytest.mark.parametrize("cursor", [0, 4, 8])
def test_holding_older_sibling_receives_the_existing_critical_share(cursor):
    symbols, keys, periods, state, targets = inputs()
    tasks, _ = plan_batch(
        symbols,
        periods,
        state=state,
        targets=targets,
        minute_decisions={},
        urgent=set(),
        limit=4,
        cursor=cursor,
        now=120,
    )
    holding = [task for task in tasks if task["key"] == keys[0]]
    assert len(holding) == 1 and holding[0]["frequency"] in ("5m", "30m")
    assert tasks[0] == holding[0], (
        "A reserved holding must not queue behind ordinary minutes"
    )
    expected = {"hot": 2, "critical": 2} if cursor == 4 else {"hot": 3, "critical": 1}
    # At cursor 4 the empty cold lane lends a slot to the older key period.
    assert Counter(task["lane"] for task in tasks) == expected
    assert len({task["key"] for task in tasks}) == 4


def test_large_pool_retains_the_hot_share_while_overdue_critical_siblings_advance():
    symbols, keys, periods, state, targets = inputs(800)
    decisions = {key: {"cold": i >= 484} for i, key in enumerate(keys)}
    tasks, _ = plan_batch(
        symbols,
        periods,
        state=state,
        targets=targets,
        minute_decisions=decisions,
        urgent=set(),
        limit=48,
        cursor=0,
        now=120,
    )
    assert Counter(task["lane"] for task in tasks) == {
        "hot": 32,
        "critical": 12,
        "cold": 4,
    }
    assert len({task["key"] for task in tasks}) == 48
    assert next(task for task in tasks if task["key"] == keys[0])["lane"] == "critical"


@pytest.mark.parametrize("now,deadline", [(30, 60), (120, 120)])
def test_future_or_equal_deadline_does_not_take_the_holdings_minute_slot(now, deadline):
    symbols, keys, periods, state, targets = inputs()
    state["attempted_monitor_periods"] = {}
    for key in keys:
        for f in ("5m", "30m"):
            state["monitor_pending_deadlines"][key + ":" + f]["deadline"] = deadline
    tasks, _ = plan_batch(
        symbols,
        periods,
        state=state,
        targets=targets,
        minute_decisions={},
        urgent=set(),
        limit=4,
        cursor=0,
        now=now,
    )
    assert tasks[0]["key"] == keys[0] and tasks[0]["frequency"] == "1m"


def test_sibling_reservation_cannot_reintroduce_filtered_retry_or_disabled_periods():
    symbols, keys, periods, state, targets = inputs()
    periods[keys[0]] = ["1m"]
    tasks, _ = plan_batch(
        symbols,
        periods,
        state=state,
        targets=targets,
        minute_decisions={},
        urgent={keys[0]},
        limit=4,
        cursor=0,
        now=120,
    )
    assert tasks[0]["key"] == keys[0] and tasks[0]["frequency"] == "1m"


def test_excluded_symbol_cannot_consume_a_reserved_slot():
    symbols, keys, periods, state, targets = inputs(5)
    tasks, _ = plan_batch(
        symbols,
        periods,
        state=state,
        targets=targets,
        minute_decisions={},
        urgent=set(),
        limit=4,
        cursor=0,
        now=120,
        excluded={keys[0]},
    )
    assert keys[0] not in {task["key"] for task in tasks}
    assert len(tasks) == 4
    assert Counter(task["lane"] for task in tasks) == {"hot": 3, "critical": 1}


@pytest.mark.parametrize("minute_deadline", [30, 60])
def test_failed_minute_cannot_keep_an_equal_or_older_age_to_exclude_siblings(
    minute_deadline,
):
    symbols, keys, periods, state, targets = inputs()
    for f in ("5m", "30m"):
        state["monitor_pending_deadlines"][keys[0] + ":" + f].update(
            revision="current", first_pending_at=0
        )
    state["monitor_pending_deadlines"][keys[0] + ":1m"]["deadline"] = minute_deadline
    state["attempted_monitor_periods"] = {
        keys[0] + ":1m": {"cutoff": 60, "revision": "current", "scheduled_at": 90}
    }
    tasks, _ = plan_batch(
        symbols,
        periods,
        state=state,
        targets=targets,
        minute_decisions={},
        urgent=set(),
        limit=4,
        cursor=0,
        now=120,
    )
    assert next(task for task in tasks if task["key"] == keys[0])["lane"] == "critical"


def test_a_previous_revision_attempt_cannot_take_the_initial_minute_turn():
    symbols, keys, periods, state, targets = inputs()
    for f in ("5m", "30m"):
        state["monitor_pending_deadlines"][keys[0] + ":" + f].update(
            revision="current", first_pending_at=0
        )
    state["monitor_pending_deadlines"][keys[0] + ":1m"]["deadline"] = 60
    state["attempted_monitor_periods"] = {
        keys[0] + ":1m": {"cutoff": 60, "revision": "previous", "scheduled_at": 90}
    }
    tasks, _ = plan_batch(
        symbols,
        periods,
        state=state,
        targets=targets,
        minute_decisions={},
        urgent=set(),
        limit=4,
        cursor=0,
        now=120,
    )
    assert tasks[0]["key"] == keys[0] and tasks[0]["frequency"] == "1m"


def test_actual_coordinator_preserves_risk_only_period_and_manual_minute_request(
    cold, monkeypatch
):
    service, _, live, clock, cutoff = cold
    state = service._load()
    stocks = [
        {"market": "a", "code": f"SH.{600088 + i}", "name": "fixture"} for i in range(4)
    ]
    keys = ["a:" + s["code"] for s in stocks]
    state["seed"].update(symbols=stocks, screening_symbols=deepcopy(stocks), codes=keys)
    state["seed"]["settings"]["frequencies"] = ["1m", "5m"]
    state["holding_symbols"] = [KEY]
    state["holding_risk_plans"] = {
        "risk-id": {
            "market": "a",
            "code": "SH.600088",
            "frequency": "30m",
            "active": True,
        }
    }
    state["signals"] = {
        f"point{i}": {
            "market": "a",
            "code": s["code"],
            "frequency": "1m",
            "stage": "confirmed",
        }
        for i, s in enumerate(stocks)
    }
    state["selection_checks"] = {
        f"{key}:{f}": {
            "revision": "current",
            "cutoff": cutoff if f == "1m" else cutoff - int(f[:-1]) * 60,
            "error": False,
            "identities": ["point"],
            "input_fingerprint": "proof",
        }
        for key in keys
        for f in ("1m", "5m", "30m")
    }
    state["minute_force_pending"] = {KEY: 7}
    state["attempted_monitor_periods"] = {
        KEY + ":1m": {
            "revision": "current",
            "cutoff": cutoff + 60,
            "scheduled_at": cutoff + 60,
        }
    }
    state["monitor_pending_deadlines"] = {
        KEY + ":" + f: {
            "seed": "baseline",
            "revision": "current",
            "target": cutoff,
            "deadline": cutoff + 60,
            "first_pending_at": cutoff,
        }
        for f in ("5m", "30m")
    }
    clock[0] += timedelta(minutes=3)
    monkeypatch.setattr("cl_app.services.signal_monitor.cpu_capacity", lambda: 1)
    service._schedule_monitor(clock[0], None)
    held = next(s for s in live.started[-1]["symbols"] if s["code"] == "SH.600088")
    assert held["_monitor_periods"] in (["5m"], ["30m"])
    assert state["minute_force_pending"][KEY] == 7
    assert "1m" in state["deferred_monitor_periods"][KEY]
    assert set(state["deferred_monitor_periods"][KEY]) | set(
        held["_monitor_periods"]
    ) == {"1m", "5m", "30m"}
