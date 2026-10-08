"""A committed result read failure cannot stall every later monitor batch."""

from datetime import datetime, timezone
from unittest.mock import Mock
import json

import pytest

from cl_app.services.signal_monitor import SignalMonitor, _signal
from cl_app.services.screening import ScreeningManager
from tests.web.test_signal_monitor import Jobs, signal
from tests.web.test_screening_results_cache import seed_run
from cl_app.services.dingtalk_notifications import DingTalkOutbox
from tests.web.test_dingtalk_notifications import Transport
from tests.web.test_signal_monitor import setup as setup, _seed
from tests.web.test_screening_results_cache import cached_run as cached_run


@pytest.fixture
def broken_batch(tmp_path, monkeypatch):
    manager = ScreeningManager(tmp_path / "runs", warm_results=False)
    directory = seed_run(manager)
    clock = [310.0]
    stocks = [
        {"market": "a", "code": code, "name": code}
        for code in ("SH.600000", "SH.600001")
    ]
    service = SignalMonitor(
        tmp_path / "monitor",
        screening=Jobs(tmp_path / "manual"),
        monitor=manager,
        notifications=DingTalkOutbox(tmp_path / "outbox.sqlite", transport=Transport()),
        now=lambda: datetime.fromtimestamp(clock[0], timezone.utc),
    )
    state = service._load()
    old = _signal({**signal(), "code": "SH.600000", "source_closed_at": 300})
    state.update(
        enabled=True,
        active_seed="seed",
        active_monitor_run=directory.name,
        active_symbols=["a:SH.600000"],
        active_cutoffs={"a:SH.600000": 300},
        attempted_cutoffs={"a:SH.600000": 300},
        signals={old["id"]: old},
        seed={
            "run_id": "seed",
            "codes": ["a:SH.600000", "a:SH.600001"],
            "symbols": stocks,
            "settings": {"frequencies": ["5m"], "point_types": ["3buy"], "workers": 2},
        },
    )
    service._save()
    monkeypatch.setattr(
        "cl_app.services.signal_monitor.closed_bar_cutoff", lambda *a: 300
    )
    monkeypatch.setattr(
        manager, "start", Mock(return_value={"run_id": "b" * 32, "status": "running"})
    )
    return service, manager, directory, clock


def test_incomplete_committed_file_is_reported_instead_of_silently_returning_no_progress(
    broken_batch,
):
    _service, manager, directory, _clock = broken_batch
    path = directory / "results.jsonl"
    path.write_bytes(path.read_bytes().rstrip(b"\n"))
    with pytest.raises(ValueError, match="结果"):
        manager.committed_results()


@pytest.mark.parametrize("damage", ["json", "missing", "partial", "shape"])
def test_repeated_bad_reads_release_the_batch_for_bounded_retry_and_other_symbols(
    broken_batch, damage
):
    service, manager, directory, clock = broken_batch
    path = directory / "results.jsonl"
    if damage == "missing":
        path.unlink()
    elif damage == "partial":
        path.write_bytes(path.read_bytes().rstrip(b"\n"))
    elif damage == "shape":
        path.write_text(
            json.dumps({"code": "SH.600000", "rows": None}) + "\n", encoding="utf-8"
        )
    else:
        path.write_bytes(b"{broken\n")
    for _ in range(3):
        service._consume_monitor()
        clock[0] += 3
    state = service._load()
    assert state["processed_run"] == directory.name
    assert state["retry_symbols"]["a:SH.600000"]["failures"] == 1
    assert state["unverified_signals"] and not state["signals"]
    assert not any(e.get("change") == "left" for e in state["events"])
    service._schedule_monitor(service.now(), None)
    assert [s["code"] for s in manager.start.call_args.args[0]["symbols"]] == [
        "SH.600001"
    ]


