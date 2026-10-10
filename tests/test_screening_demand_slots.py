"""Spawned processes share fixed slots through lease changes and memory drops."""

import json
import multiprocessing
import os
from pathlib import Path
import queue
import time

import pytest

from chanlun.screening import compute_capacity as capacity


def slot_actor(root, maximum, monitor, label, start, release, messages, counters):
    from chanlun import config

    config.get_data_path = lambda: Path(root)
    capacity.os.cpu_count = lambda: maximum
    budget = {
        "schema": "screening-compute-budget-v1",
        "root": str(Path(root) / "compute_budget"),
        "limit": maximum,
    }
    messages.put(("ready", label))
    if not start.wait(20):
        return
    with capacity.compute_slot(
        {"compute_budget": budget, **({"worker_runtime": {}} if monitor else {})}, root
    ) as slot:
        with counters.get_lock():
            counters[0] += 1
            counters[1] = max(counters[1], counters[0])
            active = counters[0]
        messages.put(("acquired", label, slot, active))
        if label == "crash":
            # Flush the observation before bypassing every Python cleanup.
            # Do not terminate while holding a test Event/Queue mutex: that
            # would test poisoned instrumentation instead of the OS slot lock.
            messages.close()
            messages.join_thread()
            with counters.get_lock():
                counters[0] -= 1
            os._exit(3)
        release.wait(20)
        with counters.get_lock():
            counters[0] -= 1
    messages.put(("released", label))


class Actors:
    def __init__(self, root, maximum):
        self.root, self.maximum = root, maximum
        self.context = multiprocessing.get_context("spawn")
        self.messages = self.context.Queue()
        self.counters = self.context.Array("i", [0, 0], lock=True)
        self.items = {}
        self.backlog = []

    def add(self, label, *, monitor=False):
        start, release = self.context.Event(), self.context.Event()
        process = self.context.Process(
            target=slot_actor,
            args=(
                str(self.root),
                self.maximum,
                monitor,
                label,
                start,
                release,
                self.messages,
                self.counters,
            ),
        )
        process.start()
        self.items[label] = (process, start, release)

    def receive(self, kind, labels, timeout=15):
        labels = set(labels)
        results = []
        deadline = time.monotonic() + timeout
        while labels:
            found = next(
                (
                    item
                    for item in self.backlog
                    if item[0] == kind and item[1] in labels
                ),
                None,
            )
            if found is not None:
                self.backlog.remove(found)
                labels.remove(found[1])
                results.append(found)
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(
                    f"waiting for {kind}: {labels}; messages={self.backlog}"
                )
            self.backlog.append(self.messages.get(timeout=remaining))
        return results

    def start(self, *labels):
        for label in labels:
            self.items[label][1].set()

    def release(self, *labels):
        for label in labels:
            self.items[label][2].set()

    def assert_waiting(self, label, seconds=0.25):
        deadline = time.monotonic() + seconds
        while True:
            assert not any(
                item[0] == "acquired" and item[1] == label for item in self.backlog
            )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            try:
                self.backlog.append(self.messages.get(timeout=remaining))
            except queue.Empty:
                return

    def close(self):
        for process, start, release in self.items.values():
            if process.is_alive():
                start.set()
                release.set()
        for process, _, _ in self.items.values():
            process.join(5)
            if process.is_alive():
                process.terminate()
                process.join(3)
            process.close()
        self.messages.close()
        self.messages.join_thread()


def publish_memory(root, limit, *, active_scan=True):
    folder = root / "compute_budget"
    folder.mkdir(exist_ok=True)
    stamp = time.time()
    temporary = folder / "memory-test.tmp"
    temporary.write_text(
        json.dumps(
            {
                "schema": "screening-memory-budget-v1",
                "limit": limit,
                "updated_at": stamp,
                "changed_at": stamp,
                "background_until": stamp + 60 if active_scan else 0,
            }
        )
    )
    temporary.replace(folder / capacity.RESOURCE_FILE)


@pytest.fixture
def configured(tmp_path, monkeypatch):
    from chanlun import config

    monkeypatch.setattr(config, "get_data_path", lambda: tmp_path)
    monkeypatch.setattr(capacity.os, "cpu_count", lambda: 4)
    monkeypatch.setattr(
        capacity,
        "memory_status",
        lambda: {"available": 64 * capacity.GIB, "reserve": 2 * capacity.GIB},
    )
    return tmp_path, capacity.budget_request()


@pytest.mark.parametrize("maximum,reserved,boost", [(4, 3, False), (16, 12, False), (16, 15, True)])
def test_spawned_live_demand_waits_for_old_work_then_gets_reserved_fixed_slots(
    configured, monkeypatch, maximum, reserved, boost
):
    root, budget = configured
    monkeypatch.setattr(capacity.os, "cpu_count", lambda: maximum)
    budget = capacity.budget_request()
    publish_memory(root, maximum)
    actors = Actors(root, maximum)
    old_names = [f"old{i}" for i in range(maximum)]
    scan_names = [f"scan{i}" for i in range(maximum - reserved)]
    live_names = [f"live{i}" for i in range(reserved)]
    try:
        for label in old_names + scan_names:
            actors.add(label)
        for label in live_names:
            actors.add(label, monitor=True)
        actors.receive("ready", actors.items, timeout=30)
        actors.start(*old_names)
        first = actors.receive("acquired", old_names)
        assert {row[2] for row in first} == set(range(maximum))
        pressure = capacity.publish_monitor_demand(
            budget, demand=maximum, owner="scheduler", expires_in=60, overdue_pressure=boost
        )
        assert (pressure["monitor_capacity"], pressure["screening_capacity"]) == (
            reserved,
            maximum - reserved,
        )
        actors.start(*scan_names, *live_names)
        actors.assert_waiting("scan0", 0.1)
        actors.assert_waiting("live0", 0.1)
        assert all(actors.items[label][0].is_alive() for label in old_names)
        actors.release(*old_names)
        second = actors.receive("acquired", scan_names + live_names)
        # Roles may safely use each other's free physical slot during return
        # of borrowed slots. Quotas apply to occupied roles, not slot numbers.
        assert len({row[2] for row in second}) == maximum
        for row in second:
            owner = capacity._slot_owner(root / 'compute_budget', row[2])
            assert owner['role'] == ('screening' if row[1].startswith('scan') else 'monitor')
        assert actors.counters[1] == maximum
    finally:
        actors.close()


