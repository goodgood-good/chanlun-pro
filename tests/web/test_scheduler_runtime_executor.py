import datetime
import os
import pathlib
import subprocess
import sys
import threading

import pytest
import cl_app.services.scheduler_executor as scheduler_executor_module
from apscheduler.executors.pool import BasePoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler

from cl_app.services.scheduler_executor import RestartableDaemonPoolExecutor


def test_scheduler_executor_runs_jobs_after_runtime_restart():
    scheduler = BackgroundScheduler(
        executors={"default": RestartableDaemonPoolExecutor(max_workers=1)}
    )
    try:
        for _ in range(2):
            completed = threading.Event()
            scheduler.start()
            scheduler.add_job(
                completed.set,
                "date",
                run_date=datetime.datetime.now() + datetime.timedelta(seconds=0.05),
            )
            assert completed.wait(timeout=1)
            scheduler.shutdown(wait=False)
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)


def test_hung_scheduler_job_does_not_block_interpreter_exit():
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    python_path = os.pathsep.join(
        [
            str(repo_root / "src"),
            str(repo_root / "web" / "chanlun_chart"),
            os.environ.get("PYTHONPATH", ""),
        ]
    )
    script = """
import threading
from apscheduler.schedulers.background import BackgroundScheduler
from cl_app.services.scheduler_executor import RestartableDaemonPoolExecutor

entered = threading.Event()
release = threading.Event()
def block():
    entered.set()
    release.wait(60)

scheduler = BackgroundScheduler(
    executors={"default": RestartableDaemonPoolExecutor(max_workers=1)}
)
scheduler.start()
scheduler.add_job(block)
assert entered.wait(1)
scheduler.shutdown(wait=False)
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repo_root,
        env={**os.environ, "PYTHONPATH": python_path},
        capture_output=True,
        text=True,
        timeout=3,
    )

    assert completed.returncode == 0, completed.stderr


def test_pool_start_failure_discards_workers_and_first_retry_succeeds(monkeypatch):
    executor = RestartableDaemonPoolExecutor(max_workers=2)
    scheduler = object()
    calls = {"count": 0}
    created_pools = []
    real_factory = scheduler_executor_module.DaemonExecutor

    def recording_factory(*args, **kwargs):
        pool = real_factory(*args, **kwargs)
        pool.submit(lambda: None).result(timeout=2)
        created_pools.append(pool)
        return pool

    def flaky_start(self, scheduler_arg, alias):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("injected-super-start-failure")
        return None

    monkeypatch.setattr(
        scheduler_executor_module,
        "DaemonExecutor",
        recording_factory,
    )
    monkeypatch.setattr(BasePoolExecutor, "start", flaky_start)
    with pytest.raises(RuntimeError, match="injected-super-start-failure"):
        executor.start(scheduler, "decision_support")
    assert executor._pool is None
    assert len(created_pools) == 1
    assert created_pools[0]._threads
    assert all(not thread.is_alive() for thread in created_pools[0]._threads)
    with pytest.raises(RuntimeError, match="shutdown"):
        created_pools[0].submit(lambda: None)
    executor.start(scheduler, "decision_support")
    assert executor._pool is not None
    assert len(created_pools) == 2
    assert calls == {"count": 2}
    executor.shutdown(wait=True)


def test_pool_start_cleanup_failure_preserves_primary_error(monkeypatch):
    executor = RestartableDaemonPoolExecutor(max_workers=1)

    class FailingCleanupPool:
        def shutdown(self, **_kwargs):
            raise RuntimeError("injected-pool-cleanup-failure")

    monkeypatch.setattr(
        scheduler_executor_module,
        "DaemonExecutor",
        lambda **_kwargs: FailingCleanupPool(),
    )
    monkeypatch.setattr(
        BasePoolExecutor,
        "start",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("injected-primary-start-failure")
        ),
    )

    with pytest.raises(RuntimeError, match="injected-primary-start-failure") as caught:
        executor.start(object(), "decision_support")

    assert executor._pool is None
    assert isinstance(caught.value.__cause__, RuntimeError)
    assert str(caught.value.__cause__) == "injected-pool-cleanup-failure"
