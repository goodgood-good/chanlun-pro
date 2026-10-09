"""Separate managers cannot publish two simultaneous runs in the same root."""
import json
import multiprocessing
import os
from pathlib import Path
import time
from types import SimpleNamespace


def _simultaneous_start(root, barrier, outcomes, launches):
    # Spawn imports this test without pytest's conftest initialization.
    from chanlun import config
    config.DATA_PATH = str(Path(root).parent / "isolated-data")
    config.DB_TYPE, config.DB_DATABASE = "sqlite", "screening_start_lock"
    config.DB_HOST, config.DB_USER, config.DB_PWD = "pytest.invalid", "", ""
    from cl_app.services import screening
    manager = screening.ScreeningManager(root, warm_results=False)
    actual_status = manager.status

    def slow_status():
        state = actual_status()
        # Without the shared file lock both managers observe idle before launch.
        time.sleep(.2)
        return state

    def fake_spawn(*args, **kwargs):
        with launches.get_lock():
            launches.value += 1
        return SimpleNamespace(pid=os.getpid())

    manager.status = slow_status
    screening.subprocess.Popen = fake_spawn
    barrier.wait(timeout=15)
    try:
        state = manager.start({"scope": "codes", "codes": ["SH.600000"]}, external_notifications=False)
        outcomes.put(("started", state["run_id"]))
    except RuntimeError as error:
        outcomes.put(("blocked", str(error)))


def test_two_process_managers_publish_exactly_one_run(tmp_path):
    context = multiprocessing.get_context("spawn")
    barrier, outcomes, launches = context.Barrier(2), context.Queue(), context.Value("i", 0)
    root = tmp_path / "manual"
    children = [context.Process(target=_simultaneous_start,
                                args=(str(root), barrier, outcomes, launches)) for _ in range(2)]
    try:
        for child in children:
            child.start()
        received = [outcomes.get(timeout=20) for _ in children]
        for child in children:
            child.join(10)
            assert child.exitcode == 0
        assert sorted(row[0] for row in received) == ["blocked", "started"]
        assert launches.value == 1
        started = next(value for status, value in received if status == "started")
        assert json.loads((root / "latest.json").read_text(encoding="utf8"))["run_id"] == started
        assert len(list(root.glob("*/request.json"))) == 1
        request = json.loads((root / started / "request.json").read_text(encoding="utf8"))
        assert request["external_notifications"] is False
        assert request["settings"]["codes"] == ["SH.600000"]
    finally:
        for child in children:
            child.join(2)
            if child.is_alive():
                child.terminate()
                child.join(2)
        outcomes.close()