def test_expired_scheduler_lease_returns_unused_slot_to_waiting_scan_process(
    configured, monkeypatch
):
    root, _ = configured
    monkeypatch.setattr(capacity.os, "cpu_count", lambda: 2)
    budget = capacity.budget_request()
    publish_memory(root, 2)
    actors = Actors(root, 2)
    try:
        actors.add("scan0")
        actors.add("scan1")
        actors.receive("ready", actors.items)
        capacity.publish_monitor_demand(
            budget, demand=2, owner="dead-scheduler", expires_in=0.8
        )
        actors.start("scan0", "scan1")
        acquired = actors.receive("acquired", ["scan0", "scan1"], timeout=5)
        assert {row[2] for row in acquired} == {0, 1}
        assert actors.counters[1] == 2
        assert capacity.update_resource_budget(budget)["monitor_demand"] == 0
    finally:
        actors.close()


def test_memory_downgrade_counts_old_slots_outside_the_new_admission_range(configured):
    root, _ = configured
    publish_memory(root, 4)
    actors = Actors(root, 4)
    try:
        for label in ["old0", "old1", "old2", "old3", "new"]:
            actors.add(label)
        actors.receive("ready", actors.items)
        actors.start("old0", "old1", "old2", "old3")
        old = actors.receive("acquired", ["old0", "old1", "old2", "old3"])
        retained = next(row[1] for row in old if row[2] == 3)
        released = [row[1] for row in old if row[2] != 3]
        publish_memory(root, 1)
        actors.release(*released)
        actors.receive("released", released)
        actors.start("new")
        actors.assert_waiting("new")
        assert actors.items[retained][0].is_alive()
        actors.release(retained)
        (result,) = actors.receive("acquired", ["new"])
        assert result[2] == 0 and result[3] == 1
    finally:
        actors.close()


def test_process_exit_releases_slot_and_does_not_require_stale_owner_cleanup(
    configured,
):
    root, _ = configured
    publish_memory(root, 1)
    actors = Actors(root, 1)
    try:
        actors.add("crash", monitor=True)
        actors.add("next", monitor=False)
        actors.receive("ready", actors.items)
        actors.start("crash")
        actors.receive("acquired", ["crash"])
        process = actors.items["crash"][0]
        process.join(5)
        assert not process.is_alive()
        assert process.exitcode == 3
        actors.start("next")
        (result,) = actors.receive("acquired", ["next"])
        assert result[2] == 0 and result[3] == 1
    finally:
        actors.close()


@pytest.mark.parametrize("first", ["scan", "live"])
def test_one_cpu_keeps_both_roles_runnable_without_overlapping(
    configured, monkeypatch, first
):
    root, _ = configured
    monkeypatch.setattr(capacity.os, "cpu_count", lambda: 1)
    budget = capacity.budget_request()
    publish_memory(root, 1)
    capacity.publish_monitor_demand(
        budget, demand=800, expires_in=60, owner="scheduler"
    )
    actors = Actors(root, 1)
    second = "live" if first == "scan" else "scan"
    try:
        actors.add("scan")
        actors.add("live", monitor=True)
        actors.receive("ready", actors.items)
        actors.start(first)
        actors.receive("acquired", [first])
        actors.start(second)
        actors.assert_waiting(second)
        actors.release(first)
        (row,) = actors.receive("acquired", [second])
        assert row[2] == 0 and row[3] == 1 and actors.counters[1] == 1
    finally:
        actors.close()


def test_scan_can_borrow_a_free_slot_while_old_monitor_keeps_borrowed_low_slot(configured):
    root, budget = configured
    publish_memory(root, 4, active_scan=False)
    capacity.publish_monitor_demand(budget, demand=4, owner='scheduler', expires_in=60)
    actors = Actors(root, 4)
    old_names = ['old0', 'old1', 'old2', 'old3']
    try:
        for name in old_names + ['new0', 'new1']:
            actors.add(name, monitor=True)
        actors.add('scan')
        actors.receive('ready', actors.items)
        actors.start(*old_names)
        old = actors.receive('acquired', old_names)
        borrowed = next(row[1] for row in old if row[2] == 0)
        released = [row[1] for row in old if row[2] != 0]
        publish_memory(root, 4, active_scan=True)
        actors.release(*released)
        actors.receive('released', released)
        actors.start('new0', 'new1')
        current = actors.receive('acquired', ['new0', 'new1'])
        assert actors.counters[0] == 3 and actors.items[borrowed][0].is_alive()
        actors.start('scan')
        scan, = actors.receive('acquired', ['scan'])
        assert scan[2] != 0 and scan[3] == 4
        assert scan[2] not in {row[2] for row in current}
        assert actors.items[borrowed][0].is_alive()
        assert actors.counters[1] == 4
    finally:
        actors.close()
