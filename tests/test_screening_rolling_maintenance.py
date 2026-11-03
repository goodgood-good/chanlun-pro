import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from chanlun.screening import rolling_maintenance as maintenance, runner, worker_pool


@pytest.fixture
def setup(tmp_path, monkeypatch):
    run = tmp_path / "run"
    run.mkdir()
    revision = "a" * 64
    monkeypatch.setattr(runner, "source_revision", lambda: revision)
    state = {"status": "completed", "completed": 2, "total": 2}
    runner.write_json(run / "status.json", state)
    stocks = [
        {
            "code": "SH.600000",
            "market": "a",
            "name": "first",
            "_monitor_periods": ["5m"],
        },
        {
            "code": "SH.600001",
            "market": "a",
            "name": "second",
            "_monitor_periods": ["5m"],
        },
    ]
    rows = [
        {
            "code": s["code"],
            "market": "a",
            "name": s["name"],
            "rows": [
                {
                    "frequency": "5m",
                    "input_fingerprint": s["code"],
                    "data_errors": [],
                    "confirmed_buys": 1 if index == 0 else 0,
                    "confirmed_sells": 0,
                    "data_quality": {},
                }
            ],
        }
        for index, s in enumerate(stocks)
    ]
    (run / "results.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
    )
    request = {
        "settings": {"frequencies": ["5m"]},
        "rolling_window_targets": {"5m": "2025-10-16"},
    }
    return run, request, stocks, revision


def test_launch_is_hidden_and_only_after_publishing_selection(setup, monkeypatch):
    run, request, stocks, revision = setup
    before = (run / "status.json").read_bytes(), (run / "results.jsonl").read_bytes()
    calls = []

    def spawn(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(pid=123)

    monkeypatch.setattr(maintenance.subprocess, "Popen", spawn)
    result = maintenance.launch(run, request, {}, stocks, revision)
    assert result["state"] == "scheduled" and result["workers"] == 2
    assert len(calls) == 1 and "--run-dir" in calls[0][0][0]
    if os.name == "nt":
        assert calls[0][1]["creationflags"] & maintenance.subprocess.CREATE_NO_WINDOW
    assert calls[0][1]["env"]["PYTHONPATH"].split(os.pathsep)[0] == str(
        Path(maintenance.__file__).resolve().parents[2]
    )
    assert before == (
        (run / "status.json").read_bytes(),
        (run / "results.jsonl").read_bytes(),
    )


@pytest.mark.parametrize("reason", ["active", "stale", "forced", "no_target"])
def test_unfinished_or_unneeded_scan_never_starts_future_work(
    setup, monkeypatch, reason
):
    run, request, stocks, revision = setup
    if reason == "active":
        runner.write_json(run / "status.json", {"status": "running"})
    elif reason == "stale":
        revision = "b" * 64
    elif reason == "forced":
        request["force_rebuild"] = True
    else:
        request.pop("rolling_window_targets")
    monkeypatch.setattr(
        maintenance.subprocess,
        "Popen",
        lambda *a, **k: pytest.fail("unexpected process"),
    )
    assert maintenance.launch(run, request, {}, stocks, revision)["state"] == "skipped"


@pytest.mark.parametrize("fail", [False, True])
def test_maintenance_is_bounded_skips_empty_signals_and_cannot_change_selection(
    setup, monkeypatch, fail
):
    run, request, stocks, revision = setup
    runner.write_json(
        run / maintenance.REQUEST,
        {
            "schema": "screening-rolling-maintenance-v1",
            "source_revision": revision,
            "settings": request["settings"],
            "contexts": {},
            "stocks": stocks,
            "targets": request["rolling_window_targets"],
        },
    )
    before = (run / "status.json").read_bytes(), (run / "results.jsonl").read_bytes()
    closed = []

    class Pool:
        def __init__(self, version, worker_target):
            assert version == revision

        def scan(
            self, tasks, settings, contexts, directory, workers, version, *, request
        ):
            assert workers == 2 and [s["code"] for s in tasks] == ["SH.600000"]
            assert "_rolling_source" in tasks[0] and request["rolling_window_targets"]
            if fail:
                raise RuntimeError("fixture maintenance failure")
            yield {"maintenance": {"state": "prepared"}}, []

        def close(self):
            closed.append(True)

    monkeypatch.setattr(worker_pool, "ReusableWorkerPool", Pool)
    maintenance.execute(run)
    status = json.loads((run / maintenance.STATUS).read_text(encoding="utf-8"))
    assert status["state"] == ("failed" if fail else "completed")
    assert status["total"] == 1 and closed == [True]
    assert before == (
        (run / "status.json").read_bytes(),
        (run / "results.jsonl").read_bytes(),
    )


def test_launch_failure_is_only_an_optional_maintenance_failure(setup, monkeypatch):
    run, request, stocks, revision = setup
    monkeypatch.setattr(
        maintenance.subprocess,
        "Popen",
        lambda *a, **k: (_ for _ in ()).throw(OSError("fixture")),
    )
    assert (
        maintenance.launch(run, request, {}, stocks, revision)["state"] == "unavailable"
    )
    assert (
        json.loads((run / "status.json").read_text(encoding="utf-8"))["status"]
        == "completed"
    )
