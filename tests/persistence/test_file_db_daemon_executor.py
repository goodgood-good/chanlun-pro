import pathlib
import subprocess
import sys
import textwrap

import pytest

import threading

from chanlun.persistence import file_db as file_db_module


def test_public_daemon_executor_contract():
    from chanlun.persistence.file_db import DaemonExecutor as ReexportedDaemonExecutor
    from chanlun.tools.daemon_executor import DaemonExecutor

    assert ReexportedDaemonExecutor is DaemonExecutor

    executor = DaemonExecutor(max_workers=1, thread_name_prefix="Contract")
    try:
        future = executor.submit(lambda value: value + 1, 1)
        assert future.result(timeout=1) == 2
        assert all(thread.daemon for thread in executor._threads)
    finally:
        executor.shutdown(wait=True, cancel_futures=True)

def test_daemon_executor_rejects_when_pending_capacity_is_full():
    from chanlun.tools.daemon_executor import DaemonExecutor

    entered = threading.Event()
    release = threading.Event()
    executor = DaemonExecutor(
        max_workers=1,
        thread_name_prefix="Bounded",
        max_pending=1,
    )

    def block():
        entered.set()
        release.wait(timeout=2)

    first = executor.submit(block)
    assert entered.wait(timeout=1)
    second = executor.submit(lambda: None)
    try:
        with pytest.raises(RuntimeError, match="queue is full"):
            executor.submit(lambda: None)
    finally:
        release.set()
        assert first.result(timeout=2) is None
        assert second.result(timeout=2) is None
        executor.shutdown(wait=True, cancel_futures=True)


def test_daemon_executor_constructor_cleans_started_workers_on_partial_failure(
    monkeypatch,
):
    from chanlun.tools.daemon_executor import DaemonExecutor

    prefix = "PartialDaemonExecutor"
    original_start = threading.Thread.start

    def fail_third_worker(thread):
        if thread.name == f"{prefix}-2":
            original_start(thread)
            raise RuntimeError("injected post-start failure")
        return original_start(thread)

    monkeypatch.setattr(threading.Thread, "start", fail_third_worker)

    with pytest.raises(RuntimeError, match="post-start failure"):
        DaemonExecutor(max_workers=4, thread_name_prefix=prefix)

    assert not any(
        thread.name.startswith(prefix) and thread.is_alive()
        for thread in threading.enumerate()
    )

def test_pickle_executor_workers_are_daemon_threads():
    file_db_module.start_pickle_writes()
    try:
        completed = threading.Event()
        future = file_db_module._PICKLE_WRITE_EXECUTOR.submit(completed.set)

        assert future.result(timeout=1) is None
        assert completed.is_set()
        threads = list(file_db_module._PICKLE_WRITE_EXECUTOR._threads)
        assert threads
        assert all(thread.daemon for thread in threads)
    finally:
        file_db_module.shutdown_pickle_writes(wait=True, cancel_pending=True)
        # The writer lifecycle is process-wide.  Restore lazy startup so this
        # test cannot poison later persistence modules in the same pytest run.
        file_db_module.allow_lazy_pickle_writes()

def test_hung_pickle_write_does_not_block_interpreter_exit():
    root = pathlib.Path(__file__).resolve().parents[2]
    script = textwrap.dedent(
        f"""
        import os
        import pathlib
        import sys
        import threading

        root = pathlib.Path({str(root)!r})
        sys.path.insert(0, str(root / "src"))
        from chanlun import config
        config.DATA_PATH = os.environ["CHANLUN_TEST_DATA_PATH"]
        config.DB_TYPE = "sqlite"
        config.DB_DATABASE = "chanlun_pytest"
        config.DB_HOST = "pytest.invalid"
        config.DB_USER = ""
        config.DB_PWD = ""
        from chanlun.persistence import file_db
        file_db.start_pickle_writes()

        started = threading.Event()
        release = threading.Event()

        def block():
            started.set()
            release.wait(60)

        file_db._PICKLE_WRITE_EXECUTOR.submit(block)
        assert started.wait(2)
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=8,
    )
    assert completed.returncode == 0, completed.stderr