def test_broken_new_screening_results_do_not_block_existing_monitor_checks(
    setup, monkeypatch
):
    service, manual, live, clock, cutoff = _seed(setup, [signal()])
    service.tick()
    old_seed = service._load()["seed"]["run_id"]
    live.finish(cutoff)
    service.tick()
    manual.start(manual.state["settings"])
    manual.finish(cutoff)
    failed = Mock(side_effect=ValueError("isolated unreadable new screening result"))
    monkeypatch.setattr(manual, "results", failed)
    service._load()["force"] = True
    service.tick()
    assert service._load()["seed"]["run_id"] == old_seed
    assert len(live.started) == 2
    assert service._load()["screening_result_error"]["run_id"] == manual.state["run_id"]
    for _ in range(4):
        service.tick()
    assert failed.call_count == 1, (
        "unchanged broken results must not be reparsed every scheduler cycle"
    )


def test_slow_full_result_reader_runs_in_background_without_publishing_an_empty_seed(
    cached_run, tmp_path, monkeypatch
):
    import threading

    manager = cached_run
    entered, release = threading.Event(), threading.Event()
    original = manager._read_results

    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(manager, "_read_results", slow)
    service = SignalMonitor(
        tmp_path / "monitor",
        screening=manager,
        monitor=Jobs(tmp_path / "live"),
        notifications=DingTalkOutbox(tmp_path / "outbox.sqlite", transport=Transport()),
    )
    try:
        pending = service._screening_results_for_monitor(manager.status())
        assert entered.wait(1) and pending["results_loading"]
        assert service._load()["seed"] is None
    finally:
        release.set()
        manager.results()
    assert not service._screening_results_for_monitor(manager.status()).get(
        "results_loading"
    )


def test_active_unreadable_worker_is_cancelled_before_another_batch_can_start(
    broken_batch,
):
    import os, time
    from chanlun.screening.runner import write_json

    service, manager, directory, clock = broken_batch
    path = directory / "status.json"
    status = json.loads(path.read_text(encoding="utf-8"))
    write_json(
        path,
        {
            **status,
            "status": "running",
            "worker_pid": os.getpid(),
            "updated_at": time.time(),
        },
    )
    (directory / "results.jsonl").write_bytes(b"{broken\n")
    for _ in range(3):
        service._consume_monitor()
        clock[0] += 3
    assert (directory / "cancel").exists()
    assert service._load()["processed_run"] is None
    service._schedule_monitor(service.now(), None)
    manager.start.assert_not_called()
    write_json(path, {**status, "status": "cancelled"})
    service._consume_monitor()
    assert service._load()["processed_run"] == directory.name
    service._schedule_monitor(service.now(), None)
    manager.start.assert_called_once()


def test_transient_read_failure_can_recover_before_batch_abandonment(broken_batch):
    service, manager, directory, _clock = broken_batch
    path = directory / "results.jsonl"
    healthy = path.read_bytes()
    path.write_bytes(b"{broken\n")
    service._consume_monitor()
    assert service._load()["monitor_read_error"]["attempts"] == 1
    path.write_bytes(healthy)
    service._consume_monitor()
    assert service._load()["processed_run"] == directory.name
    assert "monitor_read_error" not in service._load()
    assert not service._load().get("abandoned_monitor_run")


def test_previously_consumed_prefix_survives_a_later_corrupt_chunk(broken_batch):
    from chanlun.screening.runner import write_json

    service, _manager, directory, clock = broken_batch
    record = directory / "results.jsonl"
    record.write_bytes(record.read_bytes() + b"{broken\n")
    status = directory / "status.json"
    write_json(status, {**json.loads(status.read_text()), "completed": 2, "total": 2})
    state = service._load()
    old = _signal({**signal("SH.600001"), "source_closed_at": 300})
    state["signals"][old["id"]] = old
    state.update(
        active_symbols=["a:SH.600000", "a:SH.600001"],
        active_cutoffs={"a:SH.600000": 300, "a:SH.600001": 300},
        active_consumed_symbols=["a:SH.600000"],
        monitor_cursor={"run_id": directory.name, "completed": 1},
    )
    for _ in range(3):
        service._consume_monitor()
        clock[0] += 3
    assert {e["code"] for e in service._load()["signals"].values()} == {"SH.600000"}
    assert set(service._load()["retry_symbols"]) == {"a:SH.600001"}
    assert not any(e.get("change") == "left" for e in service._load()["events"])


