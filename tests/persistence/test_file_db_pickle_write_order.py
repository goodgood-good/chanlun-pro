import pickle
import threading

import pytest

import chanlun.persistence.file_db as file_db_module
from chanlun.persistence.file_db import FileCacheDB


def test_same_path_pickle_writes_finish_in_submission_order(tmp_path, monkeypatch):
    """同一路径后提交的快照必须最终胜出，即使旧写入本身更慢。"""
    file_db = object.__new__(FileCacheDB)
    target = tmp_path / "trader-state.pkl"
    first_started = threading.Event()
    second_finished = threading.Event()
    real_write = file_db._atomic_write_pickle_blocking

    def controlled_write(path, obj):
        if obj["sequence"] == 1:
            first_started.set()
            # 同一路径写入必须串行，确保先提交的快照先落盘。
            second_finished.wait(timeout=1.0)
            real_write(path, obj)
            return

        real_write(path, obj)
        second_finished.set()

    monkeypatch.setattr(file_db, "_atomic_write_pickle_blocking", controlled_write)

    first = file_db._atomic_write_pickle(target, {"sequence": 1})
    assert first_started.wait(timeout=2.0), "第一个异步写入没有启动"
    second = file_db._atomic_write_pickle(target, {"sequence": 2})

    first.result(timeout=5.0)
    second.result(timeout=5.0)

    with open(target, "rb") as fp:
        assert pickle.load(fp) == {"sequence": 2}


def test_different_path_pickle_writes_still_run_concurrently(tmp_path, monkeypatch):
    """一个路径的慢写入不能阻塞另一路径落盘。"""
    file_db = object.__new__(FileCacheDB)
    slow_target = tmp_path / "slow.pkl"
    fast_target = tmp_path / "fast.pkl"
    slow_started = threading.Event()
    fast_finished = threading.Event()
    real_write = file_db._atomic_write_pickle_blocking

    def controlled_write(path, obj):
        if path == slow_target:
            slow_started.set()
            assert fast_finished.wait(timeout=2.0), "不同路径被错误地全局串行化"
            real_write(path, obj)
            return

        real_write(path, obj)
        fast_finished.set()

    monkeypatch.setattr(file_db, "_atomic_write_pickle_blocking", controlled_write)

    slow = file_db._atomic_write_pickle(slow_target, {"target": "slow"})
    assert slow_started.wait(timeout=2.0), "慢路径写入没有启动"
    fast = file_db._atomic_write_pickle(fast_target, {"target": "fast"})

    slow.result(timeout=5.0)
    fast.result(timeout=5.0)

    with open(slow_target, "rb") as fp:
        assert pickle.load(fp) == {"target": "slow"}
    with open(fast_target, "rb") as fp:
        assert pickle.load(fp) == {"target": "fast"}


def test_same_path_queue_continues_after_async_write_failure(tmp_path, monkeypatch):
    """单次异步失败保持既有 Future 语义，且不能卡死同路径后续写入。"""
    file_db = object.__new__(FileCacheDB)
    target = tmp_path / "recover.pkl"
    failing_started = threading.Event()
    allow_failure = threading.Event()
    real_write = file_db._atomic_write_pickle_blocking

    def controlled_write(path, obj):
        if obj["sequence"] == 1:
            failing_started.set()
            assert allow_failure.wait(timeout=2.0), "失败写入未获准继续"
            raise OSError("controlled write failure")
        real_write(path, obj)

    monkeypatch.setattr(file_db, "_atomic_write_pickle_blocking", controlled_write)

    failing = file_db._atomic_write_pickle(target, {"sequence": 1})
    assert failing_started.wait(timeout=2.0), "失败写入没有启动"
    recovery = file_db._atomic_write_pickle(target, {"sequence": 2})
    allow_failure.set()

    # 保持既有契约：异步普通异常只记 warning，不从 Future.result() 抛出。
    assert failing.result(timeout=5.0) is None
    assert recovery.result(timeout=5.0) is None
    with open(target, "rb") as fp:
        assert pickle.load(fp) == {"sequence": 2}


