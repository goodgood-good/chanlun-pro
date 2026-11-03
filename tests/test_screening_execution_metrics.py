from contextlib import contextmanager
from copy import deepcopy

from chanlun.screening import compute_capacity, execution_metrics, runner
from chanlun.exchange import quote_capacity


def test_resource_metrics_separate_admission_compute_and_cleanup_without_changing_facts(monkeypatch):
    @contextmanager
    def scope(*args, **kwargs):
        yield

    monkeypatch.setattr(compute_capacity, "compute_slot", scope)
    monkeypatch.setattr(quote_capacity, "worker_quote_scope", scope)
    wall = iter((1.0, 1.25, 3.0, 3.5))
    cpu = iter((10.0, 10.5))
    io = iter(({"read_bytes": 10, "write_bytes": 20}, {"read_bytes": 40, "write_bytes": 25}))
    monkeypatch.setattr(runner.time, "monotonic", lambda: next(wall))
    monkeypatch.setattr(runner.time, "process_time", lambda: next(cpu))
    monkeypatch.setattr(execution_metrics, "io_counters", lambda: next(io))
    source = {"rows": [{"selected": [{"point": {"point_id": "unchanged"}}]}], "seconds": 1.0}
    monkeypatch.setattr(runner, "_scan_worker_symbol_with_capacity", lambda *args: deepcopy(source))
    result = runner._scan_worker_symbol({}, {}, {}, "unused", "test", {})
    assert result["rows"] == source["rows"] and result["seconds"] == source["seconds"]
    assert result["execution_metrics"] == {
        "schema": "screening-task-resources-v1",
        "slot_wait_seconds": 0.25,
        "wall_seconds": 1.75,
        "cpu_seconds": 0.5,
        "cleanup_seconds": 0.5,
        "io": {"read_bytes": 30, "write_bytes": 5},
    }