def test_completed_metadata_cannot_silently_skip_an_assigned_symbol(broken_batch):
    service, _manager, directory, _clock = broken_batch
    state = service._load()
    state["active_symbols"].append("a:SH.600001")
    state["active_cutoffs"]["a:SH.600001"] = 300
    service._consume_monitor()
    assert state["processed_run"] == directory.name
    assert state["retry_symbols"]["a:SH.600001"]["failures"] == 1
    assert (
        state["selection_checks"]["a:SH.600001:5m"]["reason"] == "RESULT_SYMBOL_MISSING"
    )


def test_read_failures_also_suspend_the_extra_bound_holding_period(broken_batch):
    service, _manager, directory, _clock = broken_batch
    state = service._load()
    state["holding_risk_plans"] = {
        "a:SH.600000:1m": {
            "active": True,
            "market": "a",
            "code": "SH.600000",
            "frequency": "1m",
        }
    }
    (directory / "results.jsonl").write_bytes(b"{broken\n")
    service._consume_monitor()
    assert state["selection_checks"]["a:SH.600000:1m"]["error"] is True


def test_new_screening_read_failure_budget_resets_when_evidence_is_repaired(
    setup, monkeypatch
):
    from types import SimpleNamespace
    from cl_app.services import signal_monitor

    service, manual, _live, _clock, _cutoff = _seed(setup, [])
    timer = [100.0]
    monkeypatch.setattr(
        signal_monitor, "time", SimpleNamespace(monotonic=lambda: timer[0])
    )
    read = Mock(side_effect=ValueError("isolated broken result"))
    monkeypatch.setattr(manual, "results", read)
    status = {**manual.status(), "evidence_version": "broken"}
    for _ in range(6):
        service._screening_results_for_monitor(status)
        timer[0] += 61
    assert read.call_count == 3
    read.side_effect = None
    read.return_value = {"status": "completed", "results_loading": False}
    assert (
        service._screening_results_for_monitor(
            {**status, "evidence_version": "repaired"}
        )["status"]
        == "completed"
    )
    assert "screening_result_error" not in service._load()


def test_completed_but_still_loading_full_results_never_replace_the_existing_pool(
    setup, monkeypatch
):
    service, manual, live, _clock, cutoff = _seed(setup, [signal()])
    service.tick()
    live.finish(cutoff)
    service.tick()
    seed = service._load()["seed"]["run_id"]
    manual.start(manual.state["settings"])
    manual.finish(cutoff)
    loading = {**manual.results(), "results_loading": True, "selected": []}
    monkeypatch.setattr(manual, "results", Mock(return_value=loading))
    service._load()["force"] = True
    service.tick()
    assert service._load()["seed"]["run_id"] == seed
    assert len(live.started) == 2
    assert [s["code"] for s in live.started[-1]["symbols"]] == ["SH.600088"]


def test_read_failure_during_a_pool_change_cannot_poison_the_replacement_pool(
    broken_batch,
):
    service, _manager, directory, _clock = broken_batch
    (directory / "results.jsonl").write_bytes(b"{broken\n")
    service._load()["seed"]["run_id"] = "replacement"
    service._consume_monitor()
    assert service._load()["processed_run"] == directory.name
    assert not service._load()["unverified_signals"]
    assert not service._load().get("monitor_read_error")


def test_retiring_a_bad_batch_clears_its_diagnostic_for_an_empty_replacement_pool(
    broken_batch,
):
    service, _manager, directory, _clock = broken_batch
    (directory / "results.jsonl").write_bytes(b"{broken\n")
    service._consume_monitor()
    state = service._load()
    assert state["monitor_read_error"]
    state["seed"] = {
        **state["seed"],
        "run_id": "replacement",
        "codes": [],
        "symbols": [],
    }
    service._consume_monitor()
    assert not state.get("monitor_read_error") and not state["last_error"]
    assert not state["errors"]