def test_logging_failure_does_not_strand_same_path_queue(tmp_path, monkeypatch):
    """错误日志自身失败时，当前 Future 应失败但后续写入仍须继续。"""
    file_db = object.__new__(FileCacheDB)
    target = tmp_path / "logging-failure.pkl"
    failing_started = threading.Event()
    allow_failure = threading.Event()
    real_write = file_db._atomic_write_pickle_blocking

    def controlled_write(path, obj):
        if obj["sequence"] == 1:
            failing_started.set()
            assert allow_failure.wait(timeout=2.0), "失败写入未获准继续"
            raise OSError("controlled write failure")
        real_write(path, obj)

    def fail_to_log(message):
        raise RuntimeError("controlled logging failure")

    monkeypatch.setattr(file_db, "_atomic_write_pickle_blocking", controlled_write)
    monkeypatch.setattr(file_db_module.LogUtil, "warning", fail_to_log)

    failing = file_db._atomic_write_pickle(target, {"sequence": 1})
    assert failing_started.wait(timeout=2.0), "失败写入没有启动"
    recovery = file_db._atomic_write_pickle(target, {"sequence": 2})
    allow_failure.set()

    with pytest.raises(RuntimeError, match="controlled logging failure"):
        failing.result(timeout=2.0)
    assert recovery.result(timeout=5.0) is None
    with open(target, "rb") as fp:
        assert pickle.load(fp) == {"sequence": 2}


def test_shutdown_fallback_write_survives_logging_failure(tmp_path, monkeypatch):
    """Executor 关闭后的同步兜底不能被告警日志异常打断。"""
    file_db = object.__new__(FileCacheDB)
    target = tmp_path / "shutdown-fallback.pkl"

    class ShutdownExecutor:
        @staticmethod
        def submit(*args, **kwargs):
            raise RuntimeError("cannot schedule new futures after shutdown")

    def fail_to_log(message):
        raise RuntimeError("controlled fallback logging failure")

    monkeypatch.setattr(file_db_module, "_PICKLE_WRITE_EXECUTOR", ShutdownExecutor())
    monkeypatch.setattr(file_db_module, "_PICKLE_WRITES_CLOSED", False)
    monkeypatch.setattr(file_db_module, "_PICKLE_WRITES_ACCEPTING", True)
    monkeypatch.setattr(file_db_module.LogUtil, "warning", fail_to_log)

    future = file_db._atomic_write_pickle(target, {"sequence": 1})

    assert future.result(timeout=2.0) is None
    with open(target, "rb") as fp:
        assert pickle.load(fp) == {"sequence": 1}
    path_key = file_db_module.os.path.normcase(
        file_db_module.os.path.abspath(file_db_module.os.fspath(target))
    )
    assert path_key not in file_db_module._PICKLE_WRITE_QUEUES


def test_async_pickle_worker_uses_absolute_path(tmp_path, monkeypatch):
    """异步提交后即使进程工作目录变化，worker 也必须写原目标。"""
    file_db = object.__new__(FileCacheDB)
    seen_paths = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        file_db,
        "_atomic_write_pickle_blocking",
        lambda path, obj: seen_paths.append(path),
    )

    future = file_db._atomic_write_pickle(file_db_module.pathlib.Path("relative.pkl"), {})

    assert future.result(timeout=2.0) is None
    assert len(seen_paths) == 1
    assert seen_paths[0].is_absolute()
    assert seen_paths[0] == tmp_path / "relative.pkl"


def test_durable_pickle_fsyncs_file_before_atomic_replace(tmp_path, monkeypatch):
    file_db = object.__new__(FileCacheDB)
    target = tmp_path / "durable.pkl"
    events = []
    real_dump = file_db_module.pickle.dump
    real_fsync = file_db_module.os.fsync
    real_replace = file_db_module.os.replace

    def tracked_dump(obj, fp, protocol=None):
        events.append("dump")
        return real_dump(obj, fp, protocol=protocol)

    def tracked_fsync(fd):
        events.append("fsync")
        return real_fsync(fd)

    def tracked_replace(source, destination):
        events.append("replace")
        return real_replace(source, destination)

    monkeypatch.setattr(file_db_module.pickle, "dump", tracked_dump)
    monkeypatch.setattr(file_db_module.os, "fsync", tracked_fsync)
    monkeypatch.setattr(file_db_module.os, "replace", tracked_replace)

    future = file_db._atomic_write_pickle(
        target, {"sequence": 1}, suppress_errors=False, durable=True
    )
    future.result(timeout=5)

    assert events[:3] == ["dump", "fsync", "replace"]


def test_durable_pickle_fsync_failure_is_visible(tmp_path, monkeypatch):
    file_db = object.__new__(FileCacheDB)
    target = tmp_path / "durable-failure.pkl"
    monkeypatch.setattr(
        file_db_module.os,
        "fsync",
        lambda fd: (_ for _ in ()).throw(OSError("controlled fsync failure")),
    )

    future = file_db._atomic_write_pickle(
        target, {"sequence": 1}, suppress_errors=False, durable=True
    )

    with pytest.raises(OSError, match="controlled fsync failure"):
        future.result(timeout=5)
    assert not target.exists()
