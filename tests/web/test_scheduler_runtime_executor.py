import datetime
import os
import pathlib
import subprocess
import sys
import threading

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
